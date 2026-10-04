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
"""Per-node health-check markers, read where the site's checker leaves them.

A site health checker runs on its own schedule and drops a small marker per node — a status
and a timestamp. This reads those into post-run events so a node's health sits beside its
validation history instead of in a directory somebody has to remember to look in.

**Deliberately not ingested into the results database.** A marker is *current state*, and
the checker rewrites it every cycle: there is no history to accumulate and nothing joins it
to anything else. Persisting it would add rows that are stale the moment they land and would
mean a fourth writer to the fleet database for data whose whole value is being fresh
(`storage-engines.mdc`). Read at request time, cached on the marker's own mtime.

The root comes from the cluster record, not a constant. The implementation this was ported
from hard-coded `/cluster/OVA/healthchecks/logs`, which exists on exactly one appliance.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

SOURCE = "external-evidence:healthcheck"

#: Marker statuses the checker writes, mapped onto our verdicts. Anything else becomes
#: UNKNOWN rather than being guessed into a pass or a fail -- a health view that invents a
#: verdict is worse than one that admits it cannot read the file.
#: ``clean``/``failed`` is the ``auto-action-trigger`` node_health vocabulary (it writes
#: ``clean`` for a run with no reason, ``failed`` otherwise); the rest cover other checkers.
_STATUS = {
    "ok": "PASS",
    "pass": "PASS",
    "passed": "PASS",
    "healthy": "PASS",
    "clean": "PASS",
    "fail": "FAIL",
    "failed": "FAIL",
    "error": "FAIL",
    "unhealthy": "FAIL",
}

_MARKER = ".last-check"
_TAIL_BYTES = 4096

#: The fields a post-run "run" row carries, mirrored from the frontend ``PostRunEvent``
#: contract (``frontend/src/types/api.ts``). A health marker is rendered through the same
#: post-run table as a Slurm validation run, so it must present every field that table reads
#: -- otherwise a cell reaches the client as ``undefined`` and crashes the view. Declared
#: once, applied by ``_marker_event``, so the contract lives in a single place as it grows.
_RUN_FIELD_DEFAULTS: dict[str, Any] = {
    "rack": "",
    "job_id": "",
    "scope": "node",
    "ended_at": "",
    "exit_code": "",
    "failed_job": False,
    "attributed_failure": False,
    "category": "",
    "output_run_id": "",
    "output_step_id": "",
}


def _epoch_to_iso(epoch: str) -> str:
    """ISO-8601 UTC from a Unix-seconds marker, or ``""`` when it is not a plain number.

    The timestamp comes from a shell ``date +%s`` and is normally clean; this validates
    rather than trusts it, so a half-written or hand-edited marker never reaches
    ``new Date(...)`` on the client as a bare integer string that renders as a non-date.
    """
    s = (epoch or "").strip()
    if not s.isdigit():
        return ""
    try:
        return datetime.fromtimestamp(int(s), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _marker_event(*, host: str, verdict: str, raw_status: str, detail: str, epoch: str, marker: Path) -> dict[str, Any]:
    """One health marker as a conformant, fully-populated post-run run event.

    Validated and sanitised at the boundary: every field is a definite type, so a partial
    marker renders as a row rather than crashing the table. ``state`` carries the verdict so
    the shared state-colour maps PASS/FAIL to green/red; the health-specific fields
    (``status``, ``raw_verdict``, ``workload``) ride alongside for any marker-aware consumer.
    Reused for every marker and every caller, so the shape cannot drift between them.
    """
    started = _epoch_to_iso(epoch)
    return {
        **_RUN_FIELD_DEFAULTS,
        "node": str(host),
        "activity_id": f"healthcheck:{host}",
        "tool": "node-healthcheck",
        "state": verdict,
        "started_at": started,
        "reason": str(detail or ""),
        "source": SOURCE,
        # Always current: the checker overwrites one marker per node, so there is no
        # superseded row to keep out of a failure count.
        "authoritative": True,
        "history_only": False,
        # Health-specific fields, kept for marker-aware consumers:
        "workload": "healthcheck",
        "test": "node-healthcheck",
        "status": verdict,
        "raw_verdict": str(raw_status),
        "detail": str(detail or ""),
        "artifact_path": str(marker),
        "observed_at": str(epoch),
    }

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def log_root() -> str:
    """Where this cluster's checker writes, or "" if it has none."""
    try:
        from catalog import context as catalog_context

        _project, cluster, _platform = catalog_context.resolve()
        if cluster is None:
            return ""
        return cluster.evidence.healthcheck_root.strip()
    except Exception:  # noqa: BLE001 - no feed configured is the common case, not a fault
        return ""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _read_marker(directory: Path) -> tuple[str, str] | None:
    """`(status, epoch)` from a node's marker, or None if it has none worth reading."""
    marker = directory / _MARKER
    try:
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    fields = text.split()
    if len(fields) < 2:
        # A marker mid-write, or one from a checker version we do not understand. Skipping is
        # right: a half-written line parsed optimistically is a fabricated verdict.
        return None
    return fields[1], fields[0]


def _failure_detail(directory: Path) -> str:
    """The tail of the node's log, for a failure that needs a reason attached."""
    log = directory / "health.log"
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - _TAIL_BYTES))
            return handle.read().decode("utf-8", "replace").strip()[-600:]
    except OSError:
        return ""


def post_run_events(*, rack: str = "", node: str = "") -> list[dict[str, Any]]:
    """Health-check events, optionally narrowed to one rack or node.

    Returns `[]` when no root is configured or the root is absent, rather than raising: most
    clusters have no such checker, and the post-run report must render either way.
    """
    root_raw = log_root()
    if not root_raw:
        return []
    root = Path(root_raw)
    if not root.is_dir():
        _logger.debug("health-check root is not present: %s", root)
        return []

    wanted_node = node.strip().casefold()
    wanted_rack = rack.strip().casefold()
    out: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        host = directory.name
        folded = host.casefold()
        if wanted_node and folded != wanted_node:
            continue
        if wanted_rack and not folded.startswith(wanted_rack):
            continue

        marker = directory / _MARKER
        try:
            stat = marker.stat()
        except OSError:
            continue
        key = f"{host}:{stat.st_mtime_ns}:{stat.st_size}"
        with _cache_lock:
            cached = _cache.get(key)
        if cached is not None:
            out.append(cached)
            continue

        parsed = _read_marker(directory)
        if parsed is None:
            continue
        raw_status, epoch = parsed
        verdict = _STATUS.get(raw_status.strip().lower(), "UNKNOWN")
        event = _marker_event(
            host=host,
            verdict=verdict,
            raw_status=raw_status,
            detail=_failure_detail(directory) if verdict == "FAIL" else "",
            epoch=epoch,
            marker=marker,
        )
        with _cache_lock:
            _cache[key] = event
        out.append(event)
    return out
