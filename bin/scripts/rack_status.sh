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
# Rack status table: NEMO log-derived done/fail/ongoing + cmsh rack/ds + sinfo node states.
# RACK_CMSH_RACK_LIST_FILE: readable file of one pNN-rMM rack id per line (skips ``cmsh -c "rack; list"``).
# When unset, defaults to ../../var/cache/cmsh_rack_list.txt under this script’s nvis-rack-status root if that file exists.
# Used by parser.sh (--tmp-status) or standalone on log files.
# Training "done" for LLM: Megatron reached the target it declared (last iteration cur>=tot), OR job still running (squeue) with perf stats.
# Which checks count toward "done" (nccl_ok ∧ llm_ok): VALIDATION_TEST (comma-separated); default all NCCL busbw lines + llm — see validation_test_env.sh.
# Perf-window OK alone does not mean the run finished; mid-crash logs can still have 90+ Step Time lines.
# FAIL: Slurm hard errors, or Python traceback / OOM lines in the log.

set -uo pipefail

# Slurm CLI on PATH (non-login SSH shells don't init Modules — see slurm-init.inc.sh).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"

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
  C_H1=$'\033[1;36m'
  C_OK=$'\033[32m'
  C_FAIL=$'\033[31m'
  C_WARN=$'\033[33m'
  C_UNK=$'\033[33m'
  C_INST=$'\033[1;34m'
  C_NOTE=$'\033[90m'
  C_SEP=$'\033[36m'
  C_PENDING=$'\033[1;33m'
else
  C_RESET='' C_BOLD='' C_DIM='' C_H1='' C_OK='' C_FAIL='' C_WARN='' C_UNK='' C_INST='' C_NOTE='' C_SEP='' C_PENDING=''
fi

USE_SQUEUE=${USE_SQUEUE:-1}
USE_CMSH=${USE_CMSH:-1}
USE_SINFO=${USE_SINFO:-1}
# BCM installs cmsh at an absolute path and does NOT put it on PATH (Nscale TX: BCM 11 keeps it at
# /cm/local/apps/cmd/bin/cmsh, reachable only via `module load cmsh`). A bare `cmsh` fails the
# command -v guards below, silently dropping the rack union and the ct_down_unk column. RACK_CMSH
# is the same knob the backend uses; honour it so both agree on which binary to run.
CMSH=${RACK_CMSH:-cmsh}
tmp_status_arg=
log_files=()
probe_json_mode=0

# Must be defined before default log glob / explicit argv filtering (bash does not pre-scan function defs).
is_nemo_nccl_fname() {
  local b=${1,,}
  [[ "$b" == *nemo* || "$b" == *nccl* ]] && return 0
  [[ "$b" == *root-ck-* || "$b" == root-ck-* ]] && return 0
  [[ "$b" == *clusterkit* ]] && return 0
  return 1
}

# run-llama.sh Slurm stdout: excluded from single-rack NEMO table (see parser results / sn-llama/).
is_llama_slurm_fname() {
  local b=${1,,}
  [[ "$b" == *nemo-llama3_8b-1n-* ]] && return 0
  return 1
}

# Multirack NEMO/NCCL log names (excluded from single-rack table; shown in nvis-rack-status multirack tab).
is_multirack_fname() {
  local b=${1,,}
  is_nemo_nccl_fname "$b" || return 1
  if [[ "$b" =~ (2|3|4|5|6|7|8|9|[1-9][0-9]+)rack ]]; then
    return 0
  fi
  # root-NEMO-p*-r[…]-ct[…],p*-r[…]-… (run-nemo-xrack.sh log basename)
  if [[ "$b" == root-nemo-* || "$b" == root-nccl-* ]] && [[ "$b" == *,p* ]]; then
    return 0
  fi
  local n
  n=$(grep -oEi 'p[0-9]+-r[0-9]+' <<<"$b" | sort -u | wc -l)
  (( n >= 2 )) && return 0
  return 1
}

