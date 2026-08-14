#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# NEMO 1-rack log parser: tables per rack + completed / failed / ongoing lists.
# Each log file is one rack run: root-NEMO-1Rack-<rack>-<N>N-<jobid>.txt
# Original metrics logic: Junli Zhang <junliz@nvidia.com> (parser v1).
# v2: column tables (NCCL all_reduce / all_gather / alltoall + Nemotron 5H 56B),
#     rack status via rack_status.sh (cmsh + sinfo + logs), optional squeue.
# v3: optional --json / --csv export (python3); skips colored tables and rack_status.sh.
# v4 (2026-07-28): UNIVERSAL INGEST. The parser now also emits a per-FILE inventory ("results") so
#     every file in a log directory is visible — not just NEMO/NCCL/MNUBERGEMM. New: directory
#     arguments and --dir are enumerated (find, depth-limited); each file gets kind detection,
#     size/mtime and first/last lines; unrecognised files come back as kind="unknown" instead of
#     being dropped. The pre-existing --json keys are UNCHANGED (strict superset) and the legacy
#     tables still receive exactly the same rows for explicitly-named file arguments, so existing
#     charts keep working byte for byte. --csv output is unchanged.
#     No new external dependencies: python3 only (already required by --json/--csv), NO jq.

set -uo pipefail

# This script is executed over a NON-LOGIN ssh shell (RACK_EXEC_MODE=ssh) whose PATH can be as thin
# as /usr/bin:/bin. Pin the standard directories so find/stat/date/head/tail/sort/python3 resolve.
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
export PATH

# Colors: off if NO_COLOR is non-empty (https://no-color.org/), or stdout is not a tty.
# Set FORCE_COLOR=1|yes|true to colorize when piping (e.g. FORCE_COLOR=1 ./parser.sh … | less -R).
use_color=0
force_c="${FORCE_COLOR:-0}"
[[ "$force_c" =~ ^(1|[Yy]|[Yy][Ee][Ss]|[Tt][Rr][Uu][Ee])$ ]] && force_on=1 || force_on=0
if [[ -z "${NO_COLOR:-}" ]] && { [[ -t 1 ]] || [[ "$force_on" -eq 1 ]]; }; then
  use_color=1
fi
if [[ "$use_color" -eq 1 ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_H1=$'\033[1;36m'   # section titles
  C_H2=$'\033[1;35m'   # subsection / list headers
  C_OK=$'\033[32m'
  C_FAIL=$'\033[31m'
  C_WARN=$'\033[33m'
  C_UNK=$'\033[33m'
  C_NOTE=$'\033[90m'   # gray note
  C_SEP=$'\033[36m'    # separators
  C_PENDING=$'\033[1;33m' # cmsh racks still missing a completed NEMO run
else
  C_RESET='' C_BOLD='' C_DIM='' C_H1='' C_H2='' C_OK='' C_FAIL='' C_WARN='' C_UNK='' C_NOTE='' C_SEP='' C_PENDING=''
fi

# `perf` (the MFU denominator) is owned by lib/llm_label.inc.sh, sourced below.
USE_SQUEUE=1
USE_CMSH=1
USE_SINFO=1
OUTPUT_FORMAT=human
_want_json=0
_want_csv=0

# ---- v4 universal-ingest knobs (all overridable by env; flags win over env) -------------------
SCAN_MAXDEPTH="${NVIS_PARSER_MAXDEPTH:-2}"       # find -maxdepth for directory arguments
INV_HEAD_LINES="${NVIS_PARSER_HEAD_LINES:-8}"    # first N lines kept per file
INV_TAIL_LINES="${NVIS_PARSER_TAIL_LINES:-8}"    # last  N lines kept per file
INV_LINE_MAXCH="${NVIS_PARSER_LINE_MAXCH:-400}"  # per-line character cap (long host lists!)
SCAN_MAX_FILES="${NVIS_PARSER_MAX_FILES:-4000}"  # hard cap on enumerated files
PROBE_CONTENT="${NVIS_PARSER_PROBE:-1}"          # 0 = classify by filename only (no content read)
PROBE_FULL_MAX="${NVIS_PARSER_PROBE_FULL_MAX:-16777216}"  # read whole file below this size
PROBE_WIN="${NVIS_PARSER_PROBE_WINDOW:-2097152}"          # else head+tail window of this size
LINECOUNT_MAX="${NVIS_PARSER_LINECOUNT_MAX:-268435456}"   # skip wc -l above this size
WANT_INVENTORY="${NVIS_PARSER_INVENTORY:-1}"     # 0 = do not emit the "results" array
INVENTORY_ONLY=0                                 # 1 = inventory only, skip legacy extractors
LEGACY_ALL="${NVIS_PARSER_LEGACY_ALL:-0}"        # 1 = legacy rows for scanned files too
ABSTAIN="${NVIS_PARSER_ABSTAIN:-0}"              # 1 = no placeholder rows for foreign logs
declare -a SCAN_DIRS=()

_need_val() { # $1=flag name, $2=value or empty
  if [[ -z "${2:-}" ]]; then
    echo "$0: $1 requires a value" >&2
    exit 1
  fi
}

while [[ "${1:-}" == -* ]]; do
  case "$1" in
    --no-squeue) USE_SQUEUE=0 ;;
    --no-cmsh)   USE_CMSH=0 ;;
    --no-sinfo)  USE_SINFO=0 ;;
    --json)      OUTPUT_FORMAT=json; _want_json=1 ;;
    --csv)       OUTPUT_FORMAT=csv; _want_csv=1 ;;
    --dir)          _need_val --dir "${2:-}";      SCAN_DIRS+=("$2"); shift ;;
    --dir=*)        SCAN_DIRS+=("${1#*=}") ;;
    --maxdepth)     _need_val --maxdepth "${2:-}"; SCAN_MAXDEPTH="$2"; shift ;;
    --maxdepth=*)   SCAN_MAXDEPTH="${1#*=}" ;;
    --head-lines)   _need_val --head-lines "${2:-}"; INV_HEAD_LINES="$2"; shift ;;
    --head-lines=*) INV_HEAD_LINES="${1#*=}" ;;
    --tail-lines)   _need_val --tail-lines "${2:-}"; INV_TAIL_LINES="$2"; shift ;;
    --tail-lines=*) INV_TAIL_LINES="${1#*=}" ;;
    --max-files)    _need_val --max-files "${2:-}"; SCAN_MAX_FILES="$2"; shift ;;
    --max-files=*)  SCAN_MAX_FILES="${1#*=}" ;;
    --abstain)       ABSTAIN=1 ;;
    --no-probe)      PROBE_CONTENT=0 ;;
    --no-inventory)  WANT_INVENTORY=0 ;;
    --inventory-only) INVENTORY_ONLY=1; WANT_INVENTORY=1 ;;
    --) shift; break ;;
    -h|--help)
      echo "Usage: $0 [options] root-NEMO-1Rack-*.txt [...] [DIR ...]" >&2
      echo "  --no-squeue  do not match Slurm job ids against squeue." >&2
      echo "  --no-cmsh    skip cmsh rack list vs completed-rack coverage." >&2
      echo "  --no-sinfo   skip sinfo Slurm node-state summary per rack." >&2
      echo "  --json       print parsed tables as JSON (stdout only; requires python3)." >&2
      echo "  --csv        print one merged CSV table (stdout only; requires python3)." >&2
      echo "  --json and --csv are mutually exclusive." >&2
      echo "Universal ingest (v4): any DIR argument, or --dir DIR, is enumerated and EVERY file in" >&2
      echo "it appears in the JSON \"results\" array (kind, size, mtime, first/last lines)." >&2
      echo "  --dir DIR          enumerate DIR (repeatable). A bare directory argument works too." >&2
      echo "  --maxdepth N       find depth for enumeration (default ${SCAN_MAXDEPTH})." >&2
      echo "  --head-lines N / --tail-lines N   lines kept per file (default ${INV_HEAD_LINES}/${INV_TAIL_LINES})." >&2
      echo "  --max-files N      cap on enumerated files (default ${SCAN_MAX_FILES})." >&2
      echo "  --abstain          OPT-IN: do not emit '---' / UNK placeholder rows in the NCCL and" >&2
      echo "                     Nemotron tables for logs that are not NCCL/NeMo logs. Off by" >&2
      echo "                     default because it changes the legacy JSON for foreign logs." >&2
      echo "  --no-probe         classify by filename only, never read file content." >&2
      echo "  --no-inventory     omit the \"results\" array (pre-v4 JSON)." >&2
      echo "  --inventory-only   list files only; skip the NCCL/NeMo/MNUBERGEMM extractors." >&2
      echo "Env: NO_COLOR, FORCE_COLOR=1|yes, SQUEUE_USER (squeue user), SINFO_OPTS (extra sinfo args)," >&2
      echo "  NVIS_PARSER_MAXDEPTH, NVIS_PARSER_HEAD_LINES, NVIS_PARSER_TAIL_LINES," >&2
      echo "  NVIS_PARSER_LINE_MAXCH, NVIS_PARSER_MAX_FILES, NVIS_PARSER_PROBE=0," >&2
      echo "  NVIS_PARSER_INVENTORY=0, NVIS_PARSER_LEGACY_ALL=1, NVIS_RACK_FNAME_RE." >&2
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

