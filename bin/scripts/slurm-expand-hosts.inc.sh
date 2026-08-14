# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# Source from bin/scripts drivers after ``module load slurm``.
# Expands a Slurm hostlist / nodespec into a comma-separated ``sbatch -w`` list using Slurm's canonical NodeName spelling.
#
# Input:  first argument = hostlist string (e.g. p05-r01-ct[01-18] or cbr01-dh01-svr-cn0102[11-18,28-37])
# Output: sets _slurm_wlist and _slurm_nnodes. Returns 1 if expansion is empty.
#
# Optional: NVIS_SLURM_HOSTLIST — comma-separated hostnames; skips expansion (site override).

_slurm_expand_to_wlist() {
  local raw="$1"
  local csv up

  if [[ -n "${NVIS_SLURM_HOSTLIST:-}" ]]; then
    _slurm_wlist="${NVIS_SLURM_HOSTLIST// /}"
    _slurm_wlist="${_slurm_wlist//,,/,}"
    _slurm_nnodes=0
    if [[ -n "${_slurm_wlist}" ]]; then
      _slurm_nnodes=$(echo "${_slurm_wlist}" | awk -F, '{n=0; for(i=1;i<=NF;i++) if($i!="") n++; print n}')
    fi
    if [[ "${_slurm_nnodes}" -eq 0 ]]; then
      echo "error: NVIS_SLURM_HOSTLIST is empty or invalid" >&2
      return 1
    fi
    return 0
  fi

  _try_hostnames() {
    local spec="$1"
    # Slurm may print one comma-separated line or one hostname per line; never take only the first line
    # (that miscounts nodes for large brackets, e.g. CBR…[11-18,28-37]).
    scontrol show hostnames "${spec}" 2>/dev/null | sed '/^$/d' | paste -sd, - | tr -d '[:space:]'
  }

  _try_hostname_lines() {
    local spec="$1"
    scontrol show hostname "${spec}" 2>/dev/null | sed '/^$/d' | paste -sd, -
  }

  csv="$(_try_hostnames "${raw}")"
  if [[ -z "${csv}" ]]; then
    up=$(echo "${raw}" | tr '[:lower:]' '[:upper:]')
    if [[ "${up}" != "${raw}" ]]; then
      csv="$(_try_hostnames "${up}")"
    fi
  fi
  if [[ -z "${csv}" ]]; then
    csv="$(_try_hostname_lines "${raw}")"
  fi
  if [[ -z "${csv}" ]]; then
    up=$(echo "${raw}" | tr '[:lower:]' '[:upper:]')
    if [[ "${up}" != "${raw}" ]]; then
      csv="$(_try_hostname_lines "${up}")"
    fi
  fi

  # ── NscaleTX: drop nodes Slurm cannot schedule ────────────────────────────
  # sbatch -w fails the WHOLE submission with "Requested node configuration is
  # not available" if any single node is down/drained. During bring-up most racks
  # have a mix, so filter to schedulable states and tell the operator what was
  # dropped. Set RACK_SLURM_ALLOW_STATES to override (default: idle,mix,alloc).
  _filter_schedulable() {
    local list="$1" allow keep drop n st
    allow="${RACK_SLURM_ALLOW_STATES:-idle,mix,mixed,alloc,allocated,completing}"
    keep=""; drop=""
    while IFS=' ' read -r n st; do
      [[ -z "${n}" ]] && continue
      st="$(echo "${st}" | tr '[:upper:]' '[:lower:]' | tr -d '*~#$@+')"
      if [[ ",${allow}," == *",${st},"* ]]; then
        keep="${keep:+${keep},}${n}"
      else
        drop="${drop:+${drop} }${n}(${st})"
      fi
    done < <(sinfo -h -N -o '%N %T' -n "${list}" 2>/dev/null | sort -u)
    if [[ -n "${drop}" ]]; then
      echo "notice: skipping non-schedulable nodes: ${drop}" >&2
    fi
    printf '%s' "${keep}"
  }

  if [[ "${RACK_SLURM_STATE_FILTER:-1}" == "1" && -n "${csv}" ]]; then
    local _filtered
    _filtered="$(_filter_schedulable "${csv}")"
    if [[ -z "${_filtered}" ]]; then
      echo "error: no schedulable nodes in: ${raw}" >&2
      echo "hint: check 'sinfo -N -o \"%N %T\" -n ${csv}' — all nodes are down/drained." >&2
      return 1
    fi
    csv="${_filtered}"
  fi

  if [[ -z "${csv}" ]]; then
    echo "error: scontrol could not expand hostlist for: ${raw}" >&2
    echo "hint: on the Slurm head run: scontrol show hostnames '${raw}'" >&2
    echo "      If empty, Slurm does not recognize that name (wrong prefix, FQDN vs short name, or typo)." >&2
    return 1
  fi

  _slurm_wlist="${csv}"
  _slurm_nnodes=$(echo "${_slurm_wlist}" | awk -F, '{n=0; for(i=1;i<=NF;i++) if($i!="") n++; print n}')
  if [[ "${_slurm_nnodes}" -eq 0 ]]; then
    echo "error: expanded hostlist is empty for: ${raw}" >&2
    return 1
  fi
  return 0
}
