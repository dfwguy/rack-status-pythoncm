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

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/nvis-ucx-job-env.inc.sh"

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
##
## NOTE: this sbatch is the SINGLE-NODE per-host ubergemm soak (a GPU power/thermal/GEMM
## stress, srun-ed directly on each allocated node). The real MULTI-NODE NVLink/fabric
## stress is `dcgmi mndiag -r mnubergemm` (DCGM 4.3+) which orchestrates the hidden
## `mnubergemm` MPI binary itself — see run_mndiag_mnubergemm.sh. The two binaries differ:
##   * ubergemm        — single-node GEMM kernel (DCGM 4.6 ships v1.31; GEMM-only CLI).
##   * mnubergemm      — multi-node MPI app, launched ONLY via `dcgmi mndiag`, not srun.
## DCGM 4.6's `ubergemm 1.31` dropped the legacy --NET_*/--CE_*/--MM_max_workload/
## --MM_sm_count options, so we build the arg set from what the discovered binary supports.
duration_sec=$((SLURM_JOB_END_TIME - SLURM_JOB_START_TIME - 600))
[[ "$duration_sec" -le 0 ]] && duration_sec=86000
# Legacy multi-engine mnubergemm args (kept for clusters that ship the old binary).
STD="--dynamic_adj --MM_max_workload 65536 --max_workload 65536 --time_to_run ${duration_sec} --workload GNC"
MM="--MM_M 0 --MM_N 4096 --MM_sm_count 112 --MM_type SX_SX_SSS --MM_force_cublas_algo SQAAABQAAAAiAAAAAQAAAAAAAAAAAAAAAAAAAAAACQAAAAAARdgBAAAAAAAAAAAAAAAAAE0AAAAAAAAAAAAAAA=="
NET="--NET_sm_count 32 --NET_link_order scatter_sum --NET_size 20480000"
CE="--CE_type H --CE_size 4194304"
# Frequency/duty shaping — valid on both legacy mnubergemm and ubergemm 1.31.
FREQQ="--freq 400 --duty 0.98 --dual_mode --dual_freq 0.0013888889 --dual_duty 0.50"

: "${NVIS_SYSINFO_SH:=${NVIS_BIN_SCRIPTS}/sysinfo.sh}"
: "${NVIS_CHECKER_SH:=${NVIS_BIN_SCRIPTS}/checker.sh}"

# sysinfo.sh / checker.sh are root-only HW health checks. This soak job usually runs as a
# non-root Slurm user, so prefer passwordless sudo when available; otherwise run directly
# (they print "Please run as root" and are skipped — non-fatal pre/post snapshots).
if [[ "$(id -u)" -eq 0 ]]; then
  _ROOT_RUN=""
elif sudo -n true 2>/dev/null; then
  _ROOT_RUN="sudo -n"
else
  _ROOT_RUN=""
  echo "WARN: not root and passwordless sudo unavailable; sysinfo/checker health snapshots will be skipped." >&2
fi

