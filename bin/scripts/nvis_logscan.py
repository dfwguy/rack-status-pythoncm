#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""nvis_logscan — extract every validation metric from a log in ONE streaming pass.

Runs ON the node that holds the log (the head, in SSH mode) and prints a small JSON summary, so
a 973 MB log never crosses the network.  Standard library only: the head has python3 and
``parser.sh --json`` already depends on it.

WHY THIS EXISTS
---------------
The shell extractors it replaces each open the file again: ``nccl_busbw_csv``,
``nccl_busbw_ext_csv``, ``nccl_oob_csv``, ``nccl_sweep_counts``, two series appenders,
``llm_stats_tsv`` (two greps), ``nemo_training_finished_log``, ``slurm_hard_fail``,
``nemo_python_fatal_fail`` and the inventory scan — about sixteen full reads per log, plus
roughly twenty spawned processes.  On a 973 MB / 8.0 M line log (Nscale TX job 140457) that
measured 21 s; this module does the same work in ~3 s because it reads the bytes once.

Worse than slow, the old path SAMPLED: ``parser.sh`` classified files above 16 MB from a
2 MB head + 2 MB tail window, and ``universal_parser`` read 2 MB + 1 MB.  Anything in the middle
of a big log was invisible — and in a 576-node run the interesting lines sit at 98% depth.

THE RULES THIS FILE FOLLOWS
---------------------------
1. Never materialise the log.  Iterate; the only whole objects are one line at a time.
2. Never sample to decide.  Every byte is scanned; caps apply to OUTPUT size, never to input.
3. Bounded state only.  Counters, Welford accumulators and fixed-size buffers.  Peak memory is
   independent of file size — see ``MAX_*`` below for every bound.
4. Tolerate interleaving.  At 2304 ranks sharing one stdout, lines arrive spliced together
   (``ink50-dh0Step Time : 105.15s...`` is real, from job 140457), so matching uses ``search``
   and never anchors at start-of-line unless the producer guarantees it.

PERFORMANCE NOTES (measured on job 140457, 973 MB / 8,043,670 lines)
--------------------------------------------------------------------
Line iteration alone costs 0.74 s, so the matching strategy is everything:

    per-line + single negative substring filter   2.9 s   <- WRONG, see _CHATTER
    mmap + literal-only finditer, slice on hit    5.7 s
    per-line bytes alternation regex              7.6 s
    4 MB block finditer with a lazy alternation  58.1 s   <- catastrophic backtracking
    every kind pattern on every line             74.0 s   <- alternation size dominates

This file lands at 15.3 s with a peak RSS of 13 MB, against 21 s for parser.sh's sixteen passes
over the same file. The memory is the point: it does not move with file size.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from typing import Any

SCAN_VERSION = 1

# ---------------------------------------------------------------------------------------------
# Output bounds. These cap what we KEEP, never what we read, so memory is O(1) in file size.
# ---------------------------------------------------------------------------------------------
#: Step samples retained for the averaging window. The window is either samples 70-89 (long runs)
#: or a proportional warmup over all samples (< 70), so the first 90 always suffice.
MAX_WINDOW_SAMPLES = 90
#: Per-iteration step series handed to the chart; matches llm_step_series_append's it_max.
MAX_STEP_SERIES = 2010
#: Rows kept per NCCL sweep block. A sweep is ~32 message sizes; 512 is slack for odd producers.
MAX_ROWS_PER_BLOCK = 512
#: Sweep blocks tracked. An LLMB run is ~24; beyond this we keep counting but stop storing series.
MAX_BLOCKS = 256
#: "Out of bounds values" samples kept.
MAX_OOB = 64
#: Head/tail lines captured for the inventory record.
INV_HEAD_LINES = 5
INV_TAIL_LINES = 5
#: Per-line cap for captured inventory text; Slurm host lists run to kilobytes.
INV_LINE_MAXCH = 400

# ---------------------------------------------------------------------------------------------
# Matching. Stage 1 rejects noise, stage 2 is a literal-only prescreen, stage 3 extracts.
# ---------------------------------------------------------------------------------------------

#: NCCL_DEBUG chatter. Two distinct jobs, and conflating them cost a correct answer twice:
#:
#:  * As a DATA-ROW guard it is essential. "…4 [1] NCCL INFO comm 0x… rank 1805 nranks 2304…"
#:    has nine-plus fields with a numeric first and eighth, so inside an armed sweep awk read
#:    size=4, busbw=1805 and job 140457 charted a RANK NUMBER as alltoall bandwidth.
#:  * As a LINE filter it is wrong. At 2304 ranks a real record gets spliced into a chatter
#:    line; log 77133 has exactly one, and dropping it shifted the 70-89 averaging window by a
#:    sample and changed the reported TFLOP/s. A filter that is right 8,043,669 times out of
#:    8,043,670 still reports the wrong number.
#:
#: So: chatter can never become a data row, but it is always searched for markers.
_CHATTER = b"NCCL INFO"

