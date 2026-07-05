# shellcheck shell=bash
# nccl_parse.inc.sh — SHARED NCCL log-parsing helpers, sourced by BOTH parser.sh (View Parser
# Results / exports) and rack_status.sh (dashboard --probe-json PASS/FAIL). Keeping a single copy
# here means a parsing fix lands in both the parser AND the dashboard at once (they used to have
# separate, drifting copies of nccl_busbw_csv — a fix in one silently missed the other).
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Handles BOTH NCCL log dialects:
#   - NEMO container binaries: all_reduce_perf_mpi / alltoall_perf_mpi / all_gather_perf_mpi,
#     each bracketed by "# Collective test starting:" and "# Collective test concluded:" lines.
#   - Standalone portable launcher (nccl-tests): plain all_reduce_perf — NO "starting" line, and
#     the "concluded" line has no _mpi suffix. When no "starting:" marker appears at all, every
#     data row is attributed to all_reduce.

# Peak out-of-place busbw (GB/s) at the largest message size, per collective. Prints "ar,aa,ag".
nccl_busbw_csv() {
  awk '
    /^# Collective test starting: all_reduce_perf(_mpi)?$/ { cur="ar"; seen=1; next }
    /^# Collective test starting: alltoall_perf(_mpi)?$/   { cur="aa"; seen=1; next }
    /^# Collective test starting: all_gather_perf(_mpi)?$/ { cur="ag"; seen=1; next }
    /^# Collective test concluded:/                        { cur="";   next }
    NF>=9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      c = (seen ? cur : "ar"); sz = $1 + 0
      if      (c=="ar" && sz>=ar_sz) { ar_sz=sz; ar=$8 }
      else if (c=="aa" && sz>=aa_sz) { aa_sz=sz; aa=$8 }
      else if (c=="ag" && sz>=ag_sz) { ag_sz=sz; ag=$8 }
    }
    END { print ar "," aa "," ag }
  ' "$1"
}

nccl_oob_csv() {
  grep 'Out of bounds values' "$1" 2>/dev/null | awk '{ print $NF }' | paste -sd, - || true
}

# First all_reduce block only: rack, job_id, size (B), out-of-place bus_bw (GB/s). Stops at the
# first all_reduce "concluded" line so a multi-round standalone log yields one representative series.
nccl_allreduce_series_append() {
  local f=$1 rack=$2 jid=$3
  [[ -f "$f" ]] || return 0
  awk -v rack="$rack" -v jid="$jid" -v OFS='\t' '
    /^# Collective test starting: all_reduce_perf(_mpi)?$/             { cur="ar";    seen=1; next }
    /^# Collective test starting: (alltoall|all_gather)_perf(_mpi)?$/  { cur="other"; seen=1; next }
    /^# Collective test concluded: all_reduce_perf(_mpi)?$/            { if (captured) exit; cur=""; next }
    /^# Collective test concluded:/                                   { cur=""; next }
    NF >= 9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      c = (seen ? cur : "ar")
      if (c=="ar") { captured=1; print rack, jid, $1, $8 }
    }
  ' "$f"
}
