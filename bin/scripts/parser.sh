#!/bin/bash
# NEMO 1-rack log parser: tables per rack + completed / failed / ongoing lists.
# Each log file is one rack run: root-NEMO-1Rack-<rack>-<N>N-<jobid>.txt
# Original metrics logic: Junli Zhang <junliz@nvidia.com> (parser v1).
# v2: column tables (NCCL all_reduce / all_gather / alltoall + Nemotron 5H 56B),
#     rack status via rack_status.sh (cmsh + sinfo + logs), optional squeue.
# v3: optional --json / --csv export (python3); skips colored tables and rack_status.sh.

set -uo pipefail

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

perf=5000
USE_SQUEUE=1
USE_CMSH=1
USE_SINFO=1
OUTPUT_FORMAT=human
_want_json=0
_want_csv=0
while [[ "${1:-}" == -* ]]; do
  case "$1" in
    --no-squeue) USE_SQUEUE=0 ;;
    --no-cmsh)   USE_CMSH=0 ;;
    --no-sinfo)  USE_SINFO=0 ;;
    --json)      OUTPUT_FORMAT=json; _want_json=1 ;;
    --csv)       OUTPUT_FORMAT=csv; _want_csv=1 ;;
    -h|--help)
      echo "Usage: $0 [options] root-NEMO-1Rack-*.txt [...]" >&2
      echo "  --no-squeue  do not match Slurm job ids against squeue." >&2
      echo "  --no-cmsh    skip cmsh rack list vs completed-rack coverage." >&2
      echo "  --no-sinfo   skip sinfo Slurm node-state summary per rack." >&2
      echo "  --json       print parsed tables as JSON (stdout only; requires python3)." >&2
      echo "  --csv        print one merged CSV table (stdout only; requires python3)." >&2
      echo "  --json and --csv are mutually exclusive." >&2
      echo "Env: NO_COLOR, FORCE_COLOR=1|yes, SQUEUE_USER (squeue user), SINFO_OPTS (extra sinfo args)." >&2
      exit 0
      ;;
    *) break ;;
  esac
  shift
done

if [[ "$_want_json" -eq 1 && "$_want_csv" -eq 1 ]]; then
  echo "$0: use only one of --json or --csv" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 [--no-squeue] [--no-cmsh] [--no-sinfo] [--json|--csv] root-NEMO-1Rack-*.txt [...]" >&2
  echo "  --no-squeue  do not match Slurm job ids against squeue." >&2
  echo "  --no-cmsh    skip cmsh rack list vs completed-rack coverage." >&2
  echo "  --no-sinfo   skip sinfo Slurm node-state summary per rack." >&2
  echo "  --json       export parsed metrics as JSON (no colored tables; no rack_status.sh)." >&2
  echo "  --csv        export merged metrics as CSV (same constraints as --json)." >&2
  echo "Env: NO_COLOR, FORCE_COLOR=1|yes, SQUEUE_USER (squeue user), SINFO_OPTS (extra sinfo args)." >&2
  echo "Rack table only: $(dirname "${BASH_SOURCE[0]}")/rack_status.sh [--flags] <logs…| --tmp-status FILE>" >&2
  exit 1
fi

log_files=("$@")

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

