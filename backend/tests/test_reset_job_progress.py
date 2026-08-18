# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The reset-family job progress contract.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``reset_job_progress`` replaced seven hand-written copies of the same phase transitions (nvswitch
check/fix/sdn-reset/system-reset, imex check/reset, precheck). The dashboard reads those rows
directly, so the payload shape is an API contract even though no HTTP route returns it verbatim: a
poll that stops seeing ``phase`` or ``ok`` shows a finished job as still running.

Nothing covered these transitions before the seven were collapsed, which is the only reason the
collapse could look safe. These tests are that missing coverage.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import host_exec
import job_registry


@pytest.fixture
def job(monkeypatch):
    """A registry row plus captured purge/lake calls, so no test touches the real sinks."""
    purged: list[str] = []
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(job_registry, "_reset_job_purge_later", lambda jid, *a, **k: purged.append(jid))
    import results_ingest

    monkeypatch.setattr(results_ingest, "_lake_push_job", lambda jid, row: pushed.append((jid, row)))
    job_id = "test-job-1"
    with job_registry._reset_jobs_lock:
        job_registry._reset_jobs[job_id] = {"phase": "queued", "rack": "r1"}
    yield job_id, purged, pushed
    with job_registry._reset_jobs_lock:
        job_registry._reset_jobs.pop(job_id, None)


def _row(job_id: str) -> dict:
    with job_registry._reset_jobs_lock:
        return dict(job_registry._reset_jobs[job_id])


def test_success_marks_done_and_strips_output(job):
    job_id, purged, pushed = job
    with job_registry.reset_job_progress(
        job_id, display_cmd="script.sh r1", label="IMEX check", timeout_sec=60
    ) as prog:
        assert _row(job_id)["phase"] == "running", "must be visible as running while the body runs"
        assert _row(job_id).get("started_running_at")
        prog.finish(0, "  out\n", "\n", nonzero_error="should not appear")
    row = _row(job_id)
    assert row["phase"] == "done"
    assert row["ok"] is True
    assert row["returncode"] == 0
    assert row["stdout"] == "out"
    assert row["stderr"] == ""
    assert row["error"] is None
    assert row["command_display"] == "script.sh r1"
    assert purged == [job_id]
    assert [j for j, _ in pushed] == [job_id], "a terminal phase publishes to the lake exactly once"


def test_nonzero_exit_reports_the_supplied_error(job):
    job_id, purged, _ = job
    with job_registry.reset_job_progress(
        job_id, display_cmd="d", label="IMEX check", timeout_sec=60
    ) as prog:
        prog.finish(3, "", "boom", nonzero_error="imex_check.sh exited non-zero (see stdout/stderr)")
    row = _row(job_id)
    assert (row["phase"], row["ok"], row["returncode"]) == ("error", False, 3)
    assert row["error"] == "imex_check.sh exited non-zero (see stdout/stderr)"
    assert purged == [job_id]


def test_none_returncode_is_reported_as_minus_one(job):
    """A HostProcess that never reported an exit status must not surface ``returncode: None``."""
    job_id, _, _ = job
    with job_registry.reset_job_progress(
        job_id, display_cmd="d", label="IMEX reset", timeout_sec=60
    ) as prog:
        prog.finish(None, None, None, nonzero_error="nope")
    row = _row(job_id)
    assert row["returncode"] == -1
    assert row["ok"] is False


def test_timeout_uses_the_label_and_records_the_deadline(job):
    job_id, purged, _ = job
    with job_registry.reset_job_progress(
        job_id, display_cmd="d", label="NVSwitch system reset", timeout_sec=1800
    ):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1800)
    row = _row(job_id)
    assert row["phase"] == "error"
    assert row["error"] == "NVSwitch system reset timed out after 1800s"
    assert row["stderr"] == "timeout after 1800s"
    assert row["returncode"] == -1
    assert purged == [job_id], "a timed-out job must still be purged"


def test_unexpected_exception_is_reported_not_raised(job):
    """A worker thread has nobody to raise to, so the row is the only error report."""
    job_id, purged, _ = job
    with job_registry.reset_job_progress(
        job_id, display_cmd="d", label="IMEX check", timeout_sec=60
    ):
        raise RuntimeError("ssh went away")
    row = _row(job_id)
    assert row["phase"] == "error"
    assert row["error"] == "ssh went away"
    assert purged == [job_id]


def test_escaping_exception_still_purges(job):
    """The purge is in a ``finally`` for the failure the handler deliberately does not catch.

    ``except Exception`` leaves ``BaseException`` alone -- an interpreter shutdown or a cancelled
    thread -- and without the ``finally`` that path leaks the registry row for the process lifetime.
    A trailing statement after the try/except passes every other test in this file, so this is the
    one that pins it.
    """
    job_id, purged, _ = job
    with (
        pytest.raises(KeyboardInterrupt),
        job_registry.reset_job_progress(
            job_id, display_cmd="d", label="IMEX check", timeout_sec=60
        ),
    ):
        raise KeyboardInterrupt
    assert purged == [job_id]


def test_missing_row_does_not_raise(job):
    """A purge that already fired must not turn a finished job into a crashed thread."""
    job_id, _, pushed = job
    with job_registry._reset_jobs_lock:
        job_registry._reset_jobs.pop(job_id)
    with job_registry.reset_job_progress(
        job_id, display_cmd="d", label="IMEX check", timeout_sec=60
    ) as prog:
        prog.finish(0, "", "", nonzero_error="x")
    assert pushed == [], "no row means nothing to publish"


class _FakeProc:
    """A HostProcess stand-in that overruns until killed."""

    def __init__(self, *, overrun: bool):
        self.overrun = overrun
        self.killed = False
        self.reaped = 0
        self.returncode = None

    def communicate(self, timeout=None):
        self.reaped += 1
        if self.overrun and not self.killed:
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        self.returncode = 0
        return "out", "err"

    def kill(self):
        self.killed = True


def test_communicate_or_kill_passes_through_on_time():
    proc = _FakeProc(overrun=False)
    assert host_exec.communicate_or_kill(proc, 60) == ("out", "err")
    assert proc.killed is False


def test_communicate_or_kill_kills_reaps_and_reraises():
    """The reap matters as much as the kill: an unclosed SSH channel costs a MaxSessions slot."""
    proc = _FakeProc(overrun=True)
    with pytest.raises(subprocess.TimeoutExpired):
        host_exec.communicate_or_kill(proc, 60)
    assert proc.killed is True
    assert proc.reaped == 2, "expected the initial wait plus one reap after the kill"


def test_communicate_or_kill_survives_a_failed_reap():
    """If the killed process will not report back either, the timeout still propagates."""

    class _Stubborn(_FakeProc):
        def communicate(self, timeout=None):
            self.reaped += 1
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    proc = _Stubborn(overrun=True)
    with pytest.raises(subprocess.TimeoutExpired):
        host_exec.communicate_or_kill(proc, 60)
    assert proc.killed is True
