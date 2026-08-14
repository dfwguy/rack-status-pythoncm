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
"""nvis_logscan: one streaming pass must extract what sixteen grep/awk passes did.

The scanner exists because the old path both re-read the file per extractor and SAMPLED it:
parser.sh classified anything over 16 MB from a 2 MB head + 2 MB tail window, and
universal_parser read 2 MB + 1 MB. In a 576-node run the interesting lines sit at 98% depth, so
the middle of the file — where the answer lives — was never looked at.

These tests pin three things: parity with the shell extractors, memory that does not grow with
the file, and the arming rules that keep NCCL_DEBUG chatter out of the bandwidth numbers.
"""
from __future__ import annotations

import importlib.util
import os
import tracemalloc
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER = REPO_ROOT / "bin" / "scripts" / "nvis_logscan.py"

_spec = importlib.util.spec_from_file_location("nvis_logscan", SCANNER)
assert _spec and _spec.loader
logscan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logscan)


HDR = (
    "#       size         count      type   redop    root     time   algbw   busbw  #wrong"
    "     time   algbw   busbw  #wrong"
)


def _row(size: int, busbw: float) -> str:
    return (
        f"   {size:>11} {size // 4:>13}     float     sum      -1   100.00    0.00 "
        f"{busbw:>7.2f}       0   100.00    0.00 {busbw:>7.2f}       0"
    )


def _block(coll: str, sizes: list[tuple[int, float]], *, concluded: bool = True) -> list[str]:
    out = [f"# Collective test starting: {coll}_perf_mpi", HDR]
    out += [_row(s, b) for s, b in sizes]
    if concluded:
        out += [
            "# Out of bounds values : 0 OK",
            "# Avg bus bandwidth    : 123.45",
            f"# Collective test concluded: {coll}_perf_mpi",
        ]
    return out


def _write(tmp_path: Path, lines: list[str], name: str = "cmods-NCCL-1Rack-p05-r01-18N-42.txt") -> str:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


# --- NCCL ------------------------------------------------------------------------------------


def test_peak_busbw_per_collective(tmp_path):
    lines = (
        _block("all_reduce", [(8, 0.0), (1 << 30, 700.0)])
        + _block("alltoall", [(8, 0.0), (1 << 30, 1805.0)])
        + _block("all_gather", [(8, 0.0), (1 << 30, 248.9)])
    )
    r = logscan.scan(_write(tmp_path, lines))
    assert r["nccl"]["peak_busbw_gbps"] == {"ar": 700.0, "aa": 1805.0, "ag": 248.9}


def test_peak_is_taken_at_the_largest_message_size_not_the_highest_row(tmp_path):
    """Definition is out-of-place busbw AT THE LARGEST SIZE, not the max over the sweep."""
    lines = _block("all_reduce", [(8, 999.0), (1 << 30, 700.0)])
    r = logscan.scan(_write(tmp_path, lines))
    assert r["nccl"]["peak_busbw_gbps"]["ar"] == 700.0


def test_an_nccl_debug_line_is_never_read_as_a_bandwidth_row(tmp_path):
    """The job 140457 regression, and the reason the dashboard showed alltoall = 1805 GB/s.

    ``... 4 [1] NCCL INFO comm 0x... rank 1805 nranks 2304 ...`` has nine-plus fields, a numeric
    first field and a numeric eighth, so awk inside an armed alltoall block read size=4,
    busbw=1805 and charted a RANK NUMBER as bandwidth. The real alltoall block produced no rows
    at all, so the honest answer is "no measurement".
    """
    lines = ["# Collective test starting: alltoall_perf_mpi", HDR]
    lines.append(
        "4 [1] NCCL INFO comm 0xaaaaaf1e73e0 rank 1805 nranks 2304 cudaDev 1 "
        "busId 906000 - Destroy COMPLETE"
    )
    r = logscan.scan(_write(tmp_path, lines))
    assert "aa" not in r["nccl"]["peak_busbw_gbps"], "a rank number is not a bandwidth"


def test_rows_without_a_marker_or_header_are_not_armed(tmp_path):
    """A NeMo log with NCCL_DEBUG=INFO prints channel lists that look like sweep rows."""
    lines = [
        "some preamble",
        "   23 24 25 26 27 28 29 30 31 32 33 34 35",
        _row(1 << 30, 812.0),
    ]
    r = logscan.scan(_write(tmp_path, lines, name="cmods-NEMO-1Rack-p05-r01-18N-7.txt"))
    assert r["nccl"]["peak_busbw_gbps"] == {}
    assert r["nccl"]["armed"] is False