#: Hot prescreen, run on EVERY line. Literal-only: no quantifiers, nothing that can backtrack.
#: Keep this list SHORT. Python's re walks alternatives in order at every position, so cost is
#: roughly linear in the number of branches — the full kind list below measured 74 s here versus
#: 8 s for these, on the same 973 MB file.
_METRIC = re.compile(
    rb"Collective test|Avg bus bandwidth|Out of bounds|out-of-place|busbw"
    rb"|MODEL_TFLOP|train_step_timing|iteration|# job=|max_steps="
    rb"|Traceback|OutOfMemory|CUDA out of memory|error:"
    rb"|--num_gpus|--gpus_per_node|-gb |SLURM_NNODES=|NODELIST:|seq_len"
)

_COLL_START = re.compile(rb"^#\s*Collective test starting:\s*([a-z_]+)_perf(?:_mpi)?\s*$")
_COLL_END = re.compile(rb"^#\s*Collective test concluded:\s*([a-z_]+)?")
_AVG_BW = re.compile(rb"Avg bus bandwidth\s*:\s*([0-9.]+)")
_OOB = re.compile(rb"Out of bounds values\s*:\s*(\S+)")
_TESTS = re.compile(rb"\btests=(\d+)\b")
_DATA_ROW = re.compile(rb"^\s*\d+\s")
_FIVE_START = re.compile(rb"^FIVE_COLLECTIVE_SWEEP_START\s+job=(\d+)\b")
_FIVE_END = re.compile(
    rb"^FIVE_COLLECTIVE_SWEEP_END\s+job=(\d+)\s+rc=(\d+)\s+detail_rc=(\d+)\b"
)
_FIVE_TEST_END = re.compile(
    rb"^TEST_END\s+test=(\d+)\s+"
    rb"collective=(all_reduce|alltoall|sendrecv|all_gather|reduce_scatter)\s+"
    rb"rc=(\d+)\s+correctness=(\S+)\s+raw=(\S+)\b"
)
_FIVE_COLLECTIVES = ("all_reduce", "alltoall", "sendrecv", "all_gather", "reduce_scatter")

_STEP_TIME = re.compile(rb"Step Time\s*:\s*([0-9.]+)\s*s")
_STEP_TFLOP = re.compile(rb"([0-9.]+)\s*MODEL_TFLOP/s/GPU")
_TRAIN_STEP = re.compile(rb"train_step_timing in s:\s*([0-9.]+)")
_TRAIN_TFLOP = re.compile(rb"\btflops:\s*([0-9.]+)")
_ITER = re.compile(rb"iteration\s+(\d+)/\s*(\d+)")
_MAX_STEPS = re.compile(rb"max_steps=(\d+)")

#: Run geometry, for tokens/s and the analytics scaling tables. app.py read these from a 14 MB
#: head, so a late "-gb" or NODELIST on a big log simply did not exist as far as it was concerned.
_G_NUM_GPUS = re.compile(rb"--num_gpus\s+(\d+)\b", re.IGNORECASE)
_G_GPN = re.compile(rb"--gpus_per_node\s+(\d+)\b", re.IGNORECASE)
_G_GB = re.compile(rb"(?<![\w-])-gb\s+(\d+)\b", re.IGNORECASE)
_G_NNODES = re.compile(rb"SLURM_NNODES=(\d+)\b")
_G_NODELIST = re.compile(rb"NODELIST:\s*(\S+)")
_G_SEQ = re.compile(rb"(?:--seq[_-]?len(?:gth)?|seq_length)[= ]\s*(\d+)\b", re.IGNORECASE)

