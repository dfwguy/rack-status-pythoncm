#!/bin/bash
# Author: Junli Zhang<junliz@nvidia.com>
#
#SBATCH -p defq
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=03:00:00
###SBATCH -N 2


#CONT=/home/cmsupport/workspace/nemo-25.11.01.sqsh
# Container to run. run-nemo-xrack.sh stages NEMO_SQSH_SRC to /raid/data and passes the staged
# path via NVIS_NEMO_CONT, so pointing NEMO_SQSH_SRC at the vetted image actually switches the
# container here. Falls back to the stock image for direct/manual runs.
CONT="${NVIS_NEMO_CONT:-/raid/data/nemo-25.11.01.sqsh}"
SYSINFO_SH=${SLURM_SUBMIT_DIR}/sysinfo.sh

single_rack=1
[ $SLURM_NNODES -gt 18 ] && single_rack=0
if [ $(( ${SLURM_NNODES} % 2 )) != 0 ]
then
  echo "ERROR: SLURM_NNODES=$SLURM_NNODES is not multiple of 2!!!"
  exit 1
fi

module load slurm
cd ${SLURM_SUBMIT_DIR}

if [ -n "${SLURM_JOB_SCRIPT:-}" ]; then
  _job_dir="$(cd "$(dirname "$SLURM_JOB_SCRIPT")" && pwd)"
else
  _job_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

echo
echo "NODELIST: ${SLURM_JOB_NODELIST}"
echo

