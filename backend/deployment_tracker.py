# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The deployment tracker: how far the bring-up has got, per node, laid out on the floor.

One dataset, two renderers (citizen 10). ``build_dataset`` reads what Rack Status already
knows -- the cluster snapshot's node inventory, its rack rows with their full-rack stages, the
SU and row geometry, and each node's own Stage 1 / Stage 2 -- and folds every node into one
**deployment state**. The SPA draws it live; ``render_site`` writes the same dataset into the
static tracker bundle so it can be published somewhere the cluster cannot be reached from.

The states, and the order they are decided in (first match wins):

``handed_over``
    The node's SU carries a ``handed_over_at`` date on the cluster record. We no longer run on
    it, so it is counted apart rather than as finished or stuck.
``in_progress``
    A stage is running now. A live run outranks an older failure: it is the retry.
``remediation``
    A stage failed, or the scheduler holds the node down or drained. This is readiness *now*,
    so a node that passed both stages and was drained since is back here -- on NC the AIC
    repair loop keeps bad nodes drained on purpose, and those are exactly the ones to chase.
``completed``
    Stage 1 (single-node NCCL) and Stage 2 (single-node LLM training) both passed.
``in_progress``
    One stage passed and the other has not run yet.
``not_started``
    Neither stage has run.

The rack stages (a run over the whole rack) are reported beside the node states, as their own
progress steps, rather than folded into them: a rack's verdict is evidence about the rack, and a
node in a rack that has not been run end to end has still earned its own two stages.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Display order, which is also the order the counts are listed in.
STATES = ("completed", "in_progress", "remediation", "not_started", "handed_over")

#: One sentence per state. Shipped in the dataset so the page's legend says exactly what the
#: classifier does -- a definition typed into the frontend would drift from this module.
DEFINITIONS = {
    "completed": "Stage 1 (single-node NCCL) and Stage 2 (single-node LLM training) both passed.",
    "in_progress": "A stage is running, or one stage has passed and the other has not run yet.",
    "remediation": "A stage failed, or Slurm holds the node down or drained.",
    "not_started": "Neither node stage has run.",
    "handed_over": "The node's scalable unit has been handed over to the customer.",
}

#: Normalised scheduler states (``schedulers.slurm.slurm_node_status``) that take a node out.
_HELD = frozenset({"down", "drained", "draining"})

UNASSIGNED_UNIT = "__unassigned"
UNPLACED_UNIT = "__unplaced"
UNPLACED_RACK = "__unplaced"
OTHER_ROW = "__other"


def _empty_counts() -> dict[str, int]:
    return {state: 0 for state in STATES}


def _stage(entry: Any) -> str:
    """A stage's state word; ``unknown`` (Stage 2 still loading) reads as not yet run."""
    state = str((entry or {}).get("state") or "pending") if isinstance(entry, Mapping) else "pending"
    return state if state in ("done", "fail", "ongoing", "pending") else "pending"


def classify(
    stage1: str,
    stage2: str,
    *,
    scheduler_status: str = "",
    inventory_status: str = "",
    handed_over: bool = False,
) -> str:
    """One node's deployment state. See the module docstring for the order and why."""
    if handed_over:
        return "handed_over"
    stages = (stage1, stage2)
    if "ongoing" in stages:
        return "in_progress"
    if "fail" in stages or scheduler_status in _HELD or inventory_status == "down":
        return "remediation"
    if stage1 == "done" and stage2 == "done":
        return "completed"
    if "done" in stages:
        return "in_progress"
    return "not_started"


def _reason(stage1: Mapping, stage2: Mapping, scheduler_state: str, state: str) -> str:
    """Why a node needs attention, in the words of whatever said so."""
    if state != "remediation":
        return ""
    for label, entry in (("Stage 1", stage1), ("Stage 2", stage2)):
        if _stage(entry) == "fail":
            why = str(entry.get("reason") or entry.get("status") or "").strip()
            return f"{label} failed" + (f": {why}" if why else "")
    if scheduler_state:
        return f"Slurm: {scheduler_state}"
    return "Node is down"


