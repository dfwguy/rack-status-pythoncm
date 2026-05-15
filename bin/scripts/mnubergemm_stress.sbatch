#!/bin/bash
# NVL72 stress variant of mnubergemm.sbatch: heavier CE, higher duty, ASCII --MM_force_cublas_algo.
# Uses WORKSPACE_DIR (NVIS workspace install): HPC-X from workspace-cuda-major-common UNPACK, CUDA cuda${CUDA_MAJ},
# NCCL from nccl/build when present. Set NVIS_BIN_SCRIPTS (run_mnubergemm_stress.sh or Flask).
# Node count / walltime can be overridden by sbatch CLI from run_mnubergemm_stress.sh.

#SBATCH -J mnubergemm-stress
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=02:30:00

module load slurm
cd "${SLURM_SUBMIT_DIR}"

: "${NVIS_BIN_SCRIPTS:?NVIS_BIN_SCRIPTS must point to bin/scripts (set by run_mnubergemm_stress.sh)}"

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-dir-common.sh"
: "${CUDA_MAJ:=13}"
export CUDA_MAJ

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-cuda-major-common.sh"

# HPC-X: prefer explicit NVIS_HPCX_ROOT (directory containing init scripts), else workspace unpack.
if [[ -n "${NVIS_HPCX_ROOT:-}" ]]; then
  _hpx="${NVIS_HPCX_ROOT}"
else
  _hpx="${WORKSPACE_DIR}/${UNPACK}"
fi
if [[ -f "${_hpx}/hpcx-mt-init-ompi.sh" ]]; then
  _hpcx_init="${_hpx}/hpcx-mt-init-ompi.sh"
elif [[ -f "${_hpx}/hpcx-init.sh" ]]; then
  _hpcx_init="${_hpx}/hpcx-init.sh"
else
  echo "ERROR: HPC-X init missing under ${_hpx} (set NVIS_HPCX_ROOT or install HPC-X under WORKSPACE_DIR=${WORKSPACE_DIR})." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "${_hpcx_init}"
hpcx_load

# CUDA toolkit from workspace install (same layout as install-cuda-nccl-workspace.sh).
if [[ -e "${WORKSPACE_DIR}/cuda${CUDA_MAJ}/bin/nvcc" ]]; then
  export CUDA_HOME="${WORKSPACE_DIR}/cuda${CUDA_MAJ}"
elif [[ -e "${WORKSPACE_DIR}/cuda/bin/nvcc" ]]; then
  export CUDA_HOME="${WORKSPACE_DIR}/cuda"
fi

# NCCL libs from workspace NCCL build, else optional explicit NVIS_NCCL_PATH, else legacy relative name.
if [[ -n "${NVIS_NCCL_PATH:-}" ]] && [[ -d "${NVIS_NCCL_PATH}/lib" || -d "${NVIS_NCCL_PATH}" ]]; then
  export NCCL_PATH="${NVIS_NCCL_PATH}"
elif [[ -d "${WORKSPACE_DIR}/nccl/build/lib" ]]; then
  export NCCL_PATH="${WORKSPACE_DIR}/nccl/build"
elif [[ -d "${WORKSPACE_DIR}/nccl/bins" ]]; then
  export NCCL_PATH="${WORKSPACE_DIR}/nccl/bins"
else
  export NCCL_PATH="${WORKSPACE_DIR}/nccl_2.28.9-1+cuda12.9_aarch64"
fi

_nccl_ld="${NCCL_PATH}/lib"
[[ -d "${_nccl_ld}" ]] || _nccl_ld="${NCCL_PATH}"

if [[ -n "${CUDA_HOME:-}" ]]; then
  export PATH="${CUDA_HOME}/bin:${PATH}"
  export LD_LIBRARY_PATH="${CUDA_HOME}/lib64:${_nccl_ld}:.:${LD_LIBRARY_PATH:-}"
else
  export LD_LIBRARY_PATH="${_nccl_ld}:.:${LD_LIBRARY_PATH:-}"
