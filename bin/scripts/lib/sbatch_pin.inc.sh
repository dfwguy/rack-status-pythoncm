# shellcheck shell=bash
# lib/sbatch_pin.inc.sh — install a PATH shim that pins a wrapped tool's own
# `sbatch` to a nodelist and redirects its --output.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Source (do not execute). Extracted from run_llmb_nccl.sh. Some tools (e.g.
# llmb-run's `single` launcher) call `sbatch -N <count> ... launch.sh` themselves
# with NO --nodelist (Slurm free-allocates) and a hard-coded --output outside the
# dashboard log dir. There is no sbatch *env var* to inject a nodelist
# (SBATCH_NODELIST is not an sbatch input var), so we drop a tiny `sbatch` shim
# ahead of the real binary on PATH that PREPENDS --nodelist/--output to the tool's
# own sbatch argv (options MUST precede the batch script, or sbatch ignores them).
#
# Public:
#   nvis_sbatch_pin_install <nodelist> [sbatch_output]  -> sets NVIS_SHIM_DIR, extends PATH
#   nvis_sbatch_pin_cleanup                              -> removes the shim dir
# Caller should: trap nvis_sbatch_pin_cleanup EXIT

nvis_sbatch_pin_install() {
  local nodelist="${1:-}"
  local sbatch_output="${2:-}"
  NVIS_SHIM_DIR=""
  [[ -n "${nodelist}" || -n "${sbatch_output}" ]] || return 0

  local real_sbatch; real_sbatch="$(command -v sbatch 2>/dev/null || true)"
  [[ -n "${real_sbatch}" ]] || { echo "nvis_sbatch_pin: sbatch not on PATH; cannot pin" >&2; return 0; }

  NVIS_SHIM_DIR="$(mktemp -d "${TMPDIR:-/tmp}/nvis_sbatch_shim.XXXXXX")"
  cat > "${NVIS_SHIM_DIR}/sbatch" <<'SHIM'
#!/usr/bin/env bash
# NVIS sbatch shim — pins the wrapped tool's batch job to the provisioned racks
# and redirects its stdout under the dashboard log dir.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
# sbatch grammar is `sbatch [OPTIONS] script [script-args]`: any token AFTER the
# batch script is handed to the script, not parsed by sbatch — so injected
# options MUST be PREPENDED (ahead of the script), never appended.
set -u
_real="${LLMB_REAL_SBATCH:?LLMB_REAL_SBATCH unset}"
# 1) strip the tool's own --output/-o when we're overriding it
filtered=(); skip=0
for a in "$@"; do
  if [[ "${skip}" -eq 1 ]]; then skip=0; continue; fi
  if [[ -n "${LLMB_SBATCH_OUTPUT:-}" ]]; then
    case "$a" in
      --output=*|-o*) continue ;;
      --output|-o) skip=1; continue ;;
    esac
  fi
  filtered+=( "$a" )
done
# 2) build the prepended option block (options precede the script)
pre=()
[[ -n "${LLMB_SBATCH_OUTPUT:-}" ]] && pre+=( "--output=${LLMB_SBATCH_OUTPUT}" )
if [[ -n "${LLMB_NODELIST:-}" ]]; then
  _has=0
  for a in "$@"; do case "$a" in -w|-w*|--nodelist|--nodelist=*) _has=1 ;; esac; done
  [[ "${_has}" -eq 0 ]] && pre+=( "--nodelist=${LLMB_NODELIST}" )
fi
exec "${_real}" ${pre[@]+"${pre[@]}"} ${filtered[@]+"${filtered[@]}"}
SHIM
  chmod +x "${NVIS_SHIM_DIR}/sbatch"
  export LLMB_REAL_SBATCH="${real_sbatch}"
  [[ -n "${nodelist}" ]] && export LLMB_NODELIST="${nodelist}"
  [[ -n "${sbatch_output}" ]] && export LLMB_SBATCH_OUTPUT="${sbatch_output}"
  export PATH="${NVIS_SHIM_DIR}:${PATH}"
}

nvis_sbatch_pin_cleanup() {
  [[ -n "${NVIS_SHIM_DIR:-}" && -d "${NVIS_SHIM_DIR}" ]] && rm -rf "${NVIS_SHIM_DIR}"
}