#: Wall-clock window for the Grafana embed: the timestamp on any training-progress line. app.py
#: read these from a 64 MB head, which on a 973 MB log meant the window ended in the wrong hour.
_TS = re.compile(rb"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
#: A line that proves training was progressing, so its timestamp bounds the run.
_LLM_MARK = re.compile(rb"train_step_timing|MODEL_TFLOP/s/GPU|consumed_samples|iteration\s+\d+/")

_GEOM = (
    ("num_gpus", _G_NUM_GPUS),
    ("gpus_per_node", _G_GPN),
    ("global_batch", _G_GB),
    ("slurm_nnodes", _G_NNODES),
    ("nodelist", _G_NODELIST),
    ("seq_len", _G_SEQ),
)

_SLURM_FAIL = re.compile(rb"srun:.*error:.*Exited|error:.*SIGNAL")
_PY_FATAL = re.compile(
    rb"Traceback \(most recent call last\)|torch\.cuda\.OutOfMemoryError"
    rb"|torch\.OutOfMemoryError|CUDA out of memory|CudaOutOfMemoryError"
)

#: Content classification, mirroring parser.sh _inv_scan. Order here is irrelevant; the emitted
#: order follows KIND_ORDER so the primary kind is stable however it was found.
_KIND_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("mnubergemm", re.compile(rb"MNUB \[")),
    ("nemotron", re.compile(rb"train_step_timing.*consumed_samples|Step Time.*MODEL_TFLOP/s/GPU"
                            rb"|megatron|Megatron|NeMo|nemotron|Nemotron")),
    # "NCCL INFO" is deliberately absent: it is NCCL_DEBUG chatter, not a result. Every NeMo log
    # carries megabytes of it, and marking those "nccl" put phantom rows in the parser tables.
    ("nccl", re.compile(rb"^#\s*Collective test starting:|Avg bus bandwidth|out-of-place"
                        rb"|all_reduce_perf|alltoall_perf|all_gather_perf|nccl-tests")),
    ("hpl", re.compile(rb"HPLinpack|T/V\s+N\s+NB|WR[0-9]+[LRC][0-9]+")),
    ("nvbandwidth", re.compile(rb"nvbandwidth|device_to_host_memcpy|host_to_device_memcpy")),
    ("nvloom", re.compile(rb"nvloom|NVLoom|NVLOOM")),
    ("clusterkit", re.compile(rb"clusterkit|Clusterkit|ClusterKit|CLUSTERKIT")),
    ("dcgm", re.compile(rb"dcgmi|DCGM Diagnostic|Deployment Module|nvvs")),
    ("ib_perftest", re.compile(rb"ib_write_bw|ib_read_bw|ib_send_bw|perftest|BW average")),
    ("nvlmapper", re.compile(rb"nvlmapper|NVLMapper|NVLink Mapper")),
    ("llmb", re.compile(rb"llmb|LLMB|llm-benchmark")),
    ("gpu_stress", re.compile(rb"gpu-burn|gpu_burn|stress-ng|Xid|ECC error")),
    ("imex", re.compile(rb"nvidia-imex|IMEX|imex_")),
    ("nvswitch", re.compile(rb"NVSwitch|nvswitch|nvidia-nvswitch")),
    ("sysinfo", re.compile(rb"nvidia-smi|Driver Version:|CUDA Version:|dmidecode|lscpu")),
    ("healthcheck", re.compile(rb"health[-_ ]?check|healthcheck|node_check|bringup")),
    ("bug_report", re.compile(rb"nvidia-bug-report|nvidia_bug_report")),
    ("slurm", re.compile(rb"^srun:|^sbatch:|^slurmstepd|SLURM_JOB_ID|salloc:")),
)

#: Most specific first; the primary kind is the first of these the file matched.
KIND_ORDER = [name for name, _ in _KIND_PATTERNS]

#: awk's short codes for the three collectives the legacy "ar,aa,ag" contract carries.
_SHORT = {"all_reduce": "ar", "alltoall": "aa", "all_gather": "ag",
          "reduce_scatter": "rs", "sendrecv": "sr"}


class Welford:
    """Online mean/variance: O(1) memory, one pass, numerically stable.

    Replaces the awk that buffered every sample in ``sv[]``/``fv[]`` and then copied the window
    into ``tv[]``/``pv[]`` just to take a standard deviation — O(rows) memory for two numbers.
    """

    __slots__ = ("_m2", "mean", "n")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self._m2 += d * (x - self.mean)

    @property
    def stdev(self) -> float:
        """Sample standard deviation (n-1), matching the awk it replaces."""
        return (self._m2 / (self.n - 1)) ** 0.5 if self.n > 1 else 0.0


def _f(b: bytes) -> float | None:
    try:
        return float(b)
    except (TypeError, ValueError):
        return None


def _clean(raw: bytes) -> str:
    """Sanitise a captured line for the inventory record: de-ANSI, tabs to spaces, cap length."""
    s = raw.decode("utf-8", "replace").rstrip("\n")
    s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
    s = s.replace("\t", " ")
    s = "".join(ch for ch in s if ch >= " " or ch == " ")
    return s[:INV_LINE_MAXCH]


def _tail_lines(path: str, size: int, want: int) -> list[str]:
    """Last *want* lines, read by seeking instead of ringing every line through a deque.

    Appending 8 million lines to a deque just to keep five of them costs seconds; one seek costs
    nothing and is exact.
    """
    if want <= 0 or size <= 0:
        return []
    span = min(size, 65536)
    try:
        with open(path, "rb") as fh:
            fh.seek(size - span)
            blob = fh.read(span)
    except OSError:
        return []
    lines = blob.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return [_clean(x) for x in lines[-want:]]


