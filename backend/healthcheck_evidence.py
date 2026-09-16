# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read the OVA-local single-node health-check mirror as current evidence."""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOG_ROOT = Path("/cluster/OVA/healthchecks/logs")
SOURCE = "nscale16k_healthcheck_index"

_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
_FAILURE_RE = re.compile(r"differs from .*?:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_cache_lock = threading.Lock()
_event_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def log_root() -> Path:
    return Path(os.environ.get("RACK_HEALTHCHECK_LOG_ROOT", "") or DEFAULT_LOG_ROOT)


def _iso_timestamp(raw: str, fallback_ns: int) -> str:
    try:
        epoch = float(raw)
    except (TypeError, ValueError):
        epoch = fallback_ns / 1_000_000_000
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _tail(path: Path, limit: int = 16_384) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _failure_reason(node_dir: Path) -> str:
    matches = _FAILURE_RE.findall(_tail(node_dir / "health.log"))
    if not matches:
        return "The latest single-node health-check run failed."
    # The checker appends execution context after ``agent:``; keep the operator
    # action list concise and leave the raw artifact available as provenance.
    detail = matches[-1].split(" agent:", 1)[0].replace("(no output)", "")
    checks = [item for item in detail.split() if item]
    return "Failed checks: " + ", ".join(checks) if checks else "The latest single-node health-check run failed."


def _event(node_dir: Path) -> dict[str, Any] | None:
    marker = node_dir / ".last-check"
    try:
        stat = marker.stat()
    except OSError:
        return None
    signature = (stat.st_mtime_ns, stat.st_size)
    cache_key = str(marker)
    with _cache_lock:
        cached = _event_cache.get(cache_key)
        if cached and cached[0] == signature:
            return dict(cached[1])
    try:
        parts = marker.read_text(encoding="utf-8", errors="replace").strip().split()
    except OSError:
        return None
    if len(parts) < 2:
        return None
    node = node_dir.name.strip()
    if not _NODE_RE.fullmatch(node):
        return None
    raw_status = parts[1].strip().lower()
    if raw_status in {"passed", "pass", "success", "successful", "done", "healthy"}:
        state, failed, reason = "PASS", False, "All configured single-node health checks passed."
    elif raw_status in {"failed", "fail", "error", "unhealthy"}:
        state, failed, reason = "FAIL", True, _failure_reason(node_dir)
    elif raw_status in {"running", "active", "in_progress"}:
        state, failed, reason = "RUNNING", False, "Single-node health checks are running."
    else:
        state, failed, reason = "UNKNOWN", False, f"Health checker reported unrecognized state: {raw_status or 'empty'}"
    timestamp = _iso_timestamp(parts[0], stat.st_mtime_ns)
    event = {
        "node": node,
        "rack": node.rsplit("-", 1)[0] if "-" in node else node,
        "job_id": f"health-{parts[0]}",
        "activity_id": f"node-health:{parts[0]}",
        "tool": "node-health-check",
        "validation_test": "node-health",
        "scope": "single-node",
        "state": state,
        "started_at": timestamp,
        "ended_at": timestamp,
        "exit_code": "",
        "failed_job": failed,
        "attributed_failure": failed,
        "category": "hardware" if failed else "",
        "reason": reason,
        "source": SOURCE,
        "authoritative": True,
        "history_only": False,
        "artifact_path": str(marker),
        "evidence_timestamp": timestamp,
        "observed_at": timestamp,
        "map_method": "OVA .last-check marker",
    }
    with _cache_lock:
        _event_cache[cache_key] = (signature, event)
    return dict(event)


def post_run_events(*, rack: str = "", node: str = "") -> list[dict[str, Any]]:
    """Return one authoritative latest health-check event per matching node."""
    root = log_root()
    if not root.is_dir():
        raise FileNotFoundError(f"health-check log root is unavailable: {root}")
    node_filter = node.strip().casefold()
    rack_filter = rack.strip().casefold()
    if node_filter:
        candidate = root / node.strip()
        event = _event(candidate) if candidate.is_dir() else None
        return [event] if event else []
    events: list[dict[str, Any]] = []
    for entry in os.scandir(root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        if rack_filter and not entry.name.casefold().startswith(rack_filter + "-"):
            continue
        event = _event(Path(entry.path))
        if event:
            events.append(event)
    events.sort(key=lambda event: str(event["node"]).casefold())
    return events


def clear_cache() -> None:
    with _cache_lock:
        _event_cache.clear()