for _n in SCAN_MAXDEPTH INV_HEAD_LINES INV_TAIL_LINES INV_LINE_MAXCH SCAN_MAX_FILES \
          PROBE_FULL_MAX PROBE_WIN LINECOUNT_MAX; do
  if [[ ! "${!_n}" =~ ^[0-9]+$ ]]; then
    echo "$0: $_n must be a non-negative integer (got '${!_n}')" >&2
    exit 1
  fi
done
unset _n

if [[ "$_want_json" -eq 1 && "$_want_csv" -eq 1 ]]; then
  echo "$0: use only one of --json or --csv" >&2
  exit 1
fi

if [[ $# -eq 0 && ${#SCAN_DIRS[@]} -eq 0 ]]; then
  echo "Usage: $0 [--no-squeue] [--no-cmsh] [--no-sinfo] [--json|--csv] root-NEMO-1Rack-*.txt [...] [DIR ...]" >&2
  echo "  --no-squeue  do not match Slurm job ids against squeue." >&2
  echo "  --no-cmsh    skip cmsh rack list vs completed-rack coverage." >&2
  echo "  --no-sinfo   skip sinfo Slurm node-state summary per rack." >&2
  echo "  --json       export parsed metrics as JSON (no colored tables; no rack_status.sh)." >&2
  echo "  --csv        export merged metrics as CSV (same constraints as --json)." >&2
  echo "Env: NO_COLOR, FORCE_COLOR=1|yes, SQUEUE_USER (squeue user), SINFO_OPTS (extra sinfo args)." >&2
  echo "Rack table only: $(dirname "${BASH_SOURCE[0]}")/rack_status.sh [--flags] <logs…| --tmp-status FILE>" >&2
  exit 1
fi

# ---- INPUT SET ------------------------------------------------------------------------------
# Positional arguments that are directories are enumerated (v4); everything else is treated as an
# explicit log file exactly as before. IN_SRC[i] is "explicit" or "scan"; only "explicit" files
# feed the legacy NCCL/NeMo tables unconditionally, so pre-v4 call sites are byte-for-byte stable.
declare -a log_files=() IN_SRC=()
declare -A _SEEN_PATH=() _SEEN_REAL=() _EXPL_BASE=()
SCAN_TRUNCATED=0

for _a in "$@"; do
  if [[ -d "$_a" ]]; then
    SCAN_DIRS+=("$_a")
  else
    log_files+=("$_a")
    IN_SRC+=(explicit)
    _SEEN_PATH["$_a"]=1
    # Canonical path + basename of the explicitly-named files only (a handful), so that a later
    # directory scan does not list the same log twice under a different spelling.
    _EXPL_BASE["$(basename -- "$_a")"]=1
    _r=$(readlink -f -- "$_a" 2>/dev/null) || _r=""
    [[ -n "$_r" ]] && _SEEN_REAL["$_r"]=1
  fi
done
unset _a _r

# GNU sort supports -z (NUL-delimited). If it does not, fall back to unsorted find output rather
# than silently enumerating nothing.
if printf 'b\000a\000' | sort -z >/dev/null 2>&1; then _SORTZ=1; else _SORTZ=0; fi

_scan_count=0
for _d in "${SCAN_DIRS[@]:-}"; do
  [[ -n "$_d" ]] || continue
  if [[ ! -d "$_d" ]]; then
    echo "${C_WARN}skip (not a directory):${C_RESET} $_d" >&2
    continue
  fi
  # -print0 + read -d '' so filenames with spaces/quotes/350-byte host lists survive intact.
  while IFS= read -r -d '' _p; do
    [[ -n "$_p" ]] || continue
    [[ -n "${_SEEN_PATH["$_p"]:-}" ]] && continue
    # Only pay for readlink when the basename collides with an explicit argument.
    if [[ -n "${_EXPL_BASE["$(basename -- "$_p")"]:-}" ]]; then
      _pr=$(readlink -f -- "$_p" 2>/dev/null) || _pr=""
      [[ -n "$_pr" && -n "${_SEEN_REAL["$_pr"]:-}" ]] && continue
    fi
    if [[ "$SCAN_MAX_FILES" -gt 0 && "$_scan_count" -ge "$SCAN_MAX_FILES" ]]; then
      SCAN_TRUNCATED=1
      break
    fi
    _scan_count=$((_scan_count + 1))
    _SEEN_PATH["$_p"]=1
    log_files+=("$_p")
    IN_SRC+=(scan)
  done < <(
    if [[ "$_SORTZ" -eq 1 ]]; then
      find -L "$_d" -maxdepth "$SCAN_MAXDEPTH" -type f -print0 2>/dev/null | sort -z
    else
      find -L "$_d" -maxdepth "$SCAN_MAXDEPTH" -type f -print0 2>/dev/null
    fi
  )
done
unset _d _p _pr _scan_count

# Header line + TSV rows from file. ASCII +/-/| borders; widths from plain text (no ANSI in cells).
render_tsv_box() {
  local hdr=$1 file=$2
  local bra='' rst=''
  [[ "$use_color" -eq 1 ]] && bra=$'\033[1m' && rst=$'\033[0m'
  {
    printf '%s\n' "$hdr"
    cat "$file"
  } | awk -v ind="  " -v bra="$bra" -v rst="$rst" '
    function hline(    t, i, j) {
      t = ind "+"
      for (i = 1; i <= nc; i++) {
        for (j = 1; j <= mw[i] + 2; j++) t = t "-"
        t = t "+"
      }
      return t "\n"
    }
    function prow(r,    t, i) {
      t = ind "|"
      for (i = 1; i <= nc; i++)
        t = t sprintf(" %-*s |", mw[i], cell[r, i])
      if (r == 1 && length(bra) > 0) return bra t rst "\n"
      return t "\n"
    }
    BEGIN { FS = "\t" }
    NR == 1 {
      nc = NF
      for (i = 1; i <= nc; i++) {
        cell[1, i] = $i
        mw[i] = length($i)
      }
      nr = 1
      next
    }
    {
      nr++
      for (i = 1; i <= nc; i++) {
        c = (i <= NF ? $i : "")
        cell[nr, i] = c
        if (length(c) > mw[i]) mw[i] = length(c)
      }
    }
    END {
      printf "%s", hline()
      printf "%s", prow(1)
      if (nr > 1) {
        printf "%s", hline()
        for (r = 2; r <= nr; r++) printf "%s", prow(r)
      }
      printf "%s", hline()
    }
  '
}

# rack_from_fname / rack_or_llama_host_from_fname / jobid_from_fname come from
# lib/rack_fname.inc.sh (sourced below), shared with rack_status.sh. This file's tables key a
# Llama log by NODE, so the per-file loop calls rack_or_llama_host_from_fname; that difference
# is now an explicit choice rather than two copies that quietly disagreed.

mnubergemm_stats_tsv() {
  # MNUBERGEMM soak output. Per-rank lines look like:
  #   MNUB [I] G: 0 <host> L: 0  T:G L=32 freq:13.4Hz duty:0.00107 P:74.4 ... GFlops:8.2173e+05
  # Emits: nodes<TAB>ranks<TAB>gflops_max<TAB>gflops_median<TAB>verdict
  # Verdict mirrors MOP §7.2: FAIL on abort/Xid/ECC, else DONE when GFlops were produced.
  local f=$1 nodes ranks gmax gmed verdict
  grep -q 'MNUB \[' "$f" 2>/dev/null || { printf '%s\t%s\t%s\t%s\t%s\n' '-' '-' '-' '-' '-'; return; }
  nodes=$(grep -m1 '^HOSTLIST:' "$f" 2>/dev/null | tr ',' '\n' | grep -c . || echo 0)
  [[ "$nodes" -eq 0 ]] && nodes='-'
  ranks=$(grep -oE 'GFlops:[0-9.eE+-]+' "$f" 2>/dev/null | wc -l | tr -d ' ')
  if [[ "${ranks:-0}" -gt 0 ]]; then
    # sort -g first so awk needs no asort() (gawk-only; the head node may ship mawk).
    read -r gmax gmed < <(grep -oE 'GFlops:[0-9.eE+-]+' "$f" 2>/dev/null | cut -d: -f2 | sort -g | \
      awk '{v[NR]=$1} END{if(NR==0){print "- -";exit} printf "%.4g %.4g\n", v[NR], v[int((NR+1)/2)]}' 2>/dev/null || echo "- -")
  fi
  : "${gmax:=-}" "${gmed:=-}"
  verdict=DONE
  if grep -qE 'Aborted \(core dumped\)|srun:.*error:.*Exited|Xid|ECC error|CUDART error' "$f" 2>/dev/null; then
    verdict=FAIL
  elif [[ "${ranks:-0}" -eq 0 ]]; then
    verdict=UNK
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' "$nodes" "${ranks:-0}" "$gmax" "$gmed" "$verdict"
}

# NCCL busbw / oob / series helpers are shared with rack_status.sh via nccl_parse.inc.sh
# (sourced below, next to validation_test_env.sh) so a parsing fix lands in both the parser and
# the dashboard at once.

# The LLM verdict (llm_label / llm_stats_tsv / slurm_hard_fail / nemo_training_finished_log /
# nemo_python_fatal_fail) is shared with rack_status.sh via lib/llm_label.inc.sh. It used to be a
# local copy here that had drifted: it checked only stats then slurm_hard_fail, so a run that
# reached its declared target without a step-timing window read UNK here while the dashboard read
# pass, and an OOM/traceback read UNK instead of FAIL. Do not re-inline it.

_PARSER_SCRIPTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Source the shared includes, but never die if a partial deploy left one behind: without these the
# script used to abort with "command not found" for every file. Degrade to abstaining stubs so the
# inventory ("results") still comes out and nothing is silently dropped.
# llm_label.inc.sh comes first: validation_test_env.sh only defines a fallback
# nemo_training_finished_log when one is not already present.
for _inc in lib/llm_label.inc.sh lib/rack_fname.inc.sh validation_test_env.sh nccl_parse.inc.sh; do
  if [[ -r "${_PARSER_SCRIPTS_DIR}/${_inc}" ]]; then
    # shellcheck source=/dev/null
    source "${_PARSER_SCRIPTS_DIR}/${_inc}" || echo "$0: failed to source ${_inc}" >&2
  else
    echo "$0: missing ${_PARSER_SCRIPTS_DIR}/${_inc} — metrics for that family will be blank" >&2
  fi
done
unset _inc
declare -F nccl_busbw_csv >/dev/null 2>&1 || nccl_busbw_csv() { echo ",,"; }
declare -F nccl_oob_csv >/dev/null 2>&1 || nccl_oob_csv() { :; }
# Abstaining stubs when llm_label.inc.sh is missing: UNK, never a guessed verdict.
declare -F llm_stats_tsv >/dev/null 2>&1 || llm_stats_tsv() { return 1; }
declare -F slurm_hard_fail >/dev/null 2>&1 || slurm_hard_fail() { return 1; }
declare -F llm_label >/dev/null 2>&1 || llm_label() { printf 'UNK\t-\t-\t-\t-\t-\n'; }
declare -F rack_from_fname >/dev/null 2>&1 || rack_from_fname() { printf '?\n'; }
declare -F jobid_from_fname >/dev/null 2>&1 || jobid_from_fname() { printf '?\n'; }
declare -F rack_or_llama_host_from_fname >/dev/null 2>&1 || rack_or_llama_host_from_fname() { rack_from_fname "$1"; }
declare -F nccl_allreduce_series_append >/dev/null 2>&1 || nccl_allreduce_series_append() { :; }
declare -F validation_eval_suite_for_file >/dev/null 2>&1 || validation_eval_suite_for_file() {
  _OUT_NCCL_OK=0
  _OUT_LLM_OK=0
}

# Per-line Nemotron steps: rack, job_id, iteration (NR = 1-based line index in extracted step lines), step_time (s, $4).
# Iterations 11..2010 (skip first 10 lines; same grep stream as stats; cap avoids huge JSON on long runs).
llm_step_series_append() {
  local f=$1 rack=$2 jid=$3
  local it_min=11 it_max=2010
  if grep -q 'train_step_timing.*consumed_samples' "$f"; then
    grep -o 'train_step_timing.*consumed_samples' "$f" 2>/dev/null | tr '|' ' ' \
      | awk -v rack="$rack" -v jid="$jid" -v min_it="$it_min" -v max_it="$it_max" -v OFS='\t' \
        '(min_it <= NR && NR <= max_it) && NF >= 4 { print rack, jid, NR, $4 }'
    return 0
  fi
  if grep -q 'Step Time.*MODEL_TFLOP/s/GPU' "$f"; then
    grep -o 'Step Time.*MODEL_TFLOP/s/GPU' "$f" 2>/dev/null | sed -e 's:s GPU: s GPU:g' -e 's:MODEL_TFLOP: MODEL_TFLOP:g' \
      | awk -v rack="$rack" -v jid="$jid" -v min_it="$it_min" -v max_it="$it_max" -v OFS='\t' \
        '(min_it <= NR && NR <= max_it) && NF >= 4 { print rack, jid, NR, $4 }'
    return 0
  fi
  return 0
}

# ===================== v4 UNIVERSAL INGEST: per-file inventory helpers =========================
# Every input file — recognised or not — produces exactly one inventory record. Records are NUL
# separated, fields are SOH (\001) separated and the captured log lines inside a field are STX
# (\002) separated. All three bytes are stripped from the captured text below, so a log can never
# forge a delimiter. This avoids jq entirely (jq is NOT assumed to exist on the head node) and
# reuses the python3 that --json already requires.
[[ "$INV_LINE_MAXCH" -ge 16 ]] || INV_LINE_MAXCH=16

# One-time capability probe: GNU grep -I ("treat a binary file as non-matching") is the cheapest
# binary sniff there is — one process, stops at the first buffer. Without it we simply treat every
# readable file as text (the awk scan strips control bytes anyway).
if printf 'x\n' | LC_ALL=C grep -qI -e '' 2>/dev/null; then _GREP_I=1; else _GREP_I=0; fi

_inv_size_mtime() { # -> "<size> <mtime_epoch>" ; "- -" when stat is unavailable
  local out=""
  out=$(stat -c '%s %Y' -- "$1" 2>/dev/null) || out=""
  [[ -n "$out" ]] || out=$(stat -f '%z %m' -- "$1" 2>/dev/null) || out=""
  [[ -n "$out" ]] || out="- -"
  printf '%s' "$out"
}

# Kind precedence, most specific first. Used for both the content and the filename pass so the
# primary "kind" is stable no matter which pass found it.
_INV_KIND_ORDER="mnubergemm nemotron nccl hpl nvbandwidth nvloom clusterkit dcgm ib_perftest nvlmapper llmb gpu_stress imex nvswitch sysinfo healthcheck bug_report slurm"

# ONE awk pass per file does everything that needs the file's bytes: content classification, line
# count, first N lines and last N lines (sanitised). Doing this with grep/head/tail/sed/tr/cut cost
# ~20 processes per file, which matters when a directory holds thousands of logs.
# Output: a single line  kinds \001 line_count \001 first_lines \001 last_lines
# with the captured lines joined by \002. De-ANSI'd, tabs->spaces, control bytes (including our own
# \001/\002 delimiters) removed, each line capped at INV_LINE_MAXCH bytes — Slurm host lists and
# progress bars routinely produce multi-kilobyte lines.
_inv_scan() {
  LC_ALL=C awk -v order="$_INV_KIND_ORDER" -v hn="$INV_HEAD_LINES" -v tn="$INV_TAIL_LINES" \
      -v maxch="$INV_LINE_MAXCH" -v doclass="$1" '
    BEGIN {
      FSEP = sprintf("%c", 1); LSEP = sprintf("%c", 2)
      ANSI = sprintf("%c", 27) "\\[[0-9;?]*[a-zA-Z]"
      CTL  = sprintf("[%c-%c%c-%c%c]", 1, 8, 11, 31, 127)
    }
    function mark(k) { seen[k] = 1 }
    function clean(s) {
      gsub(ANSI, "", s)
      gsub(/\t/, " ", s)
      gsub(CTL, "", s)
      if (length(s) > maxch) s = substr(s, 1, maxch)
      return s
    }
    doclass && /MNUB \[/                                                            { mark("mnubergemm") }
    doclass && /train_step_timing.*consumed_samples/                                { mark("nemotron") }
    doclass && /Step Time.*MODEL_TFLOP\/s\/GPU/                                     { mark("nemotron") }
    doclass && /megatron|Megatron|NeMo|nemotron|Nemotron/                           { mark("nemotron") }
    doclass && /^# Collective test starting:|Avg bus bandwidth|out-of-place/        { mark("nccl") }
    # "NCCL INFO" is deliberately NOT here. It is NCCL_DEBUG chatter, not a result: llmb runs
    # NeMo training with NCCL_DEBUG=INFO, so every NEMO log carries ~32k of it and used to be
    # marked nccl, which put an all-"---" (or worse, phantom) NCCL row in the parser tables.
    # The markers above are the ones only real nccl-tests output prints.
    doclass && /all_reduce_perf|alltoall_perf|all_gather_perf|nccl-tests/           { mark("nccl") }
    doclass && /HPLinpack|T\/V +N +NB|WR[0-9]+[LRC][0-9]+/                          { mark("hpl") }
    doclass && /nvbandwidth|device_to_host_memcpy|host_to_device_memcpy/            { mark("nvbandwidth") }
    doclass && /nvloom|NVLoom|NVLOOM/                                               { mark("nvloom") }
    doclass && /clusterkit|Clusterkit|ClusterKit|CLUSTERKIT/                        { mark("clusterkit") }
    doclass && /dcgmi|DCGM Diagnostic|Deployment Module|nvvs/                       { mark("dcgm") }
    doclass && /ib_write_bw|ib_read_bw|ib_send_bw|perftest|BW average/              { mark("ib_perftest") }
    doclass && /nvlmapper|NVLMapper|NVLink Mapper/                                  { mark("nvlmapper") }
    doclass && /llmb|LLMB|llm-benchmark/                                            { mark("llmb") }
    doclass && /gpu-burn|gpu_burn|stress-ng|Xid|ECC error/                          { mark("gpu_stress") }
    doclass && /nvidia-imex|IMEX|imex_/                                             { mark("imex") }
    doclass && /NVSwitch|nvswitch|nvidia-nvswitch/                                  { mark("nvswitch") }
    doclass && /nvidia-smi|Driver Version:|CUDA Version:|dmidecode|lscpu/           { mark("sysinfo") }
    doclass && /health[-_ ]?check|healthcheck|node_check|bringup/                   { mark("healthcheck") }
    doclass && /nvidia-bug-report|nvidia_bug_report/                                { mark("bug_report") }
    doclass && /^srun:|^sbatch:|^slurmstepd|SLURM_JOB_ID|salloc:/                   { mark("slurm") }
    {
      if (NR <= hn) hd[NR] = clean($0)
      if (tn > 0) ring[NR % tn] = clean($0)
    }
    END {
      n = split(order, pr, " ")
      kinds = ""
      for (i = 1; i <= n; i++) if (pr[i] in seen) kinds = kinds (kinds == "" ? "" : ",") pr[i]
      h = ""
      for (i = 1; i <= hn && i <= NR; i++) h = h (i > 1 ? LSEP : "") hd[i]
      t = ""
      if (tn > 0) {
        start = NR - tn + 1
        if (start < 1) start = 1
        for (i = start; i <= NR; i++) t = t (i > start ? LSEP : "") ring[i % tn]
      }
      printf "%s%s%d%s%s%s%s\n", kinds, FSEP, NR, FSEP, h, FSEP, t
    }
  ' "$2" 2>/dev/null
}

# Filename classification (always runs; the only classifier when --no-probe is used).
_inv_name_kinds() {
  local lb="${1,,}" seen="" k
  _nk() { case ",$seen," in *",$1,"*) ;; *) seen="${seen:+$seen,}$1" ;; esac; }
  case "$lb" in *mnubergemm*|*ubergemm*|*mnub*) _nk mnubergemm ;; esac
  case "$lb" in *nemotron*|*nemo*|*llama*|*megatron*) _nk nemotron ;; esac
  case "$lb" in *nccl*) _nk nccl ;; esac
  case "$lb" in *hpl*|*linpack*) _nk hpl ;; esac
  case "$lb" in *nvbandwidth*|*nvbw*) _nk nvbandwidth ;; esac
  case "$lb" in *nvloom*) _nk nvloom ;; esac
  case "$lb" in *clusterkit*|*root-ck-*) _nk clusterkit ;; esac
  case "$lb" in *dcgm*) _nk dcgm ;; esac
  case "$lb" in *ib_write*|*ib_read*|*ib_send*|*perftest*|*ib_*bw*) _nk ib_perftest ;; esac
  case "$lb" in *nvlmapper*) _nk nvlmapper ;; esac
  case "$lb" in *llmb*) _nk llmb ;; esac
  case "$lb" in *gpuburn*|*gpu-burn*|*gpu_burn*|*stress-ng*|*stress*) _nk gpu_stress ;; esac
  case "$lb" in *imex*) _nk imex ;; esac
  case "$lb" in *nvswitch*|*nvs-*) _nk nvswitch ;; esac
  case "$lb" in *sysinfo*|*sys-info*|*inventory*) _nk sysinfo ;; esac
  case "$lb" in *healthcheck*|*health-check*|*health_check*|*checker*|*bringup*) _nk healthcheck ;; esac
  case "$lb" in *bug-report*|*bug_report*) _nk bug_report ;; esac
  case "$lb" in *slurm-*|*sbatch*|*squeue*) _nk slurm ;; esac
  unset -f _nk
  # Re-order by the global precedence so name and content kinds are comparable.
  local out="" k2
  for k2 in $_INV_KIND_ORDER; do
    case ",$seen," in *",$k2,"*) out="${out:+$out,}$k2" ;; esac
  done
  printf '%s' "$out"
}

