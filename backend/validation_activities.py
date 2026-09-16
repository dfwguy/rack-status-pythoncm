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
"""Durable registry of validation activities (single-rack, multirack, adopted external logs).

Scheduler-agnostic: each launch or adopted log gets a canonical ``activity_id`` (UUID).
Child ``tasks[]`` hold optional scheduler refs (Slurm job ids) and resolved log paths.

Storage mirrors ``rack_locks.py`` / legacy ``multirack_jobs``: JSON + ``fcntl`` flock.
Path: ``var/cache/validation_activities.json`` (override ``RACK_VALIDATION_ACTIVITIES_FILE``).
Legacy ``multirack_jobs.json`` is migrated on first read when the new file is absent.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import nvis_id_templates
import results_history
import scheduler_runtime

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent

LEGACY_JOBS_FILE = Path(
    os.environ.get("RACK_MULTIRACK_JOBS_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "multirack_jobs.json")
)
ACTIVITIES_FILE = Path(
    os.environ.get("RACK_VALIDATION_ACTIVITIES_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "validation_activities.json")
)
ACTIVITY_ARCHIVE_DB = Path(
    os.environ.get("RACK_VALIDATION_ACTIVITIES_ARCHIVE_DB", "")
    or str(REPO_ROOT / "var" / "history" / "validation_activities.sqlite3")
)
MULTIRACK_LEDGER_FILE = Path(
    os.environ.get("RACK_MULTIRACK_LEDGER_FILE", "")
    or str(REPO_ROOT / "var" / "reports" / "multirack_validation_runs.json")
)
MULTIRACK_LEDGER_MARKDOWN_FILE = Path(
    os.environ.get("RACK_MULTIRACK_LEDGER_MARKDOWN_FILE", "")
    or str(REPO_ROOT / "var" / "reports" / "MULTIRACK_VALIDATION_RUNS.md")
)
MULTIRACK_LEDGER_EXPORT_STATUS_FILE = Path(
    os.environ.get("RACK_MULTIRACK_LEDGER_EXPORT_STATUS_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "multirack_ledger_parquet_status.json")
)

MAX_ACTIVITIES = 500
TAIL_CHARS = 8000
OPERATOR_MAX_LEN = 64
LEDGER_TEXT_MAX_LEN = 4000
MAX_CORRECTIVE_ACTIONS = 100
TERMINAL_PHASES = frozenset({"done", "error", "timeout", "cancelled"})
WORK_PHASES = frozenset({
    "pending",
    "submitted",
    "running",
    "collecting",
    "done",
    "failed",
    "unknown",
})
# Open work — still needs refresh / shows as not finished in the UI.
OPEN_WORK_PHASES = frozenset({"pending", "submitted", "running", "collecting", "unknown"})
TERMINAL_WORK_PHASES = frozenset({"done", "failed"})
# Rollup priority: highest open state wins for the activity.
_WORK_PHASE_RANK = {
    "running": 50,
    "collecting": 40,
    "submitted": 30,
    "pending": 20,
    "unknown": 10,
    "failed": 5,
    "done": 0,
}

TOOLS = frozenset({
    "nccl-fullrack",
    "singlerack",
    "llmb-nemo-combined",
    "llmb-nccl",
    "nemo-xrack",
    "llama",
    "llama-exemplar",
    "mnubergemm",
    "nvloom",
    "clusterkit",
    "external",
    # legacy multirack kind aliases
    "nemo",
    "llmb",
})

_LEGACY_KIND_TO_TOOL = {"nemo": "nemo-xrack", "llmb": "llmb-nccl"}

_JOBID_RE = re.compile(
    r"(?:^|\s)(?:SUBMITTED|LAUNCHED|JobID):\s*[^\n]*?jobid=(\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_JOBID_LINE_RE = re.compile(r"jobid=(\d+)", re.IGNORECASE)
# Slurm's own sbatch confirmation. Only llmb_autocollect.inc.sh and run_mnubergemm_stress.sh
# ever printed `jobid=`, so every launcher that just calls sbatch (run-nemo-xrack.sh,
# run_nccl_mpi.sh, run_clusterkit.sh, run_singlerack.sh) produced an activity with NO tasks —
# which left the UI with no job id to key on and forced it into rack-name guessing.
_SBATCH_SUBMITTED_RE = re.compile(r"Submitted batch job\s+(\d+)", re.IGNORECASE)

_UPDATABLE = frozenset({
    "phase",
    "work_phase",
    "returncode",
    "error",
    "finished_at",
    "stdout_tail",
    "stderr_tail",
    "command_display",
    "scheduler",
    "execution_provider",
    "fallback",
    "fallback_reason",
    "cancel_requested",
    "cancel_requested_at",
    "cancel_results",
    "scheduler_ref",
    "started_running_at",
    "nodes",
    "target",
    "tasks",
    "issue_summary",
    "outcome_summary",
})

_logger = logging.getLogger(__name__)
_ledger_export_queue: queue.Queue[dict[str, Any]] = queue.Queue()
_ledger_export_worker_lock = threading.Lock()
_ledger_export_worker_started = False
_archive_sync_lock = threading.Lock()
_archive_sync_signature: tuple[str, str, int, int] | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(text: object) -> str:
    s = "" if text is None else str(text)
    if len(s) <= TAIL_CHARS:
        return s
    return "…(truncated)…\n" + s[-TAIL_CHARS:]


def _sanitize_operator(operator: object) -> str:
    if not isinstance(operator, str):
        return ""
    return operator.strip().replace("\n", " ").replace("\r", " ")[:OPERATOR_MAX_LEN]


def _sanitize_ledger_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().replace("\x00", "")[:LEDGER_TEXT_MAX_LEN]


def _sanitize_corrective_actions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for raw in value[-MAX_CORRECTIVE_ACTIONS:]:
        if not isinstance(raw, dict):
            continue
        action = _sanitize_ledger_text(raw.get("action"))
        if not action:
            continue
        recorded_at = _sanitize_ledger_text(raw.get("recorded_at"))
        legacy_key = hashlib.sha256(
            f"{recorded_at}\x00{action}".encode()
        ).hexdigest()[:20]
        out.append(
            {
                "action_id": _sanitize_ledger_text(raw.get("action_id")) or f"legacy-{legacy_key}",
                "recorded_at": recorded_at,
                "operator": _sanitize_operator(raw.get("operator")),
                "action": action,
                "result": _sanitize_ledger_text(raw.get("result")),
                "status": _sanitize_ledger_text(raw.get("status")) or "recorded",
            }
        )
    return out


def _sanitize_racks(racks: object) -> list[str]:
    if not isinstance(racks, (list, tuple)):
        return []
    out: list[str] = []
    for r in racks:
        s = str(r).strip()
        if s:
            out.append(s[:64])
        if len(out) >= 64:
            break
    return out


def _normalize_tool(tool: str) -> str:
    t = (tool or "").strip().lower()
    if t in _LEGACY_KIND_TO_TOOL:
        return _LEGACY_KIND_TO_TOOL[t]
    if t in TOOLS:
        return t
    return "external"


def _legacy_kind_for_tool(tool: str) -> str:
    """Backward compat for MultirackView ``kind`` field."""
    if tool == "nemo-xrack":
        return "nemo"
    if tool == "llmb-nccl":
        return "llmb"
    return tool


def _ensure_dir() -> None:
    ACTIVITIES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _archive_upsert_rows(rows: Iterable[dict[str, Any]]) -> bool:
    """Persist activity snapshots outside the capped live-table cache.

    The JSON registry is a bounded operational working set.  It cannot also be
    the acceptance record: pruning, dismissal, or cache replacement would
    otherwise erase completed work.  SQLite lives under ``var/history`` on the
    persistent rack-data volume and is append-preserving by ``activity_id``.
    """
    encoded: list[tuple[str, str, str, str, str, str, str, str]] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        aid = str(rec.get("activity_id") or rec.get("job_id") or "").strip()
        if not aid:
            continue
        encoded.append(
            (
                aid,
                _normalize_tool(str(rec.get("tool") or rec.get("kind") or "external")),
                str(rec.get("rack") or "").strip(),
                str(rec.get("scope") or "").strip().lower(),
                str(rec.get("log_dir") or ""),
                str(rec.get("created_at") or ""),
                str(rec.get("updated_at") or rec.get("created_at") or _now_iso()),
                json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str),
            )
        )
    if not encoded:
        return True
    path = Path(ACTIVITY_ARCHIVE_DB)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS validation_activities (
                    activity_id TEXT PRIMARY KEY,
                    tool        TEXT NOT NULL,
                    rack        TEXT,
                    scope       TEXT,
                    log_dir     TEXT,
                    created_at  TEXT,
                    updated_at  TEXT,
                    payload     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_validation_activities_tool_rack
                    ON validation_activities(tool, rack);
                CREATE INDEX IF NOT EXISTS idx_validation_activities_updated
                    ON validation_activities(updated_at DESC);
                """
            )
            conn.executemany(
                """
                INSERT INTO validation_activities
                    (activity_id, tool, rack, scope, log_dir, created_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    tool = excluded.tool,
                    rack = excluded.rack,
                    scope = excluded.scope,
                    log_dir = excluded.log_dir,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    payload = excluded.payload
                WHERE excluded.updated_at >= validation_activities.updated_at
                """,
                encoded,
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except (OSError, sqlite3.Error) as exc:
        # Activity recording must remain available even if the history volume
        # is briefly unhealthy.  The next list/read bootstraps current rows.
        _logger.warning("could not persist validation activity archive %s: %s", path, exc)
        return False


def _sync_archive_from_live(rows: dict[str, dict]) -> None:
    """Bootstrap/reconcile the archive after upgrades or out-of-band cache writes."""
    global _archive_sync_signature
    try:
        st = ACTIVITIES_FILE.stat()
        signature = (
            str(Path(ACTIVITY_ARCHIVE_DB)),
            str(ACTIVITIES_FILE),
            int(st.st_mtime_ns),
            int(st.st_size),
        )
    except OSError:
        return
    with _archive_sync_lock:
        if signature == _archive_sync_signature:
            return
        if _archive_upsert_rows(rows.values()):
            _archive_sync_signature = signature


def _read_locked(f) -> dict[str, dict]:
    f.seek(0)
    blob = f.read()
    if not blob.strip():
        return {}
    try:
        raw = json.loads(blob)
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def _prune(rows: dict[str, dict]) -> dict[str, dict]:
    if len(rows) <= MAX_ACTIVITIES:
        return rows
    ordered = sorted(
        rows.items(),
        key=lambda kv: str(kv[1].get("created_at") or ""),
        reverse=True,
    )
    # Never prune an activity that still needs scheduler/log refresh.  The old
    # newest-500 policy could evict a long-running job and make it impossible
    # for the background reconciler to ever observe its terminal state.
    def _still_open(rec: dict[str, Any]) -> bool:
        explicit = str(rec.get("work_phase") or "").strip().lower()
        if explicit:
            return explicit in OPEN_WORK_PHASES
        tasks = [task for task in (rec.get("tasks") or []) if isinstance(task, dict)]
        if tasks:
            return rollup_work_phase(tasks) in OPEN_WORK_PHASES
        phase = str(rec.get("phase") or "").strip().lower()
        return phase not in TERMINAL_PHASES

    open_rows = [item for item in ordered if _still_open(item[1])]
    open_ids = {aid for aid, _rec in open_rows}
    terminal_rows = [item for item in ordered if item[0] not in open_ids]
    remaining = max(0, MAX_ACTIVITIES - len(open_rows))
    return dict(open_rows + terminal_rows[:remaining])


def _write_locked(f, rows: dict[str, dict]) -> None:
    blob = json.dumps(_prune(rows), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    f.seek(0)
    f.truncate()
    f.write(blob)
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError:
        pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a process-local report without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_multirack_archive(rows: dict[str, dict]) -> list[dict[str, Any]]:
    """Merge live multirack activities into an unpruned archive.

    The activity table is intentionally capped and UI rows may be dismissed. The ledger is
    an audit record, so it has a separate append-preserving JSON source and never removes a
    run merely because it disappeared from the live table.
    """
    MULTIRACK_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = open(MULTIRACK_LEDGER_FILE, "a+", encoding="utf-8")  # noqa: SIM115
    except OSError:
        return []
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            locked = True
        except OSError:
            locked = False
        try:
            f.seek(0)
            try:
                archive = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                archive = {}
            if not isinstance(archive, dict):
                archive = {}
            stored = archive.get("runs")
            if not isinstance(stored, dict):
                stored = {}
            for aid, rec in rows.items():
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("scope") or "").strip().lower() != "multirack":
                    continue
                prior = stored.get(aid)
                merged = dict(prior) if isinstance(prior, dict) else {}
                merged.update(rec)
                # An older activity snapshot must never erase actions already archived.
                actions = _sanitize_corrective_actions(rec.get("corrective_actions"))
                if not actions and isinstance(prior, dict):
                    actions = _sanitize_corrective_actions(prior.get("corrective_actions"))
                merged["corrective_actions"] = actions
                stored[aid] = merged
            archive = {
                "schema_version": 1,
                "updated_at": _now_iso(),
                "runs": stored,
            }
            f.seek(0)
            f.truncate()
            f.write(json.dumps(archive, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        finally:
            if locked:
                try:
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
    out = [dict(rec) for rec in stored.values() if isinstance(rec, dict)]
    out.sort(key=lambda rec: str(rec.get("created_at") or ""), reverse=True)
    return out


def _scheduler_refs(rec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for task in rec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        ref = str(task.get("scheduler_ref") or "").strip()
        if ref and ref not in out:
            out.append(ref)
    return out


def _ledger_outcome(rec: dict[str, Any]) -> str:
    if bool(rec.get("dry_run")):
        return "dry_run"
    work = str(rec.get("work_phase") or "").strip().lower()
    if work == "failed":
        return "failed"
    if work == "done":
        return "passed"
    if work in OPEN_WORK_PHASES:
        return work
    phase = str(rec.get("phase") or "").strip().lower()
    if phase in ("error", "timeout"):
        return phase
    if phase in ("starting", "running"):
        return phase
    # A successful submission wrapper is not a successful Slurm workload.
    if phase == "done" and _scheduler_refs(rec):
        return "submitted"
    if phase == "done" and rec.get("returncode") == 0:
        return "passed"
    return phase or "unknown"


def _ledger_error(rec: dict[str, Any]) -> str:
    explicit = _sanitize_ledger_text(rec.get("issue_summary"))
    if explicit:
        return explicit
    err = _sanitize_ledger_text(rec.get("error"))
    if err:
        return err
    failed = []
    for task in rec.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        wp = str(task.get("work_phase") or "").strip().lower()
        state = str(task.get("scheduler_state") or "").strip()
        if wp == "failed" or state.upper().split("+")[0] in (
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "NODE_FAIL",
            "OUT_OF_MEMORY",
            "BOOT_FAIL",
        ):
            failed.append(
                f"job {task.get('scheduler_ref') or '?'}: {state or wp or 'failed'}"
            )
    if failed:
        return "; ".join(failed)
    stderr = _sanitize_ledger_text(rec.get("stderr_tail"))
    if stderr:
        return stderr.splitlines()[-1]
    return ""


def _markdown_value(value: object) -> str:
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def render_multirack_ledger_markdown(records: Iterable[dict[str, Any]]) -> str:
    ordered = sorted(
        (dict(rec) for rec in records if isinstance(rec, dict)),
        key=lambda rec: str(rec.get("created_at") or ""),
        reverse=True,
    )
    lines = [
        "# Multi-Rack Validation Run Ledger",
        "",
        (
            "Automatically maintained from the durable validation activity registry. "
            "A successful launcher only means Slurm accepted the job; the outcome below follows "
            "the scheduler task state."
        ),
        "",
        f"Last generated: `{_now_iso()}`",
        "",
    ]
    if not ordered:
        lines.extend(["No multi-rack runs recorded.", ""])
        return "\n".join(lines)
    for rec in ordered:
        refs = _scheduler_refs(rec)
        label = ", ".join(refs) or str(rec.get("activity_id") or "unknown")
        outcome = _ledger_outcome(rec)
        racks = [str(r) for r in rec.get("racks") or [] if str(r).strip()]
        rack_text = ", ".join(racks)
        lines.extend(
            [
                f"## {label} — {outcome.upper()}",
                "",
                f"- Activity ID: `{_markdown_value(rec.get('activity_id'))}`",
                f"- Created: `{_markdown_value(rec.get('created_at'))}`",
                f"- Updated: `{_markdown_value(rec.get('updated_at'))}`",
                f"- Tool: `{_markdown_value(rec.get('tool'))}`",
                f"- Slurm job IDs: `{_markdown_value(', '.join(refs) or 'not submitted')}`",
                f"- Racks ({len(racks)}): {_markdown_value(rack_text) or 'not recorded'}",
                f"- Outcome: `{outcome}`",
            ]
        )
        outcome_summary = _sanitize_ledger_text(rec.get("outcome_summary"))
        issue = _ledger_error(rec)
        if outcome_summary:
            lines.append(f"- Outcome detail: {_markdown_value(outcome_summary)}")
        if issue:
            lines.append(f"- Error/issue: {_markdown_value(issue)}")
        actions = _sanitize_corrective_actions(rec.get("corrective_actions"))
        lines.extend(["", "Corrective actions:", ""])
        if actions:
            for action in actions:
                result = f" — {action['result']}" if action.get("result") else ""
                lines.append(
                    f"- `{action['recorded_at']}` [{action['status']}] "
                    f"{_markdown_value(action['action'])}{_markdown_value(result)}"
                )
        else:
            lines.append("- None recorded.")
        command = _markdown_value(rec.get("command_display"))
        if command:
            safe_command = command.replace("`", "'")
            lines.extend(["", f"Command: `{safe_command}`"])
        lines.append("")
    return "\n".join(lines)


def ledger_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Flat, schema-stable row sent to the Parquet lake."""
    refs = _scheduler_refs(rec)
    tasks = [
        {key: value for key, value in t.items() if key != "updated_at"}
        for t in rec.get("tasks") or []
        if isinstance(t, dict)
    ]
    actions = _sanitize_corrective_actions(rec.get("corrective_actions"))
    return {
        "schema_version": 1,
        "activity_id": str(rec.get("activity_id") or ""),
        "created_at": str(rec.get("created_at") or ""),
        "updated_at": str(rec.get("updated_at") or ""),
        "finished_at": str(rec.get("finished_at") or ""),
        "tool": str(rec.get("tool") or ""),
        "stage": str(rec.get("stage") or ""),
        "scope": "multirack",
        "operator": str(rec.get("operator") or ""),
        "racks_json": json.dumps(rec.get("racks") or [], ensure_ascii=False),
        "rack_count": len(rec.get("racks") or []),
        "scheduler": str(rec.get("scheduler") or ""),
        "scheduler_job_ids": ",".join(refs),
        "outcome": _ledger_outcome(rec),
        "outcome_summary": _sanitize_ledger_text(rec.get("outcome_summary")),
        "error_summary": _ledger_error(rec),
        "returncode": rec.get("returncode"),
        "command_display": str(rec.get("command_display") or ""),
        "tasks_json": json.dumps(tasks, ensure_ascii=False, sort_keys=True),
        "corrective_actions_json": json.dumps(actions, ensure_ascii=False, sort_keys=True),
        "corrective_action_count": len(actions),
    }


def _ledger_export_fingerprint(rec: dict[str, Any]) -> str:
    semantic = ledger_record(rec)
    # Hydration stamps updated_at even when scheduler evidence is unchanged. Do not emit a
    # duplicate Parquet snapshot solely because a dashboard poll touched that timestamp.
    semantic.pop("updated_at", None)
    blob = json.dumps(semantic, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _multirack_rows_fingerprint(rows: dict[str, dict]) -> str:
    semantic = [
        (aid, _ledger_export_fingerprint(rec))
        for aid, rec in sorted(rows.items())
        if isinstance(rec, dict)
        and str(rec.get("scope") or "").strip().lower() == "multirack"
    ]
    return hashlib.sha256(
        json.dumps(semantic, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def export_multirack_activity_to_lake(rec: dict[str, Any]) -> dict[str, Any]:
    """Write one ledger snapshot through lakewriter, which persists Parquet rows."""
    try:
        import lake_sink
    except ImportError as exc:
        return {"ok": False, "error": f"lake_sink unavailable: {exc}"}
    row = ledger_record(rec)
    refs = _scheduler_refs(rec)
    rack = str(rec.get("rack") or "") or "cluster"
    stable_ref = refs[0] if refs else str(rec.get("activity_id") or "unknown")
    # The activity UUID exists before sbatch returns and never changes. Keep it in run_id so
    # launch/submitted/terminal/action snapshots land in one append-only Parquet partition;
    # the actual scheduler reference remains queryable in job_id and parsed columns.
    run_id = lake_sink.make_run_id(
        "multirack-ledger",
        rack,
        str(rec.get("activity_id") or stable_ref),
    )
    return lake_sink.push_run(
        run_id=run_id,
        workload="multirack-ledger",
        rack=rack,
        job_id=stable_ref,
        status=str(row["outcome"]),
        parsed=row,
        log_path="synthesized:MULTIRACK_VALIDATION_RUN.md",
        log_text=render_multirack_ledger_markdown([rec]),
    )


def _ledger_lake_disabled() -> bool:
    return os.environ.get("RACK_MULTIRACK_LEDGER_LAKE_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _update_ledger_export_status(activity_id: str, mutator) -> None:
    path = MULTIRACK_LEDGER_EXPORT_STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a+", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                f.seek(0)
                try:
                    status = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    status = {}
                if not isinstance(status, dict):
                    status = {}
                entry = status.get(activity_id)
                if not isinstance(entry, dict):
                    entry = {}
                if mutator(entry) is False:
                    return
                status[activity_id] = entry
                f.seek(0)
                f.truncate()
                f.write(json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                f.flush()
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass


def _ledger_export_loop() -> None:
    while True:
        rec = _ledger_export_queue.get()
        try:
            aid = str(rec.get("activity_id") or "")
            fingerprint = _ledger_export_fingerprint(rec)
            result = export_multirack_activity_to_lake(rec)

            def _done(
                entry: dict[str, Any],
                result_snapshot: dict[str, Any] = result,
                fingerprint_snapshot: str = fingerprint,
            ) -> None:
                entry["last_attempt_at"] = _now_iso()
                entry["pending_fingerprint"] = ""
                entry["pending_pid"] = None
                entry["last_result"] = result_snapshot
                if result_snapshot.get("ok"):
                    entry["exported_fingerprint"] = fingerprint_snapshot
                    entry["exported_at"] = _now_iso()

            _update_ledger_export_status(aid, _done)
        except Exception as exc:  # noqa: BLE001 — a ledger export must never affect a run
            _logger.warning("multirack ledger Parquet export failed: %s", exc)
        finally:
            _ledger_export_queue.task_done()


def _ensure_ledger_export_worker() -> None:
    global _ledger_export_worker_started
    if _ledger_export_worker_started:
        return
    with _ledger_export_worker_lock:
        if _ledger_export_worker_started:
            return
        threading.Thread(
            target=_ledger_export_loop,
            name="multirack-ledger-parquet",
            daemon=True,
        ).start()
        _ledger_export_worker_started = True


def _schedule_multirack_exports(records: Iterable[dict[str, Any]]) -> None:
    if _ledger_lake_disabled():
        return
    pending: list[dict[str, Any]] = []
    for rec in records:
        aid = str(rec.get("activity_id") or "")
        if not aid:
            continue
        fingerprint = _ledger_export_fingerprint(rec)
        should_queue = False

        def _pending(item: dict[str, Any], fp: str = fingerprint) -> bool:
            nonlocal should_queue
            if item.get("exported_fingerprint") == fp:
                return False
            # A process can die after persisting "pending" but before its daemon worker
            # sends the row. Only a pending marker owned by this live process suppresses
            # another enqueue; a new process recovers it immediately after restart.
            if (
                item.get("pending_fingerprint") == fp
                and item.get("pending_pid") == os.getpid()
            ):
                return False
            item["pending_fingerprint"] = fp
            item["pending_pid"] = os.getpid()
            item["queued_at"] = _now_iso()
            should_queue = True
            return True

        _update_ledger_export_status(aid, _pending)
        if should_queue:
            pending.append(dict(rec))
    if not pending:
        return
    _ensure_ledger_export_worker()
    for rec in pending:
        _ledger_export_queue.put(rec)


def rebuild_multirack_ledger(rows: dict[str, dict] | None = None) -> list[dict[str, Any]]:
    """Refresh the archive, human document and asynchronous Parquet outbox."""
    if rows is None:
        rows = {
            str(rec.get("activity_id") or rec.get("job_id") or ""): rec
            for rec in list_activities(scope="multirack", limit=0)
        }
    records = _merge_multirack_archive(rows)
    if records or MULTIRACK_LEDGER_FILE.exists():
        _atomic_write_text(
            MULTIRACK_LEDGER_MARKDOWN_FILE,
            render_multirack_ledger_markdown(records),
        )
    _schedule_multirack_exports(records)
    return records


def read_multirack_ledger_markdown() -> str:
    rebuild_multirack_ledger()
    try:
        return MULTIRACK_LEDGER_MARKDOWN_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return render_multirack_ledger_markdown([])


def _migrate_legacy_if_needed() -> None:
    """One-time import from multirack_jobs.json when the new store file does not exist."""
    if ACTIVITIES_FILE.is_file():
        return
    if not LEGACY_JOBS_FILE.is_file():
        return
    try:
        raw = json.loads(LEGACY_JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or not raw:
        return
    migrated: dict[str, dict] = {}
    for jid, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        migrated[jid] = _upgrade_legacy_record(rec, activity_id=str(jid))
    _ensure_dir()
    try:
        ACTIVITIES_FILE.write_text(
            json.dumps(_prune(migrated), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        bak = LEGACY_JOBS_FILE.with_suffix(".json.bak")
        if not bak.exists():
            shutil.copy2(LEGACY_JOBS_FILE, bak)
    except OSError:
        pass


def _upgrade_legacy_record(rec: dict[str, Any], *, activity_id: str) -> dict[str, Any]:
    """Normalize an old multirack_jobs row or partial record."""
    tool_raw = str(rec.get("tool") or rec.get("kind") or "nemo")
    tool = _normalize_tool(tool_raw)
    racks = _sanitize_racks(rec.get("racks") or [])
    rack = str(rec.get("rack") or (racks[0] if racks else ""))
    scope = str(rec.get("scope") or ("multirack" if len(racks) > 1 else "rack"))
    if tool == "nemo-xrack":
        scope = "multirack"
    stage = str(rec.get("stage") or ("multirack" if scope == "multirack" else "other"))
    out = {
        "activity_id": activity_id,
        "job_id": activity_id,
        "tool": tool,
        "kind": _legacy_kind_for_tool(tool),
        "stage": stage,
        "scope": scope,
        "rack": rack,
        "racks": racks,
        "log_dir": str(rec.get("log_dir") or ""),
        "operator": _sanitize_operator(rec.get("operator")),
        "command_display": str(rec.get("command_display") or ""),
        "scheduler": str(rec.get("scheduler") or "unknown"),
        "execution_provider": str(
            rec.get("execution_provider") or rec.get("scheduler") or "unknown"
        ),
        "fallback": bool(rec.get("fallback")),
        "fallback_reason": str(rec.get("fallback_reason") or ""),
        "nodes": [str(node) for node in (rec.get("nodes") or []) if str(node).strip()],
        "target": str(rec.get("target") or ""),
        "dry_run": bool(rec.get("dry_run")),
        "phase": str(rec.get("phase") or "starting"),
        "returncode": rec.get("returncode"),
        "error": rec.get("error"),
        "created_at": str(rec.get("created_at") or _now_iso()),
        "updated_at": str(rec.get("updated_at") or _now_iso()),
        "finished_at": rec.get("finished_at"),
        "stdout_tail": str(rec.get("stdout_tail") or ""),
        "stderr_tail": str(rec.get("stderr_tail") or ""),
        "issue_summary": _sanitize_ledger_text(rec.get("issue_summary")),
        "outcome_summary": _sanitize_ledger_text(rec.get("outcome_summary")),
        "corrective_actions": _sanitize_corrective_actions(rec.get("corrective_actions")),
        "source": str(rec.get("source") or "ui"),
        "tasks": list(rec.get("tasks") or []),
    }
    return out


def _open_rw():
    _migrate_legacy_if_needed()
    _ensure_dir()
    if not ACTIVITIES_FILE.exists():
        ACTIVITIES_FILE.write_text("{}\n", encoding="utf-8")
    return open(ACTIVITIES_FILE, "r+", encoding="utf-8")


def _with_store(mutator) -> None:
    snapshot: dict[str, dict] | None = None
    ledger_changed = False
    try:
        with _open_rw() as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                rows = _read_locked(f)
                before_rows = {
                    aid: json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
                    for aid, rec in rows.items()
                }
                before = _multirack_rows_fingerprint(rows)
                mutator(rows)
                changed_rows = [
                    rec
                    for aid, rec in rows.items()
                    if before_rows.get(aid)
                    != json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str)
                ]
                # Archive before the bounded JSON write.  A newly-created row
                # therefore remains durable even when the live table prunes it.
                _archive_upsert_rows(changed_rows)
                ledger_changed = (
                    before != _multirack_rows_fingerprint(rows)
                    or not MULTIRACK_LEDGER_FILE.exists()
                )
                _write_locked(f, rows)
                if ledger_changed:
                    snapshot = {key: dict(value) for key, value in rows.items()}
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass
    if ledger_changed and snapshot is not None:
        try:
            rebuild_multirack_ledger(snapshot)
        except Exception as exc:  # noqa: BLE001 — audit side effects must not break launches
            _logger.warning("could not refresh multirack run ledger: %s", exc)


def parse_scheduler_job_ids(text: str) -> list[str]:
    """Extract Slurm job ids from llmb-run / wrapper stdout, in submission order.

    Two dialects, because the launchers are not uniform:
      * ``jobid=<n>``            — llmb_autocollect.inc.sh, run_mnubergemm_stress.sh
      * ``Submitted batch job N``— Slurm's own sbatch reply, i.e. every other launcher

    Order matters: sync_tasks_from_stdout labels the first id ``nccl`` and the second ``nemo``
    for combined runs, so ids are returned in the order they appear in the text rather than
    grouped by which pattern matched.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    matches = [
        (m.start(), m.group(1))
        for pat in (_JOBID_RE, _JOBID_LINE_RE, _SBATCH_SUBMITTED_RE)
        for m in pat.finditer(text)
    ]
    for _pos, jid in sorted(matches, key=lambda t: t[0]):
        if jid not in seen:
            seen.add(jid)
            out.append(jid)
    return out


def job_id_from_log_basename(name: str) -> str | None:
    """Slurm job id from a log basename. Delegates so app.py, native_status and this store
    cannot disagree about which digits in a filename are the job id."""
    return nvis_id_templates.job_id_from_log_basename(name)


def infer_from_log_basename(name: str) -> dict[str, str]:
    """Best-effort tool/stage/label from a validation log basename."""
    low = (name or "").lower()
    if "nccl" in low and "1rack" in low:
        return {"tool": "external", "stage": "stage1", "label": "nccl"}
    if "nemo" in low and "1rack" in low:
        return {"tool": "external", "stage": "stage2", "label": "nemo"}
    if "llama" in low:
        return {"tool": "external", "stage": "other", "label": "llama"}
    if "clusterkit" in low or low.startswith("root-ck-"):
        return {"tool": "external", "stage": "other", "label": "clusterkit"}
    if "nvloom" in low:
        return {"tool": "external", "stage": "other", "label": "nvloom"}
    if "mnubergemm" in low or "mndiag" in low:
        return {"tool": "external", "stage": "other", "label": "mnubergemm"}
    return {"tool": "external", "stage": "other", "label": "validation"}


def _find_by_log_path(rows: dict[str, dict], log_path: str) -> str | None:
    """Return activity_id if *log_path* is already tracked."""
    base = os.path.basename(log_path)
    jid = job_id_from_log_basename(base)
    for aid, rec in rows.items():
        for task in rec.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            for key in ("stdout_path", "training_log_path", "results_log_path"):
                p = str(task.get(key) or "")
                if p and (p == log_path or os.path.basename(p) == base):
                    return aid
            if jid and str(task.get("scheduler_ref") or "") == jid:
                return aid
        if jid:
            for task in rec.get("tasks") or []:
                if str(task.get("scheduler_ref") or "") == jid:
                    return aid
    return None


def record_activity(
    tool: str,
    *,
    stage: str = "other",
    scope: str = "rack",
    rack: str = "",
    racks: list[str] | None = None,
    log_dir: str = "",
    operator: str = "",
    command_display: str = "",
    dry_run: bool = False,
    scheduler: str = "unknown",
    execution_provider: str = "",
    fallback: bool = False,
    fallback_reason: str = "",
    source: str = "ui",
    activity_id: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
    nodes: list[str] | None = None,
) -> str:
    """Persist a new validation activity. Returns ``activity_id`` (UUID)."""
    aid = (activity_id or "").strip() or str(uuid.uuid4())
    tool_n = _normalize_tool(tool)
    racks_s = _sanitize_racks(racks or ([rack] if rack else []))
    primary_rack = (rack or "").strip() or (racks_s[0] if racks_s else "")
    now = _now_iso()
    rec = _upgrade_legacy_record(
        {
            "activity_id": aid,
            "job_id": aid,
            "tool": tool_n,
            "kind": _legacy_kind_for_tool(tool_n),
            "stage": stage,
            "scope": scope,
            "rack": primary_rack,
            "racks": racks_s,
            "log_dir": str(log_dir or ""),
            "operator": _sanitize_operator(operator),
            "command_display": str(command_display or ""),
            "scheduler": scheduler,
            "execution_provider": execution_provider or scheduler,
            "fallback": fallback,
            "fallback_reason": str(fallback_reason or ""),
            "nodes": [str(node) for node in (nodes or []) if str(node).strip()],
            "target": ",".join(str(node) for node in (nodes or []) if str(node).strip()),
            "dry_run": dry_run,
            "phase": "starting",
            "returncode": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "source": source,
            "tasks": tasks or [],
        },
        activity_id=aid,
    )

    def _mut(rows: dict[str, dict]) -> None:
        rows[aid] = rec

    _with_store(_mut)
    return aid


def update_activity(activity_id: str, **fields: object) -> None:
    """Merge updatable fields into a stored activity."""
    aid = (activity_id or "").strip()
    if not aid:
        return
    patch: dict[str, object] = {}
    for key, val in fields.items():
        if key == "stdout":
            patch["stdout_tail"] = _tail(val)
        elif key == "stderr":
            patch["stderr_tail"] = _tail(val)
        elif key in ("issue_summary", "outcome_summary"):
            patch[key] = _sanitize_ledger_text(val)
        elif key in _UPDATABLE:
            patch[key] = _tail(val) if key in ("stdout_tail", "stderr_tail") else val
    if not patch:
        return
    patch["updated_at"] = _now_iso()

    def _mut(rows: dict[str, dict]) -> None:
        rec = rows.get(aid)
        if rec is None:
            return
        rec.update(patch)
        rows[aid] = rec

    _with_store(_mut)
    stdout = fields.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        combined = "run_llmb_nemo" in stdout or (
            "stage 1/2" in stdout.lower() and "stage 2/2" in stdout.lower()
        )
        sync_tasks_from_stdout(aid, stdout, combined_nemo=combined)


def annotate_activity(
    activity_id: str,
    *,
    issue_summary: object | None = None,
    outcome_summary: object | None = None,
    corrective_action: object | None = None,
    corrective_action_result: object | None = None,
    corrective_action_status: object | None = None,
    operator: object = "",
) -> dict[str, Any] | None:
    """Add operator findings/actions without replacing prior corrective actions."""
    aid = (activity_id or "").strip()
    if not aid:
        return None
    issue = _sanitize_ledger_text(issue_summary) if issue_summary is not None else None
    outcome = _sanitize_ledger_text(outcome_summary) if outcome_summary is not None else None
    action_text = (
        _sanitize_ledger_text(corrective_action) if corrective_action is not None else ""
    )
    updated: dict[str, Any] | None = None

    def _mut(rows: dict[str, dict]) -> None:
        nonlocal updated
        rec = rows.get(aid)
        if rec is None:
            return
        if issue is not None:
            rec["issue_summary"] = issue
        if outcome is not None:
            rec["outcome_summary"] = outcome
        if action_text:
            actions = _sanitize_corrective_actions(rec.get("corrective_actions"))
            actions.append(
                {
                    "action_id": str(uuid.uuid4()),
                    "recorded_at": _now_iso(),
                    "operator": _sanitize_operator(operator),
                    "action": action_text,
                    "result": _sanitize_ledger_text(corrective_action_result),
                    "status": _sanitize_ledger_text(corrective_action_status) or "recorded",
                }
            )
            rec["corrective_actions"] = actions[-MAX_CORRECTIVE_ACTIONS:]
        rec["updated_at"] = _now_iso()
        rows[aid] = rec
        updated = dict(rec)

    _with_store(_mut)
    return updated


def _task_stage_kind(rec: dict[str, Any], task: dict[str, Any]) -> str | None:
    """Classify a child task as ``nccl`` (stage 1) or ``nemo`` (stage 2)."""
    label = str(task.get("label") or "").strip().lower()
    if label in ("nccl", "stage1") or "nccl" in label:
        return "nccl"
    if label in ("nemo", "stage2", "llm") or "nemo" in label or "llm" in label:
        return "nemo"
    stage = str(rec.get("stage") or "").strip().lower()
    if stage in ("stage1", "1", "s1"):
        return "nccl"
    if stage in ("stage2", "2", "s2"):
        return "nemo"
    tool = str(rec.get("tool") or "").strip().lower()
    if tool in ("llmb-nccl", "nccl-fullrack"):
        return "nccl"
    if tool in ("singlerack", "llmb-nemo-combined", "nemo-xrack", "llama", "llama-exemplar"):
        return "nemo"
    return None


def live_stage_jobs_by_rack(*, limit: int = 200) -> dict[str, dict[str, str]]:
    """Map rack → ``{\"nccl\": jid, \"nemo\": jid}`` for open activity tasks still in flight.

    The status table's Stage pills / ``last_job`` historically only flipped to ongoing when the
    newest on-disk ``*-1Rack-*`` artefact's job id was still in squeue. A newly launched Stage-1
    run (new Slurm job) would stay painted ``fail`` with the prior job id until collect wrote a
    new results log. Activities already track the live scheduler ref — expose it here so the
    console can overlay that truth.
    """
    out: dict[str, dict[str, str]] = {}
    for rec in list_activities(limit=max(1, int(limit))):
        if not isinstance(rec, dict):
            continue
        racks: list[str] = []
        raw_racks = rec.get("racks")
        if isinstance(raw_racks, list):
            for r in raw_racks:
                if isinstance(r, str) and r.strip():
                    racks.append(r.strip())
        if not racks:
            rk = str(rec.get("rack") or "").strip()
            if rk:
                racks = [rk]
        if not racks:
            continue
        act_wp = str(rec.get("work_phase") or "").strip().lower()
        for task in rec.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            ref = str(task.get("scheduler_ref") or "").strip()
            if not scheduler_runtime.valid_reference(ref):
                continue
            wp = str(task.get("work_phase") or "").strip().lower()
            if wp and wp not in OPEN_WORK_PHASES:
                continue
            if not wp:
                st = str(task.get("scheduler_state") or "").strip().upper().split("+")[0]
                if st in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"):
                    continue
                # No task-level signal — require the activity itself to still be open.
                if (
                    st not in ("RUNNING", "PENDING", "COMPLETING", "CONFIGURING")
                    and act_wp
                    and act_wp not in OPEN_WORK_PHASES
                ):
                    continue
            kind = _task_stage_kind(rec, task)
            if not kind:
                continue
            for rack in racks:
                bucket = out.setdefault(rack, {})
                # list_activities is newest-first; keep the first jid per stage.
                if kind not in bucket:
                    bucket[kind] = ref
    return out


def rollup_work_phase(tasks: list[dict] | None) -> str:
    """Derive activity-level work_phase from child tasks (worst open state wins)."""
    if not tasks:
        return "pending"
    best = "done"
    best_rank = -1
    any_task = False
    for t in tasks:
        if not isinstance(t, dict):
            continue
        any_task = True
        wp = str(t.get("work_phase") or "").strip().lower()
        if not wp:
            # Legacy rows: infer from scheduler_state / phase.
            st = str(t.get("scheduler_state") or "").strip().upper().split("+")[0]
            if st in ("RUNNING", "PENDING", "COMPLETING", "CONFIGURING"):
                wp = "running"
            elif st in ("FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY"):
                wp = "failed"
            elif st in ("COMPLETED",) or t.get("results_log_path"):
                wp = "done"
            else:
                wp = "unknown"
        rank = _WORK_PHASE_RANK.get(wp, 0)
        if rank > best_rank:
            best_rank = rank
            best = wp
    return best if any_task else "pending"


def pick_results_log(
    names: Iterable[str],
    job_id: str,
    *,
    label: str = "",
) -> str | None:
    """Choose the aggregated ``*-NCCL-1Rack-*-{jid}.txt`` / ``*-NEMO-1Rack-*-{jid}.txt`` BASENAME.

    Selection only — the caller owns the directory listing. That split matters
    because the log directory belongs to whichever host runs the jobs: when the
    app is a container and the cluster is an SSH hop away, a process-local scan of
    that path sees nothing at all.
    """
    jid = (job_id or "").strip()
    if not jid or not scheduler_runtime.valid_reference(jid):
        return None
    lab = (label or "").strip().lower()
    prefer_nccl = lab in ("nccl", "stage1", "") or "nccl" in lab
    prefer_nemo = lab in ("nemo", "stage2") or "nemo" in lab or "llm" in lab
    suffix = f"-{jid}.txt"
    candidates = [
        n
        for n in (str(x or "").strip() for x in names)
        if n.endswith(suffix)
        and ("nccl-1rack" in n.lower() or "nemo-1rack" in n.lower())
    ]
    if not candidates:
        return None
    if prefer_nemo and not prefer_nccl:
        for n in candidates:
            if "nemo-1rack" in n.lower():
                return n
    if prefer_nccl:
        for n in candidates:
            if "nccl-1rack" in n.lower():
                return n
    # Prefer NCCL then NeMo then any.
    for needle in ("nccl-1rack", "nemo-1rack"):
        for n in candidates:
            if needle in n.lower():
                return n
    return candidates[0]


def find_results_log_for_job(
    log_dir: str | Path | None,
    job_id: str,
    *,
    label: str = "",
) -> str | None:
    """``pick_results_log`` over a log_dir visible to THIS process.

    Only usable when the app and the job logs share a filesystem. Callers that may
    run against a remote execution host list the directory through the transport
    layer and pass the basenames to :func:`pick_results_log` instead.
    """
    jid = (job_id or "").strip()
    if not jid or not scheduler_runtime.valid_reference(str(jid)):
        return None
    root = Path(str(log_dir or "")).expanduser()
    if not root.is_dir():
        return None
    try:
        names = [p.name for p in root.iterdir() if p.is_file()]
    except OSError:
        return None
    picked = pick_results_log(names, jid, label=label)
    return str(root / picked) if picked else None


def add_or_update_task(
    activity_id: str,
    *,
    ordinal: int,
    label: str,
    scheduler_ref: str | None = None,
    scheduler_state: str | None = None,
    phase: str | None = None,
    work_phase: str | None = None,
    evidence: str | None = None,
    stdout_path: str | None = None,
    training_log_path: str | None = None,
    results_log_path: str | None = None,
    task_id: str | None = None,
    collecting_since: str | None = None,
    returncode: int | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> str:
    """Add or merge a child task on an activity. Returns ``task_id``."""
    aid = (activity_id or "").strip()
    if not aid:
        return ""
    tid = (task_id or "").strip() or str(uuid.uuid4())
    now = _now_iso()

    def _mut(rows: dict[str, dict]) -> None:
        current_tid = tid
        rec = rows.get(aid)
        if rec is None:
            return
        tasks = list(rec.get("tasks") or [])
        found: dict[str, Any] | None = None
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if t.get("task_id") == current_tid:
                found = t
                break
            if ordinal and t.get("ordinal") == ordinal and t.get("label") == label:
                found = t
                current_tid = str(t.get("task_id") or current_tid)
                break
        patch: dict[str, Any] = {
            "task_id": current_tid,
            "ordinal": ordinal,
            "label": label,
            "updated_at": now,
        }
        if scheduler_ref is not None:
            patch["scheduler_ref"] = scheduler_ref
        if scheduler_state is not None:
            patch["scheduler_state"] = scheduler_state
        if phase is not None:
            patch["phase"] = phase
        if work_phase is not None:
            patch["work_phase"] = work_phase
        if evidence is not None:
            patch["evidence"] = evidence
        if stdout_path is not None:
            patch["stdout_path"] = stdout_path
        if training_log_path is not None:
            patch["training_log_path"] = training_log_path
        if results_log_path is not None:
            patch["results_log_path"] = results_log_path
        if returncode is not None:
            patch["returncode"] = int(returncode)
        if started_at is not None:
            patch["started_at"] = started_at
        if finished_at is not None:
            patch["finished_at"] = finished_at
        # Empty string clears it (the task left "collecting"); None leaves it alone.
        if collecting_since is not None:
            patch["collecting_since"] = collecting_since or None
        if found is None:
            patch.setdefault("scheduler_ref", None)
            patch.setdefault("scheduler_state", None)
            patch.setdefault("phase", "starting")
            patch.setdefault("work_phase", "pending")
            patch.setdefault("evidence", None)
            patch.setdefault("stdout_path", None)
            patch.setdefault("training_log_path", None)
            patch.setdefault("results_log_path", None)
            patch.setdefault("collecting_since", None)
            tasks.append(patch)
        else:
            found.update(patch)
        rec["tasks"] = tasks
        rec["work_phase"] = rollup_work_phase(tasks)
        rec["updated_at"] = now
        rows[aid] = rec

    _with_store(_mut)
    return tid


def sync_tasks_from_stdout(activity_id: str, stdout: str, *, combined_nemo: bool = False) -> None:
    """Parse wrapper stdout and attach scheduler job ids as tasks."""
    jids = parse_scheduler_job_ids(stdout)
    if not jids:
        return
    if combined_nemo and len(jids) >= 2:
        add_or_update_task(activity_id, ordinal=1, label="nccl", scheduler_ref=jids[0])
        add_or_update_task(activity_id, ordinal=2, label="nemo", scheduler_ref=jids[1])
        return
    if len(jids) == 1:
        label = "nccl" if "nccl" in stdout.lower()[:500] else "validation"
        add_or_update_task(activity_id, ordinal=1, label=label, scheduler_ref=jids[0])
        return
    for i, jid in enumerate(jids, start=1):
        add_or_update_task(activity_id, ordinal=i, label=f"job{i}", scheduler_ref=jid)


def delete_activity(activity_id: str) -> bool:
    aid = (activity_id or "").strip()
    if not aid:
        return False
    removed = False

    def _mut(rows: dict[str, dict]) -> None:
        nonlocal removed
        if aid in rows:
            rows.pop(aid, None)
            removed = True

    _with_store(_mut)
    return removed


def get_activity(activity_id: str) -> dict[str, Any] | None:
    aid = (activity_id or "").strip()
    if not aid or not ACTIVITIES_FILE.is_file():
        return None
    try:
        with open(ACTIVITIES_FILE, "r", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                locked = False
            try:
                rows = _read_locked(f)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError):
        return None
    rec = rows.get(aid)
    return dict(rec) if isinstance(rec, dict) else None


def list_activities(
    *,
    log_dir: str | None = None,
    rack: str | None = None,
    tool: str | None = None,
    scope: str | None = None,
    limit: int = 100,
) -> list[dict]:
    _migrate_legacy_if_needed()
    if not ACTIVITIES_FILE.is_file():
        return []
    try:
        with open(ACTIVITIES_FILE, "r", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                locked = False
            try:
                rows = _read_locked(f)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError):
        return []
    _sync_archive_from_live(rows)
    out = list(rows.values())
    if log_dir:
        want = str(log_dir)
        out = [r for r in out if str(r.get("log_dir") or "") == want]
    if rack:
        rk = str(rack).strip()
        out = [
            r for r in out
            if str(r.get("rack") or "") == rk or rk in (r.get("racks") or [])
        ]
    if tool:
        t = _normalize_tool(tool)
        out = [r for r in out if _normalize_tool(str(r.get("tool") or "")) == t]
    if scope:
        sc = str(scope).strip().lower()
        out = [r for r in out if str(r.get("scope") or "").lower() == sc]
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if limit and limit > 0:
        out = out[:limit]
    return [dict(r) for r in out]


def _activity_completed(rec: dict[str, Any]) -> bool:
    """True when a non-dry-run activity has durable successful work evidence."""
    if bool(rec.get("dry_run")):
        return False
    tasks = [task for task in (rec.get("tasks") or []) if isinstance(task, dict)]
    if tasks:
        return rollup_work_phase(tasks) == "done"
    work_phase = str(rec.get("work_phase") or "").strip().lower()
    if work_phase:
        return work_phase == "done"
    phase = str(rec.get("phase") or "").strip().lower()
    return phase == "done" and rec.get("returncode") in (None, 0, "0")


def completion_rollup(
    *,
    log_dir: str | None = None,
    rack: str | None = None,
    tool: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Completed-rack rollup from the append-preserving activity archive.

    Completion is monotonic per ``tool + rack``: a later failed retry remains
    visible in the attempts list but does not erase an earlier successful
    acceptance run.
    """
    # Ensures pre-upgrade live rows are imported before the first archive query.
    list_activities(limit=0)
    path = Path(ACTIVITY_ARCHIVE_DB)
    raw_rows: list[sqlite3.Row] = []
    if path.is_file():
        try:
            conn = sqlite3.connect(str(path), timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                raw_rows = conn.execute(
                    "SELECT payload FROM validation_activities "
                    "ORDER BY updated_at DESC, created_at DESC, activity_id DESC"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            _logger.warning("could not read validation activity archive %s: %s", path, exc)

    want_tool = _normalize_tool(tool) if tool else None
    want_scope = str(scope or "").strip().lower() or None
    want_rack = str(rack or "").strip() or None
    completed: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in raw_rows:
        try:
            rec = json.loads(str(raw["payload"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict) or not _activity_completed(rec):
            continue
        rec_tool = _normalize_tool(str(rec.get("tool") or rec.get("kind") or "external"))
        if want_tool and rec_tool != want_tool:
            continue
        if log_dir and str(rec.get("log_dir") or "") != str(log_dir):
            continue
        if want_scope and str(rec.get("scope") or "").strip().lower() != want_scope:
            continue
        racks = _sanitize_racks(rec.get("racks") or [])
        primary = str(rec.get("rack") or "").strip()
        if primary and primary not in racks:
            racks.insert(0, primary)
        if want_rack:
            racks = [candidate for candidate in racks if candidate == want_rack]
        for rack_id in racks:
            bucket = completed.setdefault(rec_tool, {})
            if rack_id not in bucket:
                bucket[rack_id] = {
                    "activity_id": str(rec.get("activity_id") or rec.get("job_id") or ""),
                    "completed_at": str(
                        rec.get("finished_at") or rec.get("updated_at") or rec.get("created_at") or ""
                    ),
                }

    # Old one-rack runs predate the activity registry and some later fell out of
    # its capped JSON cache.  The parsed results database is also real,
    # fingerprinted completion evidence, so merge it into the same monotonic
    # rollup instead of requiring an operator to fabricate replacement activity
    # rows.  Activity evidence wins where both sources exist because it carries
    # the richer scheduler record.
    result_tool = {"nccl": "nccl-fullrack", "nemo": "llmb-nemo-combined"}
    result_scope_allowed = not want_scope or want_scope in {"rack", "single-rack", "one-rack"}
    if result_scope_allowed:
        try:
            durable_results = results_history.latest_successful_runs_by_rack(tuple(result_tool))
        except Exception:
            _logger.warning("could not read completed validation results history", exc_info=True)
            durable_results = {}
        for result_kind, tool_name in result_tool.items():
            if want_tool and tool_name != want_tool:
                continue
            for rack_id, run in (durable_results.get(result_kind) or {}).items():
                rack_id = str(rack_id or "").strip()
                if not rack_id or (want_rack and rack_id != want_rack):
                    continue
                source_path = str(run.get("source_log_path") or "")
                if log_dir and not source_path.startswith(str(log_dir).rstrip("/") + "/"):
                    continue
                bucket = completed.setdefault(tool_name, {})
                if rack_id not in bucket:
                    bucket[rack_id] = {
                        "activity_id": f"results:{run.get('run_id') or ''}",
                        "completed_at": str(run.get("ended_at") or run.get("started_at") or ""),
                        "source": "results_history",
                        "source_log_path": source_path,
                    }

    by_tool: dict[str, Any] = {}
    all_racks: set[str] = set()
    for tool_name, by_rack in sorted(completed.items()):
        racks = sorted(by_rack)
        all_racks.update(racks)
        by_tool[tool_name] = {
            "completed_rack_count": len(racks),
            "completed_racks": racks,
            "evidence_by_rack": by_rack,
        }
    return {
        "generated_at": _now_iso(),
        "completed_rack_count": len(all_racks),
        "completed_racks": sorted(all_racks),
        "by_tool": by_tool,
    }


def adopt_log(
    log_path: str,
    *,
    log_dir: str = "",
    rack: str = "",
    operator: str = "",
) -> str:
    """Register an external validation log; returns existing or new ``activity_id``."""
    path = str(log_path or "").strip()
    if not path:
        return ""
    base = os.path.basename(path)

    existing_id: str | None = None

    def _find_and_reconcile(rows: dict[str, dict]) -> None:
        nonlocal existing_id
        existing_id = _find_by_log_path(rows, path)
        if not existing_id:
            return
        rec = rows.get(existing_id)
        if not isinstance(rec, dict) or str(rec.get("source") or "") != "external":
            return
        # An adoption can match by basename or scheduler id for backward compatibility. Only
        # exact-path evidence is authoritative enough to rewrite persisted attribution.
        exact_path = any(
            str(task.get(key) or "") == path
            for task in rec.get("tasks") or []
            if isinstance(task, dict)
            for key in ("stdout_path", "training_log_path", "results_log_path")
        )
        if not exact_path:
            return
        changed = False
        if log_dir and str(rec.get("log_dir") or "") != log_dir:
            rec["log_dir"] = log_dir
            changed = True
        if rack and (
            str(rec.get("rack") or "") != rack or list(rec.get("racks") or []) != [rack]
        ):
            rec["rack"] = rack
            rec["racks"] = [rack]
            changed = True
        if changed:
            rec["updated_at"] = _now_iso()
            rows[existing_id] = rec

    _with_store(_find_and_reconcile)
    if existing_id:
        return existing_id

    inferred = infer_from_log_basename(base)
    jid = job_id_from_log_basename(base)
    racks = [rack] if rack else []
    aid = record_activity(
        inferred["tool"],
        stage=inferred["stage"],
        scope="rack",
        rack=rack,
        racks=racks,
        log_dir=log_dir,
        operator=operator or "external",
        command_display=f"adopted log {base}",
        source="external",
    )
    task_kw: dict[str, Any] = {
        "ordinal": 1,
        "label": inferred["label"],
        "results_log_path": path,
        "stdout_path": path,
        "phase": "done",
    }
    if jid:
        task_kw["scheduler_ref"] = jid
    add_or_update_task(aid, **task_kw)
    update_activity(aid, phase="done", finished_at=_now_iso())
    return aid


# --- Legacy multirack_jobs API (shim targets) ---

def record_job(
    kind: str,
    racks: list[str],
    log_dir: str,
    *,
    operator: str = "",
    command_display: str = "",
    dry_run: bool = False,
    job_id: str | None = None,
) -> str:
    tool = _normalize_tool(kind)
    scope = "multirack" if len(racks) > 1 else "rack"
    stage = "multirack" if scope == "multirack" else "stage1"
    if tool == "llmb-nccl":
        stage = "stage1"
    if tool == "nemo-xrack":
        stage = "multirack"
    return record_activity(
        tool,
        stage=stage,
        scope=scope,
        racks=racks,
        rack=racks[0] if racks else "",
        log_dir=log_dir,
        operator=operator,
        command_display=command_display,
        dry_run=dry_run,
        activity_id=job_id,
    )


def update_job(job_id: str, **fields: object) -> None:
    update_activity(job_id, **fields)
    stdout = fields.get("stdout")
    if isinstance(stdout, str) and stdout.strip():
        combined = "run_llmb_nemo" in stdout or (
            "stage 1/2" in stdout.lower() and "stage 2/2" in stdout.lower()
        )
        sync_tasks_from_stdout(job_id, stdout, combined_nemo=combined)


def delete_job(job_id: str) -> bool:
    return delete_activity(job_id)


def list_jobs(log_dir: str | None = None, limit: int = 100) -> list[dict]:
    return list_activities(log_dir=log_dir, scope="multirack", limit=limit)
