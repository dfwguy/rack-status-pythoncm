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
# 3) force named env vars into the JOB environment (NVIS_FORCE_EXPORT_VARS = space-separated keys).
# ⚠ THIS DOES **NOT** FIX NCCL_DEBUG FOR THE COMMS-PERF-SUITE WORKLOAD — do not re-try that here.
# Proven 2026-08-04: templates/nccl.sbatch.j2 emits `--container-env=NCCL_DEBUG=<v>` from
# testset.toml, an EXPLICIT pyxis assignment, so the job environment is irrelevant to the ranks no
# matter what --export says. The level must be written into testset.toml instead — run_llmb_nccl.sh
# does that. This block remains only as a generic job-env passthrough for launchers that DO inherit.
# MERGE, never replace: if llmb-run passed its own --export we append to ITS list, so its intent
# is preserved and we only add the keys we were asked to carry.
_pairs=""
for _k in ${NVIS_FORCE_EXPORT_VARS:-}; do
  _v="${!_k:-}"
  [[ -n "${_v}" ]] || continue
  # --export is a comma-separated list; a value containing a comma or whitespace would corrupt it.
  case "${_v}" in *,*|*[[:space:]]*) echo "nvis_sbatch_shim: skipping ${_k} (unsafe value for --export)" >&2; continue ;; esac
  _pairs="${_pairs},${_k}=${_v}"
done
if [[ -n "${_pairs}" ]]; then
  _merged=0; _out=(); _take=0
  for a in ${filtered[@]+"${filtered[@]}"}; do
    if [[ "${_take}" -eq 1 ]]; then a="${a}${_pairs}"; _merged=1; _take=0
    else
      case "$a" in
        --export=*) a="${a}${_pairs}"; _merged=1 ;;
        --export)   _take=1 ;;
      esac
    fi
    _out+=( "$a" )
  done
  filtered=( ${_out[@]+"${_out[@]}"} )
  [[ "${_merged}" -eq 0 ]] && pre+=( "--export=ALL${_pairs}" )
  echo "nvis_sbatch_shim: forcing into job env:${_pairs//,/ } (merged_into_llmb_export=${_merged})" >&2
fi
# Log llmb-run's own sbatch argv verbatim. Without this, "did llmb pass its own --export?" is
# unanswerable from the outside and the question gets guessed at instead of read.
echo "nvis_sbatch_shim: llmb sbatch argv:$(printf ' %q' "$@")" >&2
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
