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
"""Ingest a site collector's evidence snapshot into the durable results history.

Some sites run their own collector: a pipeline that walks the fleet's logs and writes one
JSON snapshot of what passed. That is *better* evidence than anything this product can
re-derive from the same logs, because the collector has already applied the site's
acceptance policy — which rack counts as accepted, how a retry supersedes an attempt, what
a partial run means. Re-litigating that here would produce a second opinion, and two
disagreeing answers to "is this rack accepted" is worse than one.

So this trusts the index and does not reopen the artifacts it points at. What it adds is
durability and queryability: the rows land in `results_history`, where the fleet views and
the post-run report already look.

**Two feeds, and the distinction is the whole design.** Acceptance is *monotonic* — a rack
that passed stays passed, because an acceptance record is a historical claim and a later
unrelated failure does not retract it. Current state is *not* monotonic — a rack that is
broken now must show broken, or a pass from last week paints it green forever. They are
therefore ingested under different `source` values and read by different queries:
`latest_successful_runs_by_rack` for acceptance, `latest_runs_by_rack(source=CURRENT)` for
now. Collapsing them into one feed loses one of the two answers.

**Nothing here knows a site's floor plan.** The implementation this was reconstructed from
computed rack ids from `su` and `rack_number` through a hard-coded table of one customer's
halls and rows. A row may name its `rack` outright, and if it gives coordinates instead they
are resolved through the cluster's own Scalable Unit map — the same record the floor view
draws.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import results_history

_logger = logging.getLogger(__name__)

#: Monotonic acceptance: a rack that passed, and stays passed.
SOURCE_SETTLED = "external-evidence:settled"

#: The current attempt per rack and stage, which may be a failure and must win over an
#: older pass. Separate `source` so a query can ask for one without the other.
SOURCE_CURRENT = "external-evidence:current"

#: Per-node GPU diagnostic rows carried in the same snapshot.
SOURCE_DCGM = "external-evidence:dcgm"

#: Snapshot sections, mapped to the workload kind `results_history` classifies by.
_SECTIONS = {
    "rack_nccl": "nccl",
    "rack_nemo": "nemo",
    "rack_mnubergemm": "mnubergemm",
}

#: `dcgmi-r1`, and `dcgmi-r3.prev2` for the attempt before last.
_DCGM_TEST_RE = re.compile(r"^(dcgmi-r[134])(?:\.prev(\d+))?$")


class SnapshotRejected(ValueError):
    """The snapshot cannot be trusted, so none of it is ingested."""


def _require_complete(snapshot: Mapping[str, Any]) -> None:
    """A partial snapshot is refused whole.

    The collector sets `complete` last. Ingesting a snapshot without it would import
    whichever racks happened to be written before it stopped, and those absent rows are
    indistinguishable from racks that legitimately have no evidence — so the gap would read
    as "not yet validated" rather than "we did not finish looking".
    """
    if snapshot.get("complete") is not True:
        raise SnapshotRejected("evidence snapshot is not marked complete")


def _rack_of(row: Mapping[str, Any], su_map: Mapping[str, list[str]] | None) -> str:
    """The rack a row is about.

    An explicit `rack` wins, because the collector knows the site. Coordinates are a
    fallback, and they resolve through the cluster's SU map rather than arithmetic: `su` names
    an entry in that map and `rack_number` indexes into its rack list. That keeps the floor
    plan in the one place that already owns it.
    """
    explicit = str(row.get("rack") or "").strip()
    if explicit:
        return explicit

    su = str(row.get("su") or "").strip()
    number = row.get("rack_number")
    if not su or number is None or su_map is None:
        return ""
    racks = su_map.get(su) or su_map.get(f"su{su.zfill(2)}") or su_map.get(f"SU{su}")
    try:
        index = int(number)
    except (TypeError, ValueError):
        return ""
    # 1-based, the way a floor is labelled.
    if not racks or index < 1 or index > len(racks):
        return ""
    return racks[index - 1]


def _su_map() -> dict[str, list[str]]:
    """The active cluster's SU-to-rack mapping, for coordinate rows."""
    try:
        from catalog import context as catalog_context

        _project, cluster, _platform = catalog_context.resolve()
        if cluster is None:
            return {}
        out: dict[str, list[str]] = {}
        for unit in cluster.scalable_units:
            out[unit.id] = list(unit.racks)
            if unit.label:
                out[unit.label] = list(unit.racks)
        return out
    except Exception:  # noqa: BLE001 - coordinates are a fallback; absence is not fatal
        return {}