class _NcclBlock:
    """One bracketed sweep block: its rows and the peak busbw at its largest message size."""

    __slots__ = ("coll", "max_bw", "max_size", "rows", "truncated")

    def __init__(self, coll: str) -> None:
        self.coll = coll
        self.rows: list[tuple[int, float]] = []
        self.max_size = -1
        self.max_bw = 0.0
        self.truncated = False

    def add(self, size: int, bw: float) -> None:
        if len(self.rows) < MAX_ROWS_PER_BLOCK:
            self.rows.append((size, bw))
        else:
            self.truncated = True
        if size >= self.max_size:
            self.max_size = size
            self.max_bw = bw


def _five_collective_metrics(
    wrapper_path: str,
    job_id: str | None,
    end_state: tuple[str, int, int] | None,
    tests: dict[str, tuple[int, int, str, str]],
) -> dict[str, Any] | None:
    """Resolve one reviewed five-collective wrapper into verified raw metrics.

    Wrapper PASS-like markers alone are never performance evidence. Every one of
    the five exact collectives must declare rc=0/correctness=OK, point beneath
    ``job-<same-id>/`` to its own expected ``.raw`` basename, and the raw file
    must independently contain a real nccl-tests table, positive numeric peak
    and average bandwidth, zero out-of-bounds values, and a conclusion marker.
    """
    if not job_id or end_state != (job_id, 0, 0) or set(tests) != set(_FIVE_COLLECTIVES):
        return None
    wrapper_real = os.path.realpath(wrapper_path)
    allowed_root = os.path.realpath(os.path.join(os.path.dirname(wrapper_real), f"job-{job_id}"))
    peaks: dict[str, float] = {}
    oob: list[str] = []
    avg: list[float] = []
    ar_series: list[dict[str, float | int]] = []
    series_by: dict[str, list[dict[str, float | int]]] = {}
    for expected_test, collective in enumerate(_FIVE_COLLECTIVES, start=1):
        test_no, rc, correctness, raw_path = tests[collective]
        if test_no != expected_test or rc != 0 or correctness.upper() != "OK":
            return None
        raw_real = os.path.realpath(raw_path)
        try:
            if os.path.commonpath((allowed_root, raw_real)) != allowed_root:
                return None
        except ValueError:
            return None
        if not re.fullmatch(
            rf"test-{test_no}-{re.escape(collective)}-[A-Za-z0-9._+-]+\.raw",
            os.path.basename(raw_real),
        ):
            return None
        raw = scan(raw_real, inventory=False, follow_five_wrapper=False)
        nccl = raw.get("nccl") if raw.get("ok") else None
        if not isinstance(nccl, dict) or not nccl.get("armed"):
            return None
        raw_peaks = nccl.get("peak_busbw_gbps") or {}
        code = _SHORT[collective]
        # Current rank-0 files include a collective marker; older files rely on the
        # table-header fallback and therefore land under ``ar`` until the wrapper
        # supplies their exact collective. Accept either, but never a metric from a
        # different explicitly-labelled collective.
        peak = raw_peaks.get(code)
        if peak is None and set(raw_peaks) == {"ar"}:
            peak = raw_peaks.get("ar")
        raw_avg = nccl.get("avg_bus_bandwidth") or []
        raw_oob = nccl.get("out_of_bounds") or []
        if not isinstance(peak, (int, float)) or peak <= 0:
            return None
        if not raw_avg or not all(isinstance(value, (int, float)) and value > 0 for value in raw_avg):
            return None
        try:
            if not raw_oob or any(int(str(value), 10) != 0 for value in raw_oob):
                return None
        except ValueError:
            return None
        if int(nccl.get("sweep_blocks_concluded") or 0) < 1:
            return None
        peaks[code] = float(peak)
        oob.append("0")
        avg.extend(float(value) for value in raw_avg)
        series = list((nccl.get("series_by_collective") or {}).get(collective) or [])
        if not series and collective == "all_reduce":
            series = list(nccl.get("all_reduce_series") or [])
        series_by[collective] = series
        if collective == "all_reduce":
            ar_series = series
    return {
        "peak_busbw_gbps": peaks,
        # Legacy parser consumes AR/AA/AG in that order from this field.
        "out_of_bounds": ["0", "0", "0"],
        "avg_bus_bandwidth": avg,
        "sweep_blocks_started": len(_FIVE_COLLECTIVES),
        "sweep_blocks_concluded": len(_FIVE_COLLECTIVES),
        "sweep_blocks_observed": len(_FIVE_COLLECTIVES),
        "sweep_expected_tests": len(_FIVE_COLLECTIVES),
        "all_reduce_series": ar_series,
        "series_by_collective": series_by,
        "armed": True,
        "five_collective_wrapper_verified": True,
    }


