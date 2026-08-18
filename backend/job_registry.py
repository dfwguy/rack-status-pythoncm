# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Async job registries: the in-process record of what is running.

Author: Rafael M. Koike & Cursor Agent ("Ada")

One ``dict`` + ``threading.Lock`` per tool family, keyed by job id. Extracted so the routes that
share a registry can live in separate modules under ``backend/api/`` -- six domains share
``_stress_jobs`` alone (clusterkit, nvloom, nccl-fullrack, mnubergemm, llmb-nccl, exemplar-llama),
so until this moved they could not be split at all.

These are shared **objects**, not values: importing ``_stress_jobs`` elsewhere binds the same dict,
and mutations are visible everywhere. So mutate them, never rebind them -- ``_stress_jobs = {}`` in
another module would silently give that module a private dict while the rest of the app kept using
this one. Nothing in the tree does that today (checked), and the locks are the reason to keep it
that way: a second dict would not be covered by the first one's lock.

State is deliberately in-process and not persisted. The durable record of a validation run is
``validation_activities.py``; these registries only track liveness for ``GET /api/rack-locks`` and
the ``*-status`` endpoints, and are expected to be empty after a restart.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import rack_locks
from core import utc_now_iso

_logger = logging.getLogger(__name__)

# Async job registries. One dict + lock per tool family, keyed by job id.

_validation_jobs_lock = threading.Lock()
_validation_jobs: dict[str, dict[str, object]] = {}

_stress_jobs_lock = threading.Lock()
_stress_jobs: dict[str, dict[str, object]] = {}

_xrack_jobs_lock = threading.Lock()
_xrack_jobs: dict[str, dict[str, object]] = {}

_llama_jobs_lock = threading.Lock()
_llama_jobs: dict[str, dict[str, object]] = {}
_reset_jobs_lock = threading.Lock()
_reset_jobs: dict[str, dict[str, object]] = {}

_analytics_jobs: dict[str, dict] = {}
_analytics_jobs_lock = threading.Lock()

_node_report_jobs: dict[str, dict] = {}
_node_report_jobs_lock = threading.Lock()


def _job_purge_later(
    jobs: dict[str, dict[str, object]],
    lock: threading.Lock,
    job_id: str,
    delay_sec: int = 900,
) -> None:
    """Drop finished job metadata after a delay, so a registry cannot grow without bound.

    One function for every registry. There were five identical copies of this -- four in app.py plus
    one here -- differing only in which dict they popped from, which is how a bounded registry quietly
    becomes an unbounded one: fix the eviction in the copy you are looking at and the other four keep
    leaking.

    The dict and lock are passed in rather than named, so the caller cannot accidentally pair one
    registry's dict with another's lock.
    """

    def _purge() -> None:
        time.sleep(float(delay_sec))
        with lock:
            jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


def _reset_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    _job_purge_later(_reset_jobs, _reset_jobs_lock, job_id, delay_sec)


def _validation_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    _job_purge_later(_validation_jobs, _validation_jobs_lock, job_id, delay_sec)


def _stress_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    _job_purge_later(_stress_jobs, _stress_jobs_lock, job_id, delay_sec)


def _xrack_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    _job_purge_later(_xrack_jobs, _xrack_jobs_lock, job_id, delay_sec)


def _llama_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    _job_purge_later(_llama_jobs, _llama_jobs_lock, job_id, delay_sec)


