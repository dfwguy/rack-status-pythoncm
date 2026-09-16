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
# Mainterner: Junli Zhang<junliz@nvidia.com>
#
#SBATCH -p defq
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=00:30:00
#SBATCH -N 1

# ``run-llama.sh`` stages the configured, allowlisted source image and exports
# the exact compute-local path. Retain the historical fallback for direct/manual
# submissions, but never silently validate a different filename than was staged.
CONT="${NVIS_LLAMA_CONT:-/raid/data/nemo-25.11.01.sqsh}"
EXPECTED_CONT_SIZE="${NVIS_LLAMA_EXPECTED_SIZE:-}"
single_rack=1

module load slurm
cd ${SLURM_SUBMIT_DIR}

echo
echo "NODELIST: ${SLURM_JOB_NODELIST}"
echo

s_time=$(date +%s)
echo "INFO: cleaning work begin"
pdsh -f 64 -R ssh -w ${SLURM_JOB_NODELIST} <<- 'EOF'|dshbak -c
pkill -9 python &>/dev/null || true
ps -ef|grep python|grep -E 'torch|nemo'|grep -v grep || true
enroot list -f|grep pyxis && enroot remove -f $(enroot list -f|grep pyxis) || true
rm -f /var/lib/systemd/coredump/* &>/dev/null || true
EOF
echo "INFO: cleaning work done"

echo "INFO: checking hardware status"
pdsh -t 3 -u 3 -R ssh -f 36 -w $SLURM_NODELIST  <<- 'EOF' | dshbak -c
nvidia-smi -L|wc -l
nvidia-smi nvlink -s|grep [0-9].*GB|wc -l
ibstatus|grep -E 'Infiniband|state:'|paste - - -|grep roce|tr -s ' '
nvidia-imex-ctl -N|awk '/Nodes:/,/Domain State:/{print $0}'|sed -e 's:\*: :g' -e 's:-::g'|tr -s ' '
EOF

if [[ ! -r "$CONT" ]]; then
  echo "LLAMA_VALIDATION_FAIL stage=image reason=missing path=$CONT"
  exit 2
fi
actual_cont_size=$(stat -c %s "$CONT") || {
  echo "LLAMA_VALIDATION_FAIL stage=image reason=stat path=$CONT"
  exit 2
}
if [[ -n "$EXPECTED_CONT_SIZE" && "$actual_cont_size" != "$EXPECTED_CONT_SIZE" ]]; then
  echo "LLAMA_VALIDATION_FAIL stage=image reason=size path=$CONT expected=$EXPECTED_CONT_SIZE actual=$actual_cont_size"
  exit 2
fi
echo "LLAMA_VALIDATION_IMAGE_READY path=$CONT size=$actual_cont_size"

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
_nvis_lib="$(dirname "${BASH_SOURCE[0]}")/lib/fabric.inc.sh"
# shellcheck disable=SC1090
[[ -f "${_nvis_lib}" ]] && . "${_nvis_lib}"
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
#
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
export NCCL_P2P_LEVEL=NVL
export NCCL_NVLS_ENABLE=1
export NCCL_CUMEM_ENABLE=1
export NCCL_MIN_CTAS=16
# Single-node qualification does not need Multi-Node NVLink. The pinned five-
# collective recipe and the 56B NeMo launcher both disable this on NscaleTX
# because CUDA fabric-handle operations are denied on the current fleet.
export NCCL_MNNVL_ENABLE=0
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=ALL

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null

# test nccl
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
all_reduce_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20
exitcode=$?
if [ $exitcode -ne 0 ]; then
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  echo "LLAMA_VALIDATION_FAIL stage=nccl rc=$exitcode image=$CONT"
  exit 1
fi

# generate config files
srun -N ${SLURM_NNODES} --ntasks-per-node=1 --gpus-per-node=1 \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
bash <<- '__END__'
set -exo pipefail
cat > /usr/local/bin/sbatch <<- 'EOF'
#!/bin/bash
echo "sbatch $@"
EOF
chmod a+x /usr/local/bin/sbatch
cd /opt/NeMo
rm -f scripts/performance/recommended_model_configs/*.csv
export NEMORUN_HOME=/mnt/nemo
rm -rf ${NEMORUN_HOME}
mkdir -p ${NEMORUN_HOME}
## Rule: num_gpus % (ep * et * tp * pp) == 0
python -m scripts.performance.llm.pretrain_llama3_8b \
  --account root \
  --partition defq \
  --log_dir ${NEMORUN_HOME} \
  --container_image ${NEMORUN_HOME}/nemo-25.11.01.sqsh \
  --nemo_home ${NEMORUN_HOME}/experiments \
  --gpu gb200 \
  --compute_dtype fp8 --fp8_recipe cs \
  --num_gpus $[4*${SLURM_NNODES}] \
  -fsdp 0 \
  -tp 1 \
  -pp 1 \
  -cp 1 \
  -ep 1 \
  -mb 4 \
  -gb $[64*${SLURM_NNODES}] \
  --gpus_per_node 4 \
  --max_steps 110
find ${NEMORUN_HOME} -name '*_fn_or_script'
CASE=$(find ${NEMORUN_HOME} -name '*_fn_or_script' 2>/dev/null | head -1)
ls -l ${CASE}
__END__
exitcode=$?
if [ $exitcode -ne 0 ]; then
  srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
  echo "LLAMA_VALIDATION_FAIL stage=config rc=$exitcode image=$CONT"
  exit 1
fi

# run nemo test
srun --export=ALL --cpu-bind=none --mpi=pmix \
--container-name=nemo \
--container-writable \
--container-image=$CONT \
--container-mounts=/dev:/dev \
bash <<- '__END__'
set -exo pipefail
export NEMORUN_HOME=/mnt/nemo
CASE=$(find ${NEMORUN_HOME} -name '*_fn_or_script' 2>/dev/null | head -1)
ls -l ${CASE}
export CPATH=/usr/local/cuda/include
export TRITON_PTXAS_PATH=/usr/local/cuda/bin/ptxas
export TRITON_LIB_DIR=/usr/local/cuda/lib64
export TORCH_NCCL_AVOID_RECORD_STREAMS=1
export TRANSFORMERS_OFFLINE=1
export TOKENIZERS_PARALLELISM=False
export NCCL_NVLS_ENABLE=0
export NVTE_FLASH_ATTN=1
export NVTE_FUSED_ATTN=1
export NEMO_LOG_MEMORY_USAGE=1
export NEMO_HOME=${NEMORUN_HOME}
export CUDA_DEVICE_MAX_CONNECTIONS=32
export NVTE_FWD_LAYERNORM_SM_MARGIN=16
export NVTE_BWD_LAYERNORM_SM_MARGIN=16
export NCCL_P2P_NET_CHUNKSIZE=2097152
export TORCH_NCCL_HIGH_PRIORITY=1
export NCCL_DEBUG=INFO
python -m nemo_run.core.runners.fdl_runner -n $(basename ${CASE}|sed -e 's:_fn_or_script::g') ${CASE} 2>&1 | tee log.txt
__END__
exitcode=$?

srun -N ${SLURM_NNODES} --ntasks-per-node=1 enroot remove -f pyxis_nemo &>/dev/null
set +x

e_time=$(date +%s)
echo "INFO: timelimit=$[(SLURM_JOB_END_TIME-SLURM_JOB_START_TIME)](s), real_took=$[e_time-s_time](s)"
if [ $exitcode -ne 0 ]; then
  echo "LLAMA_VALIDATION_FAIL stage=training rc=$exitcode image=$CONT"
  exit 1
fi
echo "LLAMA_VALIDATION_PASS node=${SLURM_JOB_NODELIST} image=$CONT"