def test_a_bare_table_header_arms_the_marker_less_launcher(tmp_path):
    """The standalone nccl-tests launcher prints no "starting:" line, only the header."""
    lines = [HDR, _row(8, 0.0), _row(1 << 30, 812.0)]
    r = logscan.scan(_write(tmp_path, lines))
    assert r["nccl"]["armed"] is True
    assert r["nccl"]["peak_busbw_gbps"]["ar"] == 812.0


def test_sweep_counts_and_a_missing_conclusion(tmp_path):
    lines = _block("all_reduce", [(8, 1.0)]) + _block("alltoall", [(8, 2.0)], concluded=False)
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert (r["sweep_blocks_started"], r["sweep_blocks_concluded"]) == (2, 1)


def test_an_llmb_manifest_expected_count_wins_over_observed_starts(tmp_path):
    """22 blocks from a declared 24-test run is incomplete, not 22/22 complete: a test that
    failed to launch emits no markers at all."""
    lines = ["# job=133460 rack=ink50-dh01-i01 nodes=18 tests=24"]
    for _ in range(22):
        lines += _block("all_reduce", [(8, 1.0)])
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert r["sweep_blocks_started"] == 24
    assert r["sweep_blocks_observed"] == 22
    assert r["sweep_blocks_concluded"] == 22


def test_out_of_bounds_and_avg_bandwidth_are_collected(tmp_path):
    lines = _block("all_reduce", [(8, 1.0)]) + _block("all_gather", [(8, 2.0)])
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert r["out_of_bounds"] == ["0", "0"]
    assert r["avg_bus_bandwidth"] == [123.45, 123.45]


def test_series_by_collective_picks_the_best_block(tmp_path):
    """An LLMB sweep runs each collective several times (NVLS on/off, -G variants); the series
    must come from the best achieved configuration, not whichever ran first."""
    lines = (
        _block("all_reduce", [(8, 0.0), (1 << 30, 700.0)])
        + _block("all_reduce", [(8, 0.0), (1 << 30, 916.0)])
    )
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert r["series_by_collective"]["all_reduce"][-1]["bus_bw_gbps"] == 916.0


def test_the_scalar_peak_is_the_last_block_not_the_best_one(tmp_path):
    """nccl_busbw_csv keeps a running max SIZE across the whole file with ties going to the later
    row, so with several all_reduce blocks the scalar is the LAST configuration at the largest
    size. The series is the best block; the scalar is not. Reading the best here came out ~3 GB/s
    high on nine production logs.
    """
    lines = (
        _block("all_reduce", [(8, 0.0), (1 << 30, 920.77)])
        + _block("all_reduce", [(8, 0.0), (1 << 30, 917.82)])
    )
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert r["peak_busbw_gbps"]["ar"] == 917.82, "scalar follows the last block"
    assert r["series_by_collective"]["all_reduce"][-1]["bus_bw_gbps"] == 920.77, "series is best"


def test_all_reduce_series_is_the_first_block(tmp_path):
    """The legacy chart series is defined as the FIRST all_reduce block; keep that contract."""
    lines = (
        _block("all_reduce", [(8, 0.0), (1 << 30, 700.0)])
        + _block("all_reduce", [(8, 0.0), (1 << 30, 916.0)])
    )
    r = logscan.scan(_write(tmp_path, lines))["nccl"]
    assert [p["bus_bw_gbps"] for p in r["all_reduce_series"]] == [0.0, 700.0]


# --- NeMo ------------------------------------------------------------------------------------


def _steps(n: int) -> list[str]:
    """*n* Megatron-Bridge samples where the first 20% are deliberately slow warmup."""
    out = []
    for i in range(1, n + 1):
        slow = i <= n // 5
        st, tf = (9.99, 900.0) if slow else (4.22, 1954.6)
        out.append(f"Step Time : {st}s GPU utilization: {tf}MODEL_TFLOP/s/GPU")
    return out


def test_a_fifty_step_run_reports_numbers(tmp_path):
    """A short benchmark must average, not vanish: the fixed 70-89 window landed past its end."""
    r = logscan.scan(_write(tmp_path, _steps(50)))["nemo"]
    assert r["samples"] == 50
    assert r["step_time_s"] == pytest.approx(4.22, rel=1e-3), "warmup must be excluded"
    assert r["tflops_per_gpu"] == pytest.approx(1954.6, rel=1e-3)


def test_the_legacy_window_is_unchanged_for_long_runs(tmp_path):
    """>= 70 samples must still average exactly samples 70-89 so historical numbers compare."""
    lines = [f"Step Time : {i}.00s GPU utilization: {i}00.0MODEL_TFLOP/s/GPU" for i in range(1, 201)]
    r = logscan.scan(_write(tmp_path, lines))["nemo"]
    assert r["step_time_s"] == pytest.approx(sum(range(70, 90)) / 20, rel=1e-6)


