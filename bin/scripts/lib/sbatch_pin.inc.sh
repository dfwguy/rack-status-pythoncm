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
  export NVIS_SHIM_DIR
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
# 4) Optional audited LLMB NCCL transformation.  The upstream Comms-Perf-Suite
# generates the proven 24-test batch dynamically, so the only safe interception
# point is the exact script handed to sbatch.  Transform a private copy, verify
# every required invariant, and never mutate the installed LLMB recipe.
if [[ "${NVIS_LLMB_AUDITED_CONTAINER:-0}" == 1 ]]; then
  _script=""; _script_index=-1
  for (( _i=${#filtered[@]}-1; _i>=0; _i-- )); do
    [[ -f "${filtered[$_i]}" ]] || continue
    _script="${filtered[$_i]}"; _script_index=$_i; break
  done
  [[ -n "$_script" ]] || { echo "nvis_sbatch_shim: audited LLMB batch script not found" >&2; exit 41; }
  _patched="${NVIS_SHIM_DIR}/audited-llmb-nccl-${RANDOM}.sbatch"
  {
    head -n 1 "$_script"
    cat <<'AUDIT_GATE'
set -o pipefail
export PMIX_MCA_gds=hash
export MELLANOX_VISIBLE_DEVICES=all
_nvis_llmb_nccl_result_gate() {
  _prior=$?
  trap - EXIT
  if (( _prior != 0 )); then
    echo "LLMB_NCCL_24TEST_FAIL batch_rc=${_prior}" >&2
    exit "$_prior"
  fi
  _count=0; _failed=0
  if [[ -n "${LOG_DIR:-}" && -d "$LOG_DIR" ]]; then
    _count=$(find "$LOG_DIR" -mindepth 2 -maxdepth 2 -name result.txt -type f | wc -l)
    _failed=$(find "$LOG_DIR" -mindepth 2 -maxdepth 2 -name result.txt -type f -exec grep -L '^ret_code="0"$' {} + | wc -l)
  fi
  if (( _count != 24 || _failed != 0 )); then
    echo "LLMB_NCCL_24TEST_FAIL completed=${_count} failed=${_failed} expected=24" >&2
    exit 42
  fi
  echo "LLMB_NCCL_24TEST_PASS completed=24 failed=0 nodes=${SLURM_JOB_NUM_NODES} ranks_per_node=4"
}
trap _nvis_llmb_nccl_result_gate EXIT
AUDIT_GATE
    tail -n +2 "$_script" | sed -E \
      -e 's/--mpi=(pmi2|none)/--mpi=pmix/g' \
      -e 's/--container-env=[^[:space:]]+[[:space:]]*//g' \
      -e 's#--container-mounts=#--container-mounts=/dev/infiniband:/dev/infiniband,#g' \
      -e 's#srun --ntasks=#srun --mpi=pmix --ntasks=#g' \
      -e 's#srun -N \$\{SLURM_JOB_NUM_NODES\}#srun -N ${SLURM_JOB_NUM_NODES} --ntasks-per-node=4#g' \
      -e 's#--container-image=([^[:space:]]+)#--container-image=\1 --container-env=PMIX_MCA_gds,MELLANOX_VISIBLE_DEVICES#g'
  } >"$_patched"
  chmod 700 "$_patched"
  _markers=$(grep -Ec 'Running test [0-9]+/24' "$_patched")
  _tests=$(grep -Ec '^[[:space:]]*srun -N .*--ntasks-per-node=4 .*--mpi=pmix .*--container-image=' "$_patched")
  _mounts=$(grep -Ec '^[[:space:]]*srun .*--container-mounts=/dev/infiniband:/dev/infiniband,' "$_patched")
  _envs=$(grep -Ec '^[[:space:]]*srun .*--container-env=PMIX_MCA_gds,MELLANOX_VISIBLE_DEVICES' "$_patched")
  _mpi=$(grep -Ec '^[[:space:]]*srun .*--mpi=pmix' "$_patched")
  _args=$(grep -Ec 'run_test .* -t1 -g1 -f2 -n20 -w5 -b8 -e16G -d(float|uint8) -R0 -c1 -G(None|2|4)' "$_patched")
  [[ "$_markers" == 24 && "$_tests" == 24 && "$_mounts" -ge 26 && "$_envs" -ge 26 && "$_mpi" -ge 26 && "$_args" == 24 ]] || {
    echo "nvis_sbatch_shim: audited recipe gate failed markers=$_markers tests=$_tests mounts=$_mounts envs=$_envs mpi=$_mpi args=$_args" >&2; exit 42;
  }
  for _spec in all_reduce_perf:6 all_gather_perf:3 reduce_scatter_perf:3 alltoall_perf:6 sendrecv_perf:6; do
    _name=${_spec%:*}; _want=${_spec#*:}
    _got=$(grep -Ec "^[[:space:]]*srun .*${_name}" "$_patched")
    [[ "$_got" == "$_want" ]] || { echo "nvis_sbatch_shim: ${_name} count=$_got expected=$_want" >&2; exit 43; }
  done
  filtered[$_script_index]="$_patched"
  pre+=( --parsable --wait --no-requeue --ntasks-per-node=4 --gpus-per-node=4 )
  echo "nvis_sbatch_shim: audited LLMB NCCL recipe ready contract=job147392-shape markers=24 tests=24 image=${RUN_CONF_IMAGE:-unset}" >&2
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
