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
# nccl_a2a_debug.sbatch — single cross-rack alltoall_perf_mpi run with NCCL_DEBUG=INFO.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY: root-cause the multi-domain NVL72 "bootstrap Message truncated (512 vs 256)" that fails
# cross-rack alltoall on BOTH NCCL 2.28.9 and 2.30.7 (jobs 198/199) while all-reduce PASSES. INFO
# always emits the NCCL version banner first (so we CONFIRM which libnccl actually loaded per rank —
# no guessing from the container name) and then the full INIT/BOOTSTRAP/NET/GRAPH trace up to the
# truncation. Fails fast at init, so the log is bounded even at INFO.
#
# RUN: sbatch -N<cross-rack pair> -w <nodelist> \
#        --export=ALL,NVIS_SCRIPTS_DIR=<scripts dir>,\
#                 NVIS_NEMO_CONT=<node image cache>/nemo-nccl2.30.7.sqsh \
#        --output=/home/cmods/workspace/nccl_a2a_debug-%j.log nccl_a2a_debug.sbatch
#SBATCH --job-name=nccl-a2a-dbg
#SBATCH --exclusive
#SBATCH --ntasks-per-node=4
#SBATCH --cpus-per-task=36
#SBATCH --gpus-per-node=4
#SBATCH --time=00:15:00
set -uo pipefail

CONT="${NVIS_NEMO_CONT:-${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}/nemo-nccl2.30.7.sqsh}"
SCR="${NVIS_SCRIPTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SIZES="${NVIS_A2A_SIZES:--b 8 -e 1M -f 2 -g 1 --iters 2}"   # tiny: bug hits at init, before real xfer

echo "== nccl_a2a_debug: NNODES=${SLURM_NNODES} CONT=${CONT} =="
echo "== nodelist: ${SLURM_JOB_NODELIST} =="

# --- Fabric env: identical to the multi-rack NeMo / A/B path -----------------------------------
# shellcheck disable=SC1090,SC1091
[[ -f "${SCR}/lib/fabric.inc.sh" ]] && . "${SCR}/lib/fabric.inc.sh"
# Rails (pinned exactly, =list), RoCE address family, the bootstrap NIC and the MPI transport
# defaults all come from the shared detector. This used to hard-code ns_nic=bond0 and fall back to
# mlx5_0,1,4,5 -- GB300 NscaleTX-NC has no bond0 and its rails are roce_vf_r1..4.
if declare -F nvis_fabric_export_pinned >/dev/null 2>&1; then nvis_fabric_export_pinned || true; fi
export MELLANOX_VISIBLE_DEVICES=all PMIX_MCA_gds=hash
export UCX_TLS=rc
export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 OMPI_MCA_coll_hcoll_enable=0 SLURM_CPU_BIND=none
export NCCL_NVLS_ENABLE=1 NCCL_MIN_CTAS=16 NCCL_RUNTIME_CONNECT=0 NCCL_IB_QPS_PER_CONNECTION=2 CUDA_MODULE_LOADING=EAGER
unset NCCL_MNNVL_ENABLE

# --- The whole point: verbose NCCL so we SEE the version + bootstrap path ----------------------
export NCCL_DEBUG="${NVIS_NCCL_DEBUG:-INFO}"
export NCCL_DEBUG_SUBSYS="${NVIS_NCCL_DEBUG_SUBSYS:-INIT,BOOTSTRAP,NET,ENV,GRAPH}"
echo "== NCCL_DEBUG=${NCCL_DEBUG} SUBSYS=${NCCL_DEBUG_SUBSYS} NCCL_IB_HCA=${NCCL_IB_HCA} =="

# UNIQUE per-job container name — pyxis reuses a fixed --container-name and IGNORES --container-image
# (and the old `enroot remove -f nccl_dbg` never matched the pyxis-prefixed "pyxis_nccl_dbg"), so a
# fixed name silently runs a stale container/image. Per-job name = guaranteed fresh import of ${CONT}.
CNAME="nccl_dbg_${SLURM_JOB_ID}"
srun -N"${SLURM_NNODES}" --ntasks-per-node=1 enroot remove -f "pyxis_${CNAME}" &>/dev/null || true
timeout 200 srun --export=ALL --cpu-bind=none --mpi=pmix \
  --container-name="${CNAME}" --container-writable --container-image="${CONT}" --container-mounts=/dev:/dev \
  alltoall_perf_mpi ${SIZES}
rc=$?
srun -N"${SLURM_NNODES}" --ntasks-per-node=1 enroot remove -f "pyxis_${CNAME}" &>/dev/null || true
echo "== alltoall_perf_mpi rc=${rc} =="
