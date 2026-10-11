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
"""NCCL row-matching must require an nccl-tests table, not just nine numeric fields.

The bug these tests pin (Nscale TX, 2026-08-03): a NeMo training log run with
``NCCL_DEBUG=INFO`` prints channel rank lists, and when 36 ranks interleave, the wrap
continuation lands on its own line as a bare numeric run. That satisfied the old row shape
(``NF>=9``, numeric ``$1``, numeric ``$8``) and, because no collective marker had been seen,
every such line was attributed to all_reduce -- so the dashboard charted size=23 bytes at
30 GB/s as a real acceptance result on every one of 36 NEMO logs.

Both parsers are covered because both had the flaw and both are load-bearing:
``bin/scripts/nccl_parse.inc.sh`` feeds the parser-results dialog, and
``backend/universal_parser.py`` feeds analytics history and bring-up.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INC = REPO_ROOT / "bin" / "scripts" / "nccl_parse.inc.sh"

sys.path.insert(0, str(REPO_ROOT / "backend"))
import universal_parser as up  # noqa: E402

# A real sweep block: "starting:" marker, the two header lines, data rows, footers.
NCCL_SWEEP = """\
# nThread 1 nGpus 1 minBytes 1048576 maxBytes 17179869184
# Collective test starting: all_reduce_perf_mpi
#                                                              out-of-place                       in-place
#       size         count      type   redop    root     time   algbw   busbw  #wrong     time   algbw   busbw  #wrong
#        (B)    (elements)
     1048576        262144     float     sum      -1    50.00   20.97   20.00       0    49.00   21.40   20.40      0
 17179869184     4294967296     float     sum      -1  24565.9  699.34  689.63       0  24558.4  699.55  689.84      0
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 354.81
# Collective test concluded: all_reduce_perf_mpi
"""

# The standalone nccl-tests launcher: NO "starting:" line, but it still prints the header.
# This is why the gate is the header and not the marker -- this dialect must keep working.
NCCL_SWEEP_NO_MARKER = """\
# nThread 1 nGpus 1 minBytes 1048576 maxBytes 1073741824
#                                                              out-of-place                       in-place
#       size         count      type   redop    root     time   algbw   busbw  #wrong     time   algbw   busbw  #wrong
     1048576        262144     float     sum      -1    50.00   20.97   20.00       0    49.00   21.40   20.40      0
  1073741824      268435456     float     sum      -1  1888.74  568.50  560.60       0  1895.80  566.38  558.51      0
# Out of bounds values : 0 OK
# Avg bus bandwidth    : 290.30
"""

# A NeMo training log with NCCL_DEBUG=INFO. The indented bare integer run is a wrapped
# "Channel NN/32 :" rank list -- 13 numeric fields, $1=23, $8=30.
NEMO_WITH_NCCL_DEBUG = """\
+ export PYTHONUNBUFFERED=1
node-11:123:123 [0] NCCL INFO comm 0xe17c3220 rank 31 nRanks 36 nNodes 1 MNNVL 1
node-11:123:123 [0] NCCL INFO Channel 05/32 :  0  1  2  3  4  5  6  7  8  9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35
 23 24 25 26 27 28 29 30 31 32 33 34 35
node-12:456:456 [1] NCCL INFO P2P Chunksize set to 524288
 23 24 25 26 27 28 29 30 31 32 33 34 35