def scan(
    path: str, *, inventory: bool = True, follow_five_wrapper: bool = True
) -> dict[str, Any]:
    """One pass over *path*. Returns every metric the shell extractors produced.

    The loop is deliberately flat — at eight million iterations a per-line function call is real
    money — so the matching order below is by expected frequency, not by tidiness.
    """
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return {"path": path, "ok": False, "read_error": str(exc), "scan_version": SCAN_VERSION}

    kinds_seen: set[str] = set()
    head: list[str] = []
    lines = 0

    # --- NCCL state ---------------------------------------------------------------------------
    armed_marker = False           # saw a "Collective test starting:" line
    armed_header = False           # saw the nccl-tests table header
    cur_coll: str | None = None    # collective of the block we are inside
    blocks: list[_NcclBlock] = []
    block: _NcclBlock | None = None
    peak_size: dict[str, int] = {}
    peak_bw: dict[str, float] = {}
    started = concluded = 0
    expected_tests = 0
    oob: list[str] = []
    avg_bw: list[float] = []
    # First all_reduce block, kept separately: the legacy chart series is defined as that block.
    ar_first: list[tuple[int, float]] = []
    ar_first_done = False
    five_job: str | None = None
    five_end: tuple[str, int, int] | None = None
    five_tests: dict[str, tuple[int, int, str, str]] = {}

    # --- NeMo state ---------------------------------------------------------------------------
    dialect: str | None = None
    win_steps: list[float] = []    # first MAX_WINDOW_SAMPLES step times
    win_tflops: list[float] = []
    n_samples = 0                  # total samples seen (unbounded count, bounded storage)
    step_series: list[float] = []
    last_iter: tuple[int, int] | None = None
    declared_max_steps: int | None = None
    slurm_fail = False
    py_fatal = False
    geom: dict[str, str] = {}
    first_ts: str | None = None
    last_ts: str | None = None

    kinds_pending = len(_KIND_PATTERNS) if inventory else 0

    with open(path, "rb", buffering=1 << 20) as fh:
        for raw in fh:
            lines += 1
            if inventory and lines <= INV_HEAD_LINES:
                head.append(_clean(raw))

            chatter = _CHATTER in raw

            # The reviewed five-collective launcher keeps its compact wrapper separate from
            # rank-0 raw performance tables. Capture only its exact structured contract here;
            # validation and raw-file parsing happen after the wrapper stream completes.
            if follow_five_wrapper and (
                b"FIVE_COLLECTIVE_SWEEP_" in raw or raw.startswith(b"TEST_END ")
            ):
                match = _FIVE_START.match(raw)
                if match:
                    five_job = match.group(1).decode("ascii")
                else:
                    match = _FIVE_END.match(raw)
                    if match:
                        five_end = (
                            match.group(1).decode("ascii"),
                            int(match.group(2)),
                            int(match.group(3)),
                        )
                    else:
                        match = _FIVE_TEST_END.match(raw)
                        if match:
                            collective = match.group(2).decode("ascii")
                            record = (
                                int(match.group(1)),
                                int(match.group(3)),
                                match.group(4).decode("ascii", "replace"),
                                match.group(5).decode("utf-8", "replace"),
                            )
                            # Duplicate conflicting declarations invalidate the contract.
                            if collective in five_tests and five_tests[collective] != record:
                                five_tests[collective] = (-1, -1, "CONFLICT", "")
                            else:
                                five_tests[collective] = record

            # Kinds only ever need to fire once each, so stop testing a pattern once it has, and
            # stop entirely when all have. Chatter is excluded by design: "NCCL INFO" is debug
            # output, not a result, and treating it as evidence put all-dash NCCL rows on every
            # NeMo log (see the "nccl" pattern).
            if kinds_pending and not chatter:
                for name, pat in _KIND_PATTERNS:
                    if name not in kinds_seen and pat.search(raw):
                        kinds_seen.add(name)
                        kinds_pending -= 1

            # Inside a sweep, table rows outnumber markers, so test them first.
            if not chatter and (armed_marker or armed_header) and _DATA_ROW.match(raw):
                fields = raw.split()
                if len(fields) >= 9 and fields[0].isdigit():
                    bw = _f(fields[7])
                    if bw is not None:
                        sz = int(fields[0])
                        # Unmarked logs never reach here (see ARMING in nccl_parse.inc.sh): nine
                        # numeric fields are as likely to be an NCCL_DEBUG channel list as a row.
                        coll = cur_coll if armed_marker else "all_reduce"
                        if block is None or block.coll != coll:
                            if len(blocks) < MAX_BLOCKS:
                                block = _NcclBlock(coll or "all_reduce")
                                blocks.append(block)
                            else:
                                block = None
                        if block is not None:
                            block.add(sz, bw)
                        # Scalar peak, defined exactly as nccl_busbw_csv does it: a running
                        # max-size per collective across the WHOLE file, ties going to the later
                        # row. That is NOT the best block — an LLMB sweep runs all_reduce several
                        # times (NVLS on/off, -G variants) and the scalar deliberately reports the
                        # last configuration at the largest size, while series_by_collective below
                        # reports the best. Taking the best here read ~3 GB/s high on nine logs.
                        code = _SHORT.get(coll or "") if armed_marker else "ar"
                        if code and sz >= peak_size.get(code, -1):
                            peak_size[code] = sz
                            peak_bw[code] = bw
                        if (
                            coll == "all_reduce"
                            and not ar_first_done
                            and len(ar_first) < MAX_ROWS_PER_BLOCK
                        ):
                            ar_first.append((sz, bw))
                        continue

            # Literal prescreen: one C-level pass that rejects everything uninteresting. Applied
            # to chatter too, so a spliced record is still found.
            if not _METRIC.search(raw):
                continue

            # --- NCCL markers ----------------------------------------------------------------
            m = _COLL_START.match(raw)
            if m:
                started += 1
                armed_marker = True
                cur_coll = m.group(1).decode("ascii", "replace")
                block = None
                continue
            if _COLL_END.match(raw):
                concluded += 1
                if cur_coll == "all_reduce" and ar_first:
                    ar_first_done = True
                cur_coll = None
                block = None
                continue
            if b"out-of-place" in raw or (raw.startswith(b"#") and b"busbw" in raw):
                armed_header = True
            m = _AVG_BW.search(raw)
            if m:
                v = _f(m.group(1))
                if v is not None and len(avg_bw) < MAX_OOB:
                    avg_bw.append(v)
                continue
            m = _OOB.search(raw)
            if m:
                if len(oob) < MAX_OOB:
                    oob.append(m.group(1).decode("ascii", "replace"))
                continue
            if raw.startswith(b"# job="):
                mt = _TESTS.search(raw)
                if mt:
                    expected_tests = max(expected_tests, int(mt.group(1)))
                continue

            # --- NeMo ------------------------------------------------------------------------
            m = _STEP_TIME.search(raw)
            if m:
                mt = _STEP_TFLOP.search(raw)
                if mt:
                    st, tf = _f(m.group(1)), _f(mt.group(1))
                    if st is not None and tf is not None:
                        dialect = dialect or "megatron_bridge"
                        n_samples += 1
                        if len(win_steps) < MAX_WINDOW_SAMPLES:
                            win_steps.append(st)
                            win_tflops.append(tf)
                        if len(step_series) < MAX_STEP_SERIES:
                            step_series.append(st)
                        continue
            m = _TRAIN_STEP.search(raw)
            if m and b"consumed_samples" in raw:
                st = _f(m.group(1))
                mt = _TRAIN_TFLOP.search(raw)
                tf = _f(mt.group(1)) if mt else None
                if st is not None:
                    dialect = dialect or "nemo2"
                    n_samples += 1
                    if len(win_steps) < MAX_WINDOW_SAMPLES:
                        win_steps.append(st)
                        win_tflops.append(tf if tf is not None else 0.0)
                    if len(step_series) < MAX_STEP_SERIES:
                        step_series.append(st)
                    continue
            m = _ITER.search(raw)
            if m:
                last_iter = (int(m.group(1)), int(m.group(2)))
            if declared_max_steps is None:
                mm = _MAX_STEPS.search(raw)
                if mm:
                    declared_max_steps = int(mm.group(1))

            # --- geometry and wall clock -----------------------------------------------------
            for key, pat in _GEOM:
                gm = pat.search(raw)
                if gm:
                    # Last wins, matching _last_regex_int: a log can echo the command twice.
                    geom[key] = gm.group(1).decode("ascii", "replace")
            if _LLM_MARK.search(raw):
                ts = _TS.search(raw)
                if ts:
                    stamp = ts.group(1).decode("ascii", "replace")
                    if first_ts is None:
                        first_ts = stamp
                    last_ts = stamp
            if not slurm_fail and _SLURM_FAIL.search(raw):
                slurm_fail = True
            if not py_fatal and _PY_FATAL.search(raw):
                py_fatal = True

    # --- derive ---------------------------------------------------------------------------------
    peaks = dict(peak_bw)

    # Per-collective series: the block with the best peak, i.e. the best achieved configuration
    # (an LLMB sweep runs each collective several times with different NVLS/-G settings).
    best: dict[str, _NcclBlock] = {}
    for b in blocks:
        if b.rows and (b.coll not in best or b.max_bw > best[b.coll].max_bw):
            best[b.coll] = b
    series_by_coll = {
        c: [{"size_bytes": s, "bus_bw_gbps": w} for s, w in b.rows] for c, b in best.items()
    }

    result: dict[str, Any] = {
        "path": path,
        "ok": True,
        "scan_version": SCAN_VERSION,
        "bytes": size,
        "lines": lines,
        "nccl": {
            "peak_busbw_gbps": peaks,
            "out_of_bounds": oob,
            "avg_bus_bandwidth": avg_bw,
            "sweep_blocks_started": max(started, expected_tests),
            "sweep_blocks_concluded": concluded,
            "sweep_blocks_observed": started,
            "sweep_expected_tests": expected_tests,
            "all_reduce_series": [{"size_bytes": s, "bus_bw_gbps": w} for s, w in ar_first],
            "series_by_collective": series_by_coll,
            "armed": armed_marker or armed_header,
        },
        "nemo": _nemo_summary(
            dialect, win_steps, win_tflops, n_samples, step_series, last_iter, declared_max_steps
        ),
        "failures": {"slurm_hard_fail": slurm_fail, "python_fatal": py_fatal},
        "geometry": geom,
        # Whole-file wall clock of the training phase, for the Grafana time window.
        "wall": {"first": first_ts, "last": last_ts},
    }
    if inventory:
        result["inventory"] = {
            "kinds": [k for k in KIND_ORDER if k in kinds_seen],
            "head": head,
            "tail": _tail_lines(path, size, INV_TAIL_LINES),
        }
    if follow_five_wrapper and five_job:
        verified = _five_collective_metrics(path, five_job, five_end, five_tests)
        result["five_collective_wrapper"] = {
            "job_id": five_job,
            "verified": verified is not None,
            "tests_declared": len(five_tests),
        }
        if verified is not None:
            result["nccl"] = verified
            if inventory and "nccl" not in result["inventory"]["kinds"]:
                result["inventory"]["kinds"].append("nccl")
    return result


