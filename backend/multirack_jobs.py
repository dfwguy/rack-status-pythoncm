#!/usr/bin/env python3
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Persistent registry of launched multi-rack jobs (NEMO x-rack + LLMB NCCL).

The in-memory ``_stress_jobs`` / ``_xrack_jobs`` dicts in ``app.py`` are per-process and
purged ~15 minutes after completion, so a job vanishes on restart or once the launching
operator's session ends. This module keeps a durable, shared record — keyed by the job's
UUID — so the Multirack view's "Multi-rack jobs" table shows every launch and its
last-known phase to **any** user, in **any** session, even after a backend restart.

Storage mirrors ``rack_locks.py``: a single JSON file guarded by ``fcntl`` advisory
flock with an atomic full rewrite. Designed for a single Flask process with a modest
number of operators. The store is capped to the most-recent ``MAX_JOBS`` entries.

Path override: ``RACK_MULTIRACK_JOBS_FILE``.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
REPO_ROOT = APP_DIR.parent
JOBS_FILE = Path(
    os.environ.get("RACK_MULTIRACK_JOBS_FILE", "")
    or str(REPO_ROOT / "var" / "cache" / "multirack_jobs.json")
)

MAX_JOBS = 200
TAIL_CHARS = 8000
OPERATOR_MAX_LEN = 64
KINDS = ("nemo", "llmb")
TERMINAL_PHASES = frozenset({"done", "error", "timeout"})

# Fields callers may set via ``update_job`` (phase transitions from the worker).
_UPDATABLE = frozenset(
    {"phase", "returncode", "error", "finished_at", "stdout_tail", "stderr_tail", "command_display"}
)


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


def _ensure_dir() -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)


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


def _prune(jobs: dict[str, dict]) -> dict[str, dict]:
    """Keep the MAX_JOBS most-recently-created entries."""
    if len(jobs) <= MAX_JOBS:
        return jobs
    ordered = sorted(
        jobs.items(),
        key=lambda kv: str(kv[1].get("created_at") or ""),
        reverse=True,
    )
    return dict(ordered[:MAX_JOBS])


def _write_locked(f, jobs: dict[str, dict]) -> None:
    blob = json.dumps(_prune(jobs), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    f.seek(0)
    f.truncate()
    f.write(blob)
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError:
        pass


def _open_rw():
    _ensure_dir()
    if not JOBS_FILE.exists():
        JOBS_FILE.write_text("{}\n", encoding="utf-8")
    return open(JOBS_FILE, "r+", encoding="utf-8")


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
    """Persist a newly launched job. Returns the job's UUID (generated when not supplied)."""
    jid = (job_id or "").strip() or str(uuid.uuid4())
    k = kind if kind in KINDS else "nemo"
    now = _now_iso()
    rec = {
        "job_id": jid,
        "kind": k,
        "racks": _sanitize_racks(racks),
        "log_dir": str(log_dir or ""),
        "operator": _sanitize_operator(operator),
        "command_display": str(command_display or ""),
        "dry_run": bool(dry_run),
        "phase": "starting",
        "returncode": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "stdout_tail": "",
        "stderr_tail": "",
    }
    try:
        with _open_rw() as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                jobs = _read_locked(f)
                jobs[jid] = rec
                _write_locked(f, jobs)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass
    return jid


def update_job(job_id: str, **fields: object) -> None:
    """Merge *fields* (restricted to ``_UPDATABLE``) into a stored job and bump ``updated_at``.

    ``stdout``/``stderr`` are accepted as aliases and stored as truncated tails.
    No-op when the job id is unknown (e.g. store pruned it).
    """
    jid = (job_id or "").strip()
    if not jid:
        return
    patch: dict[str, object] = {}
    for key, val in fields.items():
        if key == "stdout":
            patch["stdout_tail"] = _tail(val)
        elif key == "stderr":
            patch["stderr_tail"] = _tail(val)
        elif key in _UPDATABLE:
            patch[key] = _tail(val) if key in ("stdout_tail", "stderr_tail") else val
    if not patch:
        return
    patch["updated_at"] = _now_iso()
    try:
        with _open_rw() as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                jobs = _read_locked(f)
                rec = jobs.get(jid)
                if rec is None:
                    return
                rec.update(patch)
                jobs[jid] = rec
                _write_locked(f, jobs)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass


def delete_job(job_id: str) -> bool:
    """Remove a job from the store (Dismiss). Returns True when an entry was removed."""
    jid = (job_id or "").strip()
    if not jid or not JOBS_FILE.is_file():
        return False
    removed = False
    try:
        with _open_rw() as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                jobs = _read_locked(f)
                if jid in jobs:
                    jobs.pop(jid, None)
                    _write_locked(f, jobs)
                    removed = True
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        return False
    return removed


def list_jobs(log_dir: str | None = None, limit: int = 100) -> list[dict]:
    """Return jobs newest-first, optionally filtered to a *log_dir*."""
    if not JOBS_FILE.is_file():
        return []
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH)
                locked = True
            except OSError:
                locked = False
            try:
                jobs = _read_locked(f)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except (OSError, UnicodeDecodeError):
        return []
    rows = list(jobs.values())
    if log_dir:
        want = str(log_dir)
        rows = [r for r in rows if str(r.get("log_dir") or "") == want]
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    if limit and limit > 0:
        rows = rows[:limit]
    return rows