def _summarise(counts: Mapping[str, int]) -> dict[str, Any]:
    total = sum(counts.values())
    finished = counts.get("completed", 0) + counts.get("handed_over", 0)
    if total and counts.get("handed_over", 0) == total:
        status = "handed_over"
    elif total and finished == total:
        status = "completed"
    elif total and counts.get("not_started", 0) == total:
        status = "not_started"
    else:
        status = "in_progress" if total else "not_started"
    return {
        "total": total,
        "counts": dict(counts),
        # Handed-over nodes finished the bring-up before they left us, so they count as done.
        "percent_complete": round(100.0 * finished / total, 1) if total else 0.0,
        "status": status,
    }


_NUM = re.compile(r"(\d+)")


def _natural(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.casefold() for p in _NUM.split(text or "")]


def _node_sort_key(node: Mapping[str, Any]) -> tuple:
    unit = node.get("rack_unit")
    unit_key = (0, unit) if isinstance(unit, (int, float)) else (1, 0)
    return (unit_key, _natural(str(node.get("node") or "")))


def _row_of(hierarchy: Any) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """``rack -> (row key, row label)`` and the rows in floor order, from the status hierarchy."""
    if not isinstance(hierarchy, Mapping):
        return {}, []
    sections = [s for s in hierarchy.get("sections") or [] if isinstance(s, Mapping)]
    many = len(sections) > 1
    row_of: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    for section in sections:
        for row in section.get("rows") or []:
            if not isinstance(row, Mapping):
                continue
            key = f"{section.get('key', '')}/{row.get('key', '')}"
            label = str(row.get("label") or row.get("key") or "")
            # Floor rows are letters (NC: Q..X); "U" alone reads as a unit, not a row.
            if label and not label.lower().startswith("row"):
                label = f"Row {label}"
            if many and section.get("label"):
                label = f"{section.get('label')} · {label}"
            order.append(key)
            for rack in row.get("racks") or []:
                rack_id = rack.get("rack") if isinstance(rack, Mapping) else rack
                if rack_id:
                    row_of[str(rack_id)] = (key, label)
    return row_of, order


def _step(step_id: str, label: str, unit: str, states: Iterable[str]) -> dict[str, Any]:
    tally = {"done": 0, "ongoing": 0, "fail": 0, "pending": 0}
    for state in states:
        tally[state if state in tally else "pending"] += 1
    total = sum(tally.values())
    return {
        "id": step_id,
        "label": label,
        "unit": unit,
        "total": total,
        **tally,
        "percent_complete": round(100.0 * tally["done"] / total, 1) if total else 0.0,
    }