def _nemo_summary(
    dialect: str | None,
    win_steps: list[float],
    win_tflops: list[float],
    n_samples: int,
    step_series: list[float],
    last_iter: tuple[int, int] | None,
    declared_max_steps: int | None,
) -> dict[str, Any]:
    """Average the sample window and report completion.

    Window (unchanged from _llm_stats_awk so historical numbers stay comparable):
      >= 70 samples -> samples 70-89, the legacy window.
      <  70 samples -> skip a proportional warmup and average the rest, with at least
                       MIN_SAMPLES left. A fixed offset of 70 lands past the end of a 50-step
                       benchmark, which used to yield no metrics at all.
    """
    min_iter, max_iter, warmup_frac, min_samples = 70, 90, 0.2, 5
    out: dict[str, Any] = {
        "dialect": dialect,
        "samples": n_samples,
        "step_time_s": None,
        "tflops_per_gpu": None,
        "step_time_std": None,
        "perf_std": None,
        "mfu_percent": None,
        "step_series": step_series,
        "iteration": list(last_iter) if last_iter else None,
        "declared_max_steps": declared_max_steps,
        # Reaching the target the run DECLARED is what finished means. A 50-step benchmark and a
        # 2010-step soak are both real runs; NEMO_TARGET_STEPS is an opt-in acceptance floor.
        "finished": False,
    }
    if last_iter and last_iter[1] > 0 and last_iter[0] >= last_iter[1]:
        try:
            floor = int(os.environ.get("NEMO_TARGET_STEPS", "0"))
        except ValueError:
            floor = 0
        out["finished"] = last_iter[1] >= floor

    n = n_samples
    if n < 1:
        return out
    if n >= min_iter:
        lo, hi = min_iter, min(max_iter - 1, len(win_steps))
    else:
        lo, hi = int(n * warmup_frac) + 1, n
        if hi - lo + 1 < min_samples:
            return out
    if lo > len(win_steps):
        return out
    hi = min(hi, len(win_steps))

    t, p = Welford(), Welford()
    for i in range(lo - 1, hi):
        t.add(win_steps[i])
        p.add(win_tflops[i])
    if t.n < 1:
        return out

    try:
        peak = float(os.environ.get("NVIS_LLM_PEAK_TFLOPS", "5000"))
    except ValueError:
        peak = 5000.0
    out["step_time_s"] = round(t.mean, 2)
    out["tflops_per_gpu"] = round(p.mean, 2)
    out["step_time_std"] = round(t.stdev, 5)
    out["perf_std"] = round(p.stdev, 5)
    out["mfu_percent"] = round(p.mean / peak * 100, 1) if peak else None
    return out


