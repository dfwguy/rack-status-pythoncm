# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Scheduler-neutral runtime facts used by legacy Rack Status routes.

The public HTTP schema still contains historical ``slurm_*`` field and route
names for client compatibility.  This module is the boundary that prevents
those compatibility names from turning into Slurm command execution when the
deployment selects ``RACK_SCHEDULER=pythoncm``.

PythonCM command UUIDs are opaque scheduler references.  Completed and active
work is reconciled from Rack Status' durable validation-activity registry;
node identity and drain state come from CMDaemon through :mod:`bcm_query`.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import bcm_query

_PYTHONCM_REF_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_OPEN_STATES = frozenset({
    "PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUBMITTED", "COLLECTING",
})
_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
    "BOOT_FAIL", "DEADLINE", "PREEMPTED", "UNKNOWN",
})


def scheduler_kind() -> str:
    return (os.environ.get("RACK_SCHEDULER") or "slurm").strip().lower()


def is_pythoncm() -> bool:
    return scheduler_kind() == "pythoncm"


def valid_reference(value: object) -> bool:
    text = str(value or "").strip()
    if is_pythoncm():
        return bool(_PYTHONCM_REF_RE.fullmatch(text))
    # Keep the legacy array-id dialect without importing core (which imports
    # this module indirectly through app in some test configurations).
    return bool(re.fullmatch(r"[1-9][0-9]*(?:_[0-9]+)?", text))


def _activity_rows(limit: int = 500) -> list[dict[str, Any]]:
    try:
        import validation_activities

        return [
            row for row in validation_activities.list_activities(limit=max(1, limit))
            if isinstance(row, dict)
        ]
    except Exception:  # noqa: BLE001 - dashboard must render if registry is unavailable
        return []


