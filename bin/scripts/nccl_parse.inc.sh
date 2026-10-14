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
#     the "concluded" line has no _mpi suffix.
#
# ARMING (why a data row alone is never enough)
# ---------------------------------------------
# A data row is only "NF>=9, numeric $1, numeric $8", which plain text hits by accident. A NeMo
# training log run with NCCL_DEBUG=INFO prints channel rank lists like
#     ink50-…: NCCL INFO Channel 05/32 :  0  1  2 … 35
# and when 36 ranks interleave, the wrap continuation lands on its own line as " 23 24 … 35" —
# nine-plus numeric fields, read as size=23 and busbw=30. Measured 2026-08-03: every one of the
# 36 NEMO-1Rack logs on Nscale TX produced 6-18 of these phantom all_reduce points, and the
# dashboard charted them as a real (terrible) result.
#
# So rows are accepted only once the file has proved it contains an nccl-tests TABLE, by either a
# "# Collective test starting:" marker or the table header nccl-tests always prints:
#     #                              out-of-place                       in-place
#     #       size   count   type   redop   root   time   algbw   busbw #wrong   …
# The header is what keeps the marker-less standalone launcher working: it has no "starting:"
# line, but it does print the header, so it stays armed while a NeMo log never arms at all.

# COMPLETION (why an armed table row is still not enough)
# ------------------------------------------------------
# Arming proves the file holds an nccl-tests table; it does not prove a test finished. A run that
# aborts mid-sweep leaves a header, a few rows and its NCCL_DEBUG=INFO noise -- Nscale NC multirack
# job 138174 died with "Test NCCL failure ... remote process exited or there was a network error",
# and a topology line of bare rank numbers (" 56 55 54 ...") was read as all_reduce busbw=49, so the
# dashboard called the failed run done. nccl-tests ends every test it finishes with
#     # Avg bus bandwidth    : 181.07
# so a test's rows are held as pending and count only when that line arrives. A failure marker
# (Test NCCL failure / Test CUDA failure / Test failure / Test timeout) inside a test fails that
# collective for the whole file: a sweep that failed once has not passed, whatever another round
# measured. A test begins at its "starting:" marker, or at the table header in a marker-less log.
# Keep in step with nvis_logscan.py and universal_parser._extract_nccl, which apply the same rule.

# Peak out-of-place busbw (GB/s) at the largest message size, per collective, from completed tests
# only. Prints "ar,aa,ag"; a collective that failed anywhere in the file prints empty.
nccl_busbw_csv() {
  awk '
    function begin_test(c) { tcol = c; tsz = -1; tbw = "" }
    /^# Collective test starting: all_reduce_perf(_mpi)?$/ { cur="ar"; seen=1; begin_test("ar"); next }
    /^# Collective test starting: alltoall_perf(_mpi)?$/   { cur="aa"; seen=1; begin_test("aa"); next }
    /^# Collective test starting: all_gather_perf(_mpi)?$/ { cur="ag"; seen=1; begin_test("ag"); next }
    /^# Collective test starting: /                        { cur="x";  seen=1; begin_test("x");  next }
    /^# Collective test concluded:/                        { cur="";   tcol="";                   next }
    /^#/ && (/out-of-place/ || (/size/ && /busbw/))        { hdr=1; if (!seen) begin_test("ar") }
    /Test (NCCL|CUDA) failure|Test failure|Test timeout/   { f = seen ? cur : "ar"; if (f != "") failed[f] = 1; tcol = ""; next }
    /Avg bus bandwidth/ {
      if (tcol != "" && tsz >= 0) {
        if      (tcol=="ar" && tsz>=ar_sz) { ar_sz=tsz; ar=tbw }
        else if (tcol=="aa" && tsz>=aa_sz) { aa_sz=tsz; aa=tbw }
        else if (tcol=="ag" && tsz>=ag_sz) { ag_sz=tsz; ag=tbw }
      }
      tcol = ""; next
    }
    NF>=9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      # No marker and no table header => this is not nccl-tests output. Skip, rather than
      # assuming all_reduce and charting whatever numbers happen to be on the line.
      if (!seen && !hdr) next
      if (tcol == "") next
      sz = $1 + 0
      if (sz >= tsz) { tsz = sz; tbw = $8 }
    }
    END {
      if (failed["ar"]) ar = ""
      if (failed["aa"]) aa = ""
      if (failed["ag"]) ag = ""
      print ar "," aa "," ag
    }
  ' "$1"
}

