#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
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
# Container to run. run-nemo-xrack.sh / run_singlerack.sh place NEMO_SQSH_SRC in the cluster's
# node image cache (NVIS_NODE_CONTAINER_DIR) and pass the path via NVIS_NEMO_CONT, so pointing
# NEMO_SQSH_SRC at the vetted image actually switches the container here. Falls back to the stock
# image in that cache for direct/manual runs.
CONT="${NVIS_NEMO_CONT:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nemo-25.11.01.sqsh}"
SYSINFO_SH=${SLURM_SUBMIT_DIR}/sysinfo.sh
GPUS_PER_NODE="${GPUS_PER_NODE:-4}"

single_rack=1
[ $SLURM_NNODES -gt 18 ] && single_rack=0
# Force multi-rack (IB + auto-MNNVL) path regardless of node count. The heuristic above assumes
# <=18 nodes == one NVL72 rack, but a SMALL job whose nodes span >1 rack (>1 NVLink/IMEX domain)
# MUST use IB between racks — the single-rack path sets NCCL_IB_DISABLE=1 and forces
# NCCL_MNNVL_ENABLE=1, and MNNVL cannot cross a rack boundary ("Cuda failure 400 invalid resource
# handle"). run-nemo-xrack.sh sets NVIS_NEMO_XRACK=1 automatically when the allocation spans >1
# rack. NVIS_NEMO_XRACK=1 forces multi-rack; =0 forces single-rack; unset keeps the count heuristic.
if [ -n "${NVIS_NEMO_XRACK:-}" ]; then
  if [ "${NVIS_NEMO_XRACK}" = 1 ]; then single_rack=0; else single_rack=1; fi