def test_the_nemo2_dialect_is_understood(tmp_path):
    lines = ["x | train_step_timing in s: 4.10 | tflops: 1900.0 | consumed_samples" for _ in range(50)]
    r = logscan.scan(_write(tmp_path, lines))["nemo"]
    assert r["dialect"] == "nemo2"
    assert r["step_time_s"] == pytest.approx(4.10, rel=1e-3)
    assert r["tflops_per_gpu"] == pytest.approx(1900.0, rel=1e-3)


def test_mfu_denominator_is_overridable(tmp_path, monkeypatch):
    p = _write(tmp_path, _steps(50))
    base = logscan.scan(p)["nemo"]["mfu_percent"]
    monkeypatch.setenv("NVIS_LLM_PEAK_TFLOPS", "2500")
    halved = logscan.scan(p)["nemo"]["mfu_percent"]
    assert halved == pytest.approx(base * 2, abs=0.2)


def test_a_short_run_that_reached_its_declared_target_is_finished(tmp_path):
    """The 140457 case: 50/50 is a complete run. The old rule demanded tot >= 2010."""
    lines = _steps(50) + [" [2026-08-05] iteration       50/      50 | lm loss: 1.4E-02 |"]
    assert logscan.scan(_write(tmp_path, lines))["nemo"]["finished"] is True


def test_a_truncated_run_is_not_finished(tmp_path):
    lines = _steps(50) + [" [2026-08-05] iteration       50/    2010 | lm loss: 3.1E+00 |"]
    assert logscan.scan(_write(tmp_path, lines))["nemo"]["finished"] is False


def test_the_acceptance_floor_is_opt_in(tmp_path, monkeypatch):
    lines = _steps(50) + [" [2026-08-05] iteration       50/      50 |"]
    monkeypatch.setenv("NEMO_TARGET_STEPS", "2010")
    assert logscan.scan(_write(tmp_path, lines))["nemo"]["finished"] is False


def test_declared_max_steps_is_captured(tmp_path):
    lines = ["INFO: LLM params model=nemotronh size=56b dtype=fp8_cs gbs=6912 max_steps=50 gpu=gb300"]
    assert logscan.scan(_write(tmp_path, lines))["nemo"]["declared_max_steps"] == 50


# --- failure markers ---------------------------------------------------------------------------


def test_slurm_hard_failure_is_flagged(tmp_path):
    lines = ["srun: error: node-11: task 0: Exited with exit code 1"]
    assert logscan.scan(_write(tmp_path, lines))["failures"]["slurm_hard_fail"] is True


@pytest.mark.parametrize(
    "text",
    [
        "Traceback (most recent call last):",
        "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB",
    ],
)
def test_python_and_cuda_fatals_are_flagged(tmp_path, text):
    assert logscan.scan(_write(tmp_path, [text]))["failures"]["python_fatal"] is True


# --- inventory ----------------------------------------------------------------------------------


def test_kinds_are_classified_from_content(tmp_path):
    lines = _block("all_reduce", [(8, 1.0)]) + _steps(3)
    kinds = logscan.scan(_write(tmp_path, lines))["inventory"]["kinds"]
    assert "nccl" in kinds and "nemotron" in kinds


def test_nccl_debug_chatter_alone_is_not_an_nccl_log(tmp_path):
    """Every NeMo log carries megabytes of NCCL INFO; marking those "nccl" put all-dash rows in
    the parser tables."""
    lines = [f"node-{i}: NCCL INFO Channel 05/32 :  0  1  2  3" for i in range(50)]
    assert "nccl" not in logscan.scan(_write(tmp_path, lines))["inventory"]["kinds"]


def test_head_and_tail_are_captured(tmp_path):
    lines = [f"line {i}" for i in range(1, 1001)]
    inv = logscan.scan(_write(tmp_path, lines))["inventory"]
    assert inv["head"][0] == "line 1"
    assert inv["tail"][-1] == "line 1000"


def test_a_missing_file_reports_an_error_rather_than_raising(tmp_path):
    r = logscan.scan(str(tmp_path / "nope.txt"))
    assert r["ok"] is False and r["read_error"]


# --- the property the whole design exists for -----------------------------------------------------