# Classify one file. Sets: _IV_SIZE _IV_MTIME _IV_BIN _IV_READ _IV_LINES _IV_HEAD _IV_TAIL
#                          _IV_KINDS _IV_KIND _IV_NOTE
inv_classify() {
  local f=$1 base=$2 sm scan_src="" scanned="" ck="" nk="" merged="" k doclass=0 windowed=0
  _IV_NOTE=""
  _IV_HEAD=""
  _IV_TAIL=""
  sm=$(_inv_size_mtime "$f")
  _IV_SIZE="${sm%% *}"
  _IV_MTIME="${sm##* }"
  _IV_READ=1
  [[ -r "$f" ]] || { _IV_READ=0; _IV_NOTE="unreadable"; }
  _IV_BIN=0
  _IV_LINES="-"

  if [[ "$_IV_READ" -eq 1 && "$_IV_SIZE" =~ ^[0-9]+$ && "$_IV_SIZE" -gt 0 ]]; then
    if [[ "$_GREP_I" -eq 1 ]] && ! LC_ALL=C grep -qI -e '' -- "$f" 2>/dev/null; then
      _IV_BIN=1
      _IV_NOTE="${_IV_NOTE:+$_IV_NOTE; }binary"
    fi
  fi

  if [[ "$_IV_READ" -eq 1 && "$_IV_BIN" -eq 0 && "$_IV_SIZE" =~ ^[0-9]+$ && "$_IV_SIZE" -gt 0 ]]; then
    [[ "$PROBE_CONTENT" != "0" ]] && doclass=1
    # nvis_logscan already read every byte, so take its kinds and line count instead of sampling.
    # The window below then only supplies head/tail PREVIEW lines, which a window can answer
    # honestly. Classifying from one could not: it read a >16 MB file's first and last 2 MB, so a
    # 973 MB log whose only NeMo markers sat at 98% depth was classified "nccl" and lost its
    # Nemotron row entirely.
    local _scan_kinds="" _scan_lines=""
    if [[ -n "${SCAN_ROW[$f]:-}" ]]; then
      _scan_kinds=$(printf '%s' "${SCAN_ROW[$f]}" | cut -f19)
      _scan_lines=$(printf '%s' "${SCAN_ROW[$f]}" | cut -f20)
      doclass=0
    fi
    if [[ "$_IV_SIZE" -le "$PROBE_FULL_MAX" ]]; then
      scan_src="$f"
    else
      # Big file: read a head+tail window for the preview lines instead of gigabytes.
      { head -c "$PROBE_WIN" -- "$f" 2>/dev/null; printf '\n'; tail -c "$PROBE_WIN" -- "$f" 2>/dev/null; } \
        >"$tmp_probe" 2>/dev/null || : >"$tmp_probe"
      scan_src="$tmp_probe"
      windowed=1
      _IV_NOTE="${_IV_NOTE:+$_IV_NOTE; }preview from ${PROBE_WIN}B head+tail window (file > ${PROBE_FULL_MAX}B)"
    fi
    scanned=$(_inv_scan "$doclass" "$scan_src")
    if [[ -n "$scanned" ]]; then
      IFS=$'\001' read -r ck _IV_LINES _IV_HEAD _IV_TAIL <<<"$scanned"
      [[ -n "${_IV_LINES:-}" ]] || _IV_LINES="-"
      if [[ "$windowed" -eq 1 || "$_IV_SIZE" -gt "$LINECOUNT_MAX" ]]; then
        _IV_LINES="-"
      fi
    fi
    # Whole-file answers win over whatever the preview pass happened to see.
    [[ -n "$_scan_kinds" && "$_scan_kinds" != "-" ]] && ck="$_scan_kinds"
    [[ "$_scan_lines" =~ ^[0-9]+$ ]] && _IV_LINES="$_scan_lines"
  fi
  nk=$(_inv_name_kinds "$base")

  for k in $_INV_KIND_ORDER; do
    case ",$ck,$nk," in *",$k,"*) merged="${merged:+$merged,}$k" ;; esac
  done
  _IV_KINDS="$merged"
  # Content evidence wins over a filename guess; otherwise fall back to the filename.
  if [[ -n "$ck" ]]; then
    _IV_KIND="${ck%%,*}"
  elif [[ -n "$nk" ]]; then
    _IV_KIND="${nk%%,*}"
  else
    _IV_KIND="unknown"
  fi
  if [[ "$_IV_SIZE" == "0" ]]; then
    _IV_KIND="unknown"
    _IV_LINES=0
    _IV_NOTE="${_IV_NOTE:+$_IV_NOTE; }empty file"
  fi
}