fi
echo "INFO: nnodes=${SLURM_NNODES} single_rack=${single_rack} (NVIS_NEMO_XRACK=${NVIS_NEMO_XRACK:-<unset>})"
if [ "${SLURM_NNODES}" -ne 1 ] && [ $(( ${SLURM_NNODES} % 2 )) != 0 ]
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
srun --nodes="${SLURM_NNODES}" --ntasks="${SLURM_NNODES}" --ntasks-per-node=1 \
  --kill-on-bad-exit=1 --label bash -euo pipefail -c '
    pkill -9 python &>/dev/null || true
    ps -ef | grep python | grep -E "torch|nemo" | grep -v grep || true
    enroot list -f | grep pyxis && enroot remove -f $(enroot list -f | grep pyxis) || true
    rm -f /var/lib/systemd/coredump/* &>/dev/null || true
  '
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
# rate on IB; routable-GID, off-the-default-route ports on RoCE). No mlx5_N fallback.
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
  "${RACK_REMOTE_BASE_PATH:+${RACK_REMOTE_BASE_PATH}/bin/scripts/lib/fabric.inc.sh}"; do
  [[ -n "${_cand}" && -f "${_cand}" ]] && { _nvis_lib="${_cand}"; break; }
done
# shellcheck disable=SC1090
if [[ -n "${_nvis_lib}" ]]; then
  echo "INFO: fabric classifier: ${_nvis_lib}"
  . "${_nvis_lib}"
else
  echo "WARN: lib/fabric.inc.sh not found (set NVIS_SCRIPTS_DIR); NCCL/UCX will auto-detect the cross-rack plane"
fi
if declare -F nvis_fabric_scan >/dev/null 2>&1; then eval "$(nvis_fabric_scan 2>/dev/null)"; fi
ns_nic="${NVIS_FAB_NS_IFNAME:-$ns_nic}"
echo "INFO: fabric ns_nic=${ns_nic}"
# An IB-disabled preflight can skip enroot's Mellanox hook on nodes without
# /sys/class/infiniband_mad. Keep the existing default for fabric-backed runs.
export MELLANOX_VISIBLE_DEVICES="${NVIS_NEMO_MLX:-all}"
export PMIX_MCA_gds=hash
#
if [ $single_rack -eq 1 ]; then
  export UCX_TLS=tcp
  export NCCL_IB_DISABLE=1
  export UCX_NET_DEVICES=${ns_nic}
else
  # Cross-rack plane: this node's exact rails (=list), the RoCE address family and the cluster's
  # stated plane, all from lib/fabric.inc.sh -- never a guessed mlx5_0..3, which mixed IB and RoCE
  # NICs on one cluster and named no rail at all on GB300 NscaleTX-NC (rails roce_vf_r1..4, IPv6).
  # MPI's own transport then follows the fabric network: RC on InfiniBand rails, TCP on the routable
  # bootstrap netdev over RoCE (see nvis_fabric_mpi_xrack_transport).
  if declare -F nvis_fabric_export_pinned >/dev/null 2>&1; then
    nvis_fabric_export_pinned || echo "WARN: no compute rails detected; NCCL/UCX auto-detect the cross-rack plane"
  else
    echo "WARN: lib/fabric.inc.sh not loaded; NCCL/UCX auto-detect the cross-rack plane"
  fi
  if declare -F nvis_fabric_mpi_xrack_transport >/dev/null 2>&1; then
    nvis_fabric_mpi_xrack_transport
  else
    export UCX_TLS=rc
  fi
fi
# NVLS: on within one NVLink domain; across racks, off where the GPU fabric CliqueId is unset (see
# nvis_fabric_xrack_nvls). NVIS_NEMO_NVLS_ENABLE, when the launch sets it, always wins.
_nvls_default=1
if [ $single_rack -eq 0 ] && declare -F nvis_fabric_xrack_nvls >/dev/null 2>&1; then
  _nvls_default="$(nvis_fabric_xrack_nvls)"
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
export NCCL_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-${_nvls_default}}
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
  NCCL_PRELOAD_SRC="${NVIS_NCCL_PRELOAD_SRC:-${NVIS_WORKSPACE_DIR:+${NVIS_WORKSPACE_DIR%/}/nccl/bins}}"
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

# Optional HF cache overlay: pre-staged tokenizer/config on NFS for OFFLINE compute nodes (some recipes,
# e.g. nemotron_3_nano, need a HuggingFace tokenizer the nodes can't fetch). Mount it in + point HF_HOME
# there + run offline. Pre-cache on the head (has network): HF_TOKEN=.. HF_HOME=<dir> huggingface_hub
# snapshot_download('<repo>', allow_patterns=['*.json','tokenizer*',...]).
if [ -n "${NVIS_NEMO_HF_HOME:-}" ]; then
  CONT_MOUNTS="${CONT_MOUNTS},${NVIS_NEMO_HF_HOME}:/hf-cache"
  export NVIS_HF_HOME_CONT=/hf-cache
  echo "INFO: HF cache overlay -> ${NVIS_NEMO_HF_HOME} (container:/hf-cache); HF_HOME=/hf-cache, offline"
fi

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null

## test nccl
# NCCL pre-flight verbosity — default WARN (quiet). Override from the UI/launch (NVIS_NCCL_DEBUG=
# INFO|TRACE) to diagnose a silent cross-rack hang: INFO names the stuck rank/peer connection.
export NCCL_DEBUG="${NVIS_NCCL_DEBUG:-WARN}"
export NCCL_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-${_nvls_default}}

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

if [ "${NVIS_NEMO_PREFLIGHT_ONLY:-0}" = 1 ]; then
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  e_time=$(date +%s)
  echo "INFO: NCCL preflight-only validation completed in $[e_time-s_time](s)"
  echo "NCCL_PREFLIGHT_PASS nodes=${SLURM_NNODES} gpus_per_node=${GPUS_PER_NODE}"
  exit 0
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
# Training-phase verbosity — default INFO; overridable from the UI/launch via NVIS_NCCL_DEBUG.
export NCCL_DEBUG="${NVIS_NCCL_DEBUG:-INFO}"
# NVLS multicast + UserBuffers multicast. BF16 exercises the TP-comm-overlap UserBuffers path harder
# than FP8; on a marginal NVLink/NVSwitch multicast state this throws "CUDA Error ... create_communicator
# grouped2" / "Cuda failure 226 invalid peer access over nvlink". Documented workaround: UB_SKIPMC=1
# (UserBuffers fall back to IPC; keeps NVLS for NCCL). Set NVIS_NEMO_UB_SKIPMC=1 (and optionally
# NVIS_NEMO_NVLS_ENABLE=0) for BF16 acceptance on GB200 if the full-multicast path is unstable.
export NCCL_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-${_nvls_default}}
export UB_SKIPMC=${NVIS_NEMO_UB_SKIPMC:-0}
# export MAX_STEPS=2010 #For stress test, you can set a large number for MAX_STEPS. 
#export MAX_STEPS=110
# Stage-2 soak defaults (2010 steps, fp8, GBS=12*nodes). Stage-3 LLM acceptance overrides these via
# env so the SAME engine serves both: the soak AND the short perf benchmark (e.g. NVIS_NEMO_MAX_STEPS=50,
# NVIS_NEMO_DTYPE=bf16). compute_dtype choices: bf16|fp8_cs|fp8_mx|fp8_sc|nvfp4. GBS must stay divisible
# by the data-parallel size (TP=2 -> DP=nodes*2); the 12*nodes default satisfies this (18N->216, 36N->432).
export MAX_STEPS=${NVIS_NEMO_MAX_STEPS:-2010}
export NVIS_NEMO_DTYPE=${NVIS_NEMO_DTYPE:-fp8_cs}
export NVIS_NEMO_MODEL_SIZE=${NVIS_NEMO_MODEL_SIZE:-56b}
export NVIS_NEMO_GBS=${NVIS_NEMO_GBS:-$((192*SLURM_NNODES/16))}
# Parallelism/overlap args. Default: explicit perf config (tp2/pp1/gbs, FSDP off, TE CUDA graphs) —
# preserves the validated FP8 soak. NVIS_NEMO_RECIPE_DEFAULT=1 drops ALL of these so run_script.py
# uses the DGXC recipe's built-in optimal parallelism for the model/size/dtype (mirrors Mayo's
# run_nemotronh.sh, which passed no tp/pp/mb/gb) — use for BF16 where the hardcoded config OOMs.
if [ "${NVIS_NEMO_RECIPE_DEFAULT:-0}" = 1 ]; then
  export NVIS_NEMO_EXTRA_ARGS=""
else
  export NVIS_NEMO_EXTRA_ARGS="--use_megatron_fsdp 0 -tp 2 -pp 1 -cp 1 -ep 1 -mb 1 -gb ${NVIS_NEMO_GBS} --cuda_graph_impl transformer_engine --cuda_graph_scope mamba,attn"
fi
# Container CLI style + GPU config token. legacy = 25.11.01 (--model_name/--model_size);
# v2 = 26.02.01+ (--model_family_name/--model_recipe_name/--offline; has the precision-decoupled
# recipe so BF16 resolves a config). GPU token (gb200/gb300/b200/h100) selects the per-platform base.
export NVIS_NEMO_GPU_TYPE=${NVIS_NEMO_GPU:-gb300}
if [ "${NVIS_NEMO_CLI:-v2}" = "v2" ]; then
  export NVIS_NEMO_MODEL_ARGS="--model_family_name nemotronh --model_recipe_name ${NVIS_NEMO_RECIPE_NAME:-nemotronh_${NVIS_NEMO_MODEL_SIZE}}"
else
  export NVIS_NEMO_MODEL_ARGS="--model_name nemotronh --model_size ${NVIS_NEMO_MODEL_SIZE}"
fi
echo "INFO: LLM params model=nemotronh size=${NVIS_NEMO_MODEL_SIZE} dtype=${NVIS_NEMO_DTYPE} gbs=${NVIS_NEMO_GBS} max_steps=${MAX_STEPS} recipe_default=${NVIS_NEMO_RECIPE_DEFAULT:-0} cli=${NVIS_NEMO_CLI:-v2} gpu=${NVIS_NEMO_GPU_TYPE}"
echo "INFO: model_args=[${NVIS_NEMO_MODEL_ARGS}] extra_args=[${NVIS_NEMO_EXTRA_ARGS}]"
train_rc=0
srun --export=${EXPORT_ARG} --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=${CONT_MOUNTS} \
bash <<- '__END__'
set -ex
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HOME=${NVIS_HF_HOME_CONT:-/root/.cache/huggingface}

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
# Mayo-proven NemotronH perf/stability env (from the delivered DGXC recipe, dgxc-runai/run_nemotronh.sh).
# NCCL_GRAPH_REGISTER=0 is REQUIRED with expandable_segments + CUDA graphs (else "illegal memory access");
# the NVTE amax knobs match the recipe. NVLS multicast is controlled outside via NVIS_NEMO_NVLS_ENABLE
# (set 0 for BF16 to avoid the UserBuffers reduce-scatter / Cuda-226 multicast failure seen on GB200).
export NCCL_GRAPH_REGISTER=0
export NVTE_DP_AMAX_REDUCE_INTERVAL=0
export NVTE_ASYNC_AMAX_REDUCTION=1
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

# NVIS BF16 force-enable for nemotronh-56b (benchmark-only). The DGXC recipe ships FP8_CS base
# configs ONLY; the workload base config is precision-INDEPENDENT parallelism (actual precision is
# applied separately via get_precision_config("bf16")=bf16_mixed). So alias the BF16 config names to
# the FP8_CS_V1 base so get_workload_base_config() resolves, then --compute_dtype bf16 runs true BF16.
# flock-serialized + idempotent (one writer per node). NOTE: this is a FORCE-ENABLED, NVIDIA-unvalidated
# BF16 config for this model — use for HW throughput benchmarking, label it as such in any report.
if [ "${NVIS_NEMO_FORCE_BF16_ALIAS:-0}" = 1 ]; then
  _nvcfg=/opt/Megatron-Bridge/scripts/performance/configs/nemotronh/__init__.py
  (
    flock 9
    if ! grep -q "NVIS BF16 alias" "$_nvcfg" 2>/dev/null; then
      {
        echo ""
        echo "# NVIS BF16 alias (force-enable, benchmark-only): base config is precision-independent parallelism"
        echo "NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_BF16_V1 = NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_FP8_CS_V1"
        echo "NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_BF16_V2 = NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_FP8_CS_V1"
        echo "NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_BF16 = NEMOTRONH_56B_PRETRAIN_CONFIG_GB200_FP8_CS_V1"
      } >> "$_nvcfg"
      echo "INFO: NVIS BF16 alias injected into $_nvcfg"
    fi
  ) 9>/tmp/nvis_bf16_patch.lock
fi

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
  --gpu ${NVIS_NEMO_GPU_TYPE} \
  --num_gpus $[${GPUS_PER_NODE}*${SLURM_NNODES}] \
  --gpus_per_node ${GPUS_PER_NODE} \
  --compute_dtype ${NVIS_NEMO_DTYPE} \
  ${NVIS_NEMO_MODEL_ARGS} \
  ${NVIS_NEMO_EXTRA_ARGS} \
  --max_steps ${MAX_STEPS} \
  train.manual_gc=true train.manual_gc_interval=100
__END__
train_rc=$?

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
set +x

e_time=$(date +%s)
echo "INFO: timelimit=$[(SLURM_JOB_END_TIME-SLURM_JOB_START_TIME)](s), real_took=$[e_time-s_time](s)"

if [ -n "${HC_CHECKER_SCRIPT:-}" ] && [ -x "$HC_CHECKER_SCRIPT" ]; then
postcheck_rc=0
srun --nodes="${SLURM_NNODES}" --ntasks="${SLURM_NNODES}" --ntasks-per-node=1 \
  --kill-on-bad-exit=1 --label bash "$HC_CHECKER_SCRIPT" -e 1 -d || postcheck_rc=$?
else
  echo "INFO: HC_CHECKER_SCRIPT not set or not executable; skipping post-run checker"
  postcheck_rc=0
fi

if (( train_rc != 0 )); then
  echo "ERROR: NeMo training srun failed rc=${train_rc}" >&2
  exit "$train_rc"
fi
if (( postcheck_rc != 0 )); then
  echo "ERROR: NeMo post-run checker failed rc=${postcheck_rc}" >&2
  exit "$postcheck_rc"
fi
echo "NEMO_VALIDATION_PASS max_steps=${MAX_STEPS} nodes=${SLURM_NNODES}"
