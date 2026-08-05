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
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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

MAX_ACTIVITIES = 500
TAIL_CHARS = 8000
OPERATOR_MAX_LEN = 64
TERMINAL_PHASES = frozenset({"done", "error", "timeout"})
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
_SLURM_JID_BASENAME_RE = re.compile(r"N-(\d+)\.txt$", re.IGNORECASE)
_NEMO_LLAMA_JID_RE = re.compile(r"NEMO-LLAMA3_8B-1N-.+-(\d+)\.txt$", re.IGNORECASE)

_UPDATABLE = frozenset({
    "phase",
    "returncode",
    "error",
    "finished_at",
    "stdout_tail",
    "stderr_tail",
    "command_display",
    "scheduler",
    "tasks",
})


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
    return dict(ordered[:MAX_ACTIVITIES])


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
        "dry_run": bool(rec.get("dry_run")),
        "phase": str(rec.get("phase") or "starting"),
        "returncode": rec.get("returncode"),
        "error": rec.get("error"),
        "created_at": str(rec.get("created_at") or _now_iso()),
        "updated_at": str(rec.get("updated_at") or _now_iso()),
        "finished_at": rec.get("finished_at"),
        "stdout_tail": str(rec.get("stdout_tail") or ""),
        "stderr_tail": str(rec.get("stderr_tail") or ""),
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
    try:
        with _open_rw() as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX)
                locked = True
            except OSError:
                locked = False
            try:
                rows = _read_locked(f)
                mutator(rows)
                _write_locked(f, rows)
            finally:
                if locked:
                    try:
                        fcntl.flock(f, fcntl.LOCK_UN)
                    except OSError:
                        pass
    except OSError:
        pass


def parse_scheduler_job_ids(text: str) -> list[str]:
    """Extract Slurm job ids from llmb-run / wrapper stdout."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _JOBID_RE.finditer(text):
        jid = m.group(1)
        if jid not in seen:
            seen.add(jid)
            out.append(jid)
    for m in _JOBID_LINE_RE.finditer(text):
        jid = m.group(1)
        if jid not in seen:
            seen.add(jid)
            out.append(jid)
    return out


def job_id_from_log_basename(name: str) -> str | None:
    m = _NEMO_LLAMA_JID_RE.search(name)
    if m:
        return m.group(1)
    m2 = _SLURM_JID_BASENAME_RE.search(name)
    return m2.group(1) if m2 else None


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
    source: str = "ui",
    activity_id: str | None = None,
    tasks: list[dict[str, Any]] | None = None,
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
    if tool in ("singlerack", "llmb-nemo-combined", "nemo-xrack", "llama"):
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
            if not ref.isdigit():
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
            elif st in ("COMPLETED",):
                wp = "done"
            elif t.get("results_log_path"):
                wp = "done"
            else:
                wp = "unknown"
        rank = _WORK_PHASE_RANK.get(wp, 0)
        if rank > best_rank:
            best_rank = rank
            best = wp
    return best if any_task else "pending"


def find_results_log_for_job(
    log_dir: str | Path | None,
    job_id: str,
    *,
    label: str = "",
) -> str | None:
    """Locate an aggregated ``*-NCCL-1Rack-*-{jid}.txt`` / ``*-NEMO-1Rack-*-{jid}.txt`` under log_dir."""
    jid = (job_id or "").strip()
    if not jid or not str(jid).isdigit():
        return None
    root = Path(str(log_dir or "")).expanduser()
    if not root.is_dir():
        return None
    lab = (label or "").strip().lower()
    prefer_nccl = lab in ("nccl", "stage1", "") or "nccl" in lab
    prefer_nemo = lab in ("nemo", "stage2") or "nemo" in lab or "llm" in lab
    suffix = f"-{jid}.txt"
    candidates: list[Path] = []
    try:
        for p in root.iterdir():
            if not p.is_file():
                continue
            name = p.name
            if not name.endswith(suffix):
                continue
            low = name.lower()
            if "nccl-1rack" in low or "nemo-1rack" in low:
                candidates.append(p)
    except OSError:
        return None
    if not candidates:
        return None
    if prefer_nemo and not prefer_nccl:
        for p in candidates:
            if "nemo-1rack" in p.name.lower():
                return str(p)
    if prefer_nccl:
        for p in candidates:
            if "nccl-1rack" in p.name.lower():
                return str(p)
    # Prefer NCCL then NeMo then any.
    for needle in ("nccl-1rack", "nemo-1rack"):
        for p in candidates:
            if needle in p.name.lower():
                return str(p)
    return str(candidates[0])


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
        if found is None:
            patch.setdefault("scheduler_ref", None)
            patch.setdefault("scheduler_state", None)
            patch.setdefault("phase", "starting")
            patch.setdefault("work_phase", "pending")
            patch.setdefault("evidence", None)
            patch.setdefault("stdout_path", None)
            patch.setdefault("training_log_path", None)
            patch.setdefault("results_log_path", None)
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

    def _find(rows: dict[str, dict]) -> None:
        nonlocal existing_id
        existing_id = _find_by_log_path(rows, path)

    _with_store(_find)
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
