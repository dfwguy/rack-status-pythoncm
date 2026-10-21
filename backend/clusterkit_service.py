# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Product-facing ClusterKit preview, submission and reconciliation service."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clusterkit_execution import prepare_attempt
from clusterkit_planner import (
    SlurmInventory,
    build_plan,
    candidates_for,
    parse_request,
    plan_request,
)
from clusterkit_profile import ProfileResolution
from clusterkit_results import read_json
from schedulers import JobHandle, JobSpec
from schedulers.base import TERMINAL_STATES

TOKEN_VERSION = 1
DEFAULT_PREVIEW_TTL_SECONDS = 300
MAX_PREVIEW_TTL_SECONDS = 900
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
ARTIFACT_PART = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_logger = logging.getLogger(__name__)


class ConfirmationError(ValueError):
    """A preview confirmation is absent, stale or does not match current policy."""


class DuplicateSubmissionError(RuntimeError):
    """The confirmed attempt was already claimed, whether or not submission succeeded."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _collect_grace_seconds() -> int:
    try:
        return max(30, min(1800, int(os.environ.get("RACK_CLUSTERKIT_COLLECT_GRACE_SEC", "300"))))
    except ValueError:
        return 300


def _age_seconds(value: object) -> float:
    try:
        started = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret_bytes(secret: object) -> bytes:
    if isinstance(secret, bytes) and len(secret) >= 32:
        return secret
    if isinstance(secret, str) and len(secret.encode()) >= 32:
        return secret.encode()
    raise RuntimeError("A stable application secret of at least 32 bytes is required")


def profile_digest(resolution: ProfileResolution) -> str:
    if not resolution.ready or resolution.profile is None:
        raise ConfirmationError("ClusterKit profile is disabled or invalid")
    return hashlib.sha256(_json(resolution.profile.as_dict())).hexdigest()


def sign_preview(plan: dict[str, Any], secret: object, *, now: float | None = None,
                 ttl_seconds: int = DEFAULT_PREVIEW_TTL_SECONDS) -> str:
    if not 1 <= ttl_seconds <= MAX_PREVIEW_TTL_SECONDS:
        raise ValueError("Invalid preview lifetime")
    current = time.time() if now is None else now
    payload = {
        "version": TOKEN_VERSION,
        "expires_at": int(current) + ttl_seconds,
        "attempt_id": plan["attempt_id"],
        "request": plan["request"],
        "nodes": plan["nodes"],
        "plan_hash": plan["plan_hash"],
        "profile_hash": hashlib.sha256(_json(plan["effective_profile"])).hexdigest(),
    }
    encoded = _b64(_json(payload))
    signature = _b64(hmac.new(_secret_bytes(secret), encoded.encode(), hashlib.sha256).digest())
    return encoded + "." + signature


def verify_preview(token: object, secret: object, *, now: float | None = None) -> dict[str, Any]:
    if not isinstance(token, str) or len(token) > 32768 or token.count(".") != 1:
        raise ConfirmationError("Invalid preview confirmation")
    encoded, supplied = token.split(".", 1)
    expected = _b64(hmac.new(_secret_bytes(secret), encoded.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(supplied, expected):
        raise ConfirmationError("Invalid preview confirmation signature")
    try:
        payload = json.loads(_unb64(encoded))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ConfirmationError("Invalid preview confirmation payload") from exc
    current = time.time() if now is None else now
    if (not isinstance(payload, dict) or payload.get("version") != TOKEN_VERSION
            or type(payload.get("expires_at")) is not int
            or payload["expires_at"] < int(current)):
        raise ConfirmationError("Preview confirmation expired or has an unsupported version")
    return payload


def readiness(resolution: ProfileResolution) -> dict[str, Any]:
    response = resolution.public()
    if resolution.profile is None:
        return response
    profile = resolution.profile
    response.update({
        "partition": profile.partition,
        "path_comparison": profile.path_comparison,
        "explicit_nodes": list(profile.node_allowlist),
        "regex_selection_enabled": bool(profile.node_regex),
        "groups": [
            {
                "name": group.name,
                "kind": group.kind,
                "authoritative": group.authoritative,
                "source": group.source,
                "nodes": list(group.nodes),
                "node_count": len(group.nodes),
            }
            for group in profile.topology_groups
        ],
    })
    return response


def inventory(resolution: ProfileResolution, reader: SlurmInventory) -> dict[str, Any]:
    if not resolution.ready or resolution.profile is None:
        raise ValueError("ClusterKit profile is disabled or invalid")
    profile = resolution.profile
    candidates = list(profile.node_allowlist)
    if not candidates:
        candidates = sorted({node for group in profile.topology_groups for node in group.nodes})
    if not candidates:
        return {"source": "slurm", "captured_at": None, "nodes": [],
                "note": "No finite configured node inventory; preview explicit nodes to query them."}
    return reader.query(tuple(candidates))


def preview(raw_request: object, resolution: ProfileResolution, reader: SlurmInventory,
            *, scripts_dir: str, secret: object, now: float | None = None,
            ttl_seconds: int = DEFAULT_PREVIEW_TTL_SECONDS) -> dict[str, Any]:
    plan = plan_request(raw_request, resolution, reader, scripts_dir=scripts_dir)
    return {
        "plan": plan,
        "confirmation_token": sign_preview(plan, secret, now=now, ttl_seconds=ttl_seconds),
        "confirmation_expires_in_seconds": ttl_seconds,
        "confirmation_required": True,
    }


def confirm_plan(token: object, resolution: ProfileResolution, reader: SlurmInventory,
                 *, scripts_dir: str, secret: object, now: float | None = None) -> dict[str, Any]:
    payload = verify_preview(token, secret, now=now)
    if profile_digest(resolution) != payload.get("profile_hash"):
        raise ConfirmationError("ClusterKit site policy changed after preview")
    try:
        request = parse_request(payload["request"])
        candidates = candidates_for(resolution, request)
        snapshot = reader.query(candidates)
        plan = build_plan(payload["request"], resolution, snapshot, scripts_dir=scripts_dir,
                          attempt_id=payload["attempt_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfirmationError(f"Preview can no longer be confirmed: {exc}") from exc
    if plan["nodes"] != payload.get("nodes"):
        raise ConfirmationError("Selected nodes changed after preview")
    return plan


def job_spec(plan: dict[str, Any]) -> JobSpec:
    resources = plan["resources"]
    wall = resources["wall_seconds"]
    wall_text = f"{wall // 3600:02d}:{wall % 3600 // 60:02d}:{wall % 60:02d}"
    label = plan["request"]["label"]
    return JobSpec(
        name=f"ck-{label}"[:128],
        nodes=plan["node_count"],
        nodelist=",".join(plan["nodes"]),
        ntasks_per_node=1,
        script=plan["entrypoint"],
        script_args=[str(Path(plan["results_directory"]) / "plan.json"), plan["scripts_directory"]],
        output=str(Path(plan["results_directory"]) / "slurm-%j.out"),
        extra_args=[
            f"--partition={plan['effective_profile']['partition']}",
            f"--ntasks={plan['node_count']}",
            "--cpus-per-task=1",
            "--mem=1G",
            "--gres=none",
            "--no-requeue",
            f"--time={wall_text}",
        ],
        timeout=wall + 120,
    )


def submit_confirmed(token: object, resolution: ProfileResolution, reader: SlurmInventory,
                     scheduler: object, registry: object, *, scripts_dir: str, secret: object,
                     operator: str = "", now: float | None = None) -> dict[str, Any]:
    plan = confirm_plan(token, resolution, reader, scripts_dir=scripts_dir, secret=secret, now=now)
    try:
        prepared = prepare_attempt(plan, scripts_dir)
    except FileExistsError as exc:
        raise DuplicateSubmissionError("This preview was already claimed; create a new preview") from exc
    activity_id = str(uuid.UUID(plan["attempt_id"]))
    spec = job_spec(plan)
    task_id = str(uuid.uuid4())
    registry.record_activity(
        "clusterkit",
        stage="preflight" if plan["scope"] == "preflight" else "pairwise",
        scope=plan["scope"],
        log_dir=plan["results_directory"],
        operator=operator,
        command_display=plan["command_display"],
        scheduler="slurm",
        execution_provider="slurm",
        source="clusterkit-api",
        activity_id=activity_id,
        nodes=plan["nodes"],
        tasks=[{
            "task_id": task_id,
            "ordinal": 1,
            "label": spec.name,
            "phase": "starting",
            "work_phase": "pending",
            "scheduler_ref": None,
            "scheduler_state": None,
            "evidence": "clusterkit-normalized-summary",
            "stdout_path": spec.output,
            "results_log_path": str(Path(plan["results_directory"]) / "summary.json"),
            "plan_hash": plan["plan_hash"],
            "attempt_id": plan["attempt_id"],
        }],
    )
    handle = None
    try:
        handle = scheduler.submit(spec)
        stdout_path = str(spec.output).replace("%j", handle.job_id)
        submitted_at = _now_iso()
        registry.add_or_update_task(
            activity_id,
            ordinal=1,
            label=spec.name,
            task_id=task_id,
            scheduler_ref=handle.job_id,
            scheduler_state="PENDING",
            phase="running",
            work_phase="submitted",
            evidence="clusterkit-normalized-summary",
            stdout_path=stdout_path,
            results_log_path=str(Path(plan["results_directory"]) / "summary.json"),
            started_at=submitted_at,
        )
        registry.update_activity(activity_id, phase="running", work_phase="submitted",
                                 scheduler_ref=handle.job_id, started_running_at=submitted_at)
    except Exception as exc:
        if handle is not None:
            try:
                scheduler.cancel(handle)
            except Exception:
                _logger.exception("Could not cancel ClusterKit job after submission tracking failed")
        registry.update_activity(activity_id, phase="error", work_phase="failed", returncode=-1,
                                 error=str(exc), finished_at=_now_iso())
        raise
    return {
        "ok": True,
        "activity_id": activity_id,
        "scheduler_job_id": handle.job_id,
        "attempt_id": plan["attempt_id"],
        "results_directory": plan["results_directory"],
        "nodes": plan["nodes"],
        "scope": plan["scope"],
        "prepared": prepared["prepared"],
    }


def _task(activity: dict[str, Any]) -> dict[str, Any]:
    tasks = [item for item in activity.get("tasks", []) if isinstance(item, dict)]
    if len(tasks) != 1:
        raise ValueError("ClusterKit activity must contain exactly one scheduler task")
    return tasks[0]


def reconcile(activity_id: str, registry: object, scheduler: object) -> dict[str, Any]:
    activity = registry.get_activity(activity_id)
    if not isinstance(activity, dict) or activity.get("tool") != "clusterkit" \
            or activity.get("source") != "clusterkit-api":
        raise KeyError("Unknown modular ClusterKit activity")
    task = _task(activity)
    reference = str(task.get("scheduler_ref") or "")
    scheduler_error = None
    state = str(task.get("scheduler_state") or "PENDING").upper()
    if reference:
        try:
            state = str(scheduler.status(JobHandle(reference, "slurm"))).upper().split()[0].rstrip("+")
        except Exception as exc:  # noqa: BLE001 - status remains durable when its provider is unavailable.
            scheduler_error = str(exc)
    root = Path(str(activity.get("log_dir") or ""))
    summary = None
    summary_path = root / "summary.json"
    if summary_path.is_file():
        summary = read_json(summary_path)
    if not scheduler_error and reference:
        terminal = state in TERMINAL_STATES
        functional = summary.get("functional_status") if isinstance(summary, dict) else None
        success = state == "COMPLETED" and functional in {"PASS", "WARN"}
        collecting_since = task.get("collecting_since")
        collecting = state == "COMPLETED" and summary is None and (
            not collecting_since or _age_seconds(collecting_since) < _collect_grace_seconds()
        )
        if collecting and not collecting_since:
            collecting_since = _now_iso()
        collecting_value = str(collecting_since) if collecting else ("" if collecting_since else None)
        work_phase = "done" if success else ("collecting" if collecting else (
            "failed" if terminal else "running"))
        phase = "done" if success else ("running" if collecting or not terminal else "error")
        registry.add_or_update_task(
            activity_id,
            ordinal=1,
            label=str(task.get("label") or "clusterkit"),
            task_id=str(task.get("task_id") or ""),
            scheduler_ref=reference,
            scheduler_state=state,
            phase=phase,
            work_phase=work_phase,
            evidence="clusterkit-normalized-summary",
            stdout_path=str(task.get("stdout_path") or ""),
            results_log_path=str(summary_path),
            collecting_since=collecting_value,
            returncode=0 if success else (1 if terminal and not collecting else None),
            finished_at=_now_iso() if terminal and not collecting else None,
        )
        registry.update_activity(
            activity_id,
            phase=phase,
            work_phase=work_phase,
            returncode=0 if success else (1 if terminal and not collecting else None),
            error=None if success or collecting or not terminal else (
                "Scheduler completed without a passing normalized ClusterKit summary"
                if state == "COMPLETED" else f"Slurm job ended in {state}"
            ),
            finished_at=_now_iso() if terminal and not collecting else None,
        )
        activity = registry.get_activity(activity_id) or activity
    return {"activity": activity, "scheduler_state": state,
            "scheduler_error": scheduler_error, "summary": summary}


def artifacts(activity: dict[str, Any]) -> list[dict[str, Any]]:
    root = Path(str(activity.get("log_dir") or ""))
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        return []
    rows = []
    entries = 0
    pending = [(root, 0)]
    while pending and len(rows) < 128:
        directory, depth = pending.pop()
        for child in directory.iterdir():
            entries += 1
            if entries > 256:
                return sorted(rows, key=lambda item: item["name"])
            if child.is_symlink():
                continue
            if child.is_dir() and depth < 3:
                pending.append((child, depth + 1))
            elif child.is_file():
                stat = child.stat()
                rows.append({"name": str(child.relative_to(root)), "size": stat.st_size,
                             "downloadable": stat.st_size <= MAX_ARTIFACT_BYTES})
                if len(rows) >= 128:
                    break
    return sorted(rows, key=lambda item: item["name"])


def artifact(activity: dict[str, Any], name: str) -> tuple[bytes, str]:
    relative = Path(name) if isinstance(name, str) else Path(".")
    if (not isinstance(name, str) or not name or relative.is_absolute() or len(relative.parts) > 4
            or any(part in {".", ".."} or not ARTIFACT_PART.fullmatch(part) for part in relative.parts)):
        raise ValueError("Invalid artifact name")
    root = Path(str(activity.get("log_dir") or ""))
    path = root / relative
    if not root.is_absolute() or root.is_symlink() or path.is_symlink() or not path.is_file():
        raise FileNotFoundError(name)
    data = path.read_bytes()
    if len(data) > MAX_ARTIFACT_BYTES:
        raise ValueError("Artifact exceeds download limit")
    content_type = "application/json" if path.suffix == ".json" else "text/plain; charset=utf-8"
    return data, content_type
