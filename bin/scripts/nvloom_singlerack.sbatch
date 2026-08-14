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
# Single-rack / multi-node nvloom (Slurm): fabric / gpu-to-rack tests via nvloom_cli.
# Uses HPC-X and nvloom_cli under WORKSPACE_DIR (same layout as install-nvloom-workspace.sh / workspace-cuda-major-common).
# Node count and stdout path come from run_nvloom_singlerack.sh (sbatch CLI overrides).
# UCX: sources nvis-ucx-job-env.inc.sh after hpcx_load; optional NVIS_NVLOOM_UCX_TLS overrides UCX_TLS for nvloom.

#SBATCH --job-name=nvloom-rack-scale
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --gpus-per-node=4
#SBATCH --time=02:00:00

set -euo pipefail

cd "${SLURM_SUBMIT_DIR}"

: "${RACK_SAMPLES:=5}"
: "${NVIS_BIN_SCRIPTS:?NVIS_BIN_SCRIPTS must point to bin/scripts (set by run_nvloom_singlerack.sh)}"
module load boost


# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-dir-common.sh"
: "${CUDA_MAJ:=13}"
export CUDA_MAJ

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/workspace-cuda-major-common.sh"

NVLOOM_CLI_DEFAULT="${WORKSPACE_DIR}/nvloom/nvloom_cli"
INIT_DEFAULT="${WORKSPACE_DIR}/${UNPACK}/hpcx-init.sh"
if [[ ! -f "${INIT_DEFAULT}" ]]; then
  INIT_DEFAULT="${WORKSPACE_DIR}/${UNPACK}/hpcx-mt-init-ompi.sh"
fi

# Optional full-path overrides (site); otherwise workspace install paths.
if [[ -n "${NVIS_NVLOOM_HPCX_INIT:-}" ]] && [[ -f "${NVIS_NVLOOM_HPCX_INIT}" ]]; then
  INIT="${NVIS_NVLOOM_HPCX_INIT}"
else
  INIT="${INIT_DEFAULT}"
fi

if [[ ! -f "${INIT}" ]]; then
  echo "ERROR: HPC-X init not found under WORKSPACE_DIR=${WORKSPACE_DIR} (UNPACK=${UNPACK}). Install HPC-X workspace or set NVIS_NVLOOM_HPCX_INIT." >&2
  exit 1
fi
# hpcx-init.sh uses [[ "${HPCX_ENABLE_NCCL_MRC_PLUGIN}" == "1" ]] etc.; with set -u those must be set first.
export HPCX_ENABLE_NCCL_MRC_PLUGIN="${HPCX_ENABLE_NCCL_MRC_PLUGIN:-0}"
export HPCX_ENABLE_NCCL_SPCX_PLUGIN="${HPCX_ENABLE_NCCL_SPCX_PLUGIN:-0}"
# shellcheck disable=SC1090
source "${INIT}"
hpcx_load

# shellcheck disable=SC1091
. "${NVIS_BIN_SCRIPTS}/nvis-ucx-job-env.inc.sh"
# nvloom gpu-to-rack / fabric-stress: avoid a wide default UCX_TLS stack on RoCE (rdmacm CM pressure).
if [[ -n "${NVIS_NVLOOM_UCX_TLS:-}" ]]; then
  export UCX_TLS="${NVIS_NVLOOM_UCX_TLS}"
elif [[ -z ${UCX_TLS+x} ]]; then
  export UCX_TLS="rc,cuda_copy,cuda_ipc,cuda,self"
fi

if [[ -z "${NVIS_SYSINFO_SH:-}" ]] || [[ ! -f "${NVIS_SYSINFO_SH}" ]]; then
  echo "ERROR: NVIS_SYSINFO_SH must point to an existing sysinfo.sh (set by the dashboard or export before sbatch)." >&2
  exit 1
fi

bash "${NVIS_SYSINFO_SH}"

echo "### Running nvidia-imex-ctl -H"
nvidia-imex-ctl -H

if [[ -n "${NVIS_NVLOOM_CLI:-}" ]]; then
  NVLOOM_CLI="${NVIS_NVLOOM_CLI}"
else
  NVLOOM_CLI="${NVLOOM_CLI_DEFAULT}"
fi

if [[ ! -e "${NVLOOM_CLI}" ]]; then
  echo "ERROR: nvloom_cli not found at ${NVLOOM_CLI} — build nvloom under ${WORKSPACE_DIR}/nvloom or set NVIS_NVLOOM_CLI." >&2
  exit 1
fi

set -x
srun --export=ALL --label \
  "${NVLOOM_CLI}" -s gpu-to-rack pairwise fabric-stress all-to-one multicast \
  --gpuToRackSamples "${RACK_SAMPLES}"
set +x

echo "OK: nvloom_cli finished on ${SLURM_JOB_NODELIST:-?}"