def jobs(*, active_only: bool = False, limit: int = 500) -> list[dict[str, Any]]:
    """Normalized scheduler tasks, newest first, from the durable registry."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for activity in _activity_rows(limit):
        for task in activity.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            ref = str(task.get("scheduler_ref") or "").strip()
            if not ref or ref in seen:
                continue
            state = str(task.get("scheduler_state") or task.get("work_phase") or "UNKNOWN")
            state = state.strip().upper().split("+", 1)[0] or "UNKNOWN"
            if state == "DONE":
                state = "COMPLETED"
            elif state == "ERROR":
                state = "FAILED"
            if active_only and state not in _OPEN_STATES:
                continue
            seen.add(ref)
            nodes = activity.get("nodes") or activity.get("target") or []
            if isinstance(nodes, str):
                nodes = [item.strip() for item in nodes.split(",") if item.strip()]
            out.append({
                "job_id": ref,
                "name": str(task.get("label") or activity.get("tool") or "pythoncm-command"),
                "state": state,
                "nodes": list(nodes) if isinstance(nodes, list) else [],
                "nodelist": ",".join(nodes) if isinstance(nodes, list) else "",
                "start": str(task.get("started_at") or activity.get("started_at") or ""),
                "end": str(task.get("finished_at") or activity.get("finished_at") or ""),
                "elapsed": _elapsed(task, activity),
                "exit_code": str(task.get("returncode") if task.get("returncode") is not None else ""),
                "stdout": str(task.get("stdout_path") or ""),
                "activity_id": str(activity.get("activity_id") or ""),
            })
    return out


def _elapsed(task: dict[str, Any], activity: dict[str, Any]) -> str:
    started = str(task.get("started_at") or activity.get("started_at") or "").strip()
    ended = str(task.get("finished_at") or activity.get("finished_at") or "").strip()
    if not started:
        return ""
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = (
            datetime.fromisoformat(ended.replace("Z", "+00:00"))
            if ended else datetime.now(timezone.utc)
        )
        seconds = max(0, int((end_dt - start_dt).total_seconds()))
    except (TypeError, ValueError):
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def job(reference: str) -> dict[str, Any] | None:
    ref = str(reference or "").strip()
    return next((row for row in jobs() if row["job_id"] == ref), None)


def live_status(reference: str) -> tuple[str, str]:
    """Return ``(state, stdout_path)`` from PythonCM plus durable metadata."""
    ref = str(reference or "").strip()
    if not valid_reference(ref):
        raise ValueError("invalid scheduler reference")
    import host_exec
    from schedulers import JobHandle, build_scheduler

    scheduler = build_scheduler(
        "pythoncm", connection=host_exec.connection("control"), profile="control"
    )
    state = scheduler.status(JobHandle(ref, "pythoncm"))
    durable = job(ref) or {}
    return state, str(durable.get("stdout") or "")


def cancel(reference: str) -> None:
    ref = str(reference or "").strip()
    if not valid_reference(ref):
        raise ValueError("invalid scheduler reference")
    import host_exec
    from schedulers import JobHandle, build_scheduler

    scheduler = build_scheduler(
        "pythoncm", connection=host_exec.connection("control"), profile="control"
    )
    scheduler.cancel(JobHandle(ref, "pythoncm"))


def active_job_times(wanted: Iterable[str] | None = None) -> tuple[set[str], dict[str, str]]:
    selected = {str(item).strip() for item in (wanted or []) if str(item).strip()}
    rows = jobs(active_only=True)
    if selected:
        rows = [row for row in rows if row["job_id"] in selected]
    refs = {str(row["job_id"]) for row in rows}
    return refs, {str(row["job_id"]): str(row.get("elapsed") or "?") for row in rows}


def nodes() -> list[dict[str, str]]:
    """Physical nodes with normalized scheduling state and reason.

    Entity inventory is certificate-readable.  Drain state is an authenticated
    operation on BCM 11, so failure leaves the state explicitly ``unknown``;
    it must never be represented as healthy/idle merely because credentials are
    absent.
    """
    inventory = bcm_query.nodes()
    if not inventory.get("ok"):
        return []
    rows: dict[str, dict[str, str]] = {}
    for item in inventory.get("data") or []:
        if not isinstance(item, dict):
            continue
        hostname = str(item.get("hostname") or "").strip()
        if hostname:
            rows[hostname.casefold()] = {
                "node": hostname,
                "state": "unknown",
                "reason": "",
                "rack": str(item.get("rack_id") or ""),
            }
    if not rows:
        return []
    status = bcm_query.node_states()
    if status.get("ok"):
        for item in status.get("data") or []:
            if not isinstance(item, dict):
                continue
            hostname = str(item.get("hostname") or "").strip()
            row = rows.get(hostname.casefold())
            if row is None:
                continue
            row["state"] = str(
                item.get("scheduler_state") or item.get("state") or "unknown"
            ).strip().lower()
            row["reason"] = str(item.get("reason") or "").strip()
    return sorted(rows.values(), key=lambda row: row["node"].casefold())


def node_names() -> list[str]:
    return [row["node"] for row in nodes()]


def expand_nodelist(spec: str) -> list[str]:
    """Expand the bracket ranges used by BCM and the Rack Status naming templates."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in str(spec or ""):
        depth += int(char == "[")
        depth -= int(char == "]")
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current))
    out: list[str] = []
    for token in (part.strip() for part in parts if part.strip()):
        match = re.fullmatch(r"([^\[\]]+)\[([^\]]+)\]([^\[\]]*)", token)
        if not match:
            out.append(token)
            continue
        prefix, inner, suffix = match.groups()
        for piece in inner.split(","):
            piece = piece.strip()
            bounds = re.fullmatch(r"(\d+)-(\d+)", piece)
            if not bounds:
                out.append(f"{prefix}{piece}{suffix}")
                continue
            low, high = bounds.groups()
            if int(high) < int(low) or int(high) - int(low) > 10000:
                raise ValueError(f"invalid or excessive node range {piece}")
            out.extend(
                f"{prefix}{number:0{len(low)}d}{suffix}"
                for number in range(int(low), int(high) + 1)
            )
    return out


def node_state_counts() -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for row in nodes():
        rack = str(row.get("rack") or "").strip()
        if not rack:
            continue
        key = (rack, str(row.get("state") or "unknown"))
        counts[key] = counts.get(key, 0) + 1
    return counts


def terminal_state(value: object) -> bool:
    return str(value or "").strip().upper().split("+", 1)[0] in _TERMINAL_STATES
