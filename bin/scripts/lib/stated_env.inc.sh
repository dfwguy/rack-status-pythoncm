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
# lib/stated_env.inc.sh — the variables stated for a launch win over the launcher's own tuning.
#
# Source (do not execute). Used by the launchers that receive a cluster's nccl_env (NeMo, the direct
# NCCL sweep, LLMB).
#
# The launchers carry their own NCCL/UCX tuning -- NCCL_IB_QPS_PER_CONNECTION=2, NCCL_MIN_CTAS=16, the
# fabric probe's UCX_TLS -- as plain exports, and a plain export overwrites whatever the job arrived
# with. So a site's choice, stated once on its cluster record (Projects -> cluster -> NCCL environment)
# or in the launch dialog, reached the job and was then replaced: NeMo 194780 (U20+U23, Nscale NC
# 2026-10-13) ran with NCCL_IB_QPS_PER_CONNECTION=2 on every rank while the cluster said 1. The scripts
# are meant to hold defaults and the cluster to override them.
#
# Rack Status lists exactly the stated keys in NVIS_EXTRA_ENV_KEYS (the cluster's list for the launch's
# span plus the dialog's variables). A launcher captures their values before its tuning and restores
# them after it, right before it launches. Only those keys: anything else the job inherited (a login
# shell's leftovers) still yields to the script, and a launch with nothing stated behaves exactly as
# before.
#
# Public:
#   nvis_stated_env_capture         remember the value of every stated key that is set
#   nvis_stated_env_restore [WHO]   put each back, saying which of the launcher's values it replaced

_NVIS_STATED_ENV=()

nvis_stated_env_capture() {
  _NVIS_STATED_ENV=()
  local _k
  for _k in ${NVIS_EXTRA_ENV_KEYS:-}; do
    [[ "${_k}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    [[ -n "${!_k+x}" ]] || continue
    _NVIS_STATED_ENV+=("${_k}=${!_k}")
  done
  return 0
}

nvis_stated_env_restore() {
  local _who="${1:-launcher}" _kv _k _v
  for _kv in "${_NVIS_STATED_ENV[@]}"; do
    _k="${_kv%%=*}"
    _v="${_kv#*=}"
    if [[ "${!_k-}" != "${_v}" ]]; then
      echo "${_who}: ${_k}=${_v} (stated for this launch; replaces the launcher's ${_k}=${!_k-<unset>})" >&2
    fi
    export "${_k}=${_v}"
  done
  return 0
}
