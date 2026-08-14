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

import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import rack_locks

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


def _reset_job_purge_later(job_id: str, delay_sec: int = 900) -> None:
    def _purge() -> None:
        time.sleep(float(delay_sec))
        with _reset_jobs_lock:
            _reset_jobs.pop(job_id, None)

    threading.Thread(target=_purge, daemon=True).start()


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