[NeMo I] train_step_timing in s: 6.71 consumed_samples: 216
[NeMo I] train_step_timing in s: 6.69 consumed_samples: 432
"""

# Pre-llmb combined format: one job, NCCL sweep then NeMo training in a single file.
COMBINED = NCCL_SWEEP + NEMO_WITH_NCCL_DEBUG


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def _sh(func: str, path: Path, *args: str) -> str:
    """Call one shell helper from nccl_parse.inc.sh and return its stdout."""
    quoted = " ".join([f'"{a}"' for a in (str(path), *args)])
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; {func} {quoted}'],
        capture_output=True,
        text=True,
        timeout=60,
    check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# --- the negative test: a NeMo log is not an NCCL result ----------------------------------


def test_shell_yields_nothing_from_a_nemo_log(tmp_path):
    log = _write(tmp_path, "cmods-NEMO-1Rack-r01-18N-114257.txt", NEMO_WITH_NCCL_DEBUG)
    assert _sh("nccl_busbw_csv", log).strip() == ",,", "no peak may be reported"
    assert _sh("nccl_allreduce_series_append", log, "r01", "114257").strip() == ""
    assert _sh("nccl_series_by_collective_append", log, "r01", "114257").strip() == ""
    assert _sh("nccl_busbw_ext_csv", log).strip() == ","


def test_python_yields_nothing_from_a_nemo_log(tmp_path):
    bag = up._MetricBag()
    out = up._extract_nccl(NEMO_WITH_NCCL_DEBUG, bag)
    assert out.get("headline") is None
    assert out.get("extra", {}).get("nccl_collectives") == []
    assert not out.get("status_hint_pass")


def test_the_phantom_row_shape_is_present_in_the_fixture():
    """Guard the fixture itself: if this stops matching, the tests above prove nothing."""
    hits = 0
    for line in NEMO_WITH_NCCL_DEBUG.splitlines():
        f = line.split()
        if len(f) >= 9 and f[0].isdigit():
            try:
                int(f[0]), float(f[7])
            except ValueError:
                continue
            hits += 1
    assert hits >= 2, "fixture must still contain lines the old matcher would have accepted"


# --- positive tests: real sweeps still parse ----------------------------------------------


def test_shell_parses_a_real_sweep(tmp_path):
    log = _write(tmp_path, "cmods-NCCL-1Rack-r01-18N-114256.txt", NCCL_SWEEP)
    # Peak is taken at the largest message size, so 689.63 (17 GiB row), not 20.00.
    assert _sh("nccl_busbw_csv", log).strip().startswith("689.63,")
    series = _sh("nccl_allreduce_series_append", log, "r01", "114256").strip().splitlines()
    assert len(series) == 2
    assert series[-1].split("\t")[2:] == ["17179869184", "689.63"]


def test_python_parses_a_real_sweep():
    bag = up._MetricBag()
    out = up._extract_nccl(NCCL_SWEEP, bag)
    assert out["headline"]["value"] == 689.63
    assert out["extra"]["nccl_collectives"] == ["all_reduce"]


def test_marker_less_launcher_still_parses(tmp_path):
    """The header gate must not break the standalone dialect, which prints no marker."""
    log = _write(tmp_path, "cmods-NCCL-1Rack-r01-18N-1.txt", NCCL_SWEEP_NO_MARKER)
    assert _sh("nccl_busbw_csv", log).strip().startswith("560.60,")
    series = _sh("nccl_allreduce_series_append", log, "r01", "1").strip().splitlines()
    assert len(series) == 2

    bag = up._MetricBag()
    out = up._extract_nccl(NCCL_SWEEP_NO_MARKER, bag)
    assert out["headline"]["value"] == 560.60


def test_combined_log_still_yields_the_nccl_half(tmp_path):
    """Pre-llmb one-file format: the NeMo half must not suppress the NCCL half."""
    log = _write(tmp_path, "root-NEMO-1Rack-bos01-b05-18N-294.txt", COMBINED)
    assert _sh("nccl_busbw_csv", log).strip().startswith("689.63,")

    bag = up._MetricBag()
    out = up._extract_nccl(COMBINED, bag)
    assert out["headline"]["value"] == 689.63


def test_out_of_bounds_is_still_reported():
    bag = up._MetricBag()
    out = up._extract_nccl(NCCL_SWEEP.replace("Out of bounds values : 0 OK",
                                              "Out of bounds values : 5 all_reduce"), bag)
    assert out.get("status") == "fail"
    assert out.get("errors")


def test_llmb_manifest_missing_tests_forces_failure():
    text = (
        "# NVIS: aggregated from llmb-run experiment x_133460\n"
        "# job=133460 rack=ink50-dh01-i01 nodes=18 tests=24\n"
        + NCCL_SWEEP * 22
    )
    bag = up._MetricBag()
    out = up._extract_nccl(text, bag)
    assert out["status"] == "fail"
    assert out["extra"]["nccl_sweeps_expected"] == 24
    assert out["extra"]["nccl_sweeps_concluded"] == 22
    assert any(e["kind"] == "nccl_incomplete_sweep" for e in out["errors"])


def test_complete_llmb_manifest_still_pass_hints():
    text = "# job=7 rack=r01 nodes=18 tests=1\n" + NCCL_SWEEP
    bag = up._MetricBag()
    out = up._extract_nccl(text, bag)
    assert out.get("status") is None
    assert out["status_hint_pass"] is True


@pytest.mark.parametrize("text", [NCCL_SWEEP, NCCL_SWEEP_NO_MARKER, COMBINED])
def test_sniff_kind_classifies_real_sweeps_as_nccl(text):
    assert up.sniff_kind("whatever.txt", text) == "nccl"


def test_sniff_kind_does_not_call_a_nemo_log_nccl():
    """universal_parser never matched bare 'NCCL INFO'; keep it that way."""
    assert up.sniff_kind("cmods-NEMO-1Rack-r01-18N-1.txt", NEMO_WITH_NCCL_DEBUG) == "nemo"


# --- completion: an armed table is not a finished test ------------------------------------
#
# Nscale NC multirack job 138174 (2026-10-10): host mpirun, NCCL_DEBUG=INFO, aborted on
# "Test NCCL failure ... remote process exited or there was a network error" after one row. A
# topology line of bare rank numbers (" 56 55 54 ...") was read as all_reduce busbw=49, and the
# Multirack table called the failed run done. A test's rows now count only once nccl-tests ends it
# with "# Avg bus bandwidth"; a failure line in a test fails that collective for the file. All three
# parsers must agree, so every case below is checked against each.

sys.path.insert(0, str(REPO_ROOT / "bin" / "scripts"))
import nvis_logscan as logscan  # noqa: E402

_HDR = """\
#                                                              out-of-place                       in-place
#       size         count      type   redop    root     time   algbw   busbw  #wrong     time   algbw   busbw  #wrong
"""
_END = "# Out of bounds values : 0 OK\n# Avg bus bandwidth    : 300.00\n"


def _row(size: int, bw: float) -> str:
    return f"{size:>12} {size // 4:>13}     float     sum      -1   100.00   {bw:.2f}   {bw:.2f}       0   100.00   {bw:.2f}   {bw:.2f}       0\n"


ABORTED_MULTIRACK = (
    "nccl_allreduce: nodes=72 np=288 gpus/node=4 sizes=8..16G rounds=5\n"
    "===== nccl_allreduce round 1/5 =====\n" + _HDR + _row(8, 0.0)
    + "  56  55  54  53  52  51  50  49  48  47  46  45\n"
    + "sys1-gso1-p-phy-gpu75-su19: Test NCCL failure all_reduce.cu:507 'remote process exited or there was a network error / '\n"
    + "nccl_allreduce: round 1/5 FAILED rc=3 (stopping)\n"
)
SECOND_ROUND_FAILS = (
    "===== nccl_allreduce round 1/2 =====\n" + _HDR + _row(8, 0.0) + _row(1 << 34, 798.53) + _END
    + "===== nccl_allreduce round 2/2 =====\n" + _HDR + _row(8, 0.0)
    + "sys1-gso1-p-phy-gpu24-su19: Test NCCL failure common.cu:528 'remote process exited or there was a network error / '\n"
)
TWO_CLEAN_ROUNDS = (_HDR + _row(8, 0.0) + _row(1 << 34, 798.53) + _END) * 2


def _marked(coll: str, body: str, *, done: bool = True) -> str:
    return (f"# Collective test starting: {coll}_perf_mpi\n" + _HDR + body
            + (_END if done else "") + f"# Collective test concluded: {coll}_perf_mpi\n")


# alltoall ran three times: twice clean, once the GPU was not ready (cmods-NCCL-1Rack-X2-1N-126266).
LLMB_ONE_ALLTOALL_FAILED = (
    _marked("all_reduce", _row(1 << 30, 480.0))
    + _marked("alltoall", _row(1 << 30, 580.0))
    + _marked("alltoall", "sys1-gso1-p-phy-gpu12-su32: Test CUDA failure util.cu:555 'system not yet initialized'\n", done=False)
    + _marked("alltoall", _row(1 << 30, 581.0))
    + _marked("reduce_scatter", _row(1 << 30, 300.0))
    + _marked("sendrecv", "sys1-gso1-p-phy-gpu12-su32: Test CUDA failure util.cu:555 'system not yet initialized'\n", done=False)
)
# A failure line between two tests of a marked log belongs to no collective.
FAILURE_BETWEEN_TESTS = (
    _marked("all_reduce", _row(1 << 30, 480.0))
    + "sys1-gso1-p-phy-gpu12-su32: Test failure common.cu:1187\n"
    + _marked("alltoall", _row(1 << 30, 580.0))
)


def _three(tmp_path: Path, text: str) -> tuple[str, dict, dict]:
    """(shell "ar,aa,ag", logscan peaks, universal_parser peaks) for one log text."""
    log = _write(tmp_path, "nccl-mpi_U20+U21+U22+U23_138174.out", text)
    shell = _sh("nccl_busbw_csv", log).strip()
    scan = logscan.scan(str(log), inventory=False)["nccl"]["peak_busbw_gbps"]
    bag = up._MetricBag()
    up._extract_nccl(text, bag)
    py = {m["labels"]["collective"]: m["value"] for m in bag.as_list() if m["name"] == "nccl_busbw_peak"}
    return shell, scan, py


def test_an_aborted_run_reports_no_bandwidth(tmp_path):
    shell, scan, py = _three(tmp_path, ABORTED_MULTIRACK)
    assert shell == ",,"
    assert scan == {}
    assert py == {}


def test_a_later_round_that_fails_fails_the_collective(tmp_path):
    shell, scan, py = _three(tmp_path, SECOND_ROUND_FAILS)
    assert shell == ",,"
    assert "ar" not in scan
    assert "all_reduce" not in py


def test_clean_rounds_still_report_their_peak(tmp_path):
    shell, scan, py = _three(tmp_path, TWO_CLEAN_ROUNDS)
    assert shell == "798.53,,"
    assert scan == {"ar": 798.53}
    assert py == {"all_reduce": 798.53}


def test_one_failed_iteration_fails_only_its_collective(tmp_path):
    shell, scan, py = _three(tmp_path, LLMB_ONE_ALLTOALL_FAILED)
    assert shell == "480.00,,"
    assert scan.get("ar") == 480.0 and "aa" not in scan
    assert scan.get("rs") == 300.0 and "sr" not in scan
    assert py.get("all_reduce") == 480.0 and "alltoall" not in py
    log = _write(tmp_path, "x.txt", LLMB_ONE_ALLTOALL_FAILED)
    assert _sh("nccl_busbw_ext_csv", log).strip() == "300.00,"


def test_a_failure_between_tests_fails_neither_neighbour(tmp_path):
    shell, scan, py = _three(tmp_path, FAILURE_BETWEEN_TESTS)
    assert shell == "480.00,580.00,"
    assert scan == {"ar": 480.0, "aa": 580.0}
    assert py == {"all_reduce": 480.0, "alltoall": 580.0}
