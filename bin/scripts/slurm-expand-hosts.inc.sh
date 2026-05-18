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