# Write one NUL-terminated inventory record. Field order is contractual with the python block.
inv_emit() {
  local f=$1 base=$2 fdir=$3 src=$4 rack=$5 jid=$6 tables=$7
  printf '%s\001' \
    "$f" "$base" "$fdir" "$_IV_SIZE" "$_IV_MTIME" "" \
    "$_IV_KIND" "$_IV_KINDS" "$rack" "$jid" "$src" \
    "$_IV_BIN" "$_IV_READ" "$_IV_LINES" "$tables" "$_IV_NOTE" \
    "$_IV_HEAD" "$_IV_TAIL"
  printf '\000'
}

running_job_ids() {
  # Explicit override, honoured even with --no-squeue: lets a caller that already knows which
  # jobs are queued skip the squeue round trip, and lets tests drive "ongoing" deterministically.
  # Whether a job is still running is what separates a sweep that is genuinely mid-write from one
  # that merely lost a marker, so it has to be settable without a live scheduler.
  if [[ -n "${NVIS_PARSER_RUNNING_JOBS:-}" ]]; then
    printf '%s\n' "${NVIS_PARSER_RUNNING_JOBS//,/ }" | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u || true
    return 0
  fi
  if [[ "$USE_SQUEUE" -eq 0 ]]; then
    return 0
  fi
  # A non-login ssh shell may have neither USER nor LOGNAME exported; `set -u` turned that into a
  # hard "unbound variable" abort before any log was parsed.
  local _squser="${SQUEUE_USER:-${USER:-${LOGNAME:-}}}"
  [[ -n "$_squser" ]] || _squser=$(id -un 2>/dev/null) || _squser=""
  [[ -n "$_squser" ]] || return 0
  squeue -u "$_squser" -h -o '%i' 2>/dev/null | sort -u || true
}

