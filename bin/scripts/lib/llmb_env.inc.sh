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
# Source (do not execute). Used by run_llmb_nccl.sh and run_llmb_nemo.sh, which run their ranks in the
# NeMo image through llmb-run and so need the same container wiring.
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
# The container is wired for the fabric too. The ranks run in the image, so what NCCL can reach is what
# the container is given: the RDMA devices (MELLANOX_VISIBLE_DEVICES, for the node's enroot
# 99-mellanox.sh hook) on IB or RoCE, and the cluster's NCCL net plugin as a path inside the image
# (fabric.net_plugin -> NVIS_FABRIC_NET_PLUGIN). Without the devices NCCL ran a 2-rack sweep on TCP
# sockets at 7 GB/s (Slurm 189003). Once the devices are in the container MPI's UCX sees them too, and on
# a routed RoCE fabric it picks a link-local GID that cannot leave the node's subnet (NeMo job 165605);
# so MPI's own transport is set for the fabric (nvis_fabric_mpi_xrack_transport: TCP on the bootstrap
# netdev on RoCE, RC on IB) unless the cluster or the operator stated UCX values for this launch. All
# of it is forced into the job's --export, because llmb's job script is generated once at install and
# reads only the environment it is submitted with.
#
# Public:
#   nvis_llmb_container_wiring [WHO]  all of the above, in order: drop host UCX pins, force the stated
#                                  keys, wire the container for the fabric, restore the stated values,
#                                  force the wiring. Needs lib/fabric.inc.sh and lib/stated_env.inc.sh
#                                  sourced, and nvis_stated_env_capture run before the fabric probe.
#   nvis_llmb_drop_host_ucx_pins [WHO]  unset UCX_TLS / UCX_NET_DEVICES unless stated for this launch
#                                  (NVIS_LLMB_KEEP_UCX_ENV=1 keeps all of them: host runs only)
#   nvis_llmb_force_export_stated  add every NVIS_EXTRA_ENV_KEYS key to NVIS_FORCE_EXPORT_VARS

nvis_llmb_drop_host_ucx_pins() {
  [[ "${NVIS_LLMB_KEEP_UCX_ENV:-0}" == "1" ]] && return 0
  local _who="${1:-run_llmb_nccl}" _v
  for _v in UCX_TLS UCX_NET_DEVICES; do
    [[ -n "${!_v:-}" ]] || continue
    if [[ " ${NVIS_EXTRA_ENV_KEYS:-} " == *" ${_v} "* ]]; then
      echo "${_who}: keeping ${_v}=${!_v} (stated for this launch by the cluster's nccl_env or the operator)" >&2
      continue
    fi
    echo "${_who}: unsetting ${_v}=${!_v} (host-only pin; fatal inside the llmb container)" >&2
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

nvis_llmb_container_wiring() {
  local _who="${1:-run_llmb_nccl}" _k
  nvis_llmb_drop_host_ucx_pins "${_who}"
  nvis_llmb_force_export_stated
  nvis_fabric_container_env
  if [[ " ${NVIS_FABRIC_CONTAINER_EXPORTS} " == *" MELLANOX_VISIBLE_DEVICES "* \
        && " ${NVIS_EXTRA_ENV_KEYS:-} " != *" UCX_TLS "* ]]; then
    nvis_fabric_mpi_xrack_transport
    NVIS_FABRIC_CONTAINER_EXPORTS="${NVIS_FABRIC_CONTAINER_EXPORTS} UCX_TLS${UCX_NET_DEVICES:+ UCX_NET_DEVICES}"
  fi
  nvis_stated_env_restore "${_who}"
  for _k in ${NVIS_FABRIC_CONTAINER_EXPORTS}; do
    [[ " ${NVIS_FORCE_EXPORT_VARS:-} " == *" ${_k} "* ]] || NVIS_FORCE_EXPORT_VARS="${NVIS_FORCE_EXPORT_VARS:+${NVIS_FORCE_EXPORT_VARS} }${_k}"
  done
  export NVIS_FORCE_EXPORT_VARS
  return 0
}
