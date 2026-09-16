# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Import settled 16K NCCL/NeMo/MNUberGEMM evidence into Rack Status history."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import results_history

_DCGM_TEST_RE = re.compile(r"^(dcgmi-r[134])(?:\.prev(\d+))?$", re.IGNORECASE)
_DCGM_SOURCE = "nscale16k_dcgm_index"
_CURRENT_RACK_SOURCE = "nscale16k_current_rack_evidence"


def canonical_rack_id(su: int, rack_number: int) -> str:
    """Map the NScale TX SU/rack coordinate to its BCM rack identifier."""
    if 1 <= su <= 11:
        hall, row = 2, chr(ord("a") + su - 1)
    elif 12 <= su <= 14:
        hall, row = 1, chr(ord("a") + su - 12)
    else:
        raise ValueError(f"SU must be 1..14, got {su}")
    if rack_number < 1 or rack_number > 99:
        raise ValueError(f"rack number must be 1..99, got {rack_number}")
    return f"ink50-dh{hall:02d}-{row}{rack_number:02d}"


def accepted_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate only collector-qualified PASS records; never infer acceptance."""
    if snapshot.get("complete") is not True:
        raise ValueError("validation evidence snapshot is incomplete")
    records: list[dict[str, Any]] = []
    for section, kind in (
        ("rack_nccl", "nccl"),
        ("rack_nemo", "nemo"),
        ("rack_mnubergemm", "mnubergemm"),
    ):
        payload = snapshot.get(section)
        rows = payload.get("latest_by_rack") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise TypeError(f"snapshot.{section}.latest_by_rack is missing")
        for row in rows:
            if not isinstance(row, Mapping) or str(row.get("outcome") or "").upper() != "PASS":
                continue
            su = int(row.get("su") or 0)
            rack_number = int(row.get("rack_number") or 0)
            source = str(row.get("main_log") or "").strip()
            if not source:
                raise ValueError(f"accepted {kind} evidence has no main_log")
            mtime_ns = int(row.get("mtime_ns") or 0)
            records.append({
                "source_log_path": source,
                "log_basename": Path(source).name,
                "log_mtime": mtime_ns / 1_000_000_000 if mtime_ns else None,
                "workload_kind": kind,
                "rack": canonical_rack_id(su, rack_number),
                "job_id": str(row.get("job_id") or "").strip() or None,
                "status": "pass",
                "source": "nscale16k_settled_evidence",
                "extra": {
                    "collector_rack": row.get("rack"),
                    "node_count": row.get("node_count"),
                    "parquet_exact": row.get("parquet_exact"),
                    "source_sha256": row.get("source_sha256"),
                },
            })
    return records


def current_rack_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate the collector's current per-rack verdicts into stable rows.

    Acceptance history is intentionally monotonic, but the live Rack Status
    table must still show a newer terminal failure (or an unsettled partial)
    instead of allowing an older pass to win forever.  One stable run id per
    rack/stage makes each complete collector snapshot an atomic current-state
    overlay without deleting any historical runs.
    """
    if snapshot.get("complete") is not True:
        raise ValueError("validation evidence snapshot is incomplete")
    records: list[dict[str, Any]] = []
    for section, kind in (
        ("rack_nccl", "nccl"),
        ("rack_nemo", "nemo"),
        ("rack_mnubergemm", "mnubergemm"),
    ):
        payload = snapshot.get(section)
        rows = payload.get("latest_by_rack") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise TypeError(f"snapshot.{section}.latest_by_rack is missing")
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            su = int(row.get("su") or 0)
            rack_number = int(row.get("rack_number") or 0)
            rack = canonical_rack_id(su, rack_number)
            source = str(row.get("main_log") or "").strip()
            if not source:
                raise ValueError(f"current {kind} evidence has no main_log")
            outcome = str(row.get("outcome") or "").strip().upper()
            status = "pass" if outcome == "PASS" else "fail" if outcome == "FAIL" else "unknown"
            mtime_ns = int(row.get("mtime_ns") or 0)
            identity = f"nscale16k-current-rack-v1\0{kind}\0{rack}"
            records.append({
                "run_id": hashlib.sha256(identity.encode()).hexdigest(),
                "source_log_path": source,
                "log_basename": Path(source).name,
                "log_mtime": mtime_ns / 1_000_000_000 if mtime_ns else None,
                "workload_kind": kind,
                "rack": rack,
                "job_id": str(row.get("job_id") or "").strip() or None,
                "status": status,
                "source": _CURRENT_RACK_SOURCE,
                "extra": {
                    "collector_rack": row.get("rack"),
                    "collector_outcome": outcome,
                    "node_count": row.get("node_count"),
                    "parquet_exact": row.get("parquet_exact"),
                    "source_sha256": row.get("source_sha256"),
                    "terminal": row.get("terminal"),
                    "terminal_rc": row.get("terminal_rc"),
                },
            })
    return records