def _big_log(path: Path, noise_lines: int) -> None:
    """A log dominated by NCCL_DEBUG chatter with the real result at ~98% depth."""
    noise = "node-01:1234:1234 [0] NCCL INFO Channel 05/32 :  0  1  2  3  4  5  6  7  8  9\n"
    with path.open("w", encoding="utf-8") as fh:
        cut = int(noise_lines * 0.98)
        for _ in range(cut):
            fh.write(noise)
        for line in _block("all_reduce", [(8, 0.0), (1 << 30, 916.0)]) + _steps(50):
            fh.write(line + "\n")
        for _ in range(noise_lines - cut):
            fh.write(noise)


def test_a_marker_at_98_percent_depth_is_found(tmp_path):
    """The whole point. A head+tail window would miss this, which is exactly how a 973 MB log
    parsed to "no metrics" while holding a perfectly good result."""
    p = tmp_path / "deep.txt"
    _big_log(p, 200_000)
    r = logscan.scan(str(p))
    assert r["nccl"]["peak_busbw_gbps"]["ar"] == 916.0
    assert r["nemo"]["step_time_s"] == pytest.approx(4.22, rel=1e-3)


def test_memory_does_not_grow_with_the_file(tmp_path):
    """Peak allocation must be flat in file size: bounded counters, no accumulation.

    Ten times the input for roughly the same peak. The bound is generous because pytest and the
    tracer add noise; an O(lines) implementation would blow past it by orders of magnitude.
    """
    small, big = tmp_path / "s.txt", tmp_path / "b.txt"
    _big_log(small, 20_000)
    _big_log(big, 200_000)

    def peak(p: Path) -> int:
        tracemalloc.start()
        logscan.scan(str(p))
        _, pk = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return pk

    small_peak, big_peak = peak(small), peak(big)
    assert big.stat().st_size > 9 * small.stat().st_size, "the inputs must really differ"
    assert big_peak < small_peak * 2 + 256 * 1024, (small_peak, big_peak)


def test_the_scanner_is_stdlib_only():
    """It runs on the head node, which has no virtualenv and no pip install."""
    src = SCANNER.read_text(encoding="utf-8")
    third_party = {"requests", "numpy", "pandas", "yaml", "tinydb", "flask"}
    for line in src.splitlines():
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__"):
            mod = line.split()[1].split(".")[0]
            assert mod not in third_party, line


def test_records_stream_one_at_a_time(tmp_path):
    """iter_scans is a generator so a caller can push to a sink without holding every record."""
    paths = [_write(tmp_path, _steps(5), name=f"a{i}.txt") for i in range(3)]
    it = logscan.iter_scans(paths)
    assert hasattr(it, "__next__")
    assert len([r for r in it]) == 3


def test_output_is_bounded_for_a_very_long_run(tmp_path):
    """A 5000-step run must not produce a 5000-point payload."""
    r = logscan.scan(_write(tmp_path, _steps(5000)))["nemo"]
    assert r["samples"] == 5000, "the COUNT is exact"
    assert len(r["step_series"]) <= logscan.MAX_STEP_SERIES, "the PAYLOAD is capped"


def test_cli_emits_ndjson_one_record_per_line(tmp_path, capsys):
    paths = [_write(tmp_path, _steps(5), name=f"c{i}.txt") for i in range(2)]
    assert logscan.main(["--ndjson", *paths]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 2
    import json

    assert all(json.loads(line)["ok"] for line in out)


def test_env_does_not_leak_between_scans(tmp_path, monkeypatch):
    """NEMO_TARGET_STEPS is read per scan, not cached at import."""
    lines = _steps(50) + [" [2026-08-05] iteration       50/      50 |"]
    p = _write(tmp_path, lines)
    monkeypatch.delenv("NEMO_TARGET_STEPS", raising=False)
    assert logscan.scan(p)["nemo"]["finished"] is True
    monkeypatch.setenv("NEMO_TARGET_STEPS", "100")
    assert logscan.scan(p)["nemo"]["finished"] is False


def test_interleaved_output_still_matches(tmp_path):
    """At 2304 ranks the collectives share one stdout and lines arrive spliced; job 140457
    really contains ``ink50-dh0Step Time : 105.15s...``. Matching must not anchor at BOL."""
    lines = ["ink50-dh0Step Time : 4.22s GPU utilization: 1954.6MODEL_TFLOP/s/GPU"] * 20
    r = logscan.scan(_write(tmp_path, lines))["nemo"]
    assert r["samples"] == 20
    assert r["step_time_s"] == pytest.approx(4.22, rel=1e-3)


def test_scan_reports_provenance(tmp_path):
    p = _write(tmp_path, _steps(10))
    r = logscan.scan(p)
    assert r["bytes"] == os.path.getsize(p)
    assert r["lines"] == 10
    assert r["scan_version"] == logscan.SCAN_VERSION