fi

echo "NODELIST=$SLURM_JOB_NODELIST WORKSPACE_DIR=${WORKSPACE_DIR} CUDA_HOME=${CUDA_HOME:-} NCCL_PATH=${NCCL_PATH}"
hosts=($(scontrol show hostname "$SLURM_JOB_NODELIST"))

nic=bond0
GPUS_PER_NODE=4

## Ref: https://confluence.nvidia.com/pages/viewpage.action?spaceKey=DCGM&title=Mndiag+and+mnubergemm+testing+on+GB200+Colorado+%28Equinix%29+cluster+8x+NVL72
duration_sec=$((SLURM_JOB_END_TIME - SLURM_JOB_START_TIME - 600))
[[ "$duration_sec" -le 0 ]] && duration_sec=86000
STD="--dynamic_adj --MM_max_workload 65536 --max_workload 65536 --time_to_run ${duration_sec} --workload GNC"
MM="--MM_M 0 --MM_N 4096 --MM_sm_count 112 --MM_type SX_SX_SSS --MM_force_cublas_algo SQAAABQAAAAiAAAAAQAAAAAAAAAAAAAAAAAAAAAACQAAAAAARdgBAAAAAAAAAAAAAAAAAE0AAAAAAAAAAAAAAA=="
NET="--NET_sm_count 32 --NET_link_order scatter_sum --NET_size 20480000"
CE="--CE_type H --CE_size 4194304"
FREQQ="--freq 400 --duty 0.98 --dual_mode --dual_freq 0.0013888889 --dual_duty 0.50"

: "${NVIS_SYSINFO_SH:=${NVIS_BIN_SCRIPTS}/sysinfo.sh}"
: "${NVIS_CHECKER_SH:=${NVIS_BIN_SCRIPTS}/checker.sh}"

set -x
#1. Check nodes status
timeout 120 bash "${NVIS_SYSINFO_SH}"
#2. Run mnubergemm
MNUBERGEMM=/cm/local/apps/cuda-dcgm/4.5.2/libexec/datacenter-gpu-manager-4/plugins/cuda13/mnubergemm
if [[ ! -x ${MNUBERGEMM} ]]; then
  MNUBERGEMM=/usr/libexec/datacenter-gpu-manager-4/plugins/cuda13/mnubergemm
fi
ldd "${MNUBERGEMM}"

export SLURM_CPU_BIND=none
export NCCL_NVLS_ENABLE=1
export NCCL_DEBUG=INFO
export NCCL_P2P_LEVEL=NVL
export NCCL_CUMEM_ENABLE=1
export NCCL_CUMEM_HOST_ENABLE=0
export NCCL_MNNVL_ENABLE=1
export NCCL_MIN_CTAS=64
export NCCL_IB_DISABLE=1
export UCX_TLS=tcp
export NCCL_SOCKET_IFNAME=${nic}
export UCX_NET_DEVICES=${nic}
export NCCL_IB_HCA=${nic}
export OMPI_ALLOW_RUN_AS_ROOT=1
export OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1
export OMPI_MCA_pml=ucx
export OMPI_MCA_coll_hcoll_enable=0
export OMPI_MCA_btl=^openib,smcuda
export OMPI_MCA_btl_tcp_if_include=${nic}
export OMPI_MCA_oob_tcp_if_include=${nic}
export OMPI_MCA_btl_openib_warn_default_gid_prefix=0

srun --export=ALL --cpu-bind=none --mpi=pmix --label \
  "${MNUBERGEMM}" ${STD} ${FREQQ} ${MM} ${NET} ${CE}
#3. Check nodes' healthiness, drain if any issue found
_checker_q=$(printf '%q' "${NVIS_CHECKER_SH}")
# shellcheck disable=SC2029
pdsh -R ssh -f 32 -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  "bash ${_checker_q} -e 2>/dev/null | grep -v PASS || true"
set +x