# NVIS 2026-07-28: reduce_scatter + sendrecv, same definition as nccl_busbw_csv (out-of-place
# busbw at the LARGEST message size). Deliberately a SEPARATE function returning "rs,sr" so the
# 3-field "ar,aa,ag" contract of nccl_busbw_csv -- consumed by BOTH parser.sh and rack_status.sh --
# is unchanged. All five collectives emit NF=13 rows, so $8 is out-of-place busbw for each.
# No hdr arming needed here or in nccl_series_by_collective_append: both require cur != "",
# i.e. a "starting:" marker, so they can never read a row out of an unmarked log.
nccl_busbw_ext_csv() {
  awk '
    /^# Collective test starting: reduce_scatter_perf(_mpi)?$/ { cur="rs"; tsz=-1; next }
    /^# Collective test starting: sendrecv_perf(_mpi)?$/       { cur="sr"; tsz=-1; next }
    /^# Collective test starting: /                            { cur="";   next }
    /^# Collective test concluded:/                            { cur="";   next }
    /Test (NCCL|CUDA) failure|Test failure|Test timeout/       { if (cur != "") failed[cur]=1; cur=""; next }
    /Avg bus bandwidth/ {
      # COMPLETION, as in nccl_busbw_csv: the test counts only once nccl-tests concludes it.
      if      (cur=="rs" && tsz>=0 && tsz>=rs_sz) { rs_sz=tsz; rs=tbw }
      else if (cur=="sr" && tsz>=0 && tsz>=sr_sz) { sr_sz=tsz; sr=tbw }
      cur=""; next
    }
    cur != "" && NF>=9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      sz = $1 + 0
      if (sz >= tsz) { tsz = sz; tbw = $8 }
    }
    END { if (failed["rs"]) rs = ""; if (failed["sr"]) sr = ""; print rs "," sr }
  ' "$1"
}

nccl_oob_csv() {
  grep 'Out of bounds values' "$1" 2>/dev/null | awk '{ print $NF }' | paste -sd, - || true
}

# INTEGRITY: what makes an nccl-tests log not a pass, whatever bandwidth it measured.
# Peak busbw (nccl_busbw_csv) answers "how fast"; this answers "did the run hold up", for the
# whole file rather than the collectives a suite happens to want. Prints the first problem in
# operator words, or nothing for a clean log. Three things fail a log:
#   - nccl-tests' data check: "Out of bounds values : N FAILED" with N > 0 (wrong results);
#   - a failure line: Test NCCL failure / Test CUDA failure / Test failure / Test timeout;
#   - a test that never concluded: fewer "concluded" markers than the larger of the tests the
#     manifest declares (# job=... tests=N) and the tests that started.
# Nscale NC 2026-10-14: gpu92-su34 (job 167578) passed Stage 1 with 178 out-of-bounds values in
# a non-all_reduce test, and gpu124-su25 (job 126229) with an all_reduce that aborted mid-sweep,
# because the verdict only asked whether all_reduce produced a peak. A log that never armed (no
# nccl-tests table: a NeMo log, an empty or pre-start failure) has nothing to judge here.
nccl_integrity_issue() {
  [[ -f "$1" ]] || return 0
  awk '
    /^# job=/ {
      for (i = 1; i <= NF; i++) if ($i ~ /^tests=[0-9]+$/) {
        split($i, a, "="); if ((a[2] + 0) > expected) expected = a[2] + 0
      }
    }
    /^# Collective test starting:/                  { armed = 1; started++; next }
    /^# Collective test concluded:/                 { armed = 1; concluded++; next }
    /^#/ && (/out-of-place/ || (/size/ && /busbw/)) { armed = 1 }
    /Out of bounds values[[:space:]]*:/ {
      n = $0; sub(/.*Out of bounds values[[:space:]]*:[[:space:]]*/, "", n); n = n + 0
      if (n > 0) oob += n
      next
    }
    /Test (NCCL|CUDA) failure|Test failure|Test timeout/ {
      if (fail == "") { fail = $0; sub(/^[[:space:]]+/, "", fail); fail = substr(fail, 1, 160) }
      next
    }
    END {
      if (!armed) exit
      if (oob > 0)   { printf "data errors: %d out-of-bounds values (wrong results)\n", oob; exit }
      if (fail != "") { printf "test failure: %s\n", fail; exit }
      want = (expected > started) ? expected : started
      if (want > concluded) printf "%d of %d tests did not finish\n", want - concluded, want
    }
  ' "$1"
}