s_time=$(date +%s)
echo "INFO: cleaning work begin"
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no -o PreferredAuthentications=publickey"
pdsh -f 100 -R ssh -w ${SLURM_JOB_NODELIST} <<- 'EOF'|dshbak -c
pkill -9 python &>/dev/null || true
ps -ef|grep python|grep -E 'torch|nemo'|grep -v grep || true
enroot list -f|grep pyxis && enroot remove -f $(enroot list -f|grep pyxis) || true
rm -f /var/lib/systemd/coredump/* &>/dev/null || true
EOF
echo "INFO: cleaning work done"

echo "INFO: checking hardware status"
timeout 60 bash "${SYSINFO_SH}" 2>&1

# Prefer bond0 when it is up, else enP22s22f0np0 when up, else first existing interface, else bond0.
select_ns_nic() {
  local nic st
  for nic in bond0 enP22s22f0np0; do
    if [[ -r "/sys/class/net/${nic}/operstate" ]]; then
      st=$(tr -d '\n' <"/sys/class/net/${nic}/operstate")
      if [[ "$st" == "up" ]]; then
        printf '%s' "$nic"
        return 0
      fi
    fi
  done
  for nic in bond0 enP22s22f0np0; do
    if [[ -d "/sys/class/net/${nic}" ]]; then
      printf '%s' "$nic"
      return 0
    fi
  done
  printf '%s' "bond0"
}

ns_nic=$(select_ns_nic)
echo "INFO: ns_nic=${ns_nic} (auto: bond0 vs enP22s22f0np0)"
set -x
# East-west (compute) rails via the shared classifier (ACTIVE + IB/RoCE + top line
# rate; excludes slower BlueField-3 storage NICs). Falls back to mlx5_0..3.
# Locate lib/fabric.inc.sh. Slurm spools ONLY this batch script (not lib/), so
# $(dirname BASH_SOURCE) points at /var/spool/slurmd/... where lib/ does NOT exist —
# that made the fabric classifier silently no-op and fall back to the naive mlx5_0..3
# set, which MIXES IB + RoCE NICs and breaks NCCL ("Remote IB device is incompatible
# with the local mlx5_2:1/RoCE"). Prefer NVIS_SCRIPTS_DIR (exported by the launcher),
# then the submit dir, then the deployed bin/scripts path.
_nvis_lib=""
for _cand in \
  "${NVIS_SCRIPTS_DIR:-}/lib/fabric.inc.sh" \
  "${SLURM_SUBMIT_DIR:-}/lib/fabric.inc.sh" \
  "$(dirname "${BASH_SOURCE[0]}")/lib/fabric.inc.sh" \
  "${RACK_REMOTE_BASE_PATH:-/home/cmsupport/nvis-rack-status}/bin/scripts/lib/fabric.inc.sh"; do
  [[ -n "${_cand}" && -f "${_cand}" ]] && { _nvis_lib="${_cand}"; break; }
done
# shellcheck disable=SC1090
if [[ -n "${_nvis_lib}" ]]; then
  echo "INFO: fabric classifier: ${_nvis_lib}"
  . "${_nvis_lib}"
else
  echo "WARN: lib/fabric.inc.sh not found (set NVIS_SCRIPTS_DIR); falling back to mlx5_0..3 (may mix IB/RoCE)"
fi
if declare -F nvis_fabric_scan >/dev/null 2>&1; then eval "$(nvis_fabric_scan 2>/dev/null)"; fi
if [[ -n "${NVIS_FAB_IB_HCA:-}" ]]; then
  IFS=',' read -r -a ew_nics <<< "${NVIS_FAB_IB_HCA}"
else
  ew_nics=($(echo mlx5_{0..3}))
fi
export MELLANOX_VISIBLE_DEVICES=all
export PMIX_MCA_gds=hash
#
if [ $single_rack -eq 1 ]; then
  export UCX_TLS=tcp
  export NCCL_IB_DISABLE=1
  export UCX_NET_DEVICES=${ns_nic}
else
  export UCX_TLS=rc
  export UCX_NET_DEVICES="$(echo ${ew_nics[@]}|tr ' ' '\n'|xargs -I {} echo {}:1|paste -s -d',')"
  export NCCL_IB_HCA="=$(echo ${ew_nics[@]}|tr ' ' '\n'|xargs -I {} echo {}|paste -s -d',')"
fi
export NCCL_SOCKET_IFNAME=${ns_nic}
export OMPI_MCA_btl_tcp_if_include=${ns_nic}
export OMPI_MCA_oob_tcp_if_include=${ns_nic}
export NVTE_UB_SOCKET_IFNAME=${ns_nic}
export GLOO_SOCKET_IFNAME=${ns_nic}
#
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_btl_openib_warn_default_gid_prefix=0
export OMPI_MCA_coll_hcoll_enable=0
export SLURM_CPU_BIND=none
#
export NCCL_NVLS_ENABLE=1
#export NCCL_CUMEM_ENABLE=1
#export NCCL_CUMEM_HOST_ENABLE=0
# MNNVL (multi-node NVLink) is only valid WITHIN one NVL72 / IMEX domain (a single rack =
# one GPU-fabric ClusterUUID). Forcing it ON across a multi-rack job makes NCCL import CUDA
# fabric handles across the rack (ClusterUUID) boundary -> "Cuda failure 400 'invalid
# resource handle'" and the collective dies (validated on bos01-b05+b06, job 189).
# Single-rack: force MNNVL=1 (one domain, optimal NVLink). Multi-rack: let NCCL AUTO-detect
# per-rack MNNVL cliques (distinct ClusterUUID) and use IB between racks. Override with
# NVIS_NCCL_MNNVL=0|1 (e.g. NVIS_NCCL_MNNVL=0 forces IB everywhere as a fallback).
if [ -n "${NVIS_NCCL_MNNVL:-}" ]; then
  export NCCL_MNNVL_ENABLE=${NVIS_NCCL_MNNVL}
elif [ $single_rack -eq 1 ]; then
  export NCCL_MNNVL_ENABLE=1
else
  unset NCCL_MNNVL_ENABLE
fi
export NCCL_MIN_CTAS=16
#export NCCL_P2P_LEVEL=NVL
#export NCCL_P2P_LEVEL=C2C
#export NCCL_NET_GDR_C2C=1
#export NCCL_NET_GDR_LEVEL=SYS
export NCCL_RUNTIME_CONNECT=0
export NCCL_IB_QPS_PER_CONNECTION=2
#export NCCL_IB_SPLIT_DATA_ON_QPS=0
#export NVSHMEM_DISABLE_GDRCOPY=1
export CUDA_MODULE_LOADING=EAGER
export NCCL_P2P_NET_CHUNKSIZE=262144
#export NCCL_GDRCOPY_ENABLE=1
#export NCCL_GDRCOPY_SYNC_ENABLE=1

# --- Cross-rack CollNet-mismatch fix (THE multi-rack alltoall fix; keeps HPC-X/SHARP) ------------
# ROOT CAUSE (isolated 2026-07-07 on a07+a08 with NCCL_DEBUG=INFO, jobs 198-206): the container's
# HPC-X SHARP plugin /opt/hpcx/nccl_rdma_sharp_plugin/lib/libnccl-net.so predates the runtime NCCL
# (2.28.9 AND 2.30.7): its `*_v11` entry points are missing, so NCCL falls back to the v10 ABI and
# DOES load "collnet plugin SHARP (v10)". SHARP then registers an ASYMMETRIC CollNet device count
# across nodes (min 3, max 4); NCCL 2.30 treats that as fatal by default and aborts with "Detected
# mixed local CollNet device counts across ranks", which surfaced downstream as the MISLEADING
# "bootstrap Message truncated: received 512 bytes instead of 256". It was NOT the NCCL version, NOT
# PXN/PROTO/NCHANNELS (A/B job 198/199 — every knob FAILs), and NOT the degraded c15 rail (job 202
# fails without it). Two fixes work (jobs 203/205/206):
#   1. NCCL_IGNORE_COLLNET_MISMATCH=1 (DEFAULT here) — tolerate the asymmetry, KEEP HPC-X + SHARP(v10).
#   2. NCCL_NET_PLUGIN=none — drop the external plugin entirely (built-in IB, NO SHARP). Fallback.
# Prefer #1 so cross-rack allreduce still gets IB SHARP offload. Flip to #2 by setting
# NVIS_NCCL_NET_PLUGIN=none. Harmless single-rack (NVLink). The real long-term fix is a container
# HPC-X plugin that exports the v11 ABI.
export NCCL_IGNORE_COLLNET_MISMATCH="${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1}"
if [ -n "${NVIS_NCCL_NET_PLUGIN:-}" ]; then
  export NCCL_NET_PLUGIN="${NVIS_NCCL_NET_PLUGIN}"
fi

# --- Container NCCL workaround (validated 2026-07-02 on bos01-b05 / bos01-b06) --------------------
# The nemo-25.11.01 container's bundled NCCL 2.28.3 SEGFAULTS at MNNVL init on GB200 NVL72
# ("NCCL INFO MNNVL busId ... healthMask 0xaa" -> SIGSEGV, srun exit 139) — even on a SINGLE node —
# while the host NCCL 2.28.9 drives the SAME fabric fine (portable all_reduce = ~914 GB/s; nvlink
# preflight PASS; torch CUDA in-container OK). Root cause is the container NCCL, not the fabric.
# Fix: bind-mount the host NCCL dir into the container and LD_PRELOAD the newer libnccl so every rank
# (the NCCL pre-checks AND the training) uses it. Override via NVIS_NCCL_PRELOAD_SRC / _LIB; if the
# lib is absent we fall back to the container's own NCCL (and warn).
CONT_MOUNTS="/dev:/dev"
EXPORT_ARG="ALL"
# With the VETTED image (build_vetted_nemo_image.sh: libnccl + nccl-tests both baked at the vetted
# NCCL), the runtime LD_PRELOAD is redundant AND undesirable (it would shadow the image's matched
# libs). Set NVIS_NCCL_PRELOAD_DISABLE=1 when running the vetted image. On the STOCK image the
# preload is still needed (its NCCL 2.28.3 segfaults at MNNVL init).
if [ "${NVIS_NCCL_PRELOAD_DISABLE:-0}" = 1 ]; then
  echo "INFO: NCCL preload DISABLED (NVIS_NCCL_PRELOAD_DISABLE=1) — using the container's own NCCL (expected with the vetted image)"
else
  NCCL_PRELOAD_SRC="${NVIS_NCCL_PRELOAD_SRC:-/home/cmods/workspace/nccl/bins}"
  NCCL_PRELOAD_LIB="${NVIS_NCCL_PRELOAD_LIB:-}"
  if [ -z "$NCCL_PRELOAD_LIB" ] && [ -d "$NCCL_PRELOAD_SRC" ]; then
    NCCL_PRELOAD_LIB=$(ls "$NCCL_PRELOAD_SRC"/libnccl.so.2.* 2>/dev/null | sort -V | tail -1 | xargs -r basename)
  fi
  if [ -n "$NCCL_PRELOAD_LIB" ] && [ -f "${NCCL_PRELOAD_SRC}/${NCCL_PRELOAD_LIB}" ]; then
    CONT_MOUNTS="${CONT_MOUNTS},${NCCL_PRELOAD_SRC}:/host-nccl"
    EXPORT_ARG="ALL,LD_PRELOAD=/host-nccl/${NCCL_PRELOAD_LIB}"
    echo "INFO: NCCL preload workaround ON -> /host-nccl/${NCCL_PRELOAD_LIB} (host ${NCCL_PRELOAD_SRC}); container NCCL 2.28.3 MNNVL segfault bypass"
  else
    echo "WARN: host NCCL preload not found under ${NCCL_PRELOAD_SRC}; container's own NCCL will be used (may segfault at MNNVL init)"
  fi
fi

# NCCL flight-recorder dump dir on NFS, mounted into the container at /nccl_traces. The training
# container is ephemeral (enroot remove at job end), so a dump written to the container's /tmp is
# lost (job 322). Point TORCH_NCCL_DEBUG_INFO_TEMP_FILE at this NFS-backed mount so per-rank traces
# from a watchdog-timeout survive for post-mortem. $(pwd) is the NFS submit/log dir (same path on all
# nodes). Override the location with NVIS_NCCL_TRACE_HOST_DIR.
NVIS_NCCL_TRACE_HOST_DIR="${NVIS_NCCL_TRACE_HOST_DIR:-$(pwd)/nccl_traces/${SLURM_JOB_ID:-0}}"
mkdir -p "${NVIS_NCCL_TRACE_HOST_DIR}" 2>/dev/null || true
CONT_MOUNTS="${CONT_MOUNTS},${NVIS_NCCL_TRACE_HOST_DIR}:/nccl_traces"
echo "INFO: NCCL flight-recorder dumps -> ${NVIS_NCCL_TRACE_HOST_DIR} (container:/nccl_traces)"

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null

## test nccl
export NCCL_DEBUG=WARN
export NCCL_NVLS_ENABLE=1

# NCCL pre-flight selector: which collectives to run before NeMo training (space/comma
# list of: allreduce alltoall allgather). Default = all three (original behavior). The
# all-reduce alone proves the cross-rack fabric; alltoall/allgather can be skipped when the
# workload doesn't use them (Nemotron-H with ep=1 does NO all-to-all) or to sidestep a
# container-vs-host NCCL bootstrap ABI mismatch that trips only alltoall/allgather
# ("bootstrap.cc Message truncated"). Set e.g. NVIS_NCCL_PREFLIGHT="allreduce"; "none"
# skips the pre-flight entirely.
NVIS_NCCL_PREFLIGHT="${NVIS_NCCL_PREFLIGHT:-allreduce alltoall allgather}"
_preflight_on() { [[ " ${NVIS_NCCL_PREFLIGHT//,/ } " == *" $1 "* ]]; }
# all-reduce is the FATAL fabric gate. alltoall/allgather are informational by default:
# Nemotron-H (ep=1) issues neither, and their nccl-tests bootstrap can trip ("Message
# truncated") independently of a healthy fabric — so their failure must not block training.
# Set NVIS_NCCL_PREFLIGHT_STRICT=1 to make every selected pre-flight fatal.
NVIS_NCCL_PREFLIGHT_STRICT="${NVIS_NCCL_PREFLIGHT_STRICT:-0}"
echo "INFO: NCCL pre-flight tests = ${NVIS_NCCL_PREFLIGHT} (strict=${NVIS_NCCL_PREFLIGHT_STRICT})"

# alltoall/alltoallv on multi-domain NVL72 (NCCL 2.28+) can hit PXN / channel chunk-size bugs at
# the NVSwitch<->IB boundary -> NCCL bootstrap "Message truncated (512 vs 256)". Apply a
# serialized, PXN-off, SIMPLE-proto, fixed-16-channel config to the alltoall/allgather PRE-FLIGHT
# ONLY (all-reduce and the NeMo training keep full LL128 / all-channel perf). Toggle off with
# NVIS_NCCL_ALLTOALL_WORKAROUND=0; tune channels via NVIS_NCCL_A2A_NCHANNELS (default 16).
_a2a_export="${EXPORT_ARG}"
if [ "${NVIS_NCCL_ALLTOALL_WORKAROUND:-1}" = 1 ]; then
  _a2a_nch="${NVIS_NCCL_A2A_NCHANNELS:-16}"
  _a2a_export="${EXPORT_ARG},NCCL_PXN_DISABLE=1,NCCL_PROTO=SIMPLE,NCCL_MIN_NCHANNELS=${_a2a_nch},NCCL_MAX_NCHANNELS=${_a2a_nch}"
  echo "INFO: alltoall/allgather NCCL workaround ON (PXN off, PROTO=SIMPLE, ${_a2a_nch} channels) — NVIS_NCCL_ALLTOALL_WORKAROUND=0 to disable"
fi

if _preflight_on allreduce; then
echo "*********************************************"
echo "* NCCL ALL-Reduce                           *"
echo "*********************************************"

timeout 300 \
srun --export=${EXPORT_ARG} --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=${CONT_MOUNTS} \
all_reduce_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: All Reduce NCCL failed to run!!!              *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  exit 1
fi

sleep 10
fi

if _preflight_on alltoall; then
echo "*********************************************"
echo "* NCCL ALL-to-ALL                            *"
echo "*********************************************"

timeout 300 \
srun --export=${_a2a_export} --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=${CONT_MOUNTS} \
alltoall_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: AllToAll NCCL failed to run!!!              *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  if [ "${NVIS_NCCL_PREFLIGHT_STRICT}" = 1 ]; then exit 1; fi
  echo "WARN: AllToAll not required for this workload (ep=1) — continuing (NVIS_NCCL_PREFLIGHT_STRICT=1 to gate)."
fi
fi


if _preflight_on allgather; then
echo "*********************************************"
echo "* NCCL ALL-Gather                           *"
echo "*********************************************"

timeout 300 \
srun --export=${_a2a_export} --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=${CONT_MOUNTS} \
all_gather_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  echo "*********************************************"
  echo "* ERROR: All-Gather  NCCL failed to run!!!  *"
  echo "*********************************************"
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  if [ "${NVIS_NCCL_PREFLIGHT_STRICT}" = 1 ]; then exit 1; fi
  echo "WARN: All-Gather not required for this workload (ep=1) — continuing (NVIS_NCCL_PREFLIGHT_STRICT=1 to gate)."
fi
fi


sleep 10
## generate config files
### Rule: num_gpus % (ep * et * tp * pp) == 0
#python3 /opt/Megatron-Bridge/scripts/performance/setup_experiment.py ... ... #the same args with run_script.py
#
########  run nemo test
#


echo "*********************************************"
echo "* NEMO5-H 56B LLM                           *"
echo "*********************************************"
sleep 10
## generate config files
### Rule: num_gpus % (ep * et * tp * pp) == 0
#python3 /opt/Megatron-Bridge/scripts/performance/setup_experiment.py ... ... #the same args with run_script.py
#
########  run nemo test
#
## Workaround for unstable or not robust enough NVLINK: 
# With unstable or not robust enough NVLINK, you may encounter error like:
# RuntimeError: /opt/TransformerEngine/transformer_engine/common/comm_gemm_overlap/userbuffers/userbuffers-host.cpp:331 in function create_communicator_grouped2: CUDA Error: unknown error
# To workaround this issue, we can set UB_SKIPMC=1 to disables the NVSwitch/CUDA multicast path and makes Userbuffers fall back to a slower IPC-based scheme. 
# It's usually better than turning off tp_comm_overlap entirely, but worse than full multicast on a healthy, supported fabric.
# UB_SKIPMC: UserBuffers Skip Multicast
# UB = UserBuffers (the TP-overlap communication backend in TransformerEngine)
# SKIPMC = skip multicast (don't use CUDA/NVLS multicast for UB collectives)
## export NCCL_NVLS_ENABLE=0
## export UB_SKIPMC=1
### For optimal performance, it's recommended to keep NVLS enabled and let UB use multicast if possible.
export NCCL_DEBUG=INFO
export NCCL_NVLS_ENABLE=1
export UB_SKIPMC=0
# export MAX_STEPS=2010 #For stress test, you can set a large number for MAX_STEPS. 
#export MAX_STEPS=110
export MAX_STEPS=2010
srun --export=${EXPORT_ARG} --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=${CONT_MOUNTS} \
bash <<- '__END__'
set -ex
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=/root/.cache/huggingface

export NEMORUN_HOME=/mnt/nemo
export CPATH=/usr/local/cuda/include
export TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
export TRITON_LIB_DIR=/usr/local/cuda/lib64
export TORCH_NCCL_AVOID_RECORD_STREAMS=1

export NVTE_FLASH_ATTN=1
export NVTE_FUSED_ATTN=1
export CUDA_DEVICE_MAX_CONNECTIONS=1
export NVTE_FWD_LAYERNORM_SM_MARGIN=16
export NVTE_BWD_LAYERNORM_SM_MARGIN=16
export NCCL_P2P_NET_CHUNKSIZE=2097152
export TORCH_NCCL_HIGH_PRIORITY=1
export PYTHONPATH=/opt/Megatron-Bridge/scripts/performance:$PYTHONPATH

# NCCL flight recorder (job 320 post-mortem: DATA_PARALLEL collective hung, 10-min watchdog aborted
# the whole run but did not name the rank that stalled FIRST). The recorder ring-buffers the last N
# collectives per rank and, on the watchdog timeout, dumps them so the stuck rank/op is identified.
# Dumps land on node-local /tmp (collect with pdsh after a hang: /tmp/nccl_trace_<jobid>_<rank>).
# Low overhead; overridable via NVIS_NCCL_TRACE_BUFFER_SIZE. NCCL_DEBUG is already INFO for training.
export TORCH_NCCL_TRACE_BUFFER_SIZE=${NVIS_NCCL_TRACE_BUFFER_SIZE:-20000}
export TORCH_NCCL_DUMP_ON_TIMEOUT=${NVIS_NCCL_DUMP_ON_TIMEOUT:-1}
# Dump to the NFS-mounted /nccl_traces (see CONT_MOUNTS above) so traces survive container teardown.
export TORCH_NCCL_DEBUG_INFO_TEMP_FILE=/nccl_traces/trace_${SLURM_JOB_ID:-0}_rank

## Rule: num_gpus % (ep * et * tp * pp) == 0
#With CUDA graphs, then expandable segments must be disabled by "unset PYTORCH_CUDA_ALLOC_CONF".
#For arguments explanation, please refer to 
# https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/scripts/performance/argument_parser.py
#For model name and model size, please refer to 
# https://github.com/NVIDIA-NeMo/Megatron-Bridge/blob/main/scripts/performance/configs/nemotronh/nemotronh_workload_base_configs.py
numactl --cpunodebind=$[SLURM_LOCALID/4] --membind=$[SLURM_LOCALID/4] \
python /opt/Megatron-Bridge/scripts/performance/run_script.py \
  --account root \
  --partition defq \
  --log_dir ${NEMORUN_HOME} \
  --container_image ${NEMORUN_HOME}/nemo-25.11.01.sqsh \
  --nemo_home ${NEMORUN_HOME}/experiments \
  --gpu gb300 \
  --num_gpus $[4*${SLURM_NNODES}] \
  --gpus_per_node 4 \
  --compute_dtype fp8_cs \
  --model_name nemotronh --model_size 56b \
  --use_megatron_fsdp 0 \
  -tp 2 \
  -pp 1 \
  -cp 1 \
  -ep 1 \
  -mb 1 \
  -gb $[192*${SLURM_NNODES}/16] \
  --cuda_graph_impl transformer_engine --cuda_graph_scope mamba,attn \
  --max_steps ${MAX_STEPS} \
  train.manual_gc=true train.manual_gc_interval=100
__END__

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
set +x

e_time=$(date +%s)
echo "INFO: timelimit=$[(SLURM_JOB_END_TIME-SLURM_JOB_START_TIME)](s), real_took=$[e_time-s_time](s)"

if [ -n "${HC_CHECKER_SCRIPT:-}" ] && [ -x "$HC_CHECKER_SCRIPT" ]; then
pdsh -f 100 -R ssh -w ${SLURM_JOB_NODELIST} <<- EOF|dshbak -c
bash $HC_CHECKER_SCRIPT -e 1 -d
EOF
else
  echo "INFO: HC_CHECKER_SCRIPT not set or not executable; skipping post-run checker"
fi