def _write_series(dirpath: str, rec: dict[str, Any]) -> None:
    """Series for one file, named by a hash of its path so bash can find them without quoting.

    Two files: ``<key>.ar`` (the first all_reduce block, the legacy chart series) and
    ``<key>.steps`` (per-iteration step times).
    """
    key = series_key(rec["path"])
    os.makedirs(dirpath, exist_ok=True)
    with open(os.path.join(dirpath, key + ".ar"), "w", encoding="utf-8") as fh:
        fh.writelines(
            f"{p['size_bytes']}\t{p['bus_bw_gbps']}\n" for p in rec["nccl"]["all_reduce_series"]
        )
    with open(os.path.join(dirpath, key + ".steps"), "w", encoding="utf-8") as fh:
        # Iterations 11.. mirror llm_step_series_append's it_min: the first ten are warmup.
        fh.writelines(
            f"{i}\t{st}\n"
            for i, st in enumerate(rec["nemo"]["step_series"], start=1)
            if i >= 11
        )


def series_key(path: str) -> str:
    """Stable filename key for :func:`_write_series`; exposed so callers can locate the files."""
    return hashlib.sha1(path.encode("utf-8", "replace")).hexdigest()[:16]


def iter_scans(paths: list[str], *, inventory: bool = True) -> Iterator[dict[str, Any]]:
    """Yield one record per path. A generator so a caller can stream to a sink and never hold
    more than one record — the same shape the AWS DBR parser used to feed streaming_bulk."""
    for p in paths:
        yield scan(p, inventory=inventory)