# Sweep-block completeness: "<started-or-expected>,<concluded>".
#
# nccl-tests brackets each sweep with "# Collective test starting:" and "# Collective test
# concluded:", and prints the per-block summary lines only at the end. A log read WHILE the job is
# still running therefore has started > concluded and yields a truncated series -- 1 data row gives
# a 1-point chart with a meaningless peak. LLMB aggregated logs also declare ``tests=N`` in their
# manifest. That expected count must win when it is larger than the observed starts: a failed test
# emits no markers at all, so 22 starts == 22 conclusions was previously (and wrongly) called a
# complete 24-test run. Callers use the first value as the completeness denominator.
nccl_sweep_counts() {
  awk '
    /^# job=/ {
      for (i=1; i<=NF; i++) if ($i ~ /^tests=[0-9]+$/) {
        split($i, a, "="); if ((a[2]+0) > expected) expected=a[2]+0
      }
    }
    /^# Collective test starting:/  { s++ }
    /^# Collective test concluded:/ { c++ }
    END { if (expected > s) s=expected; print (s+0) "," (c+0) }
  ' "$1" 2>/dev/null || echo "0,0"
}

# First all_reduce block only: rack, job_id, size (B), out-of-place bus_bw (GB/s). Stops at the
# first all_reduce "concluded" line so a multi-round standalone log yields one representative series.
# NVIS 2026-07-28: per-collective busbw series for ALL five collectives.
# Emits: rack, jid, collective, size_bytes, busbw   (TAB separated)
# An llmb sweep runs each collective several times with different env/args (e.g. all_reduce is
# NCCL_NVLS_ENABLE=[0,1] x -G=[None,2,4] = 6 blocks). nccl_allreduce_series_append() below takes
# the FIRST all_reduce block, which on GB300 is the NVLS-DISABLED one -- ~700 GB/s vs ~916 GB/s,
# so the existing chart understates all_reduce by ~30%. Here we pick, per collective, the block
# with the highest busbw at its largest message size, i.e. the best achieved configuration.
# Left as a NEW function so nccl_allreduce_series_append()'s output is untouched.
nccl_series_by_collective_append() {
  local f=$1 rack=$2 jid=$3
  [[ -f "$f" ]] || return 0
  awk -v rack="$rack" -v jid="$jid" -v OFS='\t' '
    /^# Collective test starting: [a-z_]+_perf(_mpi)?$/ {
      coll=$5; sub(/_perf(_mpi)?$/,"",coll); blk++; cur=coll; curblk=blk; next
    }
    /^# Collective test concluded:/ { cur=""; next }
    cur != "" && NF>=9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      k = cur SUBSEP curblk
      cnt[k]++
      sz[k,cnt[k]] = $1
      bw[k,cnt[k]] = $8
      if ($1+0 >= maxsz[k]) { maxsz[k] = $1+0; maxbw[k] = $8+0 }
      blocks[k] = 1
    }
    END {
      for (k in blocks) {
        split(k, a, SUBSEP); c = a[1]
        if (!(c in best) || maxbw[k] > best[c]) { best[c] = maxbw[k]; bestk[c] = k }
      }
      for (c in bestk) {
        k = bestk[c]
        for (i = 1; i <= cnt[k]; i++) print rack, jid, c, sz[k,i], bw[k,i]
      }
    }
  ' "$f"
}

nccl_allreduce_series_append() {
  local f=$1 rack=$2 jid=$3
  [[ -f "$f" ]] || return 0
  awk -v rack="$rack" -v jid="$jid" -v OFS='\t' '
    /^#/ && /out-of-place/                                            { hdr=1 }
    /^#/ && /size/ && /busbw/                                         { hdr=1 }
    /^# Collective test starting: all_reduce_perf(_mpi)?$/             { cur="ar";    seen=1; next }
    /^# Collective test starting: (alltoall|all_gather)_perf(_mpi)?$/  { cur="other"; seen=1; next }
    /^# Collective test concluded: all_reduce_perf(_mpi)?$/            { if (captured) exit; cur=""; next }
    /^# Collective test concluded:/                                   { cur=""; next }
    NF >= 9 && $1 ~ /^[0-9]+$/ && $8 ~ /^[0-9]+(\.[0-9]+)?$/ {
      # See ARMING at the top of this file: without a marker or a table header these nine
      # numeric fields are as likely to be an NCCL_DEBUG channel list as a sweep row.
      if (!seen && !hdr) next
      c = (seen ? cur : "ar")
      if (c=="ar") { captured=1; print rack, jid, $1, $8 }
    }
  ' "$f"
}