def build_dataset(
    payload: Mapping[str, Any],
    stages: Mapping[str, Any],
    *,
    cluster_name: str = "",
    project_name: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """The tracker dataset from a status snapshot and the node stages.

    ``payload`` is the cluster snapshot with ``hierarchy`` and ``scalable_units`` merged in
    (what ``/api/status`` serves); ``stages`` is ``node_stages.node_stages(...)``'s answer.
    Pure: no I/O, so every renderer and every test sees the same thing.
    """
    now = now or datetime.now(timezone.utc)
    inventory = [n for n in payload.get("nodes") or [] if isinstance(n, Mapping) and n.get("id")]
    stage_by_node = {
        str(s.get("node")): s for s in stages.get("nodes") or [] if isinstance(s, Mapping)
    }
    rack_rows = {
        str(r.get("rack")): r for r in payload.get("rows") or [] if isinstance(r, Mapping) and r.get("rack")
    }
    row_of, row_order = _row_of(payload.get("hierarchy"))
    units_raw = [u for u in payload.get("scalable_units") or [] if isinstance(u, Mapping)]
    unit_of: dict[str, Mapping[str, Any]] = {}
    for unit in units_raw:
        for rack in unit.get("racks") or []:
            unit_of[str(rack)] = unit
    pool = str(payload.get("rack_mapping_mode") or "") == "pool"

    # ------------------------------------------------------------------ every node, classified
    nodes: list[dict[str, Any]] = []
    for entry in inventory:
        name = str(entry["id"])
        rack = str(entry.get("rack") or "")
        st = stage_by_node.get(name) or {}
        s1, s2 = st.get("stage1") or {}, st.get("stage2") or {}
        unit = unit_of.get(rack)
        scheduler_state = str(entry.get("scheduler_state") or "")
        state = classify(
            _stage(s1),
            _stage(s2),
            scheduler_status=str(entry.get("scheduler_status") or ""),
            inventory_status=str(entry.get("status") or ""),
            handed_over=bool(unit and unit.get("handed_over_at")),
        )
        nodes.append({
            "node": name,
            "rack": rack,
            "rack_unit": entry.get("rack_unit"),
            "tray": entry.get("tray"),
            "state": state,
            "stage1": _stage(s1),
            "stage2": _stage(s2),
            "slurm": scheduler_state,
            "reason": _reason(s1, s2, scheduler_state, state),
        })

    # A host the cluster manager placed nowhere is not on the floor -- on NC it is the app server,
    # which the inventory lists beside the trays. On a placed cluster such hosts are named in
    # ``unplaced`` and counted nowhere, so the totals are the compute floor and the omission is
    # published rather than silent. On a pool cluster nothing is placed, and the pool is the floor.
    unplaced = [] if pool else sorted((n["node"] for n in nodes if not n["rack"]), key=_natural)
    if not pool:
        nodes = [n for n in nodes if n["rack"]]

    # ------------------------------------------------------------------ group: SU -> row -> rack
    # unit id -> {"meta":..., "rows": {row key -> {"label":..., "racks": {rack -> [nodes]}}}}
    grouped: dict[str, dict[str, Any]] = {}
    unit_order = [str(u.get("id")) for u in units_raw]

    def _unit_meta(rack: str) -> tuple[str, str, str]:
        if not rack:  # only on a pool cluster; elsewhere unplaced hosts were set aside above
            return UNPLACED_UNIT, "Pool", ""
        unit = unit_of.get(rack)
        if unit is not None:
            return str(unit.get("id")), str(unit.get("label") or unit.get("id")), str(unit.get("handed_over_at") or "")
        if units_raw:
            return UNASSIGNED_UNIT, "Racks in no SU", ""
        return UNASSIGNED_UNIT, "All racks", ""

    for node in nodes:
        rack = node["rack"]
        unit_id, unit_label, handed = _unit_meta(rack)
        bucket = grouped.setdefault(
            unit_id, {"id": unit_id, "label": unit_label, "handed_over_at": handed, "rows": {}}
        )
        if rack:
            row_key, row_label = row_of.get(rack, (OTHER_ROW, "Other racks"))
        else:
            row_key, row_label = OTHER_ROW, bucket["label"]
        row = bucket["rows"].setdefault(row_key, {"key": row_key, "label": row_label, "racks": {}})
        row["racks"].setdefault(rack or UNPLACED_RACK, []).append(node)

    rank = {key: i for i, key in enumerate(row_order)}

    def _rack_out(rack: str, members: list[dict[str, Any]]) -> dict[str, Any]:
        counts = _empty_counts()
        for node in members:
            counts[node["state"]] += 1
        rr = rack_rows.get(rack) or {}
        return {
            "rack": rack,
            "label": "Unplaced" if rack == UNPLACED_RACK else rack,
            "stage1": _stage({"state": rr.get("stage1")}) if rr else "pending",
            "stage2": _stage({"state": rr.get("stage2")}) if rr else "pending",
            **_summarise(counts),
            "nodes": sorted(members, key=_node_sort_key),
        }

    def _roll(children: list[dict[str, Any]]) -> dict[str, int]:
        counts = _empty_counts()
        for child in children:
            for state, n in child["counts"].items():
                counts[state] += n
        return counts

    units: list[dict[str, Any]] = []
    for unit_id in sorted(
        grouped,
        key=lambda u: (
            u in (UNASSIGNED_UNIT, UNPLACED_UNIT),
            u == UNPLACED_UNIT,
            unit_order.index(u) if u in unit_order else 0,
            _natural(grouped[u]["label"]),
        ),
    ):
        bucket = grouped[unit_id]
        rows_out = []
        for row_key in sorted(
            bucket["rows"], key=lambda k: (k == OTHER_ROW, rank.get(k, len(rank)), _natural(k))
        ):
            row = bucket["rows"][row_key]
            racks_out = [
                _rack_out(rack, members)
                for rack, members in sorted(row["racks"].items(), key=lambda kv: _natural(kv[0]))
            ]
            rows_out.append({
                "key": row_key,
                "label": row["label"],
                **_summarise(_roll(racks_out)),
                "racks": racks_out,
            })
        units.append({
            "id": unit_id,
            "label": bucket["label"],
            "handed_over_at": bucket["handed_over_at"],
            "rack_count": sum(len(r["racks"]) for r in rows_out),
            **_summarise(_roll(rows_out)),
            "rows": rows_out,
        })

    # ------------------------------------------------------------------ progress steps
    # Handed-over SUs are no longer ours to run on, so they are out of every step's denominator.
    ours = [n for n in nodes if n["state"] != "handed_over"]
    handed_racks = {r for r, u in unit_of.items() if u.get("handed_over_at")}
    our_racks = [r for r in rack_rows.values() if str(r.get("rack")) not in handed_racks]
    steps = [
        _step("node_stage1", "Node Stage 1 · single-node NCCL", "nodes", (n["stage1"] for n in ours)),
        _step("node_stage2", "Node Stage 2 · single-node LLM training", "nodes", (n["stage2"] for n in ours)),
        _step("rack_stage1", "Rack Stage 1 · full-rack NCCL", "racks", (_stage({"state": r.get("stage1")}) for r in our_racks)),
        _step("rack_stage2", "Rack Stage 2 · full-rack NeMo", "racks", (_stage({"state": r.get("stage2")}) for r in our_racks)),
    ]

    fleet_counts = _empty_counts()
    for node in nodes:
        fleet_counts[node["state"]] += 1

    return {
        "meta": {
            "cluster": cluster_name,
            "project": project_name or str(payload.get("project_name") or ""),
            "generated_at": now.isoformat(timespec="seconds"),
            "snapshot_at": str(payload.get("updated_at") or ""),
            "rack_mapping_mode": str(payload.get("rack_mapping_mode") or ""),
            "stage2_source": str(stages.get("stage2_source") or ""),
            "stage2_loading": bool(stages.get("stage2_loading")),
            "states": list(STATES),
            "definitions": dict(DEFINITIONS),
        },
        "summary": {**_summarise(fleet_counts), "racks": len({n["rack"] for n in nodes if n["rack"]}), "units": len(units)},
        "steps": steps,
        "units": units,
        "unplaced": unplaced,
    }


# ---------------------------------------------------------------------------------- static site

#: Where ``npm run build`` writes the tracker's single-file page (``frontend/vite.tracker.config.ts``).
SITE_DIR = Path(__file__).resolve().parent.parent / "static" / "tracker"

#: The element the static page reads its dataset from. The bundle ships it holding ``null``.
DATA_ELEMENT = '<script id="tracker-data" type="application/json">null</script>'


class SiteBundleMissing(RuntimeError):
    """The tracker bundle was not built into this deployment."""


def embed(index_html: str, dataset: Mapping[str, Any]) -> str:
    """``index.html`` with the dataset written into its data element.

    ``</`` is escaped so nothing in a node name or a failure reason can close the script
    element early -- the JSON stays data, never markup.
    """
    if DATA_ELEMENT not in index_html:
        raise SiteBundleMissing("tracker index.html has no data element; rebuild the frontend")
    body = json.dumps(dataset, separators=(",", ":"), sort_keys=True).replace("</", "<\\/")
    return index_html.replace(
        DATA_ELEMENT, f'<script id="tracker-data" type="application/json">{body}</script>', 1
    )


def render_site(dataset: Mapping[str, Any], site_dir: Path | None = None) -> str:
    """The static tracker as one HTML file, with this dataset embedded in it.

    One file because the frontend build inlines its script, styles and fonts
    (``vite.tracker.config.ts``): a page with sibling assets renders white when opened from
    disk, since browsers refuse module scripts and fonts from ``file://``. It calls nothing back,
    so it can be attached to a mail, opened locally, or uploaded to Linkify as it is.
    """
    index = (site_dir or SITE_DIR) / "index.html"
    if not index.is_file():
        raise SiteBundleMissing(f"tracker page not built: {index} is missing")
    return embed(index.read_text(encoding="utf-8"), dataset)