mapfile -t RUNNING < <(running_job_ids)
declare -A RUNNING_SET
for j in "${RUNNING[@]:-}"; do
  [[ -n "$j" ]] || continue
  RUNNING_SET[$j]=1
done

tmp_nccl=$(mktemp)
tmp_nccl_ar=$(mktemp)
tmp_llm=$(mktemp)
tmp_llm_steps=$(mktemp)
tmp_status=$(mktemp)
tmp_mnub=$(mktemp)
tmp_inv=$(mktemp)
tmp_probe=$(mktemp)
tmp_scan=$(mktemp)
tmp_series=$(mktemp -d)
trap 'rm -f "$tmp_nccl" "$tmp_nccl_ar" "$tmp_llm" "$tmp_llm_steps" "$tmp_status" "$tmp_mnub" "$tmp_inv" "$tmp_probe" "$tmp_scan"; rm -rf "$tmp_series"' EXIT

# ============================ ONE PASS PER FILE (nvis_logscan.py) =============================
# The shell extractors below each opened the file again: nccl_busbw_csv, nccl_busbw_ext_csv,
# nccl_oob_csv, nccl_sweep_counts, two series appenders, llm_stats_tsv (two greps),
# nemo_training_finished_log, slurm_hard_fail, nemo_python_fatal_fail and the inventory scan —
# about sixteen reads and twenty processes PER FILE. On a 973 MB log that measured 21 s; one
# python pass does the same work in 15 s with a flat 13 MB RSS, and one process for the whole
# batch instead of twenty per file.
#
# Worse than the cost, the old inventory path SAMPLED: files over PROBE_FULL_MAX were classified
# from a head+tail window, so a marker in the middle of a big log was invisible — and the legacy
# tables are gated on that classification.
#
# NVIS_PARSER_ENGINE=shell falls back to the per-function extractors. Keep it working: this runs
# on live customer clusters and a bad scan must be one env var away from the old behaviour.
PARSER_ENGINE="${NVIS_PARSER_ENGINE:-scan}"
_SCANNER="${SCRIPT_DIR:-$(dirname -- "${BASH_SOURCE[0]}")}/nvis_logscan.py"
declare -A SCAN_ROW
if [[ "$PARSER_ENGINE" == "scan" ]]; then
  if [[ -r "$_SCANNER" ]] && command -v python3 >/dev/null 2>&1 && ((${#log_files[@]})); then
    if python3 "$_SCANNER" --tsv --series-dir "$tmp_series" "${log_files[@]}" >"$tmp_scan" 2>/dev/null; then
      while IFS=$'\t' read -r _p _rest; do
        [[ -n "$_p" ]] && SCAN_ROW["$_p"]="$_rest"
      done <"$tmp_scan"
    else
      echo "${C_WARN}nvis_logscan failed; falling back to shell extractors${C_RESET}" >&2
      PARSER_ENGINE="shell"
    fi
  else
    PARSER_ENGINE="shell"
  fi
fi

# Series files are named by a hash of the path so bash never has to quote a log name.
_scan_series_key() { printf '%s' "$1" | sha1sum | cut -c1-16; }

for _i in "${!log_files[@]}"; do
  f="${log_files[$_i]}"
  src="${IN_SRC[$_i]:-explicit}"
  [[ -f "$f" ]] || { echo "${C_WARN}skip (not a file):${C_RESET} $f" >&2; continue; }
  rack=$(rack_or_llama_host_from_fname "$f")
  jid=$(jobid_from_fname "$f")
  base=$(basename -- "$f")
  fdir=$(dirname -- "$f")

  # v4: classify EVERY file (cheap: one stat, one grep -qI sniff, one bounded awk pass). Also
  # needed with --no-inventory when the file came from a directory scan, because the legacy-table
  # gate below is driven by the detected kinds.
  _IV_SIZE="-" _IV_MTIME="-" _IV_BIN=0 _IV_READ=1 _IV_LINES="-"
  _IV_KINDS="" _IV_KIND="unknown" _IV_NOTE="" _IV_HEAD="" _IV_TAIL=""
  if [[ "$WANT_INVENTORY" -eq 1 || "$src" == "scan" || "$ABSTAIN" == "1" ]]; then
    inv_classify "$f" "$base" || true
  fi

  # Which files feed the LEGACY tables. Explicitly-named file arguments behave exactly as in v3
  # (every pre-v4 caller passes explicit paths, so their JSON is unchanged). Files discovered by
  # directory enumeration only join a legacy table when they actually look like that tool's log —
  # otherwise a directory scan would fill the NCCL/Nemotron charts with "---" / red FAIL rows.
  legacy=0
  if [[ "$INVENTORY_ONLY" -eq 0 ]]; then
    if [[ "$src" == "explicit" || "$LEGACY_ALL" == "1" ]]; then
      legacy=1
    else
      case ",$_IV_KINDS," in
        *,nccl,* | *,nemotron,* | *,mnubergemm,*) legacy=1 ;;
      esac
    fi
  fi

  inv_tables=""
  if [[ "$legacy" -eq 1 ]]; then
    # --abstain (opt-in, default off): suppress the all-"---" NCCL row and the UNK/FAIL Nemotron
    # row for a log that is demonstrably neither. Default OFF so the JSON stays byte-for-byte v7.
    want_nccl_row=1
    want_llm_row=1
    if [[ "$ABSTAIN" == "1" ]]; then
      case ",$_IV_KINDS," in *,nccl,*) ;; *) want_nccl_row=0 ;; esac
      case ",$_IV_KINDS," in *,nemotron,*) ;; *) want_llm_row=0 ;; esac
    fi
    _scan="${SCAN_ROW[$f]:-}"
    if [[ -n "$_scan" ]]; then
      # Field order is nvis_logscan.TSV_FIELDS minus the leading path.
      IFS=$'\t' read -r ar_bw ag_bw aa_bw _rs _sr oob _s_started _s_concl \
        _llm_status _llm_step _llm_tf _llm_sstd _llm_pstd _llm_mfu \
        _steps_cur _steps_tot _slurm_fail _py_fatal _scan_kinds _scan_lines <<<"$_scan"
      for _v in ar_bw ag_bw aa_bw oob; do
        [[ "${!_v}" == "-" ]] && printf -v "$_v" '%s' ""
      done
      llm_line=$(printf '%s\t%s\t%s\t%s\t%s\t%s' \
        "$_llm_status" "$_llm_step" "$_llm_tf" "$_llm_sstd" "$_llm_pstd" "$_llm_mfu")
    else
      bw=$(nccl_busbw_csv "$f")
      IFS=',' read -r ar_bw aa_bw ag_bw <<<"$bw"
      oob=$(nccl_oob_csv "$f")
      llm_line=$(llm_label "$f")
      _steps_cur="-" _steps_tot="-"
    fi
    IFS=',' read -r oob_ar oob_aa oob_ag _oobx <<<"${oob},,,"
    oob_fmt="${oob_ar:----},${oob_ag:----},${oob_aa:----}"

    ongoing=0
    [[ -n "${RUNNING_SET[$jid]:-}" ]] && ongoing=1
    # Completion from the same pass that produced the numbers: reaching the target the run
    # DECLARED is what finished means, so 50-step and 2010-step runs both count.
    _finished=""
    if [[ -n "$_scan" && "$_steps_tot" =~ ^[0-9]+$ && "$_steps_cur" =~ ^[0-9]+$ ]]; then
      _finished=0
      ((_steps_tot > 0 && _steps_cur >= _steps_tot && _steps_tot >= ${NEMO_TARGET_STEPS:-0})) && _finished=1
    fi
    validation_eval_suite_for_file "$f" "$ongoing" "$ar_bw" "$aa_bw" "$ag_bw" "$llm_line" "$_finished"
    nccl_ok=$_OUT_NCCL_OK
    llm_ok=$_OUT_LLM_OK

    printf '%s\t%s\t%d\t%d\t%d\n' "$rack" "$jid" "$ongoing" "$nccl_ok" "$llm_ok" >>"$tmp_status"

    # Table column order: all_reduce, all_gather, alltoall (log order is reduce → alltoall → gather).
    inv_tables="parser_flags"
    _skey=""
    [[ -n "$_scan" ]] && _skey=$(_scan_series_key "$f")
    if [[ "$want_nccl_row" -eq 1 ]]; then
      # Sweep-block counts travel with the row so a consumer can tell a partial read of a running
      # job from a finished one; without it a 1-point chart is indistinguishable from a slow rack.
      if [[ -n "$_scan" ]]; then
        _sw_started="$_s_started" _sw_concl="$_s_concl"
      else
        _sweep_counts=$(nccl_sweep_counts "$f")
        _sw_started="${_sweep_counts%%,*}" _sw_concl="${_sweep_counts##*,}"
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rack" "$jid" "${ar_bw:----}" "${ag_bw:----}" "${aa_bw:----}" "$oob_fmt" \
        "$_sw_started" "$_sw_concl" >>"$tmp_nccl"
      if [[ -n "$_skey" && -s "$tmp_series/$_skey.ar" ]]; then
        awk -v r="$rack" -v j="$jid" -v OFS='\t' '{ print r, j, $1, $2 }' \
          "$tmp_series/$_skey.ar" >>"$tmp_nccl_ar" || true
      elif [[ -z "$_scan" ]]; then
        nccl_allreduce_series_append "$f" "$rack" "$jid" >>"$tmp_nccl_ar" || true
      fi
      inv_tables="nccl_peak_out_of_place_busbw_gbps,$inv_tables"
    fi
    if [[ "$want_llm_row" -eq 1 ]]; then
      # Carry the run's own step budget so a 50-step perf run and a 2010-step soak are told
      # apart in the report instead of one of them being written off as unfinished.
      if [[ -z "$_scan" ]]; then
        _llm_prog=$(nemo_iteration_progress_log "$f" 2>/dev/null || printf '%s' "- -")
        _steps_cur="${_llm_prog%% *}" _steps_tot="${_llm_prog##* }"
      fi
      printf '%s\t%s\t%s\t%s\t%s\n' "$rack" "$jid" "$llm_line" "$_steps_cur" "$_steps_tot" >>"$tmp_llm"
      if [[ -n "$_skey" && -s "$tmp_series/$_skey.steps" ]]; then
        awk -v r="$rack" -v j="$jid" -v OFS='\t' '{ print r, j, $1, $2 }' \
          "$tmp_series/$_skey.steps" >>"$tmp_llm_steps" || true
      elif [[ -z "$_scan" ]]; then
        llm_step_series_append "$f" "$rack" "$jid" >>"$tmp_llm_steps" || true
      fi
      inv_tables="$inv_tables,nemotron_5h_56b_train_window"
    fi
    mnub_line=$(mnubergemm_stats_tsv "$f")
    printf '%s\t%s\t%s\n' "$rack" "$jid" "$mnub_line" >>"$tmp_mnub"

    case "$mnub_line" in
      "" | *$'\t'-) ;;
      *) inv_tables="$inv_tables,mnubergemm_stress" ;;
    esac
  fi

  if [[ "$WANT_INVENTORY" -eq 1 ]]; then
    inv_emit "$f" "$base" "$fdir" "$src" "$rack" "$jid" "$inv_tables" >>"$tmp_inv" || true
  fi
