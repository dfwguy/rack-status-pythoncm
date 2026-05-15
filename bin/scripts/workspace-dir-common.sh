# NVIS Rack Status — source-only: canonical workspace root (HPC-X / CUDA / NCCL / nvloom under one tree).
# Source from bin/scripts after setting _SCRIPT_DIR if needed. Safe to source multiple times.
#
# Resolution order:
#   1) WORKSPACE_DIR (already set)
#   2) NVIS_WORKSPACE_DIR
#   3) RACK_WORKSPACE_DIR
#   4) TOPDIR (install scripts use this name)
#   5) /home/cmsupport/workspace
#
# Exports: WORKSPACE_DIR, TOPDIR (alias for install-script compatibility).

if [[ -z "${WORKSPACE_DIR:-}" ]]; then
  if [[ -n "${NVIS_WORKSPACE_DIR:-}" ]]; then
    WORKSPACE_DIR="${NVIS_WORKSPACE_DIR}"
  elif [[ -n "${RACK_WORKSPACE_DIR:-}" ]]; then
    WORKSPACE_DIR="${RACK_WORKSPACE_DIR}"
  elif [[ -n "${TOPDIR:-}" ]]; then
    WORKSPACE_DIR="${TOPDIR}"
  else
    WORKSPACE_DIR="/home/cmsupport/workspace"
  fi
fi

export WORKSPACE_DIR
# Install scripts use TOPDIR; keep it aligned with the resolved workspace root.
export TOPDIR="${WORKSPACE_DIR}"
export NVIS_WORKSPACE_DIR="${WORKSPACE_DIR}"