set -x
#1. Check nodes status
timeout 120 ${_ROOT_RUN} bash "${NVIS_SYSINFO_SH}" || true
#2. Run mnubergemm. The binary name/location varies by DCGM build: it ships as
#   `mnubergemm` on some clusters and `ubergemm` on others, sometimes tucked under an
#   `updated/` subdir, and under cuda12 or cuda13 plugin trees. Probe a candidate matrix
#   (honor NVIS_MNUBERGEMM_BIN to pin an explicit path) instead of hard-coding one path.
MNUBERGEMM="${NVIS_MNUBERGEMM_BIN:-}"
if [[ -z "${MNUBERGEMM}" || ! -x "${MNUBERGEMM}" ]]; then
  MNUBERGEMM=""
  # Search the workspace DCGM extract first (install-dcgm-workspace.sh, shared /home),
  # then the system DCGM trees. The workspace binary is self-contained: its libdcgm*.so
  # resolve via a baked-in relative RPATH ($ORIGIN/../../../../../lib/<triplet>).
  for _root in "${WORKSPACE_DIR}/dcgm/current/usr/libexec/datacenter-gpu-manager-4/plugins" \
               "${WORKSPACE_DIR}/dcgm/root/usr/libexec/datacenter-gpu-manager-4/plugins" \
               /cm/local/apps/cuda-dcgm/*/libexec/datacenter-gpu-manager-4/plugins \
               /usr/libexec/datacenter-gpu-manager-4/plugins; do
    for _cu in "cuda${CUDA_MAJ}" cuda13 cuda12; do
      for _sub in "" "/updated"; do
        for _name in mnubergemm ubergemm; do
          _cand="${_root}/${_cu}${_sub}/${_name}"
          if [[ -x "${_cand}" ]]; then MNUBERGEMM="${_cand}"; break 4; fi
        done
      done
    done
  done
fi
if [[ -z "${MNUBERGEMM}" || ! -x "${MNUBERGEMM}" ]]; then
  echo "ERROR: mnubergemm/ubergemm binary not found under the DCGM plugins tree." >&2
  echo "       searched WORKSPACE_DIR/dcgm + cuda${CUDA_MAJ}/cuda13/cuda12 [+/updated] for names mnubergemm, ubergemm." >&2
  echo "       Install via Tools -> Operations & Scripts -> Install DCGM/DCGMI, or pin NVIS_MNUBERGEMM_BIN=/abs/path." >&2
  exit 1
fi
# If the binary lives in an extracted DCGM tree (…/usr/libexec/datacenter-gpu-manager-4/…),
# also prepend its usr/lib dirs to LD_LIBRARY_PATH as a fallback for builds without RPATH.
_dcgm_usr="${MNUBERGEMM%/libexec/datacenter-gpu-manager-4/*}"
if [[ "${_dcgm_usr}" != "${MNUBERGEMM}" && -d "${_dcgm_usr}" ]]; then
  for _libd in "${_dcgm_usr}/lib/aarch64-linux-gnu" "${_dcgm_usr}/lib/x86_64-linux-gnu" \
               "${_dcgm_usr}/lib64" "${_dcgm_usr}/lib"; do
    [[ -d "${_libd}" ]] && export LD_LIBRARY_PATH="${_libd}:${LD_LIBRARY_PATH:-}"
  done
fi
echo "Using mnubergemm binary: ${MNUBERGEMM}"
ldd "${MNUBERGEMM}" || true

# Pick the arg set from what THIS binary actually supports. The legacy multi-engine
# mnubergemm exposes --NET_*/--CE_* (workload "GNC"); DCGM 4.6's single-node ubergemm
# 1.31 dropped them and rejects --MM_max_workload/--MM_sm_count too (exit 2 on parse).
# Probe --help (bounded by timeout in case a build runs instead of printing usage).
_ug_help="$(timeout 20 "${MNUBERGEMM}" --help 2>&1 || true)"
if printf '%s' "${_ug_help}" | grep -q -- '--NET_sm_count'; then
  echo "Detected legacy multi-engine mnubergemm CLI (NET/CE supported)."
  UBERGEMM_ARGS="${STD} ${FREQQ} ${MM} ${NET} ${CE}"
else
  echo "Detected single-node ubergemm CLI (GEMM-only; NET/CE/MM_max_workload not supported)."
  # GEMM power/thermal soak: high duty, dual waveform, dynamic adjust, full walltime.
  UBERGEMM_ARGS="--dynamic_adj --max_workload 65536 --time_to_run ${duration_sec} --workload G ${FREQQ}"
fi
echo "ubergemm args: ${UBERGEMM_ARGS}"

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
  "${MNUBERGEMM}" ${UBERGEMM_ARGS}
#3. Check nodes' healthiness, drain if any issue found
_checker_q=$(printf '%q' "${NVIS_CHECKER_SH}")
# checker.sh hard-requires root; use the same sudo decision as above (per-node over pdsh,
# each remote shell re-evaluates sudo -n so this works even if the local node differs).
# shellcheck disable=SC2029
pdsh -R ssh -f 32 -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  "if [ \"\$(id -u)\" -ne 0 ] && sudo -n true 2>/dev/null; then _R='sudo -n'; else _R=''; fi; \$_R bash ${_checker_q} -e 2>/dev/null | grep -v PASS || true"
set +x