done
unset _i

sort -t$'\t' -k1,1V "$tmp_nccl" -o "$tmp_nccl"
sort -t$'\t' -k1,1V -k2,2V -k3,3n "$tmp_nccl_ar" -o "$tmp_nccl_ar"
sort -t$'\t' -k1,1V "$tmp_llm" -o "$tmp_llm"
sort -t$'\t' -k1,1V -k2,2V "$tmp_llm_steps" -o "$tmp_llm_steps"
sort -t$'\t' -k1,1V "$tmp_status" -o "$tmp_status"

if [[ "$OUTPUT_FORMAT" != "human" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "$0: --${OUTPUT_FORMAT} requires python3 in PATH (PATH=$PATH)" >&2
    if [[ "$OUTPUT_FORMAT" == "json" ]]; then
      # Still emit a syntactically valid, schema-shaped document so the caller's json.loads() does
      # not blow up on an empty body; the non-zero exit + stderr carry the real failure.
      printf '%s\n' '{'
      printf '%s\n' '  "export_version": 7,'
      printf '%s\n' '  "nccl_peak_out_of_place_busbw_gbps": [],'
      printf '%s\n' '  "nemotron_5h_56b_train_window": [],'
      printf '%s\n' '  "mnubergemm_stress": [],'
      printf '%s\n' '  "parser_flags": [],'
      printf '%s\n' '  "results": [],'
      printf '%s\n' '  "inventory": {"enabled": false, "file_count": 0, "error": "python3 not found"},'
      printf '%s\n' '  "inventory_version": 1,'
      printf '%s\n' '  "error": "python3 not found in PATH"'
      printf '%s\n' '}'
    fi
    exit 1
  fi
  _inv_dirs_joined=""
  for _d in "${SCAN_DIRS[@]:-}"; do
    [[ -n "$_d" ]] || continue
    _inv_dirs_joined="${_inv_dirs_joined:+${_inv_dirs_joined}$'\001'}$_d"
  done
  unset _d
  NVIS_INV_DIRS="$_inv_dirs_joined" \
  NVIS_INV_TRUNCATED="$SCAN_TRUNCATED" \
  NVIS_INV_MAXDEPTH="$SCAN_MAXDEPTH" \
  NVIS_INV_ENABLED="$WANT_INVENTORY" \
  NVIS_INV_MAXFILES="$SCAN_MAX_FILES" \
  NVIS_INV_ONLY="$INVENTORY_ONLY" \
  python3 - "$tmp_nccl" "$tmp_llm" "$tmp_status" "$tmp_llm_steps" "$tmp_nccl_ar" "$OUTPUT_FORMAT" "$tmp_mnub" "$tmp_inv" <<'PY'
import csv, json, os, sys
from typing import Any, Dict, List, Optional, Tuple

# ---- v4 inventory ("results") ---------------------------------------------------------------
# Record separator NUL, field separator SOH (\001), captured-line separator STX (\002). The shell
# strips all three from any text it captures, so a log line can never forge a delimiter.
INV_FIELDS = [
    "path", "basename", "dir", "size_bytes", "mtime_epoch", "mtime_iso",
    "kind", "kinds", "rack", "job_id", "source",
    "is_binary", "readable", "line_count", "tables", "note",
    "first_lines", "last_lines",
]

def _as_int(v: str) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None

def _iso_utc(epoch: Optional[int]) -> str:
    if epoch is None:
        return ""
    try:
        import time
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
    except (ValueError, OverflowError, OSError):
        return ""

def read_inventory(path: str) -> List[Dict[str, Any]]:
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for rec in blob.split(b"\x00"):
        if not rec:
            continue
        parts = rec.decode("utf-8", "replace").split("\x01")
        row: Dict[str, Any] = {}
        for i, name in enumerate(INV_FIELDS):
            row[name] = parts[i] if i < len(parts) else ""
        _mt = _as_int(row["mtime_epoch"])
        entry: Dict[str, Any] = {
            "path": row["path"],
            "basename": row["basename"],
            "dir": row["dir"],
            "size_bytes": _as_int(row["size_bytes"]),
            "mtime_epoch": _mt,
            "mtime_iso": row["mtime_iso"] or _iso_utc(_mt),
            "kind": row["kind"] or "unknown",
            "kinds": [k for k in row["kinds"].split(",") if k],
            "rack": row["rack"],
            "job_id": row["job_id"],
            "source": row["source"] or "explicit",
            "is_binary": row["is_binary"] == "1",
            "readable": row["readable"] != "0",
            "line_count": _as_int(row["line_count"]),
            "tables": [t for t in row["tables"].split(",") if t],
            "note": row["note"],
            "first_lines": [ln for ln in row["first_lines"].split("\x02")] if row["first_lines"] else [],
            "last_lines": [ln for ln in row["last_lines"].split("\x02")] if row["last_lines"] else [],
        }
        entry["parsed"] = bool(entry["tables"])
        out.append(entry)
    out.sort(key=lambda d: (str(d.get("dir", "")), str(d.get("basename", ""))))
    return out

def read_tsv(path: str) -> List[List[str]]:
    rows: List[List[str]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            rows.append(line.rstrip("\n").split("\t"))
    return rows

def row_to_dict(row: List[str], fields: List[str], ints: Tuple[str, ...] = ()) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for i, name in enumerate(fields):
        v = row[i] if i < len(row) else ""
        if name in ints:
            try:
                v = int(v)
            except ValueError:
                pass
        out[name] = v
    return out

def main() -> None:
    p_nccl, p_llm, p_st, p_llm_steps, p_nccl_ar, mode = (
        sys.argv[1],
        sys.argv[2],
        sys.argv[3],
        sys.argv[4],
        sys.argv[5],
        sys.argv[6],
    )
    # Optional 7th arg: MNUBERGEMM stats TSV. Optional so an older caller still works.
    p_mnub = sys.argv[7] if len(sys.argv) > 7 else ""
    # Optional 8th arg (v4): per-file inventory records. Absent -> "results": [].
    p_inv = sys.argv[8] if len(sys.argv) > 8 else ""
    nccl_rows = read_tsv(p_nccl)
    llm_rows = read_tsv(p_llm)
    st_rows = read_tsv(p_st)
    step_rows = read_tsv(p_llm_steps)
    nccl_ar_rows = read_tsv(p_nccl_ar)

    nccl_fields = [
        "rack",
        "job_id",
        "all_reduce_gbps",
        "all_gather_gbps",
        "alltoall_gbps",
        "oob_ar_ag_aa",
        # Sweep-block counts from nccl_sweep_counts(). started > concluded means the log was read
        # while the job was still running, so the series below is partial.
        "sweep_blocks_started",
        "sweep_blocks_concluded",
    ]
    llm_fields = [
        "rack",
        "job_id",
        "status",
        "step_s",
        "tflops_per_gpu",
        "step_time_std",
        "perf_std",
        "mfu_percent",
        # The run's own step budget, from the last "iteration <cur>/<tot>" marker. A run is
        # finished when it reaches the target IT declared, so the report has to show which
        # target that was: 50/50 and 2010/2010 are both complete runs, at different lengths.
        "steps_completed",
        "steps_target",
    ]
    st_fields = ["rack", "job_id", "ongoing", "nccl_ok", "llm_ok"]

    if mode == "json":
        step_by: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in step_rows:
            if len(r) < 4:
                continue
            rk, j, it_s, st = r[0], r[1], r[2], r[3]
            try:
                it_i = int(it_s)
                st_f = float(st)
            except ValueError:
                continue
            step_by.setdefault((str(rk), str(j)), []).append({"iteration": it_i, "step_time": st_f})
        for k in step_by:
            step_by[k].sort(key=lambda x: int(x["iteration"]))

        ar_series: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for r in nccl_ar_rows:
            if len(r) < 4:
                continue
            rk, j, sz_s, bw_s = r[0], r[1], r[2], r[3]
            try:
                sz_i = int(sz_s)
                bw_f = float(bw_s)
            except ValueError:
                continue
            ar_series.setdefault((str(rk), str(j)), []).append(
                {"size_bytes": sz_i, "bus_bw_gbps": bw_f}
            )
        for k in ar_series:
            ar_series[k].sort(key=lambda x: int(x["size_bytes"]))

        # ongoing comes from squeue (RUNNING_SET), keyed the same way as the tables.
        ongoing_by: Dict[Tuple[str, str], bool] = {}
        for r in st_rows:
            if len(r) < 3:
                continue
            ongoing_by[(str(r[0]), str(r[1]))] = str(r[2]) == "1"

        nccl_dicts = [row_to_dict(r, nccl_fields) for r in nccl_rows]
        for d in nccl_dicts:
            key = (str(d.get("rack", "")), str(d.get("job_id", "")))
            d["all_reduce_busbw_series"] = ar_series.get(key, [])
            # Derived so the UI does not have to know the convention. A log with no markers at all
            # (0 started) is treated as complete: not every producer brackets its sweeps, and
            # calling those "partial" forever would cry wolf on every non-nccl-tests log.
            started = _as_int(d.get("sweep_blocks_started")) or 0
            concluded = _as_int(d.get("sweep_blocks_concluded")) or 0
            d["sweep_blocks_started"] = started
            d["sweep_blocks_concluded"] = concluded
            # A missing conclusion marker means "still running" ONLY while the job is actually
            # queued. Once it has left the queue the sweep is as finished as it will ever be, and
            # a lost marker is a gap in the log, not a running job: at 576 nodes / 2304 ranks the
            # collectives share one stdout and lines arrive interleaved and spliced (job 140457
            # concluded 2 of 3 blocks yet every step COMPLETED in Slurm). Reporting that as
            # "still running" hid a finished 32-rack run behind a permanent progress banner.
            missing = started - concluded if started > concluded else 0
            d["sweep_blocks_missing"] = missing
            d["sweep_complete"] = started == 0 or missing == 0 or not ongoing_by.get(key, False)
            d["sweep_running"] = missing > 0 and ongoing_by.get(key, False)

        llm_dicts = [row_to_dict(r, llm_fields) for r in llm_rows]
        for d in llm_dicts:
            key = (str(d.get("rack", "")), str(d.get("job_id", "")))
            d["step_series"] = step_by.get(key, [])

        # MNUBERGEMM (MOP §7.2 GPU/power stress). Only rows where the tool actually ran are
        # emitted, so a NEMO/NCCL-only log yields [] and the UI can hide the tab.
        mnub_dicts: List[Dict[str, Any]] = []
        if p_mnub:
            mnub_fields = ["rack", "job_id", "nodes", "ranks", "gflops_max", "gflops_median", "verdict"]
            for r in read_tsv(p_mnub):
                if not r or len(r) < 7:
                    continue
                d = row_to_dict(r, mnub_fields)
                if str(d.get("verdict", "-")) in ("-", ""):
                    continue
                d["gpus"] = "-"
                try:
                    d["gpus"] = int(d["nodes"]) * 4
                except (TypeError, ValueError):
                    pass
                mnub_dicts.append(d)

        # v4 universal ingest. "results" is ADDITIVE: one entry per input file, including files no
        # extractor understood (kind="unknown"), so the UI can list/open everything in the log dir.
        # The four keys above are byte-for-byte what v7 produced for the same explicit file args.
        results = read_inventory(p_inv)
        kind_counts: Dict[str, int] = {}
        for e in results:
            k = str(e.get("kind") or "unknown")
            kind_counts[k] = kind_counts.get(k, 0) + 1
        dirs_env = os.environ.get("NVIS_INV_DIRS", "")
        inv_meta = {
            "enabled": os.environ.get("NVIS_INV_ENABLED", "1") != "0",
            "inventory_only": os.environ.get("NVIS_INV_ONLY", "0") == "1",
            "scanned_dirs": [d for d in dirs_env.split("\x01") if d],
            "maxdepth": _as_int(os.environ.get("NVIS_INV_MAXDEPTH", "")),
            "max_files": _as_int(os.environ.get("NVIS_INV_MAXFILES", "")),
            "truncated": os.environ.get("NVIS_INV_TRUNCATED", "0") == "1",
            "file_count": len(results),
            "unparsed_count": sum(1 for e in results if not e.get("parsed")),
            "kind_counts": kind_counts,
        }

        obj = {
            "export_version": 7,
            "nccl_peak_out_of_place_busbw_gbps": nccl_dicts,
            "nemotron_5h_56b_train_window": llm_dicts,
            "mnubergemm_stress": mnub_dicts,
            "parser_flags": [row_to_dict(r, st_fields, ("ongoing", "nccl_ok", "llm_ok")) for r in st_rows],
            "results": results,
            "inventory": inv_meta,
            "inventory_version": 1,
        }
        json.dump(obj, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    if mode == "csv":
        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        order: List[Tuple[str, str]] = []

        def touch(k: Tuple[str, str]) -> Dict[str, Any]:
            if k not in merged:
                merged[k] = {"rack": k[0], "job_id": k[1]}
                order.append(k)
            return merged[k]

        for r in nccl_rows:
            if len(r) < 2:
                continue
            d = touch((r[0], r[1]))
            d.update(
                {
                    "all_reduce_gbps": r[2] if len(r) > 2 else "",
                    "all_gather_gbps": r[3] if len(r) > 3 else "",
                    "alltoall_gbps": r[4] if len(r) > 4 else "",
                    "oob_ar_ag_aa": r[5] if len(r) > 5 else "",
                }
            )
        for r in llm_rows:
            if len(r) < 2:
                continue
            d = touch((r[0], r[1]))
            d.update(
                {
                    "status": r[2] if len(r) > 2 else "",
                    "step_s": r[3] if len(r) > 3 else "",
                    "tflops_per_gpu": r[4] if len(r) > 4 else "",
                    "step_time_std": r[5] if len(r) > 5 else "",
                    "perf_std": r[6] if len(r) > 6 else "",
                    "mfu_percent": r[7] if len(r) > 7 else "",
                }
            )
        for r in st_rows:
            if len(r) < 2:
                continue
            d = touch((r[0], r[1]))
            for name, idx in (("ongoing", 2), ("nccl_ok", 3), ("llm_ok", 4)):
                if idx < len(r):
                    try:
                        d[name] = int(r[idx])
                    except ValueError:
                        d[name] = r[idx]
                else:
                    d.setdefault(name, "")

        fieldnames = [
            "rack",
            "job_id",
            "all_reduce_gbps",
            "all_gather_gbps",
            "alltoall_gbps",
            "oob_ar_ag_aa",
            "status",
            "step_s",
            "tflops_per_gpu",
            "step_time_std",
            "perf_std",
            "mfu_percent",
            "ongoing",
            "nccl_ok",
            "llm_ok",
        ]
        w = csv.DictWriter(sys.stdout, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for k in order:
            row = {fn: merged[k].get(fn, "") for fn in fieldnames}
            w.writerow(row)
        return

    sys.stderr.write("parser export: unknown mode\n")
    sys.exit(1)

if __name__ == "__main__":
    main()
PY
  exit $?
fi

echo "${C_SEP}==========${C_RESET} ${C_H1}NCCL (peak out-of-place busbw, GB/s)${C_RESET} ${C_SEP}==========${C_RESET}"
_hdr_nccl=$'rack\tjob_id\tall_reduce\tall_gather\talltoall\tOOB(AR,AG,AA)'
# Color only rows that contain "|" (not +/- rule lines). ANSI SGR is zero-width on terminals.
render_tsv_box "$_hdr_nccl" "$tmp_nccl" | sed -E \
  -e '/[|]/s/OK,OK,OK/'"${C_OK}"'OK,OK,OK'"${C_RESET}"'/g' \
  -e '/[|]/s/---,---,---/'"${C_DIM}${C_WARN}"'---,---,---'"${C_RESET}"'/g' \
  -e '/[|]/s/[|] ---([[:space:]]*)/| '"${C_DIM}${C_WARN}"'---'"${C_RESET}"'\1/g'

echo
echo "${C_SEP}==========${C_RESET} ${C_H1}Nemotron 5H 56B (LLM train window)${C_RESET} ${C_SEP}==========${C_RESET}"
_hdr_llm=$'rack\tjob_id\tstatus\tstep_s\tTFLOPs/GPU\tSstd\tPstd\tMFU%\tsteps\ttarget'
render_tsv_box "$_hdr_llm" "$tmp_llm" | sed -E \
  -e '/[|]/s/[|] OK([[:space:]]+)[|]/| '"${C_OK}"'OK'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] FAIL([[:space:]]+)[|]/| '"${C_FAIL}"'FAIL'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] UNK([[:space:]]+)[|]/| '"${C_UNK}"'UNK'"${C_RESET}"'\1|/g'

# v4: everything the parser saw, including files no extractor understood. Only shown when a
# directory was enumerated or at least one file produced no metrics row, so single-log human runs
# look exactly like they did in v3.
if [[ "$WANT_INVENTORY" -eq 1 && -s "$tmp_inv" ]]; then
  _tmp_inv_tbl=$(mktemp)
  LC_ALL=C tr '\000' '\n' <"$tmp_inv" \
    | awk -F'\001' '
        # UTC ISO-8601 from a Unix epoch without strftime() (mawk on the head node may not have it)
        # and without one date(1) fork per file. Civil-from-days, Howard Hinnant algorithm.
        function iso_utc(t,   days, secs, hh, mm, ss, z, era, doe, yoe, y, doy, mp, d, m) {
          if (t !~ /^[0-9]+$/) return "-"
          days = int(t / 86400); secs = t - days * 86400
          hh = int(secs / 3600); mm = int((secs % 3600) / 60); ss = secs % 60
          z = days + 719468
          era = int(z / 146097)
          doe = z - era * 146097
          yoe = int((doe - int(doe / 1460) + int(doe / 36524) - int(doe / 146096)) / 365)
          y = yoe + era * 400
          doy = doe - (365 * yoe + int(yoe / 4) - int(yoe / 100))
          mp = int((5 * doy + 2) / 153)
          d = doy - int((153 * mp + 2) / 5) + 1
          m = mp + (mp < 10 ? 3 : -9)
          if (m <= 2) y++
          return sprintf("%04d-%02d-%02dT%02d:%02d:%02dZ", y, m, d, hh, mm, ss)
        }
        NF > 1 {
          sz = ($4 == "" ? "-" : $4)
          when = iso_utc($5)
          parsed = ($15 == "" ? "no" : "yes")
          # Slurm host-list filenames run to hundreds of bytes; keep the box readable. The JSON
          # "results" array always carries the untruncated basename and full path.
          nm = $2
          if (length(nm) > 64) nm = substr(nm, 1, 30) "..." substr(nm, length(nm) - 30)
          printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", nm, $7, sz, when, ($9 == "" ? "-" : $9), ($10 == "" ? "-" : $10), parsed
        }' >"$_tmp_inv_tbl"
  _unparsed=$(awk -F'\t' '$7 == "no" { n++ } END { print n + 0 }' "$_tmp_inv_tbl")
  if [[ -s "$_tmp_inv_tbl" ]] && { [[ ${#SCAN_DIRS[@]} -gt 0 ]] || [[ "${_unparsed:-0}" -gt 0 ]]; }; then
    echo
    echo "${C_SEP}==========${C_RESET} ${C_H1}Log inventory (every file seen)${C_RESET} ${C_SEP}==========${C_RESET}"
    render_tsv_box $'file\tkind\tbytes\tmtime(UTC)\track\tjob_id\tparsed' "$_tmp_inv_tbl"
    if [[ "$SCAN_TRUNCATED" -eq 1 ]]; then
      echo "  ${C_WARN}note:${C_RESET} enumeration stopped at --max-files=${SCAN_MAX_FILES}; some files are not listed." >&2
    fi
  fi
  rm -f "$_tmp_inv_tbl"
fi

echo
_rs_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
USE_SQUEUE=$USE_SQUEUE USE_CMSH=$USE_CMSH USE_SINFO=$USE_SINFO \
  bash "$_rs_dir/rack_status.sh" --tmp-status="$tmp_status"