def _run_id(*parts: object) -> str:
    """A stable id for a row, so re-ingesting the same evidence updates rather than duplicates.

    Derived from identity rather than from content: the *current* feed re-states the same
    rack and stage every refresh with a changing verdict, and a content-derived id would make
    each refresh a new row and the rack's history a pile of near-duplicates.
    """
    joined = "|".join(str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def _common_extra(row: Mapping[str, Any]) -> dict[str, Any]:
    """Collector provenance, carried through opaquely.

    Kept because it is how somebody later answers "why did we believe this": which artifact,
    whether the parquet matched exactly, what the terminal state was. None of it is
    interpreted here — interpreting it would be forming the second opinion this module
    exists to avoid.
    """
    return {
        "collector_rack": row.get("rack"),
        "node_count": row.get("node_count"),
        "parquet_exact": row.get("parquet_exact"),
        "source_sha256": row.get("source_sha256"),
        "terminal": row.get("terminal"),
        "terminal_rc": row.get("terminal_rc"),
    }


def _section_rows(snapshot: Mapping[str, Any], section: str) -> list[Mapping[str, Any]]:
    payload = snapshot.get(section)
    if payload is None:
        return []
    rows = payload.get("latest_by_rack") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise SnapshotRejected(f"{section}.latest_by_rack is missing or not a list")
    return [row for row in rows if isinstance(row, Mapping)]


def accepted_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Monotonic acceptance rows: the passes, and only the passes.

    A failure is deliberately not recorded here. Acceptance is never *inferred* — the absence
    of a pass is not a recorded failure, and importing failures into the acceptance feed
    would let a later retry's failure retract a pass that genuinely happened.
    """
    _require_complete(snapshot)
    su_map = _su_map()
    out: list[dict[str, Any]] = []
    for section, kind in _SECTIONS.items():
        for row in _section_rows(snapshot, section):
            if str(row.get("outcome") or "").upper() != "PASS":
                continue
            source_log = str(row.get("main_log") or "").strip()
            if not source_log:
                # A pass with no artifact behind it is not evidence.
                raise SnapshotRejected(f"accepted {kind} evidence has no main_log")
            rack = _rack_of(row, su_map)
            if not rack:
                raise SnapshotRejected(
                    f"accepted {kind} evidence names no resolvable rack "
                    f"(give `rack`, or `su`/`rack_number` this cluster's SU map covers)"
                )
            out.append({
                "run_id": _run_id(SOURCE_SETTLED, kind, rack, source_log),
                "source_log_path": source_log,
                "log_basename": Path(source_log).name,
                "log_mtime": _mtime_seconds(row.get("mtime_ns")),
                "workload_kind": kind,
                "rack": rack,
                "job_id": str(row.get("job_id") or ""),
                "status": "pass",
                "source": SOURCE_SETTLED,
                "extra": _common_extra(row),
            })
    return out


def current_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The current attempt per rack and stage, pass or fail.

    This is the half that is allowed to go backwards, and it has to be: acceptance history
    says a rack passed once, and an operator looking at the fleet needs to know it is failing
    now. The `run_id` is keyed on rack and stage *without* the log path, so each refresh
    updates one row per rack instead of appending a new one.
    """
    _require_complete(snapshot)
    su_map = _su_map()
    out: list[dict[str, Any]] = []
    for section, kind in _SECTIONS.items():
        for row in _section_rows(snapshot, section):
            rack = _rack_of(row, su_map)
            if not rack:
                continue  # a row we cannot place cannot overlay a rack
            outcome = str(row.get("outcome") or "").upper()
            out.append({
                "run_id": _run_id(SOURCE_CURRENT, kind, rack),
                "source_log_path": str(row.get("main_log") or ""),
                "log_basename": Path(str(row.get("main_log") or "")).name,
                "log_mtime": _mtime_seconds(row.get("mtime_ns")),
                "workload_kind": kind,
                "rack": rack,
                "job_id": str(row.get("job_id") or ""),
                "status": "pass" if outcome == "PASS" else "fail",
                "source": SOURCE_CURRENT,
                "extra": {**_common_extra(row), "outcome": outcome},
            })
    return out


def dcgm_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Per-node GPU diagnostic rows from the snapshot's attempt list.

    Only rows the collector labelled `dcgm` and whose test name is recognised. An unknown
    test is skipped rather than imported under a guessed name: a row nobody can interpret is
    noise in a view operators use to decide whether to drain a node.
    """
    _require_complete(snapshot)
    lake = snapshot.get("lake")
    if not isinstance(lake, Mapping):
        return []
    attempts = lake.get("validation_attempts")
    if not isinstance(attempts, list):
        return []

    out: list[dict[str, Any]] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        evidence = attempt.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("workload") or "").strip().casefold() != "dcgm":
                continue
            match = _DCGM_TEST_RE.fullmatch(str(item.get("test") or "").strip().lower())
            if not match:
                continue
            node = str(item.get("node") or "").strip()
            artifact = str(item.get("artifact_path") or "").strip()
            if not node or not artifact:
                continue
            test, prev = match.group(1), match.group(2)
            out.append({
                "run_id": _run_id(SOURCE_DCGM, node, test, prev or "0"),
                "source_log_path": artifact,
                "log_basename": Path(artifact).name,
                "workload_kind": "dcgm",
                "node": node,
                "job_id": str(item.get("job_id") or attempt.get("job_id") or ""),
                "status": "pass" if str(item.get("verdict") or "").upper() == "PASS" else "fail",
                "source": SOURCE_DCGM,
                "extra": {
                    "test": test,
                    # `.prevN` rows are the attempts this one superseded. Marked so the
                    # post-run report can keep them for an export without counting them
                    # toward a node's failure total.
                    "history_only": prev is not None,
                    "authoritative": prev is None,
                    "raw_verdict": item.get("verdict"),
                    "detail": item.get("detail"),
                    "attempt": attempt.get("attempt"),
                    "slurm_state": attempt.get("slurm_state"),
                },
            })
    return out


def _mtime_seconds(raw: object) -> float:
    try:
        return int(raw) / 1_000_000_000 if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _fingerprint(path: Path, snapshot: Mapping[str, Any]) -> str:
    """Identifies this snapshot, so re-ingesting an unchanged one is skipped.

    Content-derived rather than mtime-derived: a collector that rewrites its output every
    refresh moves the mtime whether or not anything changed, and a re-ingest of identical
    evidence is wasted work on the fleet database.
    """
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{path.name}:{hashlib.sha256(payload).hexdigest()[:32]}"


def ingest_snapshot(path: str | Path) -> dict[str, Any]:
    """Read a collector snapshot and land its rows in the results history.

    All three feeds go in under one checkpoint, because they describe one observation of the
    fleet: importing acceptance and then failing on the diagnostics would leave the database
    claiming a consistency it does not have.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotRejected(f"cannot read evidence snapshot {path}: {exc}") from exc
    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnapshotRejected(f"evidence snapshot {path} is not valid JSON: {exc}") from exc
    if not isinstance(snapshot, Mapping):
        raise SnapshotRejected("evidence snapshot root must be an object")

    records = [
        *accepted_records(snapshot),
        *current_records(snapshot),
        *dcgm_records(snapshot),
    ]
    try:
        stat = path.stat()
        mtime_ns, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        mtime_ns, size = None, None

    summary = results_history.ingest_runs_checkpointed(
        records,
        source_id="external-evidence",
        fingerprint=_fingerprint(path, snapshot),
        source_path=str(path),
        source_mtime_ns=mtime_ns,
        source_size=size,
        collected_at=str(snapshot.get("collected_at") or ""),
    )
    _logger.info(
        "external evidence: %s ingested, %s skipped from %s",
        summary.get("ingested"), summary.get("skipped"), path,
    )
    return summary
