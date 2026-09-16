# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Hybrid Nscale scheduler runtime facts used by Rack Status routes.

The Nscale frontend calls provider-neutral scheduler URLs. Historical Slurm URL
aliases remain for client compatibility. PythonCM is the primary provider;
numeric Slurm references are accepted only for explicit feature fallbacks.

PythonCM command UUIDs are opaque scheduler references.  Completed and active
work is reconciled from Rack Status' durable validation-activity registry;
node identity and drain state come from CMDaemon through :mod:`bcm_query`.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import bcm_query

_PYTHONCM_REF_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_SLURM_REF_RE = re.compile(r"^[1-9][0-9]*(?:_[0-9]+)?$")
_OPEN_STATES = frozenset({
    "PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "SUBMITTED", "COLLECTING",
})
_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY",
    "BOOT_FAIL", "DEADLINE", "PREEMPTED", "UNKNOWN",
})
_INVENTORY_LOCK = threading.Lock()
_INVENTORY_CACHE: tuple[float, list[dict[str, str]]] = (0.0, [])
_INVENTORY_SOURCE: tuple[object, object] = (None, None)
_TOPOLOGY_LOCK = threading.Lock()
_TOPOLOGY_CACHE: tuple[float, dict[str, list[Any]]] = (
    0.0,
    {"racks": [], "node_groups": []},
)
_TOPOLOGY_SOURCE: tuple[int, int] = (0, 0)


def scheduler_kind() -> str:
    # Nscale fail-safe: an omitted setting must never activate a legacy provider.
    kind = (os.environ.get("RACK_SCHEDULER") or "pythoncm").strip().lower()
    cluster_id = (os.environ.get("RACK_CLUSTER_ID") or "").strip().lower()
    customer = (os.environ.get("RACK_CLUSTER_CUSTOMER_NAME") or "").strip().lower()
    if (cluster_id == "nscale-tx" or customer == "nscale") and kind != "pythoncm":
        raise RuntimeError(
            f"Nscale TX requires RACK_SCHEDULER=pythoncm; refusing unsafe value {kind!r}"
        )
    return kind


def is_pythoncm() -> bool:
    return scheduler_kind() == "pythoncm"


def reference_kind(value: object) -> str | None:
    text = str(value or "").strip()
    if _PYTHONCM_REF_RE.fullmatch(text):
        return "pythoncm"
    if _SLURM_REF_RE.fullmatch(text):
        return "slurm"
    return None


def valid_reference(value: object) -> bool:
    """Accept native PythonCM UUIDs and explicit Slurm fallback job IDs."""
    return reference_kind(value) is not None


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
        # Provider provenance is mandatory: a UUID cannot masquerade as Slurm,
        # and a numeric fallback id cannot masquerade as PythonCM.
        activity_scheduler = str(activity.get("scheduler") or "").strip().lower()
        if activity_scheduler not in {"pythoncm", "slurm"}:
            continue
        for task in activity.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            ref = str(task.get("scheduler_ref") or "").strip()
            if reference_kind(ref) != activity_scheduler or ref in seen:
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
                "start": str(
                    task.get("started_at")
                    or activity.get("started_running_at")
                    or activity.get("started_at")
                    or activity.get("created_at")
                    or ""
                ),
                "end": str(task.get("finished_at") or activity.get("finished_at") or ""),
                "elapsed": _elapsed(task, activity),
                "exit_code": str(task.get("returncode") if task.get("returncode") is not None else ""),
                "stdout": str(task.get("stdout_path") or ""),
                "activity_id": str(activity.get("activity_id") or ""),
                "scheduler": activity_scheduler,
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
    """Return ``(state, stdout_path)`` from the reference's native provider."""
    ref = str(reference or "").strip()
    if not valid_reference(ref):
        raise ValueError("invalid scheduler reference")
    if reference_kind(ref) == "slurm":
        import shlex

        import host_exec
        from platforms import get_platform

        proc = host_exec.run_slurm_on_host(
            get_platform().scontrol_cmd(f"show job -o {shlex.quote(ref)}"), timeout=60
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "scontrol failed").strip())
        state_match = re.search(r"\bJobState=(\S+)", proc.stdout or "")
        stdout_match = re.search(r"\bStdOut=(\S+)", proc.stdout or "")
        if not state_match:
            raise RuntimeError("scontrol returned no JobState")
        stdout = stdout_match.group(1) if stdout_match else ""
        return state_match.group(1).upper(), ("" if stdout in {"/dev/null", "(none)"} else stdout)
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
    if reference_kind(ref) == "slurm":
        import shlex

        import host_exec
        from platforms import get_platform

        proc = host_exec.run_slurm_on_host(
            get_platform().scancel_cmd(shlex.quote(ref)), timeout=60
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "scancel failed").strip())
        return
    import host_exec
    from schedulers import JobHandle, build_scheduler

    scheduler = build_scheduler(
        "pythoncm", connection=host_exec.connection("control"), profile="control"
    )
    scheduler.cancel(JobHandle(ref, "pythoncm"))


