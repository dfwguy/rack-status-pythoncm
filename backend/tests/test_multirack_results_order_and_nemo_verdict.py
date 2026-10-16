# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Results & logs on disk: the newest run first, and a NeMo run passes only if it trained.

Two faults seen on Nscale NC on the same table (2026-10-16):

* U2+U8 (Slurm 207346) passed, yet "did not show": rows came in file-name order, ten to a page,
  so 56 days-old ``nccl-mpi_*`` debug sweeps filled the first pages and the run sat on page 6.
* U23+U24 (Slurm 206757) read ``done`` although SU19 went down at step 890 of 2010 and Slurm
  killed the step. Multirack logs were judged by RACK_MULTIRACK_VALIDATION_TEST
  (``nccl_all_reduce``), which for a NeMo log checks only the NCCL preflight that opens the job.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import native_status
import pytest
import rack_status_engine
from native_status import MultirackTableState

from tests.test_nccl_log_verdicts import CASES, RACK_STATUS

# --- order -----------------------------------------------------------------------------------------


def _rows(monkeypatch, logs: list[tuple[str, int]]) -> list[dict[str, str]]:
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr(native_status, "augment_running_set_for_job_ids", lambda *a, **k: None)
    st = MultirackTableState("test-order", Path("/tmp"))
    st.log_paths = [(PurePosixPath(f"/logs/{name}"), mtime, 100) for name, mtime in logs]
    st.per_path_probe = {f"/logs/{name}": {"ok": True, "nemo_state": "done", "nccl_ok": True, "llm_ok": True}
                         for name, _ in logs}
    st.sinfo_done = st.log_done = True
    return st.build_rows(lambda bn: bn, lambda bn: [])


def test_the_newest_run_comes_first_whatever_its_name(monkeypatch):
    rows = _rows(monkeypatch, [
        ("nccl-mpi_a2a-128-u1921-peer16_178533.out", 1_000),   # old debug sweep, sorts first by name
        ("root-NEMO-U2+U8-36N-207346.txt", 3_000),             # the run that "did not show"
        ("root-NEMO-U19+U20-36N-203673.txt", 2_000),
    ])
    assert [r["log_file"] for r in rows] == [
        "root-NEMO-U2+U8-36N-207346.txt",
        "root-NEMO-U19+U20-36N-203673.txt",
        "nccl-mpi_a2a-128-u1921-peer16_178533.out",
    ]


# --- which suite a log is judged by ----------------------------------------------------------------


@pytest.mark.parametrize(("name", "suite", "want"), [
    ("root-NEMO-U23+U24-36N-206757.txt", "nccl_all_reduce", "nccl_all_reduce,llm"),
    ("root-NEMO-U23+U24-36N-206757.txt", "nccl_all_reduce,llm", "nccl_all_reduce,llm"),
    ("root-NEMO-U23+U24-36N-206757.txt", " llm ", "llm"),
    ("cmods-NEMO-1Rack-U19-18N-1.txt", "nccl_all_reduce", "nccl_all_reduce,llm"),
    ("rkoike-NEMO-p5-r[01-02]-36N-9.txt", "", "llm"),
    # NCCL logs keep exactly the suite they were given: they carry no training to check.
    ("nccl-mpi_a2a-128-u1921-peer16_178533.out", "nccl_all_reduce", "nccl_all_reduce"),
    ("cmods-NCCL-1Rack-U19-1N-126352.txt", "nccl_all_reduce", "nccl_all_reduce"),
    ("rkoike-nemotron-notes.txt", "nccl_all_reduce", "nccl_all_reduce"),
])
def test_a_nemo_log_is_always_judged_on_training(name, suite, want):
    assert rack_status_engine.nemo_log_suite(name, suite) == want


def test_the_probe_forwards_the_training_check_for_a_nemo_log(monkeypatch, tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen.update(kw.get("env_extra") or {})
        return subprocess.CompletedProcess(cmd, 0, '{"ok": true}\n', "")

    monkeypatch.setattr(rack_status_engine, "_script_exists", lambda _p: True)
    monkeypatch.setattr(rack_status_engine.host_exec, "run_on_host", run)
    monkeypatch.delenv("VALIDATION_TEST", raising=False)
    monkeypatch.setenv("RACK_MULTIRACK_VALIDATION_TEST", "nccl_all_reduce")
    rack_status_engine._probe_log_json_via_rack_status(tmp_path / "root-NEMO-U23+U24-36N-206757.txt")
    assert seen["VALIDATION_TEST"] == "nccl_all_reduce,llm"


def test_a_cache_written_by_the_old_rule_is_not_served():
    # Verdicts are cached by log mtime and size; 206757's stale "done" must be re-judged.
    assert native_status._NATIVE_LOG_CACHE_VERSION >= 5


# --- the real verdict, end to end ------------------------------------------------------------------

_PREFLIGHT = next(c for c in CASES if c.id == "valid-standalone-marker-less").text


def _nemo_log(last_step: int, tail: str = "") -> str:
    steps = "".join(
        f" [2026-10-15 12:{i % 60:02d}:00] iteration {i:8d}/    2010 | consumed samples: {i * 432} | "
        f"elapsed time per iteration (ms): 4264.2 | lm loss: 5.3E-03 |\n"
        for i in range(1, last_step + 1, max(1, last_step // 40))
    )
    if last_step not in range(1, last_step + 1, max(1, last_step // 40)):
        steps += (f" [2026-10-15 13:20:31] iteration {last_step:8d}/    2010 | elapsed time per iteration "
                  f"(ms): 4264.2 | lm loss: 4.9E-03 |\n")
    return _PREFLIGHT + steps + tail


def _probe(tmp_path: Path, name: str, text: str, suite: str) -> dict:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(RACK_STATUS), "--no-squeue", "--no-cmsh", "--no-sinfo", "--probe-json", str(p)],
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "VALIDATION_TEST": suite, "NO_COLOR": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_nemo_run_cut_off_mid_training_fails_with_the_training_check(tmp_path):
    name = "root-NEMO-U23+U24-36N-206757.txt"
    log = _nemo_log(890, "[2026-10-15T13:20:35.519] error: *** STEP 206757.5 ON sys1-gso1-p-phy-gpu73-su19 "
                         "CANCELLED AT 2026-10-15T13:20:35 DUE to SIGNAL Killed ***\n")
    # The old rule: the NCCL preflight alone passed it.
    assert _probe(tmp_path, name, log, "nccl_all_reduce")["nemo_state"] == "done"
    judged = _probe(tmp_path, name, log, rack_status_engine.nemo_log_suite(name, "nccl_all_reduce"))
    assert judged["nemo_state"] == "fail"
    assert judged["llm_ok"] is False


def test_a_nemo_run_that_trained_to_the_end_still_passes(tmp_path):
    name = "root-NEMO-U2+U8-36N-207346.txt"
    judged = _probe(tmp_path, name, _nemo_log(2010), rack_status_engine.nemo_log_suite(name, "nccl_all_reduce"))
    assert judged["nemo_state"] == "done"
