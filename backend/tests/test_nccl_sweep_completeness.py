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
"""A sweep still being written must not be presented as a rack's result.

nccl-tests brackets each sweep with ``# Collective test starting:`` / ``concluded:`` and prints the
per-block summary only at the end. Parsing a log mid-run therefore yields a truncated series: on
Nscale TX job 130406, opening Parser Results while the job ran charted ONE point with a peak of
91 GB/s; the finished log charts 32 points peaking at 821. The two were indistinguishable in the UI.

``nccl_sweep_counts`` gives the parser the started/concluded counts so the dialog can say "partial"
and re-parse, instead of letting an operator read a half-written sweep as a slow rack.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "bin" / "scripts"
INC = SCRIPTS / "nccl_parse.inc.sh"

HDR = "#       size         count      type   redop    root     time   algbw   busbw  #wrong     time   algbw   busbw  #wrong"


def _bash_major() -> int:
    """parser.sh uses declare -A / mapfile, so it needs bash 4+. macOS ships 3.2."""
    try:
        out = subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, timeout=30, check=False)
        return int((out.stdout or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


# On bash 3.2 parser.sh emits its valid-but-empty fallback document, which would fail these
# assertions for a platform reason rather than a real one. CI runs Linux (bash 5), as does the cluster.
requires_bash4 = pytest.mark.skipif(_bash_major() < 4, reason="parser.sh requires bash 4+")


def _row(size: int, busbw: float) -> str:
    return f"   {size:>11} {size // 4:>13}     float     sum      -1   100.00    0.00 {busbw:>7.2f}       0   100.00    0.00 {busbw:>7.2f}       0"


def _block(sizes: list[tuple[int, float]], *, concluded: bool) -> str:
    out = ["# Collective test starting: all_reduce_perf", HDR]
    out += [_row(s, b) for s, b in sizes]
    if concluded:
        out += ["# Out of bounds values : 0 OK", "# Avg bus bandwidth    : 123.45", "# Collective test concluded: all_reduce_perf"]
    return "\n".join(out) + "\n"


def _counts(log: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; nccl_sweep_counts "$1"', "_", str(log)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    started, _, concluded = proc.stdout.strip().partition(",")
    return int(started or 0), int(concluded or 0)


def test_finished_single_block(tmp_path):
    p = tmp_path / "a.out"
    p.write_text(_block([(8, 0.0), (1 << 30, 800.0)], concluded=True), encoding="utf-8")
    assert _counts(p) == (1, 1)


def test_block_still_running_is_started_but_not_concluded(tmp_path):
    """The exact mid-run shape: rows exist, no concluded marker."""
    p = tmp_path / "b.out"
    p.write_text(_block([(8, 0.0)], concluded=False), encoding="utf-8")
    assert _counts(p) == (1, 0)


def test_five_rounds_all_finished(tmp_path):
    """job 130406 ran 5 rounds; complete means concluded == started."""
    p = tmp_path / "c.out"
    p.write_text("".join(_block([(8, 0.0), (1 << 34, 821.7)], concluded=True) for _ in range(5)), encoding="utf-8")
    assert _counts(p) == (5, 5)


def test_llmb_manifest_exposes_tests_that_never_started(tmp_path):
    """22 successful blocks from a 24-test LLMB run is incomplete, not 22/22 complete."""
    p = tmp_path / "llmb.txt"
    p.write_text(
        "# NVIS: aggregated from llmb-run experiment x_133460\n"
        "# job=133460 rack=ink50-dh01-i01 nodes=18 tests=24\n"
        + "".join(_block([(8, 0.0)], concluded=True) for _ in range(22)),
        encoding="utf-8",
    )
    assert _counts(p) == (24, 22)


def test_last_round_still_running(tmp_path):
    p = tmp_path / "d.out"
    body = "".join(_block([(8, 0.0), (1 << 34, 821.7)], concluded=True) for _ in range(4))
    body += _block([(8, 0.0)], concluded=False)
    p.write_text(body, encoding="utf-8")
    started, concluded = _counts(p)
    assert (started, concluded) == (5, 4)
    assert concluded < started, "must read as partial"


def test_a_log_with_no_markers_is_not_reported_as_partial(tmp_path):
    """Not every producer brackets its sweeps; 0/0 must not cry wolf forever."""
    p = tmp_path / "e.out"
    p.write_text("some unrelated log\nwith no collective markers\n", encoding="utf-8")
    assert _counts(p) == (0, 0)


def test_missing_file_is_zeros(tmp_path):
    assert _counts(tmp_path / "nope.out") == (0, 0)


# --- the parser surfaces it on the row -----------------------------------------------------


def _parser_nccl_row(tmp_path, *, concluded: bool, running: str = "") -> dict:
    import json

    log = tmp_path / "cmods-NCCL-1Rack-p5-r01-18N-4242.txt"
    log.write_text(_block([(8, 0.0), (1 << 30, 700.0)], concluded=concluded), encoding="utf-8")
    env = {**os.environ, "NO_COLOR": "1", "VALIDATION_TEST": "nccl_all_reduce"}
    if running:
        env["NVIS_PARSER_RUNNING_JOBS"] = running
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "parser.sh"), "--json", "--no-squeue", "--no-cmsh", "--no-sinfo", str(log)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=env,
    )
    d = json.loads(proc.stdout)
    rows = d.get("nccl_peak_out_of_place_busbw_gbps") or []
    assert rows, "expected one NCCL row"
    return rows[0]


@requires_bash4
def test_parser_row_carries_sweep_counts(tmp_path):
    """parser.sh must expose started/concluded per NCCL row."""
    r = _parser_nccl_row(tmp_path, concluded=True)
    assert r["sweep_blocks_started"] == 1
    assert r["sweep_blocks_concluded"] == 1
    assert r["sweep_complete"] is True
    assert r["sweep_blocks_missing"] == 0


@requires_bash4
def test_a_missing_marker_on_a_RUNNING_job_is_a_partial_read(tmp_path):
    """The 130406 case: the job is still queued, so the series really is half-written."""
    r = _parser_nccl_row(tmp_path, concluded=False, running="4242")
    assert r["sweep_blocks_concluded"] == 0
    assert r["sweep_blocks_missing"] == 1
    assert r["sweep_running"] is True
    assert r["sweep_complete"] is False


@requires_bash4
def test_a_missing_marker_on_a_FINISHED_job_is_a_marker_gap_not_a_partial_read(tmp_path):
    """The 140457 case. The job has left the queue, so re-parsing can never add the marker.

    At 576 nodes the collectives share one stdout and lines arrive interleaved and spliced, so a
    conclusion marker can simply be lost. Calling that "still running" pinned a finished 32-rack
    run behind a progress banner that would never clear.
    """
    r = _parser_nccl_row(tmp_path, concluded=False, running="")
    assert r["sweep_blocks_concluded"] == 0
    assert r["sweep_blocks_missing"] == 1, "the gap is still reported"
    assert r["sweep_running"] is False
    assert r["sweep_complete"] is True, "a finished job is not 'still running'"


# --- the .out job id ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # The regression: an MPI launcher log uses "_" before the id and ends .out, so every
        # .txt pattern missed it and the table showed job "?".
        ("nccl-mpi_ink50-dh01-h15+ink50-dh01-h16_130406.out", "130406"),
        ("nccl-mpi_ink50-dh01-h17+ink50-dh01-h18_130410.out", "130410"),
        ("llmb-nccl_nodes-127318.out", "127318"),
        # Unchanged .txt behaviour.
        ("cmods-NEMO-1Rack-p05-r01-18N-127327.txt", "127327"),
        ("cmods-MNUBERGEMM-ink50-dh01-i05-4242.txt", "4242"),
        ("sysinfo.txt", None),
    ],
)
def test_job_id_from_out_and_txt_names(name, expected):
    """Shell and Python extractors must agree, including on .out."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import nvis_id_templates as nid

    assert nid.job_id_from_log_basename(name) == expected

    proc = subprocess.run(
        ["bash", "-c", f'source "{SCRIPTS / "lib" / "rack_fname.inc.sh"}"; jobid_from_fname "$1"', "_", name],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    got = proc.stdout.strip()
    assert got == (expected if expected is not None else "?"), got