while [[ "${1:-}" == -* ]]; do
  case "$1" in
    --no-squeue) USE_SQUEUE=0; shift ;;
    --no-cmsh)   USE_CMSH=0; shift ;;
    --no-sinfo)  USE_SINFO=0; shift ;;
    --probe-json)
      probe_json_mode=1
      shift
      ;;
    --tmp-status)
      [[ -n "${2:-}" ]] || {
        echo "Usage: $0 [--no-squeue] [--no-cmsh] [--no-sinfo] {--probe-json LOG.txt | --tmp-status FILE | LOG.txt ...}" >&2
        echo "  Log paths default to *.txt in the current directory when none are given." >&2
        exit 1
      }
      tmp_status_arg=$2
      shift 2
      ;;
    *)
      echo "Usage: $0 [--no-squeue] [--no-cmsh] [--no-sinfo] {--probe-json LOG.txt | --tmp-status FILE | LOG.txt ...}" >&2
      echo "  Log paths default to *.txt in the current directory when none are given." >&2
      echo "  --probe-json FILE  JSON probe for one log (nvis-rack-status multirack API; includes multirack names)." >&2
      echo "  --tmp-status FILE  TSV: rack<TAB>jobid<TAB>ongoing<TAB>nccl_ok<TAB>llm_ok (parser.sh internal)." >&2
      echo "  Otherwise pass NEMO log paths; nccl/llm/squeue are evaluated like parser.sh." >&2
      echo "Env: NO_COLOR, FORCE_COLOR, SQUEUE_USER, SINFO_OPTS, NEMO_TARGET_STEPS (opt-in floor on the declared total; unset = accept whatever the run declared)." >&2
      exit 1
      ;;
  esac
done

if [[ -n "$tmp_status_arg" ]]; then
  [[ -f "$tmp_status_arg" ]] || {
    echo "$0: not a file: $tmp_status_arg" >&2
    exit 1
  }
  tmp_status=$tmp_status_arg
  tmp_rack=$(mktemp)
  tmp_ds=$(mktemp)
  trap 'rm -f "$tmp_rack" "$tmp_ds"' EXIT