def ingest_snapshot(path: str | Path) -> list[str]:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(snapshot, Mapping):
        raise TypeError("validation evidence snapshot root must be an object")
    rack_ids = results_history.ingest_runs(
        [*accepted_records(snapshot), *current_rack_records(snapshot)]
    )
    lake = snapshot.get("lake")
    if not isinstance(lake, Mapping) or not isinstance(lake.get("validation_attempts"), list):
        return rack_ids
    dcgm = ingest_dcgm_snapshot(snapshot, source_path=path)
    return [*rack_ids, *dcgm["run_ids"]]


def dcgm_records(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate the collector's bounded DCGM evidence index into durable runs.

    Artifact contents are deliberately not reopened here. The collector already
    applied its explicit-verdict policy and retained its mapping provenance.
    Historic ``.prevN`` rows remain history and are never selected as current.
    """
    if snapshot.get("complete") is not True:
        raise ValueError("validation evidence snapshot is incomplete")
    lake = snapshot.get("lake")
    attempts = lake.get("validation_attempts") if isinstance(lake, Mapping) else None
    if not isinstance(attempts, list):
        raise TypeError("snapshot.lake.validation_attempts is missing")
    observed_at = str(snapshot.get("collected_at") or "").strip()
    records: list[dict[str, Any]] = []
    for attempt_row in attempts:
        if not isinstance(attempt_row, Mapping):
            continue
        evidence_rows = attempt_row.get("evidence")
        if not isinstance(evidence_rows, list):
            continue
        for evidence in evidence_rows:
            if not isinstance(evidence, Mapping) or str(evidence.get("workload") or "").casefold() != "dcgm":
                continue
            raw_test = str(evidence.get("test") or "").strip().lower()
            match = _DCGM_TEST_RE.fullmatch(raw_test)
            if not match:
                continue
            canonical_test = match.group(1).lower()
            node = str(evidence.get("node") or "").strip()
            artifact_path = str(evidence.get("artifact_path") or "").strip()
            if not node or not artifact_path:
                continue
            raw_verdict = str(evidence.get("verdict") or "UNKNOWN").strip().upper()
            status = "pass" if raw_verdict == "PASS" else "fail" if raw_verdict == "FAIL" else "unknown"
            job_id = str(evidence.get("job_id") or attempt_row.get("job_id") or "").strip()
            identity = json.dumps(
                [artifact_path, node, raw_test, raw_verdict, job_id],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            run_id = hashlib.sha256(f"dcgm-index-v1\0{identity}".encode()).hexdigest()
            records.append({
                "run_id": run_id,
                "source_log_path": artifact_path,
                "log_basename": Path(artifact_path).name,
                "workload_kind": "dcgm",
                "rack": canonical_rack_for_node(node),
                "nodes": [node],
                "job_id": job_id or None,
                # The index has observation time, not artifact completion time.
                # Preserve that distinction in extra rather than inventing one.
                "started_at": observed_at or None,
                "status": status,
                "source": _DCGM_SOURCE,
                "extra": {
                    "validation_test": canonical_test,
                    "raw_test": raw_test,
                    "raw_verdict": raw_verdict,
                    "detail": str(evidence.get("detail") or ""),
                    "map_method": str(evidence.get("map_method") or ""),
                    "map_note": str(evidence.get("map_note") or ""),
                    "attempt": attempt_row.get("attempt"),
                    "attempt_outcome": attempt_row.get("outcome"),
                    "slurm_state": attempt_row.get("slurm_state"),
                    "state_full": attempt_row.get("state_full"),
                    "exit_code": attempt_row.get("exit_code"),
                    "history_only": bool(match.group(2)),
                    "evidence_timestamp": "",
                    "observed_at": observed_at,
                },
            })
    return records


def canonical_rack_for_node(node: str) -> str:
    return node.rsplit("-", 1)[0] if "-" in node else node


def ingest_dcgm_snapshot(snapshot: Mapping[str, Any], *, source_path: str | Path) -> dict[str, Any]:
    records = dcgm_records(snapshot)
    identity_rows = sorted(
        (row["run_id"], row["status"], row["extra"]["raw_verdict"])
        for row in records
    )
    fingerprint = hashlib.sha256(
        json.dumps(identity_rows, separators=(",", ":")).encode()
    ).hexdigest()
    path = Path(source_path)
    try:
        stat = path.stat()
        mtime_ns, size = stat.st_mtime_ns, stat.st_size
    except OSError:
        mtime_ns = size = None
    return results_history.ingest_runs_checkpointed(
        records,
        source_id=_DCGM_SOURCE,
        fingerprint=fingerprint,
        source_path=str(path),
        source_mtime_ns=mtime_ns,
        source_size=size,
        collected_at=str(snapshot.get("collected_at") or ""),
    )


def _job_sequence(value: Any) -> tuple[int, str]:
    text = str(value or "").strip()
    return (int(text), text) if text.isdigit() else (-1, text)


def dcgm_post_run_events() -> list[dict[str, Any]]:
    """Return all DCGM attempts, marking one current row per node/test."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = results_history.list_runs(
            {"workload_kind": "dcgm", "source": _DCGM_SOURCE},
            limit=5000,
            offset=offset,
        )
        rows.extend(page)
        if len(page) < 5000:
            break
        offset += len(page)

    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
        nodes = row.get("nodes") if isinstance(row.get("nodes"), list) else []
        node = str(nodes[0] if nodes else "").strip()
        test = str(extra.get("validation_test") or "").strip().lower()
        if node and test and not extra.get("history_only"):
            candidates.setdefault((node.casefold(), test), []).append(row)
    authoritative = {
        max(group, key=lambda row: (_job_sequence(row.get("job_id")), str(row.get("started_at") or ""), str(row.get("run_id") or "")))["run_id"]
        for group in candidates.values()
    }

    events: list[dict[str, Any]] = []
    for row in rows:
        extra = row.get("extra") if isinstance(row.get("extra"), Mapping) else {}
        nodes = row.get("nodes") if isinstance(row.get("nodes"), list) else []
        node = str(nodes[0] if nodes else "").strip()
        if not node:
            continue
        raw_verdict = str(extra.get("raw_verdict") or row.get("status") or "UNKNOWN").upper()
        run_id = str(row.get("run_id") or "")
        events.append({
            "node": node,
            "rack": str(row.get("rack") or canonical_rack_for_node(node)),
            "job_id": str(row.get("job_id") or ""),
            "activity_id": f"dcgm:{run_id}",
            "tool": str(extra.get("raw_test") or extra.get("validation_test") or "dcgm"),
            "scope": "single-node",
            "state": raw_verdict,
            "started_at": str(row.get("started_at") or ""),
            "ended_at": str(row.get("ended_at") or ""),
            "exit_code": str(extra.get("exit_code") or ""),
            "failed_job": raw_verdict == "FAIL",
            "attributed_failure": raw_verdict == "FAIL",
            "category": "gpu" if raw_verdict == "FAIL" else "",
            "reason": str(extra.get("detail") or ""),
            "source": _DCGM_SOURCE,
            "validation_test": str(extra.get("validation_test") or ""),
            "raw_test": str(extra.get("raw_test") or ""),
            "authoritative": run_id in authoritative,
            "history_only": bool(extra.get("history_only")),
            "artifact_path": str(row.get("source_log_path") or ""),
            "evidence_timestamp": str(extra.get("evidence_timestamp") or ""),
            "observed_at": str(extra.get("observed_at") or ""),
            "map_method": str(extra.get("map_method") or ""),
        })
    return events