def active_job_times(wanted: Iterable[str] | None = None) -> tuple[set[str], dict[str, str]]:
    selected = {str(item).strip() for item in (wanted or []) if str(item).strip()}
    # The 30-second Racks path uses only this durable registry snapshot. Include
    # known Slurm fallback allocations so the UI does not hide them, but never
    # poll squeue/sacct/scontrol from this recurring path.
    rows = jobs(active_only=True)
    if selected:
        rows = [row for row in rows if row["job_id"] in selected]
    refs = {str(row["job_id"]) for row in rows}
    return refs, {str(row["job_id"]): str(row.get("elapsed") or "?") for row in rows}


def _inventory_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("RACK_PYTHONCM_STATUS_TTL_SEC", "25")))
    except ValueError:
        return 25.0


def _load_nodes() -> list[dict[str, str]]:
    """Physical nodes with normalized scheduling state and reason.

    Entity inventory and physical device status are certificate-readable on the
    Nscale BCM 11 controller.  A missing drain result may be represented as
    physical ``up``, but never as scheduler-eligible ``idle``; operation admission
    therefore remains fail-closed without an explicit UNDRAINED result.
    """
    inventory = bcm_query.nodes()
    if not inventory.get("ok"):
        return []
    rows: dict[str, dict[str, str]] = {}
    import nvis_id_templates

    naming = nvis_id_templates.get_nvis_naming()
    matcher = naming.matcher()
    for item in inventory.get("data") or []:
        if not isinstance(item, dict):
            continue
        hostname = str(item.get("hostname") or "").strip()
        if hostname:
            physical_rack = str(item.get("rack_id") or "").strip()
            hostname_match = matcher.match_hostname(hostname)
            rows[hostname.casefold()] = {
                "node": hostname,
                "state": "unknown",
                "reason": "",
                # PythonCM's Rack entity is the warehouse coordinate (WH/EH),
                # while Rack Status actions/logs use the logical rack prefix
                # from the real compute hostname (ink50-dhXX-xNN).
                "rack": hostname_match.rack_id if hostname_match else physical_rack,
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
            if item.get("device_status"):
                row["device_status"] = str(item["device_status"]).strip().upper()

    # PythonCM ProgramRunner work is the only allocation source in PythonCM mode.
    # Do not consult cluster.workload/WlmCluster here: on Nscale that is the Slurm
    # workload manager exposed through PythonCM, which would make the Racks refresh
    # Slurm-derived even though no CLI was executed.
    active_nodes: set[str] = set()
    for job_row in jobs(active_only=True):
        for node in job_row.get("nodes") or []:
            active_nodes.add(str(node).casefold())
    for key in active_nodes:
        row = rows.get(key)
        if row is not None and row["state"] not in {"down", "drain", "draining"}:
            row["state"] = "alloc"
    return sorted(rows.values(), key=lambda row: row["node"].casefold())


def nodes(*, force: bool = False) -> list[dict[str, str]]:
    """Cached PythonCM physical-node status used by every dashboard refresh worker."""
    global _INVENTORY_CACHE, _INVENTORY_SOURCE
    # Source identity and cached data are one consistency boundary.  Tests swap
    # the BCM readers, and production can refresh while multiple dashboard
    # workers call this function; checking the source outside the lock allowed
    # another thread to repopulate an old idle result after invalidation.
    with _INVENTORY_LOCK:
        now = time.monotonic()
        # Keep the callable objects themselves.  Storing only ``id()`` lets a
        # later replacement reuse a freed object's address and appear unchanged,
        # which can replay a cached eligible state from a different query source.
        source = (bcm_query.nodes, bcm_query.node_states)
        if source != _INVENTORY_SOURCE:
            _INVENTORY_CACHE = (0.0, [])
            _INVENTORY_SOURCE = source
        cached_at, cached = _INVENTORY_CACHE
        if not force and cached and now - cached_at < _inventory_ttl():
            return [dict(row) for row in cached]
        fresh = _load_nodes()
        if fresh:
            _INVENTORY_CACHE = (time.monotonic(), fresh)
            return [dict(row) for row in fresh]
        # Preserve only cached identity on a transient CMDaemon/auth failure.
        # Replaying an old idle/allocated/drained value as current would make a
        # failed PythonCM refresh look healthy to the 30-second Racks poll.
        stale: list[dict[str, str]] = []
        for cached_row in cached:
            row = dict(cached_row)
            row["state"] = "unknown"
            row["reason"] = "PythonCM inventory refresh unavailable"
            row["stale"] = "true"
            stale.append(row)
        return stale


def _topology_ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")))
    except ValueError:
        return 900.0


def _expected_racks_from_node_groups(
    node_groups: list[dict[str, Any]], naming: Any
) -> set[str]:
    """Expand an opt-in site rack-number contract across authoritative SU rows."""
    raw = (os.environ.get("RACK_NATIVE_EXPECTED_RACK_NUMBERS") or "").strip()
    if not raw:
        return set()
    numbers: set[int] = set()
    for token in (part.strip() for part in raw.split(",") if part.strip()):
        if "-" in token:
            left, right = token.split("-", 1)
            if not left.isdigit() or not right.isdigit():
                continue
            start, end = int(left), int(right)
            numbers.update(range(min(start, end), max(start, end) + 1))
        elif token.isdigit():
            numbers.add(int(token))
    if not numbers:
        return set()
    matcher = naming.matcher()
    expected: set[str] = set()
    for group in node_groups:
        if not re.fullmatch(r"SU\d+", str(group.get("name") or "").strip(), re.IGNORECASE):
            continue
        row_key = ""
        for raw_node in group.get("nodes") or []:
            matched = matcher.match_hostname(str(raw_node or "").strip())
            if matched is not None:
                parts = naming.rack_parts(matched.rack_id)
                row_key = parts.row_key if parts is not None else ""
                if row_key:
                    break
        if row_key:
            expected.update(f"{row_key}{number:02d}" for number in numbers if 0 < number < 100)
    return expected


def _load_topology() -> dict[str, list[Any]]:
    """Read racks and NodeGroups together, with a racks-only compatibility fallback."""
    result = bcm_query.topology()
    data = result.get("data") if result.get("ok") else None
    if isinstance(data, dict):
        physical_racks = sorted(
            {
                str(item.get("name") or "").strip()
                for item in (data.get("racks") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            },
            key=str.casefold,
        )
        node_groups = [
            {
                "name": str(item.get("name") or "").strip(),
                "nodes": [
                    str(node).strip()
                    for node in (item.get("nodes") or [])
                    if str(node).strip()
                ],
            }
            for item in (data.get("node_groups") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        import nvis_id_templates

        naming = nvis_id_templates.get_nvis_naming()
        rack_group_labels = naming.rack_labels_from_node_groups(node_groups)
        logical_racks_from_nodes: set[str] = set()
        matcher = naming.matcher()
        for raw in data.get("nodes") or []:
            matched = matcher.match_hostname(str(raw or "").strip())
            if matched is not None:
                logical_racks_from_nodes.add(matched.rack_id)
        logical_racks = sorted(
            set(rack_group_labels)
            | logical_racks_from_nodes
            | _expected_racks_from_node_groups(node_groups, naming),
            key=naming.rack_sort_key,
        )
        if logical_racks or physical_racks:
            return {
                # Nscale's UI/action namespace is the hostname-derived logical
                # rack. BCM Rack entities (EH/WH coordinates) remain physical
                # inventory and must not become validation targets.
                "racks": logical_racks or physical_racks,
                "node_groups": node_groups,
            }

    # Older deployed query helpers do not know ``topology`` yet. Preserve rack
    # inventory while making the missing SU labels explicit through an empty map.
    fallback = bcm_query.racks()
    racks = sorted(
        {
            str(item.get("name") or "").strip()
            for item in (fallback.get("data") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        },
        key=str.casefold,
    ) if fallback.get("ok") else []
    return {"racks": racks, "node_groups": []}


def _topology(*, force: bool = False, cached_only: bool = False) -> dict[str, list[Any]]:
    """Slow-changing PythonCM topology, shared by rack and NodeGroup consumers."""
    global _TOPOLOGY_CACHE, _TOPOLOGY_SOURCE
    now = time.monotonic()
    source = (id(bcm_query.topology), id(bcm_query.racks))
    if source != _TOPOLOGY_SOURCE:
        _TOPOLOGY_CACHE = (0.0, {"racks": [], "node_groups": []})
        _TOPOLOGY_SOURCE = source
    cached_at, cached = _TOPOLOGY_CACHE
    if cached_only or (
        not force and cached.get("racks") and now - cached_at < _topology_ttl()
    ):
        return {
            "racks": list(cached.get("racks") or []),
            "node_groups": [dict(group) for group in cached.get("node_groups") or []],
        }
    with _TOPOLOGY_LOCK:
        cached_at, cached = _TOPOLOGY_CACHE
        if not force and cached.get("racks") and now - cached_at < _topology_ttl():
            return {
                "racks": list(cached.get("racks") or []),
                "node_groups": [dict(group) for group in cached.get("node_groups") or []],
            }
        fresh = _load_topology()
        if fresh.get("racks"):
            _TOPOLOGY_CACHE = (time.monotonic(), fresh)
            cached = fresh
        return {
            "racks": list(cached.get("racks") or []),
            "node_groups": [dict(group) for group in cached.get("node_groups") or []],
        }


def rack_names(*, force: bool = False) -> list[str]:
    """Rack entities from CMDaemon, cached independently of fast node status."""
    return [str(name) for name in _topology(force=force)["racks"]]


def node_groups(*, force: bool = False, cached_only: bool = False) -> list[dict[str, Any]]:
    """PythonCM NodeGroups sharing the slow-changing rack-topology cache."""
    return [dict(group) for group in _topology(force=force, cached_only=cached_only)["node_groups"]]


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