rack_from_fname() {
  # NEMO 1Rack: rack id from basename. Llama Slurm stdout: …-NEMO-LLAMA3_8B-1N-<ct-hostname>-<jid>.txt → use CT host (not rack).
  local b _save
  b=$(basename "$1")
  if [[ "$b" =~ 1Rack-(p[0-9]+-r[0-9]+)(\[|-) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$b" =~ 1Rack-(CBR[0-9]{2}-DH[0-9]{2}-SVR-CN[0-9]{4})(\[|-) ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  _save=$(shopt -p nocasematch 2>/dev/null || printf 'shopt -u nocasematch\n')
  shopt -s nocasematch 2>/dev/null || true
  if [[ "$b" =~ NEMO-LLAMA3_8B-1N-(.+)-([0-9]+)\.txt$ ]]; then
    echo "${BASH_REMATCH[1]}"
    eval "$_save"
    return
  fi
  eval "$_save"
  echo "?"
}

jobid_from_fname() {
  # ...-18N-212.txt -> 212 ; LLAMA: ...-ct05-212.txt -> 212
  local b
  b=$(basename "$1")
  if [[ "$b" =~ NEMO-LLAMA3_8B-1N-.+-([0-9]+)\.txt$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return
  fi
  [[ "$b" =~ N-([0-9]+)\.txt$ ]] && echo "${BASH_REMATCH[1]}" || echo "?"
}

# NCCL busbw / oob / series helpers are shared with rack_status.sh via nccl_parse.inc.sh
# (sourced below, next to validation_test_env.sh) so a parsing fix lands in both the parser and
# the dashboard at once.

slurm_hard_fail() {
  grep -E -q 'srun:.*error:.*Exited|error:.*SIGNAL' "$1" 2>/dev/null
}

# Prints: step_s tflops sstd pstd mfu  (tab-separated) or empty line if no valid window.
llm_stats_tsv() {
  local f=$1
  if grep -q 'Step Time.*MODEL_TFLOP/s/GPU' "$f"; then
    grep -o 'Step Time.*MODEL_TFLOP/s/GPU' "$f" 2>/dev/null | sed -e 's:s GPU: s GPU:g' -e 's:MODEL_TFLOP: MODEL_TFLOP:g' \
      | awk -v min_iter=70 -v max_iter=90 -v perf="$perf" '
BEGIN { ct = 0; ts = 0; ps = 0 }
(min_iter <= NR && NR < max_iter) { ct++; tv[ct] = $4; ts += $4; pv[ct] = $8; ps += $8 }
END {
  if (ct < 1) exit 1
  t_mean = ts / ct
  t_var = 0
  for (i = 1; i <= ct; i++) t_var += (tv[i] - t_mean) ^ 2
  t_std = (ct > 1 ? sqrt(t_var / (ct - 1)) : 0)
  p_mean = ps / ct
  p_var = 0
  for (i = 1; i <= ct; i++) p_var += (pv[i] - p_mean) ^ 2
  p_std = (ct > 1 ? sqrt(p_var / (ct - 1)) : 0)
  mfu = ps / ct / perf * 100
  printf "%.2f\t%.2f\t%.5f\t%.5f\t%.1f\n", ts / ct, ps / ct, t_std, p_std, mfu
}'
    return 0
  fi
  if grep -q 'train_step_timing.*consumed_samples' "$f"; then
    grep -o 'train_step_timing.*consumed_samples' "$f" 2>/dev/null | tr '|' ' ' \
      | awk -v min_iter=70 -v max_iter=90 -v perf="$perf" '
BEGIN { ct = 0; ts = 0; ps = 0 }
(min_iter <= NR && NR < max_iter) { ct++; tv[ct] = $4; ts += $4; pv[ct] = $6; ps += $6 }
END {
  if (ct < 1) exit 1
  t_mean = ts / ct
  t_var = 0
  for (i = 1; i <= ct; i++) t_var += (tv[i] - t_mean) ^ 2
  t_std = (ct > 1 ? sqrt(t_var / (ct - 1)) : 0)
  p_mean = ps / ct
  p_var = 0
  for (i = 1; i <= ct; i++) p_var += (pv[i] - p_mean) ^ 2
  p_std = (ct > 1 ? sqrt(p_var / (ct - 1)) : 0)
  mfu = ps / ct / perf * 100
  printf "%.2f\t%.2f\t%.5f\t%.5f\t%.1f\n", ts / ct, ps / ct, t_std, p_std, mfu
}'
    return 0
  fi
  return 1
}

llm_label() {
  local f=$1 stats
  if stats=$(llm_stats_tsv "$f" 2>/dev/null) && [[ -n "$stats" ]]; then
    awk -v OFS='\t' '{ printf "OK\t%s\t%s\t%s\t%s\t%s\n", $1, $2, $3, $4, $5 }' <<<"$stats"
    return
  fi
  if slurm_hard_fail "$f"; then
    printf 'FAIL\t-\t-\t-\t-\t-\n'
    return
  fi
  printf 'UNK\t-\t-\t-\t-\t-\n'
}

_PARSER_SCRIPTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=/dev/null
source "${_PARSER_SCRIPTS_DIR}/validation_test_env.sh"
# shellcheck source=/dev/null
source "${_PARSER_SCRIPTS_DIR}/nccl_parse.inc.sh"

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

running_job_ids() {
  if [[ "$USE_SQUEUE" -eq 0 ]]; then
    return 0
  fi
  squeue -u "${SQUEUE_USER:-$USER}" -h -o '%i' 2>/dev/null | sort -u || true
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
trap 'rm -f "$tmp_nccl" "$tmp_nccl_ar" "$tmp_llm" "$tmp_llm_steps" "$tmp_status"' EXIT

for f in "${log_files[@]}"; do
  [[ -f "$f" ]] || { echo "${C_WARN}skip (not a file):${C_RESET} $f" >&2; continue; }
  rack=$(rack_from_fname "$f")
  jid=$(jobid_from_fname "$f")
  base=$(basename "$f")

  bw=$(nccl_busbw_csv "$f")
  IFS=',' read -r ar_bw aa_bw ag_bw <<<"$bw"
  oob=$(nccl_oob_csv "$f")
  IFS=',' read -r oob_ar oob_aa oob_ag _oobx <<<"${oob},,,"
  oob_fmt="${oob_ar:----},${oob_ag:----},${oob_aa:----}"

  ongoing=0
  [[ -n "${RUNNING_SET[$jid]:-}" ]] && ongoing=1
  llm_line=$(llm_label "$f")
  validation_eval_suite_for_file "$f" "$ongoing" "$ar_bw" "$aa_bw" "$ag_bw" "$llm_line"
  nccl_ok=$_OUT_NCCL_OK
  llm_ok=$_OUT_LLM_OK

  printf '%s\t%s\t%d\t%d\t%d\n' "$rack" "$jid" "$ongoing" "$nccl_ok" "$llm_ok" >>"$tmp_status"

  # Table column order: all_reduce, all_gather, alltoall (log order is reduce → alltoall → gather).
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rack" "$jid" "${ar_bw:----}" "${ag_bw:----}" "${aa_bw:----}" "$oob_fmt" >>"$tmp_nccl"
  nccl_allreduce_series_append "$f" "$rack" "$jid" >>"$tmp_nccl_ar" || true
  printf '%s\t%s\t%s\n' "$rack" "$jid" "$llm_line" >>"$tmp_llm"
  llm_step_series_append "$f" "$rack" "$jid" >>"$tmp_llm_steps" || true
done

sort -t$'\t' -k1,1V "$tmp_nccl" -o "$tmp_nccl"
sort -t$'\t' -k1,1V -k2,2V -k3,3n "$tmp_nccl_ar" -o "$tmp_nccl_ar"
sort -t$'\t' -k1,1V "$tmp_llm" -o "$tmp_llm"
sort -t$'\t' -k1,1V -k2,2V "$tmp_llm_steps" -o "$tmp_llm_steps"
sort -t$'\t' -k1,1V "$tmp_status" -o "$tmp_status"

if [[ "$OUTPUT_FORMAT" != "human" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "$0: --${OUTPUT_FORMAT} requires python3 in PATH" >&2
    exit 1
  fi
  python3 - "$tmp_nccl" "$tmp_llm" "$tmp_status" "$tmp_llm_steps" "$tmp_nccl_ar" "$OUTPUT_FORMAT" <<'PY'
import csv, json, sys
from typing import Any, Dict, List, Tuple

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

        nccl_dicts = [row_to_dict(r, nccl_fields) for r in nccl_rows]
        for d in nccl_dicts:
            key = (str(d.get("rack", "")), str(d.get("job_id", "")))
            d["all_reduce_busbw_series"] = ar_series.get(key, [])

        llm_dicts = [row_to_dict(r, llm_fields) for r in llm_rows]
        for d in llm_dicts:
            key = (str(d.get("rack", "")), str(d.get("job_id", "")))
            d["step_series"] = step_by.get(key, [])

        obj = {
            "export_version": 6,
            "nccl_peak_out_of_place_busbw_gbps": nccl_dicts,
            "nemotron_5h_56b_train_window": llm_dicts,
            "parser_flags": [row_to_dict(r, st_fields, ("ongoing", "nccl_ok", "llm_ok")) for r in st_rows],
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
_hdr_llm=$'rack\tjob_id\tstatus\tstep_s\tTFLOPs/GPU\tSstd\tPstd\tMFU%'
render_tsv_box "$_hdr_llm" "$tmp_llm" | sed -E \
  -e '/[|]/s/[|] OK([[:space:]]+)[|]/| '"${C_OK}"'OK'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] FAIL([[:space:]]+)[|]/| '"${C_FAIL}"'FAIL'"${C_RESET}"'\1|/g' \
  -e '/[|]/s/[|] UNK([[:space:]]+)[|]/| '"${C_UNK}"'UNK'"${C_RESET}"'\1|/g'

echo
_rs_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
USE_SQUEUE=$USE_SQUEUE USE_CMSH=$USE_CMSH USE_SINFO=$USE_SINFO \
  bash "$_rs_dir/rack_status.sh" --tmp-status="$tmp_status"
