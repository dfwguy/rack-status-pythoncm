# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable, evidence-attributed post-run node history.

The validation activity ledger tells us which validation was launched and Slurm
accounting tells us which exact nodes were allocated.  This module snapshots the
join so node participation history survives Slurm's accounting retention window.

A failed multi-node job is deliberately *not* counted as a failure for every
allocated node.  Failure attribution requires either an explicit node mention in
the validation evidence, a single-node allocation, or a structured node finding
(deep audit/operator mark).  This keeps a rack-wide control-plane reset from
turning 256 innocent trays red.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = Path(
    os.environ.get("RACK_POST_RUN_HISTORY_FILE", "")
    or str(REPO_ROOT / "var" / "history" / "post_run_nodes.json")
)
MAX_EVENTS = max(1000, int(os.environ.get("RACK_POST_RUN_MAX_EVENTS", "100000") or "100000"))

_lock = threading.RLock()
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")
_TERMINAL_FAIL = {
    "FAILED", "NODE_FAIL", "BOOT_FAIL", "OUT_OF_MEMORY", "TIMEOUT",
    "PREEMPTED", "DEADLINE",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty() -> dict[str, Any]:
    return {"schema_version": 1, "updated_at": "", "events": []}


def _read(handle) -> dict[str, Any]:
    handle.seek(0)
    raw = handle.read()
    if not raw.strip():
        return _empty()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return _empty()
    return data


def _write(handle, data: dict[str, Any]) -> None:
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


def load_events() -> list[dict[str, Any]]:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(HISTORY_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            return [dict(row) for row in _read(handle).get("events", []) if isinstance(row, dict)]
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def merge_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """UPSERT node/run participation events and return merge statistics."""
    clean: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        node = str(raw.get("node") or "").strip()
        job_id = str(raw.get("job_id") or "").strip()
        activity_id = str(raw.get("activity_id") or "").strip()
        if not _NODE_RE.fullmatch(node) or not (job_id or activity_id):
            continue
        row = dict(raw)
        row["node"] = node
        row["rack"] = str(row.get("rack") or rack_for_node(node)).strip()
        row["job_id"] = job_id[:64]
        row["activity_id"] = activity_id[:128]
        row["updated_at"] = _now_iso()
        clean.append(row)

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock, open(HISTORY_FILE, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            data = _read(handle)
            indexed: dict[str, dict[str, Any]] = {}
            for row in data.get("events", []):
                if not isinstance(row, dict):
                    continue
                key = event_key(row)
                if key:
                    indexed[key] = row
            before = len(indexed)
            for row in clean:
                indexed[event_key(row)] = row
            rows = list(indexed.values())
            rows.sort(key=lambda row: str(row.get("started_at") or row.get("updated_at") or ""))
            if len(rows) > MAX_EVENTS:
                rows = rows[-MAX_EVENTS:]
            data["events"] = rows
            _write(handle, data)
            return {
                "before": before,
                "after": len(rows),
                "merged": len(clean),
                "path": str(HISTORY_FILE),
                "updated_at": data.get("updated_at") or "",
            }
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def event_key(row: dict[str, Any]) -> str:
    node = str(row.get("node") or "").strip().casefold()
    job = str(row.get("job_id") or "").strip()
    activity = str(row.get("activity_id") or "").strip()
    return f"{node}|{job}|{activity}" if node and (job or activity) else ""


def rack_for_node(node: str) -> str:
    text = str(node or "").strip()
    return text.rsplit("-", 1)[0] if "-" in text else text


def split_hostlist(spec: str) -> list[str]:
    """Expand common Slurm hostlists without another ``scontrol`` round trip."""
    text = str(spec or "").strip()
    if not text or text in {"(null)", "None", "n/a"}:
        return []

    def top_parts(value: str) -> list[str]:
        out: list[str] = []
        depth = 0
        start = 0
        for idx, char in enumerate(value):
            if char == "[":
                depth += 1
            elif char == "]":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                out.append(value[start:idx])
                start = idx + 1
        out.append(value[start:])
        return [part.strip() for part in out if part.strip()]

    def expand_one(value: str) -> list[str]:
        left = value.find("[")
        if left < 0:
            return [value]
        right = value.find("]", left + 1)
        if right < 0:
            return [value]
        prefix, body, suffix = value[:left], value[left + 1:right], value[right + 1:]
        choices: list[str] = []
        for token in body.split(","):
            token = token.strip()
            match = re.fullmatch(r"(-?\d+)-(-?\d+)", token)
            if not match:
                choices.append(token)
                continue
            lo_s, hi_s = match.groups()
            lo, hi = int(lo_s), int(hi_s)
            width = max(len(lo_s.lstrip("-")), len(hi_s.lstrip("-")))
            step = 1 if hi >= lo else -1
            choices.extend(f"{num:0{width}d}" for num in range(lo, hi + step, step))
        out: list[str] = []
        for choice in choices:
            out.extend(expand_one(prefix + choice + suffix))
        return out

    result: list[str] = []
    seen: set[str] = set()
    for part in top_parts(text):
        for node in expand_one(part):
            key = node.casefold()
            if _NODE_RE.fullmatch(node) and key not in seen:
                seen.add(key)
                result.append(node)
    return result


def category_for(tool: str, state: str, evidence: str) -> str:
    low = f"{tool} {state} {evidence}".lower()
    if any(mark in low for mark in ("cmdaemon", "cmd daemon", "node was stopped", "mass-down")):
        return "control_plane"
    if any(mark in low for mark in ("nccl", "rdma", "infiniband", "ibv_", "mlx5", "network", "link flap")):
        return "network"
    if any(mark in low for mark in ("cuda", "gpu", "xid", "ecc", "nvlink", "nvswitch", "fabric")):
        return "gpu"
    if any(mark in low for mark in ("thermal", "temperature", "cooling", "overheat")):
        return "thermal"
    if any(mark in low for mark in ("pcie", "aer", "dpc", "nvme", "raid", "storage")):
        return "hardware"
    return "validation"


def events_from_accounting(
    activities: list[dict[str, Any]], accounting: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Join durable activity metadata to exact Slurm allocations."""
    by_job: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for activity in activities:
        for task in activity.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            jid = str(task.get("scheduler_ref") or "").split(".")[0].strip()
            if jid:
                by_job[jid] = (activity, task)

    out: list[dict[str, Any]] = []
    for job in accounting:
        jid = str(job.get("job_id") or "").split(".")[0].strip()
        pair = by_job.get(jid)
        if not pair:
            continue
        activity, task = pair
        nodes = split_hostlist(str(job.get("nodelist") or ""))
        if not nodes:
            continue
        state = str(job.get("state") or task.get("scheduler_state") or "UNKNOWN").upper().split("+")[0]
        evidence = "\n".join(
            str(activity.get(key) or "")
            for key in ("issue_summary", "outcome_summary", "stdout_tail", "stderr_tail")
        )
        evidence += "\n" + str(task.get("message") or "")
        explicit = {node.casefold() for node in nodes if node.casefold() in evidence.casefold()}
        failed_job = state in _TERMINAL_FAIL
        for node in nodes:
            attributed = bool(failed_job and (len(nodes) == 1 or node.casefold() in explicit))
            category = category_for(str(activity.get("tool") or ""), state, evidence)
            reason = ""
            if attributed:
                reason = (
                    str(activity.get("issue_summary") or "").strip()
                    or str(task.get("message") or "").strip()
                    or f"{activity.get('tool') or 'validation'} job {jid} ended {state} and explicitly implicated {node}"
                )[:2000]
            out.append(
                {
                    "node": node,
                    "rack": rack_for_node(node),
                    "job_id": jid,
                    "activity_id": str(activity.get("activity_id") or ""),
                    "tool": str(activity.get("tool") or "external"),
                    "scope": str(activity.get("scope") or ""),
                    "state": state,
                    "started_at": str(job.get("started_at") or activity.get("created_at") or ""),
                    "ended_at": str(job.get("ended_at") or activity.get("finished_at") or ""),
                    "exit_code": str(job.get("exit_code") or ""),
                    "failed_job": failed_job,
                    "attributed_failure": attributed,
                    "category": category if attributed else "",
                    "reason": reason,
                    "source": "validation_activity+slurm",
                }
            )
    return out


POOL_GROUP = "pool"
UNPLACED_GROUP = "unplaced"


def _natural_node_key(name: str) -> list[tuple[int, int, str]]:
    """gpu9 before gpu10: digit runs compare as numbers."""
    return [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", name.casefold()) if part]


def placement_from_snapshot(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Where each node sits on the floor, read from the cluster snapshot the Racks page draws.

    The post-run report used to name a node's rack by stripping the last ``-`` component of its
    hostname. That is the ``direct`` mapping's rule and nobody else's: on a ``translate`` cluster
    like Nscale NC (``sys1-gso1-p-phy-gpu100-su20``) it grouped the same node number across every
    SU, and on a ``pool`` cluster it invented racks where none exist. The snapshot's ``nodes``
    already carry the placement the cluster's mapping mode, naming templates and provider produced
    (``native_status.build_node_inventory``), so the report reads that instead of re-deriving it.

    Returns ``{"mode", "nodes": {host.casefold(): {"rack", "rack_unit", "tray"}}, "rack_order",
    "su_by_rack": {rack: {"id", "label"}}}``. A snapshot without an inventory yields an empty
    ``nodes`` map, which the caller reports as unplaced rather than guessing.
    """
    payload = payload if isinstance(payload, dict) else {}
    mode = str(payload.get("rack_mapping_mode") or "direct")
    nodes: dict[str, dict[str, Any]] = {}
    inventory = payload.get("nodes")
    for entry in inventory if isinstance(inventory, list) else []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        nodes[str(entry["id"]).casefold()] = {
            "rack": str(entry.get("rack") or ""),
            "rack_unit": str(entry.get("rack_unit") or ""),
            "tray": entry.get("tray") if isinstance(entry.get("tray"), int) else None,
        }
    rack_order = [
        str(row.get("rack"))
        for row in payload.get("rows") or []
        if isinstance(row, dict) and row.get("rack")
    ]
    su_by_rack: dict[str, dict[str, str]] = {}
    for unit in payload.get("scalable_units") or []:
        if not isinstance(unit, dict):
            continue
        for rack in unit.get("racks") or []:
            su_by_rack[str(rack)] = {
                "id": str(unit.get("id") or ""),
                "label": str(unit.get("label") or unit.get("id") or ""),
            }
    return {
        "mode": mode if mode in {"direct", "translate", "pool"} else "direct",
        "nodes": nodes,
        "rack_order": rack_order,
        "su_by_rack": su_by_rack,
    }


def group_nodes(
    rows: list[dict[str, Any]], placement: dict[str, Any]
) -> list[dict[str, Any]]:
    """Group node rows the way the cluster's mapping mode places them.

    * ``pool`` -- no placement exists, so every node goes in the one pool card (a rack here would
      assert a physical fact the cluster does not have).
    * ``direct`` / ``translate`` -- one group per rack, in the Racks page's order, tagged with its
      Scalable Unit when the cluster models one; a node the mapping could not place is kept in an
      "Unplaced" group, never folded into a neighbour.
    """
    mode = placement.get("mode") or "direct"
    if mode == "pool":
        if not rows:
            return []
        return [{
            "rack": POOL_GROUP, "label": "Node pool", "kind": "pool",
            "scalable_unit": None, "nodes": rows,
        }]
    by_rack: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_rack.setdefault(str(row.get("rack") or ""), []).append(row)
    order = {rack: i for i, rack in enumerate(placement.get("rack_order") or [])}
    su_by_rack = placement.get("su_by_rack") or {}
    groups: list[dict[str, Any]] = []
    for rack in sorted((r for r in by_rack if r), key=lambda r: (order.get(r, len(order)), r)):
        groups.append({
            "rack": rack, "label": rack, "kind": "rack",
            "scalable_unit": su_by_rack.get(rack), "nodes": by_rack[rack],
        })
    if "" in by_rack:
        groups.append({
            "rack": UNPLACED_GROUP, "label": "Unplaced", "kind": "unplaced",
            "scalable_unit": None, "nodes": by_rack[""],
        })
    return groups


def build_report(
    *,
    events: list[dict[str, Any]],
    inventory: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    audit_results: list[dict[str, Any]],
    rack: str = "",
    node: str = "",
    placement: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rack_filter = rack.strip().casefold()
    node_filter = node.strip().casefold()
    placement = placement or placement_from_snapshot(None)
    placed = placement.get("nodes") or {}
    pool = placement.get("mode") == "pool"
    by_node: dict[str, dict[str, Any]] = {}

    def ensure(raw_node: str, _raw_rack: str = "") -> dict[str, Any] | None:
        # The rack a stored event, finding or audit carries was derived from the hostname when it
        # was written, so it is ignored: placement is the cluster's current mapping, and a node
        # moved between racks must not keep its old group in the history.
        name = str(raw_node or "").strip()
        if not _NODE_RE.fullmatch(name):
            return None
        where = placed.get(name.casefold()) or {}
        rk = "" if pool else str(where.get("rack") or "")
        if rack_filter and rk.casefold() != rack_filter and not (pool and rack_filter == POOL_GROUP):
            return None
        if node_filter and name.casefold() != node_filter:
            return None
        key = name.casefold()
        return by_node.setdefault(
            key,
            {
                "node": name,
                "rack": rk,
                "rack_unit": str(where.get("rack_unit") or ""),
                "tray": where.get("tray"),
                "slurm_state": "unknown",
                "slurm_reason": "",
                "runs": [],
                "findings": [],
                "hardware_status": None,
                "network_status": None,
            },
        )

    for row in inventory:
        rec = ensure(str(row.get("node") or ""), str(row.get("rack") or ""))
        if rec is not None:
            rec["slurm_state"] = str(row.get("state") or "unknown")
            rec["slurm_reason"] = str(row.get("reason") or "")

    for event in events:
        rec = ensure(str(event.get("node") or ""), str(event.get("rack") or ""))
        if rec is not None:
            rec["runs"].append(dict(event))

    for finding in findings:
        if finding.get("invalidated_at"):
            continue
        rec = ensure(str(finding.get("node") or ""), str(finding.get("rack") or ""))
        if rec is not None:
            rec["findings"].append(dict(finding))

    for audit in audit_results:
        rec = ensure(str(audit.get("node") or ""), str(audit.get("rack") or ""))
        if rec is None:
            continue
        kind = str(audit.get("kind") or "").lower()
        key = "hardware_status" if kind == "hardware" else "network_status"
        prior = rec.get(key)
        if not isinstance(prior, dict) or str(audit.get("started_at") or "") > str(prior.get("started_at") or ""):
            rec[key] = dict(audit)

        # Deep audits are first-class validation participations too.  They are
        # launched by the workflow engine rather than validation_activities, so
        # add their durable record here instead of leaving a successful Slurm
        # allocation invisible in the node's run history.
        run_id = str(audit.get("run_id") or "").strip()
        if run_id and not any(
            str(row.get("activity_id") or "") == run_id for row in rec["runs"]
        ):
            status = str(audit.get("status") or "inconclusive").lower()
            run_state = str(audit.get("run_state") or status).upper()
            attributed = status == "fail"
            rec["runs"].append({
                "node": rec["node"],
                "rack": rec["rack"],
                "job_id": str(audit.get("scheduler_ref") or ""),
                "activity_id": run_id,
                "tool": str(audit.get("workflow_id") or f"{kind}-deep-audit"),
                "scope": "workflow",
                "state": run_state,
                "started_at": str(audit.get("started_at") or ""),
                "ended_at": str(audit.get("updated_at") or ""),
                "exit_code": "",
                "failed_job": run_state in _TERMINAL_FAIL,
                "attributed_failure": attributed,
                "category": kind if attributed else "",
                "reason": str(audit.get("reason") or "") if attributed else "",
                "source": "deep-audit-workflow",
                "output_run_id": run_id,
                "output_step_id": "audit",
            })

    rows: list[dict[str, Any]] = []
    for rec in by_node.values():
        all_runs = sorted(
            rec.pop("runs"), key=lambda row: str(row.get("started_at") or ""), reverse=True
        )
        # An evidence feed may present the same check several times -- a current verdict plus
        # the attempts it superseded. Those older rows are worth keeping for an export, and
        # counting them would be wrong twice over: it inflates `failure_count` past the
        # flag threshold and it drags `trend` toward whatever the history happened to be.
        # A row is current unless it says otherwise, so a producer that knows nothing about
        # this stays unaffected.
        runs = [row for row in all_runs if row.get("authoritative") is not False]
        findings_rows = sorted(rec.pop("findings"), key=lambda row: str(row.get("created_at") or ""), reverse=True)
        attributed_runs = [row for row in runs if row.get("attributed_failure")]
        distinct_failure_runs = {
            str(row.get("job_id") or row.get("activity_id") or "") for row in attributed_runs
        }
        distinct_failure_runs.update(
            str(row.get("scheduler_ref") or row.get("validation_run_id") or row.get("issue_id") or "")
            for row in findings_rows
        )
        distinct_failure_runs.discard("")
        recent = runs[:5]
        prior = runs[5:10]
        recent_rate = sum(bool(row.get("attributed_failure")) for row in recent) / max(1, len(recent))
        prior_rate = sum(bool(row.get("attributed_failure")) for row in prior) / max(1, len(prior))
        if not prior:
            trend = "new" if recent_rate else "flat"
        elif recent_rate > prior_rate:
            trend = "up"
        elif recent_rate < prior_rate:
            trend = "down"
        else:
            trend = "flat"
        failure_count = len(attributed_runs) + len(findings_rows)
        rec.update(
            {
                "participation_count": len(runs),
                # Reported rather than discarded: if a node shows 2 participations from 7
                # rows, an operator reading the export needs to know why the numbers differ.
                "superseded_count": len(all_runs) - len(runs),
                "failed_job_participations": sum(bool(row.get("failed_job")) for row in runs),
                "failure_count": failure_count,
                "distinct_failure_runs": len(distinct_failure_runs),
                "flagged": failure_count >= 3 and len(distinct_failure_runs) >= 3,
                "trend": trend,
                "last_run_at": str(runs[0].get("started_at") or "") if runs else "",
                "last_failure_at": max(
                    [str(row.get("started_at") or "") for row in attributed_runs]
                    + [str(row.get("created_at") or "") for row in findings_rows]
                    + [""]
                ),
                "runs": runs,
                "findings": findings_rows,
            }
        )
        rows.append(rec)

    rows.sort(key=lambda row: (str(row.get("rack") or ""), _natural_node_key(str(row.get("node") or ""))))
    return {
        "rack_mapping_mode": placement.get("mode") or "direct",
        "generated_at": _now_iso(),
        "history_path": str(HISTORY_FILE),
        "summary": {
            "nodes": len(rows),
            "participations": sum(int(row.get("participation_count") or 0) for row in rows),
            "attributed_failures": sum(int(row.get("failure_count") or 0) for row in rows),
            "flagged_nodes": sum(bool(row.get("flagged")) for row in rows),
            "drained_nodes": sum(str(row.get("slurm_state") or "").lower().startswith("drain") for row in rows),
        },
        "racks": group_nodes(rows, placement),
        "nodes": rows,
    }
