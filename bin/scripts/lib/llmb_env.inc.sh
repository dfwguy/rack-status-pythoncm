# shellcheck shell=bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# lib/llmb_env.inc.sh — the job environment llmb-run's in-container ranks inherit.
#
# Source (do not execute). Used by run_llmb_nccl.sh.
#
# Two kinds of UCX value reach the launcher, and only one of them belongs in the container:
#   - host pins, exported by the fabric probe for the HOST mpirun path: UCX_TLS=^tcp forbids the only
#     transport the container has ("no active messages transport", every rank Killed -- job 65827),
#     and UCX_NET_DEVICES=mlx5_*:1 names verbs devices the container does not expose. Dropped.
#   - values STATED for this launch, by the cluster's nccl_env or the operator's dialog; Rack Status
#     lists exactly those keys in NVIS_EXTRA_ENV_KEYS. Kept, and forced into the job's --export.
#     That is how a site pins MPI's own transport for an in-container sweep: Nscale NC states
#     OMPI_MCA_pml=ucx and UCX_NET_DEVICES=<its north-south netdev> for rack launches, because a rack
#     that mixes nodes with and without their RoCE VFs otherwise splits -- UCX fails to bind on
#     eth_rN_vf0 and falls back to ob1/TCP on most nodes, starts on a VF-less one, the halves cannot
#     reach each other, and the MPI_Allgather nccl-tests does first aborts every test ~35 s in
#     (racks U6, V6, R24 on 2026-10-11). A netdev name is a device UCX's tcp transport can use there.
#
# Public:
#   nvis_llmb_drop_host_ucx_pins   unset UCX_TLS / UCX_NET_DEVICES unless stated for this launch
#                                  (NVIS_LLMB_KEEP_UCX_ENV=1 keeps all of them: host runs only)
#   nvis_llmb_force_export_stated  add every NVIS_EXTRA_ENV_KEYS key to NVIS_FORCE_EXPORT_VARS

nvis_llmb_drop_host_ucx_pins() {
  [[ "${NVIS_LLMB_KEEP_UCX_ENV:-0}" == "1" ]] && return 0
  local _v
  for _v in UCX_TLS UCX_NET_DEVICES; do
    [[ -n "${!_v:-}" ]] || continue
    if [[ " ${NVIS_EXTRA_ENV_KEYS:-} " == *" ${_v} "* ]]; then
      echo "run_llmb_nccl: keeping ${_v}=${!_v} (stated for this launch by the cluster's nccl_env or the operator)" >&2
      continue
    fi
    echo "run_llmb_nccl: unsetting ${_v}=${!_v} (host-only pin; fatal inside the llmb container)" >&2
    unset "${_v}"
  done
  return 0
}

nvis_llmb_force_export_stated() {
  local _k
  for _k in ${NVIS_EXTRA_ENV_KEYS:-}; do
    [[ " ${NVIS_FORCE_EXPORT_VARS:-} " == *" ${_k} "* ]] && continue
    NVIS_FORCE_EXPORT_VARS="${NVIS_FORCE_EXPORT_VARS:+${NVIS_FORCE_EXPORT_VARS} }${_k}"
  done
  export NVIS_FORCE_EXPORT_VARS
  return 0
}