else
  if [[ "${probe_json_mode:-0}" -eq 1 ]]; then
    # Probe path evaluates one log (including multirack); skip 1-rack-only log_files filtering below.
    log_files=()
  elif [[ $# -eq 0 ]]; then
    shopt -s nullglob
    log_files=()
    for f in *.txt; do
      [[ -f "$f" ]] || continue
      is_nemo_nccl_fname "$f" || continue
      is_llama_slurm_fname "$f" && continue
      is_multirack_fname "$f" && continue
      log_files+=("$f")
    done
    shopt -u nullglob
    if [[ ${#log_files[@]} -eq 0 ]]; then
      echo "$0: no matching *.txt (require nemo or nccl in name; multirack logs excluded) in current directory; pass log paths or use --tmp-status FILE" >&2
      echo "Usage: $0 [--no-squeue] [--no-cmsh] [--no-sinfo] {--tmp-status FILE | LOG.txt ...}" >&2
      exit 1
    fi
  else
    log_files=()
    for f in "$@"; do
      [[ -f "$f" ]] || {
        echo "${C_WARN}skip (not a file):${C_RESET} $f" >&2
        continue
      }
      bn=$(basename "$f")
      is_nemo_nccl_fname "$bn" || {
        echo "${C_WARN}skip (basename must contain nemo or nccl):${C_RESET} $f" >&2
        continue
      }
      is_llama_slurm_fname "$bn" && {
        echo "${C_WARN}skip (LLAMA log; use results view / sn-llama):${C_RESET} $f" >&2
        continue
      }
      is_multirack_fname "$bn" && {
        echo "${C_WARN}skip (multirack log; use nvis-rack-status multirack tab):${C_RESET} $f" >&2
        continue
      }
      log_files+=("$f")
    done
    if [[ ${#log_files[@]} -eq 0 ]]; then
      echo "$0: no usable log files after filtering (nemo/nccl in name; multirack excluded)" >&2
      exit 1
    fi
  fi
fi

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

# rack_from_fname / jobid_from_fname come from lib/rack_fname.inc.sh (sourced below), shared
# with parser.sh. Sourcing it also gains this table the generic NVIS_RACK_FNAME_RE fallback,
# without which every Nscale TX log (ink50-dh01-h22) showed rack "?" here while the Parser
# Results tables resolved it correctly.

condense_ct_nums() {
  local -a sorted=() out=()
  local x n s e i w max=0
  for x in $1; do
    [[ "$x" =~ ^[0-9]+$ ]] || continue
    n=$((10#$x))
    (( n > max )) && max=$n
  done
  mapfile -t sorted < <(for x in $1; do
    [[ "$x" =~ ^[0-9]+$ ]] && echo "$((10#$x))"
  done | sort -n -u)
  ((${#sorted[@]})) || return 0
  if ((max < 100)); then
    w=2
  else
    w=${#max}
  fi
  s=${sorted[0]}
  e=${sorted[0]}
  for ((i = 1; i < ${#sorted[@]}; i++)); do
    n=${sorted[i]}
    if ((n == e + 1)); then
      e=$n
    else
      if ((s == e)); then
        out+=("$(printf "%0${w}d" "$s")")
      else
        out+=("$(printf "%0${w}d" "$s")-$(printf "%0${w}d" "$e")")
      fi
      s=$n
      e=$n
    fi
  done
  if ((s == e)); then
    out+=("$(printf "%0${w}d" "$s")")
  else
    out+=("$(printf "%0${w}d" "$s")-$(printf "%0${w}d" "$e")")
  fi
  local IFS=,
  echo "${out[*]}"
}

sinfo_state_trim() {
  local s=$1
  s="${s,,}"
  while [[ -n "$s" ]]; do
    case "${s: -1}" in
      '*'|'~'|'$'|'#'|':') s="${s%?}" ;;
      *) break ;;
    esac
  done
  echo "$s"
}

format_sinfo_cell() {
  local r=$1 k st cnt
  local -a pairs=() sorted=() out=()
  for k in "${!sinfo_ct[@]}"; do
    [[ "$k" == "${r}|"* ]] || continue
    st="${k#*|}"
    cnt=${sinfo_ct[$k]}
    pairs+=("$cnt $st")
  done
  ((${#pairs[@]})) || {
    echo '-'
    return 0
  }
  mapfile -t sorted < <(printf '%s\n' "${pairs[@]}" | sort -k1,1nr -k2,2)
  for ln in "${sorted[@]}"; do
    read -r cnt st <<<"$ln"
    out+=("${st}=${cnt}")
  done
  local IFS=,
  echo "${out[*]}"
}

# The LLM verdict (llm_label / llm_stats_tsv / nemo_training_finished_log / slurm_hard_fail /
# nemo_python_fatal_fail) lives in lib/llm_label.inc.sh, and nccl_busbw_csv / nccl_oob_csv in
# nccl_parse.inc.sh — both shared with parser.sh (sourced below, next to validation_test_env.sh)
# so the dashboard PASS/FAIL and the Parser Results tables stay in lockstep.

_RACK_SCRIPTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# llm_label.inc.sh first: validation_test_env.sh only defines a fallback
# nemo_training_finished_log when one is not already present.
# shellcheck source=/dev/null
source "${_RACK_SCRIPTS_DIR}/lib/llm_label.inc.sh"
# shellcheck source=/dev/null
source "${_RACK_SCRIPTS_DIR}/lib/rack_fname.inc.sh"
# shellcheck source=/dev/null
source "${_RACK_SCRIPTS_DIR}/validation_test_env.sh"
# shellcheck source=/dev/null
source "${_RACK_SCRIPTS_DIR}/nccl_parse.inc.sh"

# When USE_SQUEUE=1: RUNNING_SET[jobid]=1 and JOB_TIME[jobid]=elapsed wall time (squeue %M, e.g. 0:29 or 1-00:15:30).
load_squeue_jobs() {
  if [[ "$USE_SQUEUE" -eq 0 ]]; then
    return 0
  fi
  while IFS='|' read -r sj stime || [[ -n "$sj" ]]; do
    sj="${sj//[[:space:]]/}"
    [[ -n "$sj" ]] || continue
    RUNNING_SET[$sj]=1
    JOB_TIME[$sj]=${stime//[[:space:]]/}
  done < <(squeue -u "${SQUEUE_USER:-$USER}" -h -o '%i|%M' 2>/dev/null || true)
}

# Emit one JSON object for a single log (used by nvis-rack-status multirack API). Exits the script.
if [[ "${probe_json_mode:-0}" -eq 1 ]]; then
  f="${1:-}"
  if [[ -z "$f" || ! -f "$f" ]]; then
    python3 -c 'import json; print(json.dumps({"ok": False, "error": "missing or invalid file"}))'
    exit 1
  fi
  declare -A RUNNING_SET
  declare -A JOB_TIME
  load_squeue_jobs
  jid=$(jobid_from_fname "$f")
  bw=$(nccl_busbw_csv "$f")
  IFS=',' read -r ar_bw aa_bw ag_bw <<<"$bw"
  ongoing=0
  [[ -n "${RUNNING_SET[$jid]:-}" ]] && ongoing=1
  llm_line=$(llm_label "$f")
  validation_eval_suite_for_file "$f" "$ongoing" "$ar_bw" "$aa_bw" "$ag_bw" "$llm_line"
  nccl_ok=$_OUT_NCCL_OK
  llm_ok=$_OUT_LLM_OK
  nemo_state=fail
  if [[ "$ongoing" -eq 1 ]]; then
    nemo_state=ongoing
  elif [[ "$nccl_ok" -eq 1 && "$llm_ok" -eq 1 ]]; then
    nemo_state=done
  fi
  jt="${JOB_TIME[$jid]:-}"
  bn=$(basename "$f")
  vt="$(validation_test_raw)"
  export _RJ_BN=$bn _RJ_JID=$jid _RJ_ON=$ongoing _RJ_NO=$nccl_ok _RJ_LO=$llm_ok _RJ_ST=$nemo_state _RJ_JT=$jt _RJ_VT=$vt
  export _RJ_NI="${_OUT_NCCL_ISSUE:-}"
  python3 <<'PY'
import json, os

def g(k, d=""):
    return os.environ.get(k, d)

try:
    on = int(g("_RJ_ON", "0"))
    nco = int(g("_RJ_NO", "0"))
    lko = int(g("_RJ_LO", "0"))
except ValueError:
    on, nco, lko = 0, 0, 0
print(
    json.dumps(
        {
            "ok": True,
            "basename": g("_RJ_BN"),
            "jid": g("_RJ_JID"),
            "ongoing": bool(on),
            "nccl_ok": bool(nco),
            # Why an NCCL log is not a pass despite its bandwidth (wrong results, a failed or
            # unfinished test); empty when clean. See nccl_integrity_issue.
            "nccl_issue": g("_RJ_NI"),
            "llm_ok": bool(lko),
            "nemo_state": g("_RJ_ST", "fail"),
            "squeue_time": g("_RJ_JT"),
            "validation_test": g("_RJ_VT"),
        }
    )
)
PY
  exit 0
fi

if [[ -z "${tmp_status_arg:-}" ]]; then
  declare -A RUNNING_SET
  declare -A JOB_TIME
  load_squeue_jobs
  tmp_status=$(mktemp)
  tmp_rack=$(mktemp)
  tmp_ds=$(mktemp)
  trap 'rm -f "$tmp_status" "$tmp_rack" "$tmp_ds"' EXIT
  for f in "${log_files[@]}"; do
    [[ -f "$f" ]] || {
      echo "${C_WARN}skip (not a file):${C_RESET} $f" >&2
      continue
    }
    rack=$(rack_from_fname "$f")
    jid=$(jobid_from_fname "$f")
    bw=$(nccl_busbw_csv "$f")
    IFS=',' read -r ar_bw aa_bw ag_bw <<<"$bw"
    ongoing=0
    [[ -n "${RUNNING_SET[$jid]:-}" ]] && ongoing=1
    llm_line=$(llm_label "$f")
    validation_eval_suite_for_file "$f" "$ongoing" "$ar_bw" "$aa_bw" "$ag_bw" "$llm_line"
    nccl_ok=$_OUT_NCCL_OK
    llm_ok=$_OUT_LLM_OK
    printf '%s\t%s\t%d\t%d\t%d\n' "$rack" "$jid" "$ongoing" "$nccl_ok" "$llm_ok" >>"$tmp_status"
  done
  sort -t$'\t' -k1,1V "$tmp_status" -o "$tmp_status"
fi

echo "${C_SEP}==========${C_RESET} ${C_H1}Rack status${C_RESET} ${C_SEP}==========${C_RESET}"
if [[ "$USE_SQUEUE" -eq 0 ]]; then
  echo "${C_NOTE}(squeue disabled: use default squeue user=$USER to classify ongoing.)${C_RESET}"
fi
if [[ "$USE_SINFO" -eq 0 ]]; then
  echo "${C_NOTE}(sinfo disabled: no Slurm node-state column on rack table.)${C_RESET}"
fi

declare -A rack_state rack_job rack_nccl_ok rack_llm_ok
while IFS=$'\t' read -r rack jid is_run nccl_ok llm_ok; do
  [[ -z "${rack:-}" ]] && continue
  if [[ "$is_run" -eq 1 ]]; then
    rack_state[$rack]=ongoing
  elif [[ "$nccl_ok" -eq 1 && "$llm_ok" -eq 1 ]]; then
    rack_state[$rack]=done
  else
    rack_state[$rack]=fail
  fi
  rack_job[$rack]=$jid
  rack_nccl_ok[$rack]=$nccl_ok
  rack_llm_ok[$rack]=$llm_ok
done <"$tmp_status"

# One-line hover text for the dashboard (tab/pipe-free; column nemo_hint in TSV).
nemo_hint_for_row() {
  local nem=$1 st=$2 jid=$3 nc=$4 ll=$5
  local vt parts=() oifs
  case "$nem" in
    done) printf '' ;;
    pending)
      printf '%s' "No NEMO or NCCL validation log in the log directory matches this rack yet (rack is listed but no matching log basename)."
      ;;
    ongoing*)
      if [[ -n "${jid:-}" && "$jid" != "-" ]]; then
        printf '%s' "Slurm job ${jid} is running; the validation log is still being written."
      else
        printf '%s' "Slurm reports a running validation job; elapsed time is in the status label."
      fi
      ;;
    fail)
      vt="$(validation_test_raw)"
      parts=()
      (( nc == 0 )) && parts+=("NCCL suite not satisfied for VALIDATION_TEST=${vt} (missing busbw line(s) or below threshold).")
      (( ll == 0 )) && parts+=("LLM/Nemotron not OK, training not finished to target steps, or log incomplete.")
      if ((${#parts[@]})); then
        oifs=$IFS
        IFS=' '
        printf '%s' "${parts[*]}"
        IFS=$oifs
      else
        printf '%s' "Validation did not reach done; inspect the log or adjust VALIDATION_TEST in Settings."
      fi
      ;;
    *) printf '%s' "See validation log and Slurm state for this rack." ;;
  esac
}

# --tmp-status path never ran load_squeue_jobs above; still need JOB_TIME for ongoing [TIME].
if [[ -n "${tmp_status_arg:-}" ]]; then
  declare -A JOB_TIME
  declare -A RUNNING_SET
  load_squeue_jobs
fi

# Default rack list snapshot (nvis-rack-status writes var/cache/cmsh_rack_list.txt).
if [[ -z "${RACK_CMSH_RACK_LIST_FILE:-}" ]]; then
  # NVIS_VAR_DIR is exported by the app (script_tree.script_env). The relative fallback is the
  # old behaviour and is correct only while this script sits exactly two levels under the
  # deployment root -- true for bin/scripts and for scripts/<platform>, not for a deeper layout.
  _def_var="${NVIS_VAR_DIR:-${_RACK_SCRIPTS_DIR}/../../var}"
  _def_cmsh_rl="${_def_var}/cache/cmsh_rack_list.txt"
  if [[ -r "$_def_cmsh_rl" ]]; then
    RACK_CMSH_RACK_LIST_FILE=$_def_cmsh_rl
  fi
fi

cm_racks=()
declare -A in_cm
cm_ok=0
declare -A ct_dn ct_inst ct_up ct_acc ct_seen
ds_ok=0
if [[ "$USE_CMSH" -eq 1 ]] && command -v "$CMSH" &>/dev/null; then
  # nvis-rack-status may set RACK_CMSH_RACK_LIST_FILE (one pNN-rMM id per line) to avoid ``cmsh -c "rack; list"`` every run.
  if [[ -n "${RACK_CMSH_RACK_LIST_FILE:-}" && -r "${RACK_CMSH_RACK_LIST_FILE}" ]]; then
    if command -v perl &>/dev/null; then
      mapfile -t cm_racks < <(perl -ne 'next unless /^p(\d+)-r(\d+)$/i; my $r=0+$2; next if $r==0 && !$ENV{RACK_CMSH_INCLUDE_R00}; printf "p%d-r%02d\n", 0+$1, $r' "${RACK_CMSH_RACK_LIST_FILE}" 2>/dev/null | sort -uV)
    else
      mapfile -t cm_racks < <(grep -E '^p[0-9]+-r[0-9]+$' "${RACK_CMSH_RACK_LIST_FILE}" 2>/dev/null | tr '[:upper:]' '[:lower:]' | sort -uV)
    fi
  elif command -v perl &>/dev/null; then
    # Word boundaries + canonical pN-rMM (two-digit rack): merge p5-r1 with p5-r01 (native_status).
    mapfile -t cm_racks < <("$CMSH" -c "rack; list" 2>/dev/null | perl -ne '
      while (/\bp(\d+)-r(\d+)\b/gi) {
        my ($p, $r) = (0 + $1, 0 + $2);
        next if $r == 0 && !$ENV{RACK_CMSH_INCLUDE_R00};
        printf "p%d-r%02d\n", $p, $r;
      }
    ' | sort -uV)
  else
    mapfile -t cm_racks < <("$CMSH" -c "rack; list" 2>/dev/null | grep -oE 'p[0-9]+-r[0-9]+' | tr '[:upper:]' '[:lower:]' | sort -uV)
  fi
  ((${#cm_racks[@]})) && cm_ok=1
  for rr in "${cm_racks[@]}"; do in_cm[$rr]=1; done
  : >"$tmp_ds"
  if "$CMSH" -c "ds" >"$tmp_ds" 2>/dev/null && [[ -s "$tmp_ds" ]]; then
    ds_ok=1
    shopt -s nocasematch
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" ]] && continue
      [[ "$line" =~ \[([^\]]+)\] ]] || continue
      inner="${BASH_REMATCH[1]}"
      bracket="${inner//[[:space:]]/}"
      bracket="${bracket,,}"
      # cmsh ds: [DOWN]/[UNREACHABLE] → DN; other non-[UP] → INST; [UP] → UP.
      st=
      case "$bracket" in
        up) st=UP ;;
        down|unreachable) st=DN ;;
        *) st=INST ;;
      esac
      while IFS= read -r h || [[ -n "$h" ]]; do
        [[ -z "$h" ]] && continue
        h="${h,,}"
        r=${h%-ct*}
        [[ "$h" =~ -ct([0-9]+)$ ]] || continue
        nn="${BASH_REMATCH[1]}"
        key="${r}|${st}|${nn}"
        [[ -n "${ct_seen[$key]:-}" ]] && continue
        ct_seen[$key]=1
        acc_key="${r}|${st}"
        ct_acc[$acc_key]="${ct_acc[$acc_key]:+${ct_acc[$acc_key]} }$nn"
      # The state belongs to the device named before the bracket; the message after it can name
      # another device ("... [ DOWN ] (failed to send jobs to <host>)") and must not mark that one.
      # A line that leads with the state ("[ UP ] <host>") names its device after the bracket.
      done < <(dev="${line%%\[*}"; [[ "$dev" =~ [[:alnum:]] ]] || dev="${line#*\]}"
               grep -oEi 'p[0-9]+-r[0-9]+-ct[0-9]+' <<<"$dev" | sort -u)
    done <"$tmp_ds"
    shopt -u nocasematch
    for acc_k in "${!ct_acc[@]}"; do
      rr=${acc_k%|*}
      stb=${acc_k#*|}
      ranges=$(condense_ct_nums "${ct_acc[$acc_k]}")
      [[ -z "$ranges" ]] && continue
      blob="${rr}-ct[${ranges}]"
      case "$stb" in
        DN)   ct_dn[$rr]=$blob ;;
        INST) ct_inst[$rr]=$blob ;;
        UP)   ct_up[$rr]=$blob ;;
      esac
    done
  fi
fi

declare -A sinfo_ct
if [[ "$USE_SINFO" -eq 1 ]] && command -v sinfo &>/dev/null; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "${line//[[:space:]]/}" ]] && continue
    read -r node st <<<"$line"
    [[ -z "$node" || -z "$st" ]] && continue
    st=$(sinfo_state_trim "$st")
    [[ -z "$st" ]] && continue
    _hosts=()
    if [[ "$node" == *'['*']'* ]]; then
      if command -v scontrol &>/dev/null; then
        mapfile -t _hosts < <(scontrol show hostnames "$node" 2>/dev/null) || true
      fi
      ((${#_hosts[@]})) || continue
    else
      _hosts=("$node")
    fi
    for h in "${_hosts[@]}"; do
      hh="${h,,}"
      hh="${hh%%.*}"
      [[ "$hh" =~ ^(p[0-9]+-r[0-9]+)(-ct[0-9]+)?$ ]] || continue
      rr="${BASH_REMATCH[1]}"
      key="${rr}|${st}"
      sinfo_ct[$key]=$((${sinfo_ct[$key]:-0} + 1))
    done
  done < <(sinfo -N -h -o '%N %t' ${SINFO_OPTS:-} 2>/dev/null)
fi

declare -A union
for k in "${!rack_state[@]}"; do union[$k]=1; done
for rr in "${cm_racks[@]:-}"; do union[$rr]=1; done

n_done=0 n_fail=0 n_on=0 n_pending=0 n_cm_no=0
mapfile -t rack_keys < <(printf '%s\n' "${!union[@]}" | sort -V)
: >"$tmp_rack"
for r in "${rack_keys[@]}"; do
  st=${rack_state[$r]:-}
  jid=${rack_job[$r]:-}
  if [[ "$USE_CMSH" -eq 1 ]] && [[ "$cm_ok" -eq 1 ]] && [[ -z "${in_cm[$r]:-}" ]] && [[ -n "$st" ]]; then
    ((++n_cm_no)) || true
  fi

  hint=
  if [[ -z "$st" ]]; then
    nem=pending
    job=-
    hint=$(nemo_hint_for_row "$nem" "$st" "$job" 0 0)
    ((++n_pending)) || true
  else
    nem=$st
    job=$jid
    case "$nem" in
      done)    ((++n_done)) || true ;;
      fail)    ((++n_fail)) || true ;;
      ongoing)
        ((++n_on)) || true
        if [[ "$USE_SQUEUE" -eq 1 ]]; then
          if [[ -n "${JOB_TIME[$jid]:-}" ]]; then
            nem="ongoing [${JOB_TIME[$jid]}]"
          else
            nem="ongoing [?]"
          fi
        else
          nem="ongoing [n/a]"
        fi
        ;;
    esac
    nc=${rack_nccl_ok[$r]:-0}
    ll=${rack_llm_ok[$r]:-0}
    hint=$(nemo_hint_for_row "$nem" "$st" "$job" "$nc" "$ll")
  fi
  if [[ "$USE_CMSH" -eq 0 ]]; then
    ctv=n/a
  elif ! command -v cmsh &>/dev/null; then
    ctv='?'
  elif [[ "$ds_ok" -eq 0 ]]; then
    ctv='?'
  else
    _dn="${ct_dn[$r]:-}"
    _inst="${ct_inst[$r]:-}"
    _up="${ct_up[$r]:-}"
    _parts=()
    [[ -n "$_dn" ]] && _parts+=("DN:${_dn}")
    [[ -n "$_inst" ]] && _parts+=("INST:${_inst}")
    [[ -n "$_up" ]] && _parts+=("UP:${_up}")
    if ((${#_parts[@]})); then
      oifs=$IFS
      IFS=';'
      ctv="${_parts[*]}"
      IFS=$oifs
    else
      ctv='-'
    fi
  fi
  if [[ "$USE_SINFO" -eq 0 ]]; then
    siv=n/a
  elif ! command -v sinfo &>/dev/null; then
    siv='?'
  else
    siv=$(format_sinfo_cell "$r")
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$r" "$nem" "$job" "$siv" "$ctv" "$hint" >>"$tmp_rack"
done

_hdr_rs=$'rack\tnemo\tlast_job\tsinfo\tct_down_unk\tnemo_hint'
echo "${C_DIM}  rack; list (cmsh). nemo: done|fail|ongoing [TIME]|pending — ongoing [TIME] uses squeue %M (elapsed) for last_job when USE_SQUEUE=1, else [n/a] or [?]. last_job: Slurm id. nemo_hint: dashboard hover (why pending/fail/ongoing). sinfo: Slurm compact states (%t) per rack (pNN-rMM-ct… nodes). ct_down_unk: cmsh ds — DN:… (DOWN/UNREACHABLE, red); INST:… (other non-UP states, blue); UP:… (green); \"-\" if none.${C_RESET}"
render_tsv_box "$_hdr_rs" "$tmp_rack" | sed -E \
  -e '/[|]/s/[|] done([[:space:]]+)[|]/| '"${C_OK}"'done'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] fail([[:space:]]+)[|]/| '"${C_FAIL}"'fail'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] (ongoing(\[[^]]*\])?)([[:space:]]+)[|]/| '"${C_WARN}"'\1'"${C_RESET}"'\3|/g' \
  -e '/[|]/s/[|] pending([[:space:]]+)[|]/| '"${C_PENDING}"'pending'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/(DN:)([^|]*)/'"${C_FAIL}"'\1'"${C_RESET}"'\2/g' \
  -e '/[|]/s/(INST:)([^|]*)/'"${C_INST}"'\1'"${C_RESET}"'\2/g' \
  -e '/[|]/s/(UP:)([^|]*)/'"${C_OK}"'\1'"${C_RESET}"'\2/g'

echo "${C_DIM}  Summary:${C_RESET} ${C_OK}${n_done} done${C_RESET}  ${C_FAIL}${n_fail} fail${C_RESET}  ${C_WARN}${n_on} ongoing${C_RESET}  ${C_PENDING}${n_pending} pending (CM, no log)${C_RESET}"
if [[ "$USE_CMSH" -eq 1 ]] && [[ "$cm_ok" -eq 1 ]] && ((n_cm_no > 0)); then
  echo "${C_DIM}  (${n_cm_no} rack(s) in logs but not matched in cmsh list.)${C_RESET}"
fi
if [[ "$USE_CMSH" -eq 1 ]] && ! command -v "$CMSH" &>/dev/null; then
  # Name the binary we actually looked for. BCM keeps cmsh off PATH, so "cmsh not found" sends
  # the reader hunting for a PATH problem when the answer is to point RACK_CMSH at it.
  echo "${C_NOTE}  '${CMSH}' not found (rack union may be log-only). Set RACK_CMSH to its absolute path, or --no-cmsh to skip.${C_RESET}"
elif [[ "$USE_CMSH" -eq 1 ]] && [[ "$cm_ok" -eq 0 ]]; then
  echo "${C_NOTE}  cmsh produced no pNN-rMM ids from rack; list (check ${CMSH} -c \"rack; list\").${C_RESET}"
fi
if [[ "$USE_CMSH" -eq 1 ]] && command -v "$CMSH" &>/dev/null && [[ "$ds_ok" -eq 0 ]]; then
  echo "${C_NOTE}  cmsh -c ds produced no parseable output; ct_down_unk column is \"?\".${C_RESET}"
fi