#: Scalars parser.sh needs per file, in a fixed order. TSV because the caller is bash and this
#: keeps it to ONE python process for the whole batch instead of one per extractor per file.
TSV_FIELDS = [
    "path", "ar", "ag", "aa", "rs", "sr", "oob", "started", "concluded",
    "status", "step_s", "tflops", "sstd", "pstd", "mfu",
    "steps_cur", "steps_tot", "slurm_fail", "py_fatal", "kinds", "lines",
]


def _tsv_row(rec: dict[str, Any]) -> str:
    """One scanner record as the TSV line parser.sh consumes. '-' means "no measurement"."""
    def s(v: Any) -> str:
        return "-" if v is None or v == "" else str(v)

    if not rec.get("ok"):
        return "\t".join([rec.get("path", "-")] + ["-"] * (len(TSV_FIELDS) - 1))

    n, c = rec["nemo"], rec["nccl"]
    peaks = c["peak_busbw_gbps"]
    # llm_label's verdict order: measured numbers, else reached-its-target, else fatal, else UNK.
    if n["step_time_s"] is not None or n["finished"]:
        status = "OK"
    elif rec["failures"]["slurm_hard_fail"] or rec["failures"]["python_fatal"]:
        status = "FAIL"
    else:
        status = "UNK"
    it = n["iteration"] or [None, None]
    return "\t".join(
        s(v)
        for v in (
            rec["path"],
            peaks.get("ar"), peaks.get("ag"), peaks.get("aa"), peaks.get("rs"), peaks.get("sr"),
            ",".join(c["out_of_bounds"]) or None,
            c["sweep_blocks_started"], c["sweep_blocks_concluded"],
            status, n["step_time_s"], n["tflops_per_gpu"], n["step_time_std"], n["perf_std"],
            n["mfu_percent"], it[0], it[1],
            1 if rec["failures"]["slurm_hard_fail"] else 0,
            1 if rec["failures"]["python_fatal"] else 0,
            ",".join(rec.get("inventory", {}).get("kinds", [])) or None,
            rec["lines"],
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Single-pass, bounded-memory validation log scanner.")
    ap.add_argument("paths", nargs="*")
    # A directory scan is capped at 4000 files, and 1020 paths already make a ~150 KB argv.
    # Reading the list from a file keeps a full-fleet batch clear of ARG_MAX.
    ap.add_argument("--paths-from", help="file with one log path per line ('-' for stdin)")
    ap.add_argument("--ndjson", action="store_true", help="one JSON record per line (streaming)")
    ap.add_argument("--tsv", action="store_true", help="one TSV scalar row per file (for parser.sh)")
    ap.add_argument("--series-dir", help="with --tsv, write per-file series TSVs into this dir")
    ap.add_argument("--no-inventory", action="store_true", help="skip kind/head/tail capture")
    args = ap.parse_args(argv)

    paths = list(args.paths)
    if args.paths_from == "-":
        paths.extend(ln.strip() for ln in sys.stdin if ln.strip())
    elif args.paths_from:
        with open(args.paths_from, encoding="utf-8") as fh:
            paths.extend(ln.strip() for ln in fh if ln.strip())
    if not paths:
        ap.error("no paths given (positional args or --paths-from)")
    args.paths = paths

    inv = not args.no_inventory
    if args.tsv:
        for rec in iter_scans(args.paths, inventory=inv):
            sys.stdout.write(_tsv_row(rec) + "\n")
            if args.series_dir and rec.get("ok"):
                _write_series(args.series_dir, rec)
        sys.stdout.flush()
        return 0
    if args.ndjson:
        for rec in iter_scans(args.paths, inventory=inv):
            sys.stdout.write(json.dumps(rec) + "\n")
            sys.stdout.flush()
        return 0
    json.dump(
        {"scan_version": SCAN_VERSION, "results": list(iter_scans(args.paths, inventory=inv))},
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
