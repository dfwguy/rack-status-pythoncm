# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable node issue history and Google Sheets punchcard import.

The punchcard is an input, not the database.  Google exposes the fixed-cell values
(``▲n``, ``✖``, ``⚑`` and ``▦``) through Sheets API v4, but its UI comments are not
returned by either Sheets or Drive for this document.  We therefore retain those
counts/flags as an imported baseline and append every new validation finding here
with its full evidence, validation id and Slurm drain result.

Storage follows the rest of Rack Status: JSON under ``var/cache`` protected by
``flock`` and replaced atomically.  The bearer token is read only server-side and is
never returned from this module.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ISSUES_FILE = Path(
    os.environ.get("RACK_NODE_ISSUES_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "node_issues.json")
)
GSHEETS_TOKEN_FILE = Path(
    os.environ.get("RACK_PUNCHCARD_TOKEN_FILE", "")
    or "/run/secrets/gsheets-token"
)
PUNCHCARD_SPREADSHEET_ID = os.environ.get(
    "RACK_PUNCHCARD_SPREADSHEET_ID",
    "1NSs3c7v6r5mf5h-GKNZHNekVU9XsAAgJp3KyeeJK-PA",
).strip()
PUNCHCARD_RANGE = os.environ.get("RACK_PUNCHCARD_RANGE", "Sheet2!A1:Z1000").strip()
PUNCHCARD_CACHE_SEC = max(60, int(os.environ.get("RACK_PUNCHCARD_CACHE_SEC", "300") or "300"))

_lock = threading.RLock()
_COUNT_RE = re.compile(r"▲\s*(\d+)")
_ROW_RE = re.compile(r"^Row\s+([A-Za-z0-9]+)\b", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "updated_at": "",
        "issues": [],
        "punchcard": {"synced_at": "", "source": "", "nodes": {}},
    }


def _read_locked(handle) -> dict[str, Any]:
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return _empty()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("issues", [])
    data.setdefault("punchcard", {"synced_at": "", "source": "", "nodes": {}})
    return data


def _write_locked(handle, data: dict[str, Any]) -> None:
    data["schema_version"] = 1
    data["updated_at"] = _now_iso()
    blob = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle.seek(0)
    handle.truncate()
    handle.write(blob)
    handle.flush()
    try:
        os.fsync(handle.fileno())
    except OSError:
        pass


def _mutate(fn):
    ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(ISSUES_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = _read_locked(handle)
            result = fn(data)
            _write_locked(handle, data)
            return result
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load() -> dict[str, Any]:
    ISSUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(ISSUES_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            return _read_locked(handle)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def record_issue(
    *,
    node: str,
    rack: str,
    category: str,
    code: str,
    reason: str,
    severity: str,
    source: str,
    validation_run_id: str = "",
    scheduler_ref: str = "",
    evidence: str = "",
    drained: bool = False,
    slurm_reason: str = "",
    drain_result: str = "",
    operator: str = "rack-status",
) -> dict[str, Any]:
    """Append one immutable finding and return it."""
    entry = {
        "issue_id": uuid.uuid4().hex,
        "node": str(node).strip()[:128],
        "rack": str(rack).strip()[:128],
        "category": str(category).strip().lower()[:32] or "hardware",
        "code": str(code).strip()[:64] or "validation_failure",
        "reason": str(reason).strip()[:2000],
        "severity": str(severity).strip().lower()[:16] or "failure",
        "source": str(source).strip()[:64] or "deep-audit",
        "validation_run_id": str(validation_run_id).strip()[:128],
        "scheduler_ref": str(scheduler_ref).strip()[:64],
        "evidence": str(evidence).strip()[:8000],
        "drained": bool(drained),
        "slurm_reason": str(slurm_reason).strip()[:256],
        "drain_result": str(drain_result).strip()[:2000],
        "operator": str(operator).strip()[:64] or "rack-status",
        "created_at": _now_iso(),
    }

    def append(data: dict[str, Any]):
        issues = data.setdefault("issues", [])
        if not isinstance(issues, list):
            issues = data["issues"] = []
        issues.append(entry)
        # Keep years of normal rack work without allowing accidental unbounded growth.
        if len(issues) > 20000:
            del issues[:-20000]
        return dict(entry)

    return _mutate(append)


def invalidate_validation_runs(
    run_ids: list[str] | set[str] | tuple[str, ...], *, reason: str, operator: str = "rack-status"
) -> dict[str, Any]:
    """Retract findings produced by invalid validator runs without deleting evidence."""
    wanted = {str(run_id).strip() for run_id in run_ids if str(run_id).strip()}
    if not wanted:
        return {"invalidated": 0, "run_ids": []}
    invalidated_at = _now_iso()

    def mark(data: dict[str, Any]):
        count = 0
        for issue in data.setdefault("issues", []):
            if not isinstance(issue, dict) or str(issue.get("validation_run_id") or "") not in wanted:
                continue
            issue["invalidated_at"] = invalidated_at
            issue["invalidation_reason"] = str(reason).strip()[:2000]
            issue["invalidated_by"] = str(operator).strip()[:64] or "rack-status"
            count += 1
        return {"invalidated": count, "run_ids": sorted(wanted), "invalidated_at": invalidated_at}

    return _mutate(mark)


def _rack_for_node(node: str) -> str:
    return node.rsplit("-", 1)[0] if "-" in node else node


def _prefix_from_rack(rack: str) -> str:
    """``ink50-dh01-h01`` -> ``ink50-dh01`` for punchcard row/rack cells."""
    parts = [p for p in str(rack).strip().split("-") if p]
    return "-".join(parts[:-1]) if len(parts) >= 2 else str(rack).strip()


def _fetch_sheet_values() -> list[list[str]]:
    token = GSHEETS_TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("punchcard token file is empty")
    encoded_range = urllib.parse.quote(PUNCHCARD_RANGE, safe="")
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        f"{PUNCHCARD_SPREADSHEET_ID}/values/{encoded_range}?majorDimension=ROWS"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    rows = body.get("values") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise TypeError("punchcard response has no values array")
    return [[str(cell) for cell in row] for row in rows if isinstance(row, list)]


def _parse_punchcard(rows: list[list[str]], prefix: str) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    row_name = ""
    rack_cols: list[str] = []
    for row in rows:
        first = row[0].strip() if row else ""
        header = _ROW_RE.match(first)
        if header:
            row_name = header.group(1).lower()
            rack_cols = [cell.strip() for cell in row[1:]]
            continue
        if not row_name or not rack_cols or not first.isdigit():
            continue
        tray = first.zfill(2)
        for idx, rack_num in enumerate(rack_cols, start=1):
            if not rack_num or idx >= len(row):
                continue
            cell = row[idx].strip()
            if not cell:
                continue
            match = _COUNT_RE.search(cell)
            count = int(match.group(1)) if match else 0
            node = f"{prefix}-{row_name}{rack_num.zfill(2)}-{tray}"
            nodes[node] = {
                "count": count,
                "drained": "✖" in cell,
                "nccl": "⚑" in cell,
                "maintenance": "▦" in cell,
                "cell_value": cell[:128],
            }
    return nodes


def sync_punchcard(rack: str, *, force: bool = False) -> dict[str, Any]:
    """Refresh imported counts/flags for the active cluster prefix."""
    current = load().get("punchcard") or {}
    synced_at = str(current.get("synced_at") or "")
    if synced_at and not force:
        try:
            age = time.time() - datetime.fromisoformat(synced_at).timestamp()
            if age < PUNCHCARD_CACHE_SEC:
                return {"ok": True, "cached": True, "synced_at": synced_at,
                        "nodes": len(current.get("nodes") or {})}
        except (TypeError, ValueError):
            pass

    prefix = _prefix_from_rack(rack)
    if not prefix:
        raise ValueError("rack is required to derive the punchcard node prefix")
    parsed = _parse_punchcard(_fetch_sheet_values(), prefix)
    now = _now_iso()

    def replace(data: dict[str, Any]):
        data["punchcard"] = {
            "synced_at": now,
            "source": f"google:{PUNCHCARD_SPREADSHEET_ID}:{PUNCHCARD_RANGE}",
            "prefix": prefix,
            "nodes": parsed,
        }

    _mutate(replace)
    return {"ok": True, "cached": False, "synced_at": now, "nodes": len(parsed)}


def query(*, rack: str = "", node: str = "") -> dict[str, Any]:
    """Return per-node rollups plus immutable local finding history."""
    data = load()
    rack = str(rack).strip()
    node = str(node).strip()
    raw_issues = data.get("issues") or []
    issues = [i for i in raw_issues if isinstance(i, dict) and not i.get("invalidated_at")]
    baseline = (data.get("punchcard") or {}).get("nodes") or {}
    if rack:
        issues = [i for i in issues if str(i.get("rack") or _rack_for_node(str(i.get("node") or ""))) == rack]
        baseline = {n: b for n, b in baseline.items() if _rack_for_node(str(n)) == rack}
    if node:
        issues = [i for i in issues if i.get("node") == node]
        baseline = {n: b for n, b in baseline.items() if n == node}
    issues.sort(key=lambda i: str(i.get("created_at") or ""), reverse=True)

    by_node: dict[str, dict[str, Any]] = {}
    all_nodes = set(baseline) | {str(i.get("node") or "") for i in issues}
    for node_name in sorted(n for n in all_nodes if n):
        imported = baseline.get(node_name) if isinstance(baseline, dict) else {}
        imported = imported if isinstance(imported, dict) else {}
        local = [i for i in issues if i.get("node") == node_name]
        by_node[node_name] = {
            "node": node_name,
            "rack": _rack_for_node(node_name),
            "issue_count": int(imported.get("count") or 0) + len(local),
            "imported_count": int(imported.get("count") or 0),
            "local_count": len(local),
            "drained": bool(imported.get("drained")) or any(bool(i.get("drained")) for i in local),
            "nccl": bool(imported.get("nccl")) or any(i.get("category") == "network" for i in local),
            "maintenance": bool(imported.get("maintenance")),
            "latest": local[0] if local else None,
        }

    affected = [v for v in by_node.values() if v["issue_count"] > 0 or v["drained"]]
    return {
        "updated_at": data.get("updated_at") or "",
        "punchcard_synced_at": (data.get("punchcard") or {}).get("synced_at") or "",
        "summary": {
            "nodes_tracked": len(by_node),
            "nodes_with_issues": len(affected),
            "currently_flagged_drained": sum(1 for v in by_node.values() if v["drained"]),
            "repeat_nodes": sum(1 for v in by_node.values() if v["issue_count"] > 1),
            "local_findings": len(issues),
            "hardware_findings": sum(1 for i in issues if i.get("category") == "hardware"),
            "network_findings": sum(1 for i in issues if i.get("category") == "network"),
        },
        "nodes": by_node,
        "issues": issues,
    }
