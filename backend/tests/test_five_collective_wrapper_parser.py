#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from openpyxl import load_workbook

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import app as app_module  # noqa: E402


COLLECTIVES = ("all_reduce", "alltoall", "sendrecv", "all_gather", "reduce_scatter")
PEAKS = {
    "all_reduce": 387.13,
    "alltoall": 80.38,
    "sendrecv": 20.93,
    "all_gather": 384.70,
    "reduce_scatter": 386.37,
}


def _raw(collective: str, *, oob: int = 0, with_metrics: bool = True) -> str:
    if not with_metrics:
        return "PASS\n"
    peak = PEAKS[collective]
    return (
        f"# Collective test starting: {collective}_perf_mpi\n"
        "#                                                              out-of-place                       in-place\n"
        "#       size         count      type   redop    root     time   algbw   busbw  #wrong\n"
        f"        8192          2048     float     sum      -1   100.00    0.08    0.12       0\n"
        f" 34359738368    8589934592     float     sum      -1   100.00  100.00 {peak:.2f}       0\n"
        f"# Out of bounds values : {oob} {'OK' if oob == 0 else 'FAILED'}\n"
        f"# Avg bus bandwidth    : {peak / 2:.3f}\n"
        f"# Collective test concluded: {collective}_perf_mpi\n"
    )


def _fixture(tmp_path: Path, *, metrics: bool = True, bad_oob: bool = False) -> Path:
    root = tmp_path / "SU5" / "rack01" / "multiNodeNCCL"
    raw_dir = root / "job-46984"
    raw_dir.mkdir(parents=True)
    ends: list[str] = []
    for number, collective in enumerate(COLLECTIVES, start=1):
        raw = raw_dir / f"test-{number}-{collective}-8k-32g.raw"
        raw.write_text(
            _raw(
                collective,
                oob=1 if bad_oob and collective == "all_reduce" else 0,
                with_metrics=metrics,
            ),
            encoding="utf-8",
        )
        ends.append(
            f"TEST_END test={number} collective={collective} rc=0 correctness=OK "
            f"raw={raw} live={raw}.live nccl_info_live={raw}.info\n"
        )
    wrapper = root / "nvftp-nccl-five-collective-46984.out"
    wrapper.write_text(
        "FIVE_COLLECTIVE_SWEEP_START job=46984 nodes=18 gpus=72\n"
        + "".join(ends)
        + "FIVE_COLLECTIVE_SWEEP_END job=46984 rc=0 detail_rc=0 summary=x\n",
        encoding="utf-8",
    )
    return wrapper


def _parse(wrapper: Path) -> dict:
    env = {
        **os.environ,
        "NO_COLOR": "1",
        "NVIS_PARSER_INVENTORY": "0",
        "VALIDATION_TEST": "nccl_all_reduce,nccl_alltoall,nccl_allgather",
    }
    proc = subprocess.run(
        [
            "bash",
            str(REPO / "bin" / "scripts" / "parser.sh"),
            "--json",
            "--no-squeue",
            "--no-cmsh",
            "--no-sinfo",
            str(wrapper),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_verified_wrapper_populates_parser_and_excel(tmp_path: Path) -> None:
    wrapper = _fixture(tmp_path)
    parsed = _parse(wrapper)

    rows = parsed["nccl_peak_out_of_place_busbw_gbps"]
    assert len(rows) == 1
    assert rows[0]["all_reduce_gbps"] == "387.13"
    assert rows[0]["all_gather_gbps"] == "384.7"
    assert rows[0]["alltoall_gbps"] == "80.38"
    assert rows[0]["oob_ar_ag_aa"] == "0,0,0"
    assert rows[0]["sweep_blocks_started"] == 5
    assert rows[0]["sweep_blocks_concluded"] == 5
    assert parsed["parser_flags"][0]["nccl_ok"] == 1

    app_module._annotate_parser_result_racks(parsed, [wrapper], wrapper.parent)
    assert rows[0]["rack"] == "ink50-dh02-e01"
    workbook = load_workbook(io.BytesIO(app_module._workbook_bytes_from_parser_export(parsed)))
    assert workbook["nccl all_reduce"]["A3"].value == "ink50-dh02-e01"
    assert workbook["nccl all_reduce"]["C3"].value == 387.13
    assert workbook["nccl all_to_all"]["C3"].value == 80.38
    assert workbook["nccl all_gather"]["C3"].value == 384.7


def test_wrapper_pass_markers_without_numeric_raw_evidence_abstain(tmp_path: Path) -> None:
    parsed = _parse(_fixture(tmp_path, metrics=False))

    assert parsed["parser_flags"][0]["nccl_ok"] == 0
    app_module._drop_parser_placeholder_rows(parsed)
    assert parsed["nccl_peak_out_of_place_busbw_gbps"] == []


def test_wrapper_with_raw_correctness_failure_abstains(tmp_path: Path) -> None:
    parsed = _parse(_fixture(tmp_path, bad_oob=True))

    assert parsed["parser_flags"][0]["nccl_ok"] == 0
    app_module._drop_parser_placeholder_rows(parsed)
    assert parsed["nccl_peak_out_of_place_busbw_gbps"] == []