@contextmanager
def _track_sync_op(rack: str | object, kind: str, operator: str = ""):
    """Register a synthetic entry in ``_reset_jobs`` so long-running sync handlers and
    write-y SSE streams show up in ``GET /api/rack-locks`` for their duration.

    Accepts a single rack id or a list/tuple. ``ValueError`` from a bad rack id is logged
    and swallowed -- the wrapped handler runs untracked rather than breaking the route."""
    racks: list[str] = []
    if isinstance(rack, (list, tuple)):
        candidates = rack
    else:
        candidates = [rack] if rack else []
    for raw_rack in candidates:
        if not isinstance(raw_rack, str) or not raw_rack:
            continue
        try:
            racks.append(rack_locks.normalize_rack_id(raw_rack))
        except ValueError:
            continue
    if not racks:
        # Nothing to lock against; still yield so the route runs.
        yield None
        return
    job_id = "sync-" + uuid.uuid4().hex[:12]
    entry = {
        "job_id": job_id,
        "rack": racks[0] if len(racks) == 1 else None,
        "racks": racks,
        "kind": str(kind or "operation"),
        "operator": rack_locks.sanitize_operator(operator),
        "phase": "running",
        "sync": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    with _reset_jobs_lock:
        _reset_jobs[job_id] = entry
    try:
        yield job_id
    finally:
        with _reset_jobs_lock:
            row = _reset_jobs.get(job_id)
            if row is not None:
                row["phase"] = "done"
                row["finished_at"] = datetime.now(timezone.utc).isoformat()
        _reset_job_purge_later(job_id, delay_sec=15)


# --- Reset-family job progress -------------------------------------------------------------------
# Seven background workers (nvswitch check/fix/sdn-reset/system-reset, imex check/reset, precheck)
# each carried its own copy of the code below: a _patch closure, the running -> done/error phase
# transitions, the timeout and exception payloads, and the purge. 479 lines, in which the _patch
# closure was byte-identical seven times over.
#
# What legitimately differed is NOT the bookkeeping but the transport and the timeout policy: the
# reset family holds a HostProcess open and kills it on overrun, while the check family is a
# fire-and-wait run_on_host with the timeout handled inside. That difference is real, so this owns
# only the job-state half and leaves each worker to run its command its own way.


class ResetJobProgress:
    """The phase transitions of one reset-family job.

    ``patch`` is the only writer of a ``_reset_jobs`` row. It snapshots inside the lock and pushes to
    the results lake outside it, so a slow sink can never hold the mutex that ``GET /api/rack-locks``
    and every ``*-status`` poll need.
    """

    def __init__(self, job_id: str, *, display_cmd: str, label: str, timeout_sec: int) -> None:
        self.job_id = job_id
        self.display_cmd = display_cmd
        self.label = label
        self.timeout_sec = timeout_sec

    def patch(self, **kwargs: object) -> None:
        finished = None
        with _reset_jobs_lock:
            row = _reset_jobs.get(self.job_id)
            if row is not None:
                row.update(kwargs)
                # Snapshot inside the lock; push outside it so the lake call never holds the mutex.
                if str(kwargs.get("phase") or "") in ("done", "error"):
                    finished = dict(row)
        if finished is not None:
            # Imported here rather than at module scope: results_ingest reaches the transport and the
            # history store, and this module is imported by nearly every api/ module.
            from results_ingest import _lake_push_job

            _lake_push_job(self.job_id, finished)

    def finish(
        self,
        returncode: int | None,
        stdout: str | None,
        stderr: str | None,
        *,
        nonzero_error: str,
    ) -> None:
        """Record the terminal state. ``nonzero_error`` is shown only when the command failed."""
        rc = -1 if returncode is None else returncode
        ok = rc == 0
        self.patch(
            phase="done" if ok else "error",
            ok=ok,
            finished_at=utc_now_iso(),
            returncode=rc,
            stdout=(stdout or "").strip(),
            stderr=(stderr or "").strip(),
            command_display=self.display_cmd,
            error=None if ok else nonzero_error,
        )


@contextmanager
def reset_job_progress(job_id: str, *, display_cmd: str, label: str, timeout_sec: int):
    """Drive a reset-family job's row from ``running`` to a terminal phase.

    Marks the job running, yields a :class:`ResetJobProgress` for the body to ``finish``, and turns
    either kind of failure into the error payload the dashboard expects. A worker that raises
    ``subprocess.TimeoutExpired`` -- which ``host_exec.communicate_or_kill`` does after killing an
    overrunning process -- gets the timeout wording; anything else is reported verbatim.

    The purge is in a ``finally``, so a job cannot leak a row by failing in an unexpected way.
    """
    prog = ResetJobProgress(
        job_id, display_cmd=display_cmd, label=label, timeout_sec=timeout_sec
    )
    try:
        prog.patch(phase="running", started_running_at=utc_now_iso())
        yield prog
    except subprocess.TimeoutExpired:
        prog.patch(
            phase="error",
            ok=False,
            finished_at=utc_now_iso(),
            returncode=-1,
            stdout="",
            stderr=f"timeout after {timeout_sec}s",
            command_display=display_cmd,
            error=f"{label} timed out after {timeout_sec}s",
        )
    except Exception as e:
        # A worker thread has nobody to raise to, so the job row IS the error report.
        _logger.debug("reset_job_progress(%s): reporting error on the job row", label, exc_info=True)
        prog.patch(
            phase="error",
            ok=False,
            finished_at=utc_now_iso(),
            returncode=-1,
            stdout="",
            stderr="",
            command_display=display_cmd,
            error=str(e),
        )
    finally:
        _reset_job_purge_later(job_id)
