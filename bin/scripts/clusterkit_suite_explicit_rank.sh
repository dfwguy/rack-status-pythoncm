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
# One MPI rank, invoked only by the bounded two-node batch entrypoint.
set -eo pipefail
export HPCX_ENABLE_NCCL_MRC_PLUGIN=0 HPCX_ENABLE_NCCL_SPCX_PLUGIN=0
source "$NVIS_CLUSTERKIT_HPCX_INIT"
hpcx_load
stage="${RACK_CLUSTERKIT_SUITE_STAGE:?}"
export UCX_NET_DEVICES="$RACK_CLUSTERKIT_SUITE_HCA"
export UCX_TLS=rc,sm,self UCX_LOG_LEVEL="${RACK_CLUSTERKIT_SUITE_LOG_LEVEL:-info}" UCX_VFS_ENABLE=n
export UCX_LOG_FILE="$RACK_CLUSTERKIT_SUITE_OUTDIR/ucx-$stage-%h-%p.log"
export UCX_LOG_FILE_SIZE=16mb UCX_LOG_FILE_ROTATE=1
export OMPI_MCA_pml=ucx OMPI_MCA_coll_hcoll_enable=0 OMPI_MCA_coll_ucc_enable=0
# The helper pins only the CPU assigned by Slurm; MPI must preserve that mask.
export OMPI_MCA_hwloc_base_binding_policy=none
export HCA_TAG="$UCX_NET_DEVICES"
# Keep UCX's normal path policy explicit so inherited settings cannot skew it.
export UCX_RC_MLX5_NUM_PATHS=auto
export CUDA_VISIBLE_DEVICES=""
unset CK_OUTPUT_SUBDIR
args=(-d bw -d lat --biters=8 --bsize=8388608 --liters=256 --lsize=8)
case "$stage" in
  baseline)
    case "${RACK_CLUSTERKIT_SUITE_GID_POLICY:-global}" in
      global)
        export UCX_IB_GID_INDEX
        UCX_IB_GID_INDEX=$(python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" gid) || exit 1
        ;;
      auto) unset UCX_IB_GID_INDEX ;;
      *) echo "ERROR: unknown GID policy" >&2; exit 1 ;;
    esac
    ;;
  global-gid|single-path)
    export UCX_IB_GID_INDEX
    UCX_IB_GID_INDEX=$(python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" gid) || exit 1
    if [[ "$stage" == single-path ]]; then export UCX_RC_MLX5_NUM_PATHS=1; fi
    ;;
  tcp-control)
    export OMPI_MCA_pml=ob1 OMPI_MCA_btl=tcp,self OMPI_MCA_btl_base_verbose=20
    export OMPI_MCA_btl_tcp_if_include
    OMPI_MCA_btl_tcp_if_include=$(python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" control-interface) || exit 1
    args=(-d lat --liters=16 --lsize=8)
    ;;
  *) echo "ERROR: unknown diagnostic stage" >&2; exit 1 ;;
esac
echo "STAGE=$stage HOST=$(hostname) RANK=$SLURM_PROCID PML=$OMPI_MCA_pml HCA=$UCX_NET_DEVICES GID_ENV=${UCX_IB_GID_INDEX:-auto} TCP_IFACE=${OMPI_MCA_btl_tcp_if_include:-unused}"
exec python3 "$NVIS_BIN_SCRIPTS/clusterkit_suite_node_diag.py" exec-bound -- \
  "$HPCX_DIR/clusterkit/bin/clusterkit" "${args[@]}" \
  --disable-retest --skip_intra_node --unidirectional --time-limit=30 \
  --outputdir="$RACK_CLUSTERKIT_SUITE_CK_OUTDIR"
