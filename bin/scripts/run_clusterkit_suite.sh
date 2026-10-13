#!/usr/bin/env bash
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
# ClusterKit SU-suite runner for Rack Status.
#
# Usage:
#   run_clusterkit_suite.sh plan --nodelist <node1,node2> --hpcx-init <path> [options]
#   run_clusterkit_suite.sh run --dry-run --nodelist <node1,node2> --hpcx-init <path> [options]
#   run_clusterkit_suite.sh run <DOMAIN> <PREFIX> <SU_LABEL>
#   run_clusterkit_suite.sh check [SU_LABEL ...]
#
# Example:
#   DRY_RUN=1 ./bin/scripts/run_clusterkit_suite.sh run dh02 j SU10
#   ./bin/scripts/run_clusterkit_suite.sh check SU10
#
# This intentionally submits explicit tier sbatch files instead of generating
# Slurm scripts dynamically. The runner owns node selection, result layout, and
# coverage/performance checks; the sbatch files own ClusterKit execution.

set -uo pipefail

# Explicit-node planning exits before initializing the legacy SU runner.
# Live submission requires --execute, exactly two nodes, an explicit HCA/port,
# and a time limit of at most five minutes. It submits one bounded CPU/RoCE job.
plan_explicit_nodes() {
  local mode="$1" nodelist="" label="explicit-nodes"
  local hpcx_init="${NVIS_CLUSTERKIT_HPCX_INIT:-}"
  local partition="${RACK_CLUSTERKIT_SUITE_PARTITION:-${RACK_CLUSTERKIT_PARTITION:-defq}}"
  local walltime="00:05:00" dry_run="${DRY_RUN:-0}"
  local execute=0 hca="" duration jobid
  local gid_policy="${RACK_CLUSTERKIT_SUITE_GID_POLICY:-global}"
  local log_level="${RACK_CLUSTERKIT_SUITE_LOG_LEVEL:-info}" path_check=0
  local script_dir repo_dir results_root workspace outdir exports other _node0_lower _node1_lower
  local nodes=() cmd=()
  shift
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || return 1
  repo_dir="$(cd "${script_dir}/../.." && pwd)" || return 1
  results_root="${RACK_CLUSTERKIT_SUITE_RESULTS_ROOT:-${repo_dir}/results}"

  while (($#)); do
    case "$1" in
      --dry-run) dry_run=1; shift ;;
      --execute) execute=1; shift ;;
      --path-check) path_check=1; shift ;;
      --nodelist|--hpcx-init|--results-root|--partition|--time|--label|--hca|--gid-policy|--log-level)
        if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
          echo "ERROR: $1 requires a value" >&2; return 1
        fi
        case "$1" in
          --nodelist) nodelist="$2" ;;
          --hpcx-init) hpcx_init="$2" ;;
          --results-root) results_root="$2" ;;
          --partition) partition="$2" ;;
          --time) walltime="$2" ;;
          --label) label="$2" ;;
          --hca) hca="$2" ;;
          --gid-policy) gid_policy="$2" ;;
          --log-level) log_level="$2" ;;
        esac
        shift 2
        ;;
      -h|--help)
        cat <<'PLAN_USAGE'
Print one two-node sbatch plan, or explicitly submit one bounded CPU/RoCE test.
Usage: run_clusterkit_suite.sh plan --nodelist NODE1,NODE2 --hpcx-init /path/hpcx-init.sh
       run_clusterkit_suite.sh run --dry-run --nodelist NODE1,NODE2 --hpcx-init /path/hpcx-init.sh
Options: --results-root /path (default: <repo>/results), --partition NAME (default: defq),
         --time HH:MM:SS (default: 00:05:00), --label NAME (default: explicit-nodes),
         --hca DEVICE:PORT. Live run requires --execute and --hca, and at most five minutes.
         --gid-policy global|auto (default: global; auto reproduces the diagnostic baseline).
         --log-level info|debug (default: info), --path-check (also compare one UCX RC path).
Use literal hostnames, not Slurm bracket expressions. Physical rack membership is not inferred.
PLAN_USAGE
        return 0
        ;;
      *) echo "ERROR: unknown explicit-node planning option '$1'" >&2; return 1 ;;
    esac
  done

  if [[ "$mode" != plan && "$dry_run" != 1 && "$execute" != 1 ]]; then
    echo "ERROR: explicit-node execution is disabled by default; use plan, --dry-run, or --execute" >&2
    return 1
  fi
  if [[ ! "$nodelist" =~ ^[[:alnum:]][[:alnum:]._-]*(,[[:alnum:]][[:alnum:]._-]*)+$ ]]; then
    echo "ERROR: --nodelist requires comma-separated literal hostnames (no blanks or ranges)" >&2
    return 1
  fi
  IFS=, read -r -a nodes <<<"$nodelist"
  if ((${#nodes[@]} != 2)); then
    echo "ERROR: this planning mode requires exactly two nodes" >&2; return 1
  fi
  _node0_lower=$(printf '%s' "${nodes[0]}" | tr '[:upper:]' '[:lower:]')
  _node1_lower=$(printf '%s' "${nodes[1]}" | tr '[:upper:]' '[:lower:]')
  if [[ "${_node0_lower}" == "${_node1_lower}" ]]; then
    echo "ERROR: duplicate hostname (case-insensitive)" >&2; return 1
  fi
  if [[ ! "$label" =~ ^[[:alnum:]][[:alnum:]_-]*$ || ! "$partition" =~ ^[[:alnum:]][[:alnum:]_-]*$ ]]; then
    echo "ERROR: label and partition must contain only letters, digits, '_' or '-'" >&2
    return 1
  fi
  if [[ ! "$walltime" =~ ^([0-9]{1,3}):([0-5][0-9]):([0-5][0-9])$ ]]; then
    echo "ERROR: --time must be a positive HH:MM:SS duration" >&2; return 1
  fi
  if ((10#${BASH_REMATCH[1]} + 10#${BASH_REMATCH[2]} + 10#${BASH_REMATCH[3]} == 0)); then
    echo "ERROR: --time must be positive" >&2; return 1
  fi
  duration=$((10#${BASH_REMATCH[1]} * 3600 + 10#${BASH_REMATCH[2]} * 60 + 10#${BASH_REMATCH[3]}))
  if [[ -n "$hca" && ! "$hca" =~ ^[[:alnum:]_]+:[1-9][0-9]*$ ]]; then
    echo "ERROR: --hca must be one RDMA device and port, e.g. roce_vf_r1:1" >&2; return 1
  fi
  if [[ "$gid_policy" != global && "$gid_policy" != auto ]]; then
    echo "ERROR: --gid-policy must be global or auto" >&2; return 1
  fi
  if [[ "$log_level" != info && "$log_level" != debug ]]; then
    echo "ERROR: --log-level must be info or debug" >&2; return 1
  fi
  if [[ "$mode" == run && "$dry_run" != 1 && ( -z "$hca" || "$duration" -gt 300 ) ]]; then
    echo "ERROR: live explicit-node tests require --hca and a time limit of at most five minutes" >&2
    return 1
  fi
  if [[ "$hpcx_init" != /* || ! -r "$hpcx_init" || ! -f "$hpcx_init" ]]; then
    echo "ERROR: --hpcx-init must name an absolute, readable file (it will not be sourced)" >&2
    return 1
  fi
  if [[ "$results_root" != /* ]]; then
    echo "ERROR: --results-root must be absolute" >&2; return 1
  fi
  # Slurm --export uses commas as separators; shell quoting cannot escape them.
  for other in "$hpcx_init" "$results_root" "$script_dir"; do
    if [[ "$other" == *','* || "$other" == *$'\n'* || "$other" == *$'\r'* ]]; then
      echo "ERROR: paths must not contain commas or line breaks" >&2; return 1
    fi
  done
  if [[ ! -f "${script_dir}/clusterkit_suite_explicit_2_node.sbatch" ]]; then
    echo "ERROR: missing clusterkit_suite_explicit_2_node.sbatch" >&2; return 1
  fi

  workspace="${hpcx_init%/*}"
  workspace="${workspace%/*}"
  outdir="${results_root%/}/${label}/2_node_test"
  exports="ALL,NVIS_BIN_SCRIPTS=${script_dir},NVIS_CLUSTERKIT_HPCX_INIT=${hpcx_init}"
  exports+=",RACK_CLUSTERKIT_SUITE_OUTDIR=${outdir},RACK_CLUSTERKIT_SUITE_TEST_TYPE=2_node_test"
  exports+=",RACK_CLUSTERKIT_SUITE_RUN_LABEL=${label},RACK_CLUSTERKIT_SUITE_SU_LABEL=${label}"
  exports+=",CUDA_MAJ=${CUDA_MAJ:-13},WORKSPACE_DIR=${workspace},NVIS_WORKSPACE_DIR=${workspace},TOPDIR=${workspace}"
  exports+=",RACK_CLUSTERKIT_SUITE_HCA=${hca},RACK_CLUSTERKIT_SUITE_EXPECTED_NODES=${nodelist//,/:}"
  exports+=",RACK_CLUSTERKIT_SUITE_GID_POLICY=${gid_policy}"
  exports+=",RACK_CLUSTERKIT_SUITE_LOG_LEVEL=${log_level},RACK_CLUSTERKIT_SUITE_PATH_CHECK=${path_check}"
  cmd=(sbatch --parsable "--partition=${partition}" --nodes=2 --ntasks=2 --ntasks-per-node=1
       --cpus-per-task=1 --mem=1G --gres=none --no-requeue
       "--time=${walltime}" "--nodelist=${nodelist}" "--export=${exports}"
       "--output=${outdir}/slurm-2-node-%j.out" "${script_dir}/clusterkit_suite_explicit_2_node.sbatch")

  if [[ "$mode" == run && "$dry_run" != 1 && "$execute" == 1 ]]; then
    # Never archive, reuse, or overwrite previous results. A label is one attempt.
    if [[ -e "$outdir" ]]; then
      echo "ERROR: output directory already exists; choose a new --label" >&2; return 1
    fi
    source "${script_dir}/slurm-init.inc.sh"
    command -v sbatch >/dev/null || { echo "ERROR: sbatch unavailable" >&2; return 1; }
    mkdir -p "${results_root%/}/${label}" || return 1
    mkdir "$outdir" || return 1
    unset SBATCH_EXCLUSIVE SBATCH_OVERCOMMIT SBATCH_OVERSUBSCRIBE
    printf 'Submitting one two-node CPU/RoCE test: %s, HCA=%s, limit=%s\n' "$nodelist" "$hca" "$walltime"
    jobid=$("${cmd[@]}") || { echo "ERROR: sbatch failed; no retry" >&2; return 1; }
    printf '%s\n' "$jobid" >"${outdir}/jobid.txt"
    printf 'Submitted job: %s\nResults: %s\n' "$jobid" "$outdir"
    return 0
  fi

  printf 'PRINT-ONLY DRY-RUN: no allocation, Slurm query, HPC-X load, or ClusterKit execution.\n'
  printf 'Nodes: %s\nHPC-X init (not sourced): %s\nResults (not created): %s\n' "$nodelist" "$hpcx_init" "$outdir"
  printf 'Topology: explicit selection; physical rack membership is unverified.\n'
  printf 'HCA/port: %s (verified on both nodes inside the job before testing).\n' "${hca:-unresolved}"
  printf 'GID policy: %s (global resolves the matching RoCE-v2 interface GID on each node).\n' "$gid_policy"
  printf 'UCX logging: %s; single-path comparison: %s.\n' "$log_level" "$path_check"
  printf 'Allocation: nonexclusive; one CPU and 1 GiB per node; no GPU resources.\n'
  printf 'The Slurm output directory would need to exist before a future submission.\n'
  printf 'SBATCH PLAN (not executed):\n'
  printf '%q ' "${cmd[@]}"
  printf '\n'
}

if [[ "${1:-}" == plan || ( "${1:-}" == run && "${2:-}" == --* ) ]]; then
  plan_explicit_nodes "$@"
  exit "$?"
fi

# Slurm CLI on PATH (non-login SSH shells do not init Modules).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
RESULTS_ROOT="${RACK_CLUSTERKIT_SUITE_RESULTS_ROOT:-${JOB_LOG_DIR%/}/clusterkit-suite}"
RESULT_SRC="${RESULT_SRC:-${RESULTS_ROOT}}"
POLL="${RACK_CLUSTERKIT_SUITE_POLL:-30}"

RACKS=(01 02 03 04 05 06 07 08 17 18 19 20 21 22 23 24)
EXCLUDE_RACKS="${EXCLUDE_RACKS:-}"
if [[ -n "${EXCLUDE_RACKS}" ]]; then
  _excluded=" $(tr ',' ' ' <<<"${EXCLUDE_RACKS}" | tr -d '[:alpha:]') "
  _kept=()
  for _rack in "${RACKS[@]}"; do
    [[ "${_excluded}" == *" ${_rack} "* ]] || _kept+=("${_rack}")
  done
  RACKS=("${_kept[@]}")
fi

TEST_TYPES=(per_rack 2_node_test 2_node_cross_rack_test 2_rack_test entire_SU)

BW_MIN="${RACK_CLUSTERKIT_SUITE_BW_MIN:-170000}"
BW_MAX="${RACK_CLUSTERKIT_SUITE_BW_MAX:-200000}"
LAT_MIN="${RACK_CLUSTERKIT_SUITE_LAT_MIN:-1.0}"
LAT_MAX="${RACK_CLUSTERKIT_SUITE_LAT_MAX:-7.0}"
BW_SANE_MAX="${RACK_CLUSTERKIT_SUITE_BW_SANE_MAX:-1000000}"
LAT_SANE_MAX="${RACK_CLUSTERKIT_SUITE_LAT_SANE_MAX:-10000}"
EXPECTED_HCAS="${RACK_CLUSTERKIT_SUITE_EXPECTED_HCAS:-4}"

BW_FILE='*bandwidth.txt'
LAT_FILE='*latency.txt'
BW_JSON='*bandwidth.json'
LAT_JSON='*latency.json'
NODE_RE="${RACK_CLUSTERKIT_SUITE_NODE_RE:-ink50-dh0[12]-[a-z][0-9]+-[0-9]+}"

ISSUES=0
FAILED_TESTS=()
GLITCH_TOTAL=0

export NVIS_BIN_SCRIPTS="${_SCRIPT_DIR}"

# shellcheck disable=SC1091
. "${_SCRIPT_DIR}/workspace-dir-common.sh"
export WORKSPACE_DIR
export CUDA_MAJ="${CUDA_MAJ:-${RACK_CLUSTERKIT_CUDA_MAJOR:-${RACK_NVLOOM_CUDA_MAJOR:-13}}}"

sep() { printf '=%.0s' {1..70}; echo; }
banner() { echo ""; sep; echo "  $1"; sep; }
fail() { ISSUES=$((ISSUES + 1)); FAILED_TESTS+=("$1"); }

usage() {
  sed -n '12,26p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

gt() { awk "BEGIN{exit($1+0 > $2+0)?0:1}" 2>/dev/null; }
lt() { awk "BEGIN{exit($1+0 < $2+0)?0:1}" 2>/dev/null; }

parse_metric() {
  local raw
  raw=$(sed "s/.*$2:[[:space:]]*//; s/[[:space:]].*//" <<<"$1")
  raw="${raw//,/}"
  [[ "$raw" =~ ^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$ ]] || return 1
  echo "$raw"
}

check_perf() {
  local dir="$1"
  local bw_fails=0 lat_fails=0 bw_count=0 lat_count=0 glitches=0
  local worst_bw="" worst_lat="" line val rundir gtag stats

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    bw_count=$((bw_count + 1))
    rundir=$(basename "$(dirname "${line%%:*}")")
    if ! val=$(parse_metric "$line" "Minimum bandwidth"); then
      echo "      GLITCH: unreadable Minimum bandwidth in $rundir"
      glitches=$((glitches + 1)); continue
    fi
    if gt "$val" "$BW_SANE_MAX" || lt "$val" 0; then
      echo "      GLITCH: implausible min bandwidth ${val} MB/s in $rundir (ignored)"
      glitches=$((glitches + 1)); continue
    fi
    if [[ -z "$worst_bw" ]] || lt "$val" "$worst_bw"; then worst_bw="$val"; fi
    if lt "$val" "$BW_MIN"; then
      echo "      BW LOW: ${val} MB/s < ${BW_MIN} in $rundir"
      bw_fails=$((bw_fails + 1))
    fi
  done < <(grep -r --include="$BW_FILE" --exclude-dir=old "Minimum bandwidth:" "$dir" 2>/dev/null)

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    rundir=$(basename "$(dirname "${line%%:*}")")
    if ! val=$(parse_metric "$line" "Maximum bandwidth"); then
      echo "      GLITCH: unreadable Maximum bandwidth in $rundir"
      glitches=$((glitches + 1)); continue
    fi
    if gt "$val" "$BW_SANE_MAX" || lt "$val" 0; then
      echo "      GLITCH: implausible max bandwidth ${val} MB/s in $rundir (ignored)"
      glitches=$((glitches + 1)); continue
    fi
    if gt "$val" "$BW_MAX"; then
      echo "      BW HIGH: ${val} MB/s > ${BW_MAX} in $rundir"
      bw_fails=$((bw_fails + 1))
    fi
  done < <(grep -r --include="$BW_FILE" --exclude-dir=old "Maximum bandwidth:" "$dir" 2>/dev/null)

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    lat_count=$((lat_count + 1))
    rundir=$(basename "$(dirname "${line%%:*}")")
    if ! val=$(parse_metric "$line" "Maximum latency"); then
      echo "      GLITCH: unreadable Maximum latency in $rundir"
      glitches=$((glitches + 1)); continue
    fi
    if gt "$val" "$LAT_SANE_MAX" || lt "$val" 0; then
      echo "      GLITCH: implausible max latency ${val} usec in $rundir (ignored)"
      glitches=$((glitches + 1)); continue
    fi
    if [[ -z "$worst_lat" ]] || gt "$val" "$worst_lat"; then worst_lat="$val"; fi
    if gt "$val" "$LAT_MAX"; then
      echo "      LAT HIGH: ${val} usec > ${LAT_MAX} in $rundir"
      lat_fails=$((lat_fails + 1))
    fi
  done < <(grep -r --include="$LAT_FILE" --exclude-dir=old "Maximum latency:" "$dir" 2>/dev/null)

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    rundir=$(basename "$(dirname "${line%%:*}")")
    if ! val=$(parse_metric "$line" "Minimum latency"); then
      echo "      GLITCH: unreadable Minimum latency in $rundir"
      glitches=$((glitches + 1)); continue
    fi
    if gt "$val" "$LAT_SANE_MAX" || lt "$val" 0; then
      echo "      GLITCH: implausible min latency ${val} usec in $rundir (ignored)"
      glitches=$((glitches + 1)); continue
    fi
    if lt "$val" "$LAT_MIN"; then
      echo "      LAT LOW: ${val} usec < ${LAT_MIN} in $rundir"
      lat_fails=$((lat_fails + 1))
    fi
  done < <(grep -r --include="$LAT_FILE" --exclude-dir=old "Minimum latency:" "$dir" 2>/dev/null)

  stats=""
  [[ -n "$worst_bw" ]] && stats="min_bw=${worst_bw} MB/s"
  [[ -n "$worst_lat" ]] && stats="${stats:+$stats  }max_lat=${worst_lat} usec"
  gtag=""
  ((glitches > 0)) && gtag=", ${glitches} glitched"

  if ((bw_count == 0 && lat_count == 0)); then
    echo "    PERF: no result files"
    fail "${dir} - no perf data"
    return 1
  elif ((bw_fails == 0 && lat_fails == 0)); then
    echo "    PERF OK (${bw_count} bw, ${lat_count} lat${gtag})  ${stats}"
    ((glitches > 0)) && GLITCH_TOTAL=$((GLITCH_TOTAL + glitches))
    return 0
  else
    echo "    PERF FAIL: ${bw_fails}/${bw_count} bw, ${lat_fails}/${lat_count} lat${gtag}  ${stats}"
    fail "${dir} - $((bw_fails + lat_fails)) perf violation(s)"
    ((glitches > 0)) && GLITCH_TOTAL=$((GLITCH_TOTAL + glitches))
    return 1
  fi
}

idle_nodes() {
  sinfo -N -h -t idle -o "%N" 2>/dev/null | grep -E "$1" | sort -u
}

largest_even() {
  local nodes=() n
  mapfile -t nodes
  n=${#nodes[@]}
  ((n % 2)) && ((n--))
  ((n < 2)) && return 1
  local IFS=,
  echo "${nodes[*]:0:n}"
}

count_of() {
  local list="${1:-}"
  [[ -n "$list" ]] || { echo 0; return; }
  awk -F, '{n=0; for (i=1; i<=NF; i++) if ($i != "") n++; print n}' <<<"$list"
}

existing_node_count() {
  local dir="$1" f n best=0
  [[ -d "$dir" ]] || { echo 0; return; }
  while IFS= read -r f; do
    n=$(grep -ohE "$NODE_RE" "$f" 2>/dev/null | sort -u | wc -l | tr -d ' ')
    ((n > best)) && best=$n
  done < <(find "$dir" -mindepth 2 -maxdepth 3 \( -name "$BW_FILE" -o -name "$LAT_FILE" \) -not -path '*/old/*' 2>/dev/null)
  echo "$best"
}

archive_existing() {
  local dir="$1" d moved=0 stamp
  stamp=$(date +%Y%m%d_%H%M%S)
  mkdir -p "${dir}/old"
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    mv "$d" "${dir}/old/" 2>/dev/null && moved=$((moved + 1))
  done < <(find "$dir" -mindepth 1 -maxdepth 1 -type d -name '20*_*' 2>/dev/null)
  ((moved > 0)) && echo "$stamp" >"${dir}/old/.archived_${stamp}"
  echo "$moved"
}

rack_pool() {
  local pat="" r
  for r in "$@"; do pat+="${pat:+|}^${NODE_BASE}${r}-"; done
  idle_nodes "$pat" | largest_even
}

wait_for_jobs() {
  local list
  list=$(IFS=,; echo "$*")
  while [[ -n $(squeue -j "$list" -h 2>/dev/null) ]]; do
    sleep "$POLL"
  done
}

sbatch_for_type() {
  case "$1" in
    per_rack) echo "${_SCRIPT_DIR}/clusterkit_suite_per_rack.sbatch" ;;
    2_node_test) echo "${_SCRIPT_DIR}/clusterkit_suite_2_node.sbatch" ;;
    2_node_cross_rack_test) echo "${_SCRIPT_DIR}/clusterkit_suite_2_node_cross_rack.sbatch" ;;
    2_rack_test) echo "${_SCRIPT_DIR}/clusterkit_suite_2_rack.sbatch" ;;
    entire_SU) echo "${_SCRIPT_DIR}/clusterkit_suite_entire_su.sbatch" ;;
    *) echo "ERROR: unknown test type '$1'" >&2; return 1 ;;
  esac
}

submit() {
  local ttype="$1" label="$2" nodelist="$3" outdir="$4" sbatch_script jid
  sbatch_script=$(sbatch_for_type "$ttype") || return 1
  [[ -f "$sbatch_script" ]] || { echo "    ERROR: missing ${sbatch_script}" >&2; return 1; }
  mkdir -p "$outdir"

  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "    would run: sbatch -w <$(count_of "$nodelist") nodes> ${sbatch_script}" >&2
    echo "DRYRUN"
    return 0
  fi

  _sbatch_extra=()
  if [[ -n "${RACK_CLUSTERKIT_SUITE_PARTITION:-${RACK_CLUSTERKIT_PARTITION:-}}" ]]; then
    _sbatch_extra+=(-p "${RACK_CLUSTERKIT_SUITE_PARTITION:-${RACK_CLUSTERKIT_PARTITION:-}}")
  fi

  jid=$(
    sbatch --parsable \
      "${_sbatch_extra[@]}" \
      -w "$nodelist" \
      --export="ALL,NVIS_BIN_SCRIPTS=${NVIS_BIN_SCRIPTS},RACK_CLUSTERKIT_SUITE_OUTDIR=${outdir},RACK_CLUSTERKIT_SUITE_TEST_TYPE=${ttype},RACK_CLUSTERKIT_SUITE_RUN_LABEL=${label},RACK_CLUSTERKIT_SUITE_SU_LABEL=${SU_LABEL},CUDA_MAJ=${CUDA_MAJ},WORKSPACE_DIR=${WORKSPACE_DIR},NVIS_WORKSPACE_DIR=${WORKSPACE_DIR},TOPDIR=${WORKSPACE_DIR}" \
      --output="${outdir%/}/slurm-${ttype//_/-}-%j.out" \
      "$sbatch_script" 2>/dev/null
  )
  [[ -n "$jid" ]] || { echo "    ERROR: sbatch failed for ${label}" >&2; return 1; }
  echo "$jid"
}

validate_job_results() {
  local jid="$1" dest="$2" found=0
  [[ "$jid" == "DRYRUN" ]] && return 0
  found=$(find "$dest" -maxdepth 1 -type d -name "*_${jid}_*" 2>/dev/null | wc -l | tr -d ' ')
  if ((found == 0)); then
    echo "    WARNING: job ${jid} left no HCA result directories under ${dest}/"
    fail "${dest} - no result dirs for job ${jid}"
    return 1
  fi
  echo "    job ${jid}: ${found} HCA dir(s) under ${dest}/"
  if ((EXPECTED_HCAS > 0 && found != EXPECTED_HCAS)); then
    echo "    WARNING: expected ${EXPECTED_HCAS} HCA dirs, got ${found}"
    fail "${dest} - ${found}/${EXPECTED_HCAS} HCA dirs"
  fi
  check_perf "$dest" || true
}

run_one() {
  local ttype="$1" label="$2" nodelist="$3" dest="$4" jid new_n old_n archived

  if [[ -z "$nodelist" ]]; then
    echo "[$(date +%H:%M:%S)] SKIP ${label} - fewer than 2 idle nodes"
    fail "${label} - skipped, no usable nodes"
    return 0
  fi

  new_n=$(count_of "$nodelist")
  old_n=$(existing_node_count "$dest")

  if ((old_n > 0)); then
    if [[ "${FORCE:-0}" == "1" ]]; then
      echo "[$(date +%H:%M:%S)] ${label} (${new_n} nodes) - FORCE, existing ${old_n}n"
    elif ((old_n >= new_n)); then
      echo "[$(date +%H:%M:%S)] SKIP ${label} - existing ${old_n}n result >= ${new_n}n available"
      return 0
    else
      echo "[$(date +%H:%M:%S)] ${label} (${new_n} nodes) - improves on existing ${old_n}n"
    fi
    if [[ "${DRY_RUN:-0}" != "1" ]]; then
      archived=$(archive_existing "$dest")
      echo "    archived ${archived} old dir(s) -> ${dest}/old/"
    else
      echo "    would archive existing ${old_n}n result -> ${dest}/old/"
    fi
  else
    echo "[$(date +%H:%M:%S)] ${label} (${new_n} nodes)"
  fi

  if ! jid=$(submit "$ttype" "$label" "$nodelist" "$dest"); then
    fail "${label} - sbatch submit failed"
    return 0
  fi
  [[ "$jid" == "DRYRUN" ]] && return 0

  echo "    job ${jid} submitted - waiting..."
  wait_for_jobs "$jid"
  validate_job_results "$jid" "$dest" || true
}

scan_dir() {
  local dir="$1"
  P_JIDS=(); P_NODES=(); P_RACKS=(); P_HCAS=()
  SCAN_TOTAL=0; SCAN_RUNDIRS=0; SCAN_INCOMPLETE=0; SCAN_MISSING=()

  declare -A J_NODES=() J_HCAS=()
  local d base jid hca nodes f miss all="" i=0 un rk

  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    SCAN_RUNDIRS=$((SCAN_RUNDIRS + 1))
    base=$(basename "$d")
    jid=$(sed 's/.*_\([0-9][0-9]*\)_.*/\1/' <<<"$base")
    hca=$(sed 's/.*_[0-9][0-9]*_//' <<<"$base")

    miss=""
    for f in "$BW_FILE" "$LAT_FILE" "$BW_JSON" "$LAT_JSON"; do
      compgen -G "$d/$f" >/dev/null || miss+=" ${f#\*}"
    done
    if [[ -n "$miss" ]]; then
      SCAN_INCOMPLETE=$((SCAN_INCOMPLETE + 1))
      SCAN_MISSING+=("$(basename "$d") missing:${miss}")
    fi

    nodes=$(grep -ohE "$NODE_RE" "$d"/$BW_FILE 2>/dev/null | sort -u)
    [[ -z "$nodes" ]] && nodes=$(grep -ohE "$NODE_RE" "$d"/$LAT_FILE 2>/dev/null | sort -u)

    J_NODES["$jid"]="${J_NODES[$jid]:-}"$'\n'"$nodes"
    J_HCAS["$jid"]="${J_HCAS[$jid]:-} $hca"
    all+=$'\n'"$nodes"
  done < <(find "$dir" -type d -name '20*_*' -not -path '*/old/*' 2>/dev/null | sort)

  SCAN_TOTAL=$(grep . <<<"$all" | sort -u | grep -c . || true)

  for jid in $(printf '%s\n' "${!J_NODES[@]}" | grep . | sort -n); do
    un=$(grep . <<<"${J_NODES[$jid]}" | sort -u)
    rk=$(sed 's/.*-dh0[12]-\([a-z][0-9]*\)-[0-9]*/\1/' <<<"$un" | grep . | sort -u | tr '\n' ' ' | sed 's/ $//')
    P_JIDS[$i]="$jid"
    P_NODES[$i]=$(grep -c . <<<"$un" || true)
    P_RACKS[$i]="$rk"
    P_HCAS[$i]=$(tr ' ' '\n' <<<"${J_HCAS[$jid]}" | grep . | sort -u | tr '\n' ' ' | sed 's/ $//')
    i=$((i + 1))
  done
}

all_racks_found() {
  local i
  for ((i = 0; i < ${#P_JIDS[@]}; i++)); do echo "${P_RACKS[$i]}"; done | tr ' ' '\n' | sort -u | grep .
}

detect_prefix() {
  echo "$1" | sed 's/[0-9]*$//' | grep . | sort | uniq -c | sort -rn | head -1 | awk '{print $2}'
}

do_run() {
  local DOMAIN="${1:-}" P="${2:-}"
  SU_LABEL="${3:-}"

  [[ -n "$DOMAIN" && -n "$P" && -n "$SU_LABEL" ]] || {
    echo "ERROR: run mode needs <DOMAIN> <PREFIX> <SU_LABEL>"
    echo "       e.g. $0 run dh02 j SU10"
    exit 1
  }
  [[ "$DOMAIN" == "dh01" || "$DOMAIN" == "dh02" ]] || {
    echo "ERROR: DOMAIN must be dh01 or dh02 (got '$DOMAIN')"
    exit 1
  }

  NODE_BASE="ink50-${DOMAIN}-${P}"
  local OUTDIR="${RESULTS_ROOT%/}/${SU_LABEL}"

  banner "ClusterKit suite run - ${DOMAIN} / prefix ${P} (${SU_LABEL})"
  echo "  Nodes:    ${NODE_BASE}<rack>-<unit>"
  echo "  Racks:    ${RACKS[*]}"
  echo "  Results:  ${OUTDIR}/<test_type>/"
  echo "  Limits:   bw ${BW_MIN}-${BW_MAX} MB/s, lat ${LAT_MIN}-${LAT_MAX} usec"
  echo "  Started:  $(date)"
  echo ""
  mkdir -p "$OUTDIR"

  echo "Pre-flight - available nodes vs existing results:"
  printf "    %-6s %6s %8s %10s   %s\n" "rack" "idle" "will run" "existing" "action"
  printf "    %-6s %6s %8s %10s   %s\n" "------" "------" "--------" "----------" "------"
  local total=0 short=0 n_run=0 n_skip=0 n_rerun=0 c r use old ex act
  for r in "${RACKS[@]}"; do
    c=$(idle_nodes "^${NODE_BASE}${r}-" | wc -l | tr -d ' ')
    total=$((total + c))
    use=$((c % 2 ? c - 1 : c))
    ((c < 18)) && short=$((short + 1))
    old=$(existing_node_count "${OUTDIR}/per_rack/${P}${r}")
    if ((old == 0)); then
      ex="-"; act="run"; n_run=$((n_run + 1))
    elif [[ "${FORCE:-0}" == "1" ]]; then
      ex="${old}n"; act="re-run (FORCE)"; n_rerun=$((n_rerun + 1))
    elif ((old >= use)); then
      ex="${old}n"; act="skip"; n_skip=$((n_skip + 1))
    else
      ex="${old}n"; act="re-run (archive ${old}n)"; n_rerun=$((n_rerun + 1))
    fi
    ((use < 2)) && act="skip (no usable nodes)"
    printf "    %-6s %6d %8d %10s   %s\n" "${P}${r}" "$c" "$use" "$ex" "$act"
  done
  echo "  Total idle in SU: ${total}/288"
  echo "  per_rack plan: ${n_run} new, ${n_rerun} re-run, ${n_skip} skipped"

  if ((short > 0)); then
    echo ""
    echo "  NOTE: ${short} rack(s) short of 18 idle nodes."
  fi
  if [[ -t 0 ]] && ((short > 0 || n_rerun > 0)); then
    read -rp "  Continue? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
  fi

  banner "PHASE 1: per_rack - ${#RACKS[@]} racks"
  if [[ "${PARALLEL:-0}" != "1" || "${DRY_RUN:-0}" == "1" ]]; then
    for r in "${RACKS[@]}"; do
      run_one "per_rack" "per_rack ${P}${r}" "$(rack_pool "$r")" "${OUTDIR}/per_rack/${P}${r}"
    done
  else
    declare -A RACK_JOB=()
    local ALL_JIDS=() pool jid
    for r in "${RACKS[@]}"; do
      pool=$(rack_pool "$r")
      if [[ -z "$pool" ]]; then
        echo "    SKIP ${P}${r}: fewer than 2 idle nodes"
        fail "per_rack ${P}${r} - skipped, no usable nodes"
        continue
      fi
      jid=$(submit "per_rack" "per_rack ${P}${r}" "$pool" "${OUTDIR}/per_rack/${P}${r}") || {
        fail "per_rack ${P}${r} - sbatch submit failed"; continue;
      }
      RACK_JOB["$r"]="$jid"
      ALL_JIDS+=("$jid")
      printf "[%s] per_rack %s%s %2d nodes -> job %s\n" "$(date +%H:%M:%S)" "$P" "$r" "$(count_of "$pool")" "$jid"
    done
    if ((${#ALL_JIDS[@]})); then
      echo ""
      echo "  ${#ALL_JIDS[@]} jobs submitted - waiting..."
      wait_for_jobs "${ALL_JIDS[@]}"
      echo "  Checking results:"
      for r in "${RACKS[@]}"; do
        [[ -n "${RACK_JOB[$r]:-}" ]] && validate_job_results "${RACK_JOB[$r]}" "${OUTDIR}/per_rack/${P}${r}" || true
      done
    fi
  fi

  banner "PHASE 2: scaling tests"
  local FIRST="${RACKS[0]}" SECOND="${RACKS[1]}" LAST="${RACKS[${#RACKS[@]}-1]}"
  local two_intra n1 n2 two_cross

  two_intra=$(idle_nodes "^${NODE_BASE}${FIRST}-" | head -2 | paste -sd,)
  [[ $(count_of "$two_intra") -eq 2 ]] || two_intra=""
  run_one "2_node_test" "2_node intra-rack (${P}${FIRST})" "$two_intra" "${OUTDIR}/2_node_test"

  n1=$(idle_nodes "^${NODE_BASE}${FIRST}-" | head -1)
  n2=$(idle_nodes "^${NODE_BASE}${LAST}-" | tail -1)
  two_cross=""
  [[ -n "$n1" && -n "$n2" ]] && two_cross="${n1},${n2}"
  run_one "2_node_cross_rack_test" "2_node cross-rack (${P}${FIRST} + ${P}${LAST})" "$two_cross" "${OUTDIR}/2_node_cross_rack_test"

  run_one "2_rack_test" "2_rack (${P}${FIRST} + ${P}${SECOND})" "$(rack_pool "$FIRST" "$SECOND")" "${OUTDIR}/2_rack_test"
  run_one "entire_SU" "entire_SU (${#RACKS[@]} racks)" "$(rack_pool "${RACKS[@]}")" "${OUTDIR}/entire_SU"

  banner "${DOMAIN}-${P} (${SU_LABEL}) complete - $(date)"
  echo ""
  echo "Layout under ${OUTDIR}/:"
  local sub n
  for sub in "${TEST_TYPES[@]}"; do
    if [[ -d "${OUTDIR}/${sub}" ]]; then
      n=$(find "${OUTDIR}/${sub}" -type d -name "20*_*" 2>/dev/null | wc -l | tr -d ' ')
      printf "    %-24s %3d HCA dir(s)\n" "${sub}/" "$n"
    else
      printf "    %-24s missing\n" "${sub}/"
    fi
  done
  report_failures
  echo ""
  echo "Full re-check:  $0 check ${SU_LABEL}"
}

do_check() {
  local su_labels=()
  if (($#)); then
    su_labels=("$@")
  else
    mapfile -t su_labels < <(find "$RESULTS_ROOT" -maxdepth 1 -mindepth 1 -type d -print 2>/dev/null | sed 's|.*/||' | sort -V)
  fi
  if ((${#su_labels[@]} == 0)); then
    echo "ERROR: no SU directories found under ${RESULTS_ROOT}/"
    exit 1
  fi

  declare -A SUMMARY
  local su tdir ttype job_count total_nodes racks_list rack_count rack_str pfx i jid jn jr rc missing ok run_count

  for su in "${su_labels[@]}"; do
    local sudir="${RESULTS_ROOT%/}/${su}"
    if [[ ! -d "$sudir" ]]; then
      banner "${su} - NOT FOUND"
      for ttype in "${TEST_TYPES[@]}"; do SUMMARY["${su}_${ttype}"]="NO_DIR"; done
      fail "${su} - directory missing"
      continue
    fi

    banner "${su} (${sudir})"
    for ttype in "${TEST_TYPES[@]}"; do
      tdir="${sudir}/${ttype}"
      echo ""
      echo "--- $ttype ---"

      if [[ ! -d "$tdir" ]]; then
        echo "  MISSING directory"
        SUMMARY["${su}_${ttype}"]="MISSING"
        fail "${su}/${ttype} - missing"
        continue
      fi

      scan_dir "$tdir"
      run_count=$SCAN_RUNDIRS
      if ((run_count == 0)); then
        echo "  EMPTY - no run directories"
        SUMMARY["${su}_${ttype}"]="EMPTY"
        fail "${su}/${ttype} - empty"
        continue
      fi

      job_count=${#P_JIDS[@]}
      total_nodes=$SCAN_TOTAL
      racks_list=$(all_racks_found 2>/dev/null || true)
      rack_count=$(echo "$racks_list" | grep -c . 2>/dev/null || echo 0)
      rack_str=$(echo "$racks_list" | tr '\n' ' ')
      pfx=$(detect_prefix "$racks_list")

      echo "  ${job_count} job(s), ${total_nodes} node(s), ${rack_count} rack(s): ${rack_str}"
      echo "  ${run_count} run dir(s)"

      if ((SCAN_INCOMPLETE > 0)); then
        echo "  *** ${SCAN_INCOMPLETE} INCOMPLETE run dir(s):"
        local m
        for m in "${SCAN_MISSING[@]}"; do echo "        $m"; done
        fail "${su}/${ttype} - ${SCAN_INCOMPLETE} incomplete run dir(s)"
      fi

      for ((i = 0; i < job_count; i++)); do
        local nh
        nh=$(wc -w <<<"${P_HCAS[$i]}")
        if ((EXPECTED_HCAS > 0 && nh != EXPECTED_HCAS)); then
          echo "    job ${P_JIDS[$i]}: ${nh}/${EXPECTED_HCAS} HCAs (${P_HCAS[$i]})"
          fail "${su}/${ttype} job ${P_JIDS[$i]} - ${nh}/${EXPECTED_HCAS} HCAs"
        fi
      done

      case "$ttype" in
        per_rack)
          for ((i = 0; i < job_count; i++)); do
            jid="${P_JIDS[$i]}"; jn="${P_NODES[$i]}"; jr="${P_RACKS[$i]}"
            rc=$(wc -w <<<"$jr")
            warn=""
            ((rc != 1)) && { warn="$warn WARN:spans-${rc}-racks"; fail "${su}/per_rack job ${jid} spans ${rc} racks"; }
            ((jn != 18)) && { warn="$warn WARN:${jn}/18-nodes"; fail "${su}/per_rack ${jr} - ${jn}/18 nodes"; }
            echo "    job $jid  $jr  ${jn}n${warn}"
          done
          missing=""
          for r in "${RACKS[@]}"; do
            echo "$racks_list" | grep -qx "${pfx}${r}" || missing="$missing ${pfx}${r}"
          done
          if [[ -n "$missing" ]]; then
            echo "  MISSING racks:$missing"
            SUMMARY["${su}_${ttype}"]="${rack_count}/${#RACKS[@]} GAPS"
            fail "${su}/per_rack - missing racks:${missing}"
          else
            echo "  All ${#RACKS[@]} racks covered"
            SUMMARY["${su}_${ttype}"]="${#RACKS[@]}/${#RACKS[@]} OK"
          fi
          check_perf "$tdir" || true
          ;;
        2_node_test|2_node_cross_rack_test)
          local want_cross=0
          [[ "$ttype" == "2_node_cross_rack_test" ]] && want_cross=1
          ok=true
          for ((i = 0; i < job_count; i++)); do
            jid="${P_JIDS[$i]}"; jn="${P_NODES[$i]}"; jr="${P_RACKS[$i]}"
            rc=$(wc -w <<<"$jr")
            echo "    job $jid  racks=$jr  ${jn}n"
            if ((jn != 2)); then
              echo "      FAIL: expected 2 nodes, got $jn"
              fail "${su}/${ttype} job ${jid} - ${jn} nodes"; ok=false
            fi
            if ((want_cross)); then
              if ((rc < 2)); then
                echo "      FAIL: expected different racks"
                fail "${su}/${ttype} job ${jid} - not cross-rack"; ok=false
              else
                echo "      OK cross-rack"
              fi
            else
              if ((rc != 1)); then
                echo "      FAIL: expected same rack"
                fail "${su}/${ttype} job ${jid} - not intra-rack"; ok=false
              else
                echo "      OK intra-rack"
              fi
            fi
          done
          $ok && SUMMARY["${su}_${ttype}"]="${total_nodes}n OK" || SUMMARY["${su}_${ttype}"]="${total_nodes}n WARN"
          check_perf "$tdir" || true
          ;;
        2_rack_test)
          ok=true
          for ((i = 0; i < job_count; i++)); do
            jid="${P_JIDS[$i]}"; jn="${P_NODES[$i]}"; jr="${P_RACKS[$i]}"
            rc=$(wc -w <<<"$jr")
            echo "    job $jid  racks=$jr  ${jn}n"
            if ((rc < 2)); then
              echo "      FAIL: expected 2+ racks"
              fail "${su}/2_rack_test job ${jid} - ${rc} rack(s)"; ok=false
            else
              echo "      OK $rc racks"
            fi
          done
          ((job_count > 1)) && { echo "  NOTE: ${job_count} jobs - expected 1"; fail "${su}/2_rack_test - ${job_count} jobs"; }
          $ok && SUMMARY["${su}_${ttype}"]="${total_nodes}n ${rack_count}r OK" || SUMMARY["${su}_${ttype}"]="${total_nodes}n WARN"
          check_perf "$tdir" || true
          ;;
        entire_SU)
          for ((i = 0; i < job_count; i++)); do
            jid="${P_JIDS[$i]}"; jn="${P_NODES[$i]}"; jr="${P_RACKS[$i]}"
            rc=$(wc -w <<<"$jr")
            echo "    job $jid  ${rc} rack(s)  ${jn}n"
          done
          missing=""
          for r in "${RACKS[@]}"; do
            echo "$racks_list" | grep -qx "${pfx}${r}" || missing="$missing ${pfx}${r}"
          done
          if [[ -n "$missing" ]]; then
            echo "  MISSING racks:$missing"
            SUMMARY["${su}_${ttype}"]="${rack_count}/${#RACKS[@]} ${total_nodes}n GAPS"
            fail "${su}/entire_SU - missing racks:${missing}"
          else
            echo "  All ${#RACKS[@]} racks, $total_nodes nodes"
            SUMMARY["${su}_${ttype}"]="${#RACKS[@]}/${#RACKS[@]} ${total_nodes}n OK"
            ((total_nodes < 288)) && echo "  NOTE: ${total_nodes}/288 nodes - unavailable nodes were skipped"
          fi
          check_perf "$tdir" || true
          ;;
      esac
    done
  done

  banner "SUMMARY"
  echo ""
  printf "  %-10s  %-16s  %-12s  %-14s  %-16s  %-22s\n" "SU" "per_rack" "2_node" "2_node_xrack" "2_rack" "entire_SU"
  printf "  %-10s  %-16s  %-12s  %-14s  %-16s  %-22s\n" "----------" "--------" "------" "------------" "------" "---------"
  for su in "${su_labels[@]}"; do
    printf "  %-10s" "$su"
    printf "  %-16s" "${SUMMARY["${su}_per_rack"]:-???}"
    printf "  %-12s" "${SUMMARY["${su}_2_node_test"]:-???}"
    printf "  %-14s" "${SUMMARY["${su}_2_node_cross_rack_test"]:-???}"
    printf "  %-16s" "${SUMMARY["${su}_2_rack_test"]:-???}"
    printf "  %-22s" "${SUMMARY["${su}_entire_SU"]:-???}"
    echo ""
  done
  report_failures
}

report_failures() {
  echo ""
  printf -- '-%.0s' {1..60}; echo
  if ((ISSUES == 0)); then
    echo "  ALL CLEAR"
    echo "    bandwidth ${BW_MIN}-${BW_MAX} MB/s, latency ${LAT_MIN}-${LAT_MAX} usec"
  else
    echo "  ${ISSUES} issue(s) flagged:"
    local t
    for t in "${FAILED_TESTS[@]}"; do echo "    - ${t}"; done
  fi
  if ((GLITCH_TOTAL > 0)); then
    echo ""
    echo "  ${GLITCH_TOTAL} glitched reading(s) ignored."
  fi
  printf -- '-%.0s' {1..60}; echo
}

MODE="${1:-}"
shift || true

case "$MODE" in
  run) do_run "$@" ;;
  check) do_check "$@" ;;
  help|-h|--help) usage 0 ;;
  "") echo "ERROR: no mode given"; echo ""; usage 1 ;;
  *) echo "ERROR: unknown mode '$MODE'"; echo ""; usage 1 ;;
esac

exit $((ISSUES > 0 ? 1 : 0))
