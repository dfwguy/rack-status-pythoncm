# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Workflow Engine — YAML template CRUD, state machine, and execution engine.

Workflows are defined as YAML files in var/workflows/ with mermaid diagrams
and step metadata. Execution state is persisted as JSON in var/workflows/runs/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import host_exec
import rack_locks
import scheduler_runtime
import workflow_models
import yaml

_logger = logging.getLogger(__name__)

# Pattern used to discover a rack id inside a substituted ``host:`` command.
_WORKFLOW_RACK_RE = re.compile(r"\bp[0-9]+-r[0-9]+\b", re.IGNORECASE)

APP_DIR = Path(__file__).resolve().parent
# Workflow definitions/runs live under repo-root var/ (parent of backend/).
REPO_ROOT = APP_DIR.parent
WORKFLOWS_DIR = Path(
    os.environ.get("RACK_WORKFLOWS_DIR", "") or str(REPO_ROOT / "var" / "workflows")
)
RUNS_DIR = WORKFLOWS_DIR / "runs"

_runs_lock = threading.Lock()
_active_runs: dict[str, dict] = {}
# Native scheduler objects cannot be serialized, so keep them beside the
# durable provider/reference recorded on each run. This lets an in-process
# cancel reach the exact PythonCM/Slurm child while state.json remains useful
# for recovery, reporting, and best-effort cancellation after a restart.
_active_scheduler_jobs: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Workflow template CRUD
# ---------------------------------------------------------------------------


def _ensure_dirs():
    WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def list_workflows() -> list[dict]:
    _ensure_dirs()
    results = []
    for f in sorted(WORKFLOWS_DIR.glob("*.yaml")):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            results.append({
                "id": data.get("id", f.stem),
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "version": data.get("version", 1),
                "step_count": len(data.get("steps", [])),
            })
        except (OSError, yaml.YAMLError) as exc:
            _logger.warning("Failed to read workflow %s: %s", f, exc)
    return results


def get_workflow(workflow_id: str) -> dict | None:
    _ensure_dirs()
    path = WORKFLOWS_DIR / f"{workflow_id}.yaml"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        _logger.error("Failed to load workflow %s: %s", workflow_id, exc)
        return None


def save_workflow(data: dict) -> dict:
    _ensure_dirs()
    wf_id = data.get("id")
    if not wf_id or not isinstance(wf_id, str):
        wf_id = str(uuid.uuid4())[:8]
        data["id"] = wf_id

    data.setdefault("name", wf_id)
    data.setdefault("steps", [])
    data.setdefault("mermaid", "")

    # Validate the optional composition blocks (connection/scheduler/evaluation/
    # capability). Absent blocks == today's behavior, so existing workflows pass.
    problems = workflow_models.validate_workflow(data)
    if problems:
        raise ValueError("invalid workflow: " + "; ".join(problems))
    if "target" in data and isinstance(data["target"], dict):
        data["target"].setdefault("mode", "rack")
        data["target"].setdefault("racks", [])
        data["target"].setdefault("nodes", [])

    # Auto-increment version: read existing version from disk and bump
    path = WORKFLOWS_DIR / f"{wf_id}.yaml"
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}
            current_version = existing.get("version", 0)
            data["version"] = current_version + 1
        except (OSError, yaml.YAMLError):
            data.setdefault("version", 1)
    else:
        data.setdefault("version", 1)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return data


def delete_workflow(workflow_id: str) -> bool:
    path = WORKFLOWS_DIR / f"{workflow_id}.yaml"
    if path.is_file():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Workflow execution state machine
# ---------------------------------------------------------------------------

# States: pending -> running -> waiting_manual | waiting_decision -> completed | failed | cancelled

VALID_STATES = {"pending", "running", "waiting_manual", "waiting_decision", "completed", "failed", "cancelled"}
TERMINAL_STATES = {"completed", "failed", "cancelled"}


def _run_dir(run_id: str) -> Path:
    """Per-run directory for state + output logs."""
    return RUNS_DIR / run_id


def _save_run(run: dict) -> None:
    _ensure_dirs()
    run_id = run["run_id"]
    run_path = _run_dir(run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    path = run_path / "state.json"
    # Scheduler completion and an operator cancel can save concurrently. A
    # shared `state.tmp` lets one writer replace the other's temp file; use a
    # unique sibling while retaining atomic os.replace semantics.
    tmp = run_path / f".state.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2, default=str)
    tmp.replace(path)


def _load_run(run_id: str) -> dict | None:
    # Try new per-directory format first
    path = _run_dir(run_id) / "state.json"
    if not path.is_file():
        # Fallback to legacy flat format
        path = RUNS_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_step_output(run_id: str, step_id: str, output: str) -> str:
    """Save step output to a .log file and return the relative path."""
    run_path = _run_dir(run_id)
    run_path.mkdir(parents=True, exist_ok=True)
    log_file = run_path / f"{step_id}.log"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(output)
    return f"{run_id}/{step_id}.log"


def _get_output_snippet(output: str, max_lines: int = 500) -> str:
    """Return the tail of the output so the inline view shows the final result."""
    lines = output.strip().split("\n")
    if len(lines) <= max_lines:
        return output.strip()
    return f"... ({len(lines) - max_lines} lines above) ...\n\n" + "\n".join(lines[-max_lines:])


def get_step_output(run_id: str, step_id: str) -> str | None:
    """Read full step output from the log file."""
    log_file = _run_dir(run_id) / f"{step_id}.log"
    if not log_file.is_file():
        return None
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def start_workflow_run(workflow_id: str, params: dict | None = None) -> dict:
    """Start a new workflow execution instance."""
    wf = get_workflow(workflow_id)
    if not wf:
        raise ValueError(f"Workflow '{workflow_id}' not found")

    steps = wf.get("steps", [])
    if not steps:
        raise ValueError(f"Workflow '{workflow_id}' has no steps")

    run_id = str(uuid.uuid4())
    merged_params = dict(params or {})
    tgt = wf.get("target") or {}
    if isinstance(tgt, dict) and not merged_params.get("rack"):
        racks = tgt.get("racks") or []
        if racks:
            merged_params.setdefault("rack", racks[0] if len(racks) == 1 else ",".join(racks))
        nodes = tgt.get("nodes") or []
        if nodes:
            merged_params.setdefault("target", ",".join(nodes))
    run: dict[str, Any] = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "workflow_name": wf.get("name", workflow_id),
        "state": "pending",
        "current_step_id": steps[0]["id"],
        "params": merged_params,
        "history": [],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    with _runs_lock:
        _active_runs[run_id] = run

    _save_run(run)

    threading.Thread(target=_execute_run, args=(run_id,), daemon=True).start()

    return {"run_id": run_id, "state": "pending", "current_step_id": run["current_step_id"]}


def get_run_state(run_id: str) -> dict | None:
    with _runs_lock:
        run = _active_runs.get(run_id)
    if run:
        return dict(run)
    return _load_run(run_id)


def invalidate_run(run_id: str, *, reason: str, operator: str = "rack-status") -> dict:
    """Hide an invalid QA run while retaining its state and output as evidence."""
    run = _load_run(run_id)
    if not run:
        raise ValueError(f"workflow run '{run_id}' not found")
    if str(run.get("state") or "") not in TERMINAL_STATES:
        raise ValueError(f"workflow run '{run_id}' is not terminal")
    run["invalidated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run["invalidation_reason"] = str(reason).strip()[:2000]
    run["invalidated_by"] = str(operator).strip()[:128] or "rack-status"
    _save_run(run)
    with _runs_lock:
        if run_id in _active_runs:
            _active_runs[run_id] = dict(run)
    return dict(run)


def list_runs() -> list[dict]:
    _ensure_dirs()
    results = []
    with _runs_lock:
        for run in _active_runs.values():
            if run.get("invalidated_at"):
                continue
            results.append({
                "run_id": run["run_id"],
                "workflow_id": run["workflow_id"],
                "workflow_name": run.get("workflow_name", ""),
                "state": run["state"],
                "current_step_id": run.get("current_step_id"),
                "started_at": run.get("started_at"),
                "updated_at": run.get("updated_at"),
            })

    # Scan per-run directories (new format)
    for d in sorted(RUNS_DIR.iterdir(), reverse=True):
        if d.is_dir():
            state_file = d / "state.json"
            if not state_file.is_file():
                continue
            run_id = d.name
            if any(r["run_id"] == run_id for r in results):
                continue
            try:
                with open(state_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data.get("invalidated_at"):
                    continue
                results.append({
                    "run_id": data["run_id"],
                    "workflow_id": data["workflow_id"],
                    "workflow_name": data.get("workflow_name", ""),
                    "state": data["state"],
                    "current_step_id": data.get("current_step_id"),
                    "started_at": data.get("started_at"),
                    "updated_at": data.get("updated_at"),
                })
            except (OSError, json.JSONDecodeError, KeyError):
                continue

    # Also scan legacy flat .json files
    for f in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        run_id = f.stem
        if any(r["run_id"] == run_id for r in results):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("invalidated_at"):
                continue
            results.append({
                "run_id": data["run_id"],
                "workflow_id": data["workflow_id"],
                "workflow_name": data.get("workflow_name", ""),
                "state": data["state"],
                "current_step_id": data.get("current_step_id"),
                "started_at": data.get("started_at"),
                "updated_at": data.get("updated_at"),
            })
        except (OSError, json.JSONDecodeError, KeyError):
            continue

    results.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return results[:50]


def advance_run(run_id: str, action: dict) -> dict:
    """Advance a workflow that's waiting for manual/decision input."""
    with _runs_lock:
        run = _active_runs.get(run_id)

    if not run:
        run = _load_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")
        with _runs_lock:
            _active_runs[run_id] = run

    if run["state"] not in ("waiting_manual", "waiting_decision"):
        raise ValueError(f"Run is in state '{run['state']}', cannot advance")

    wf = get_workflow(run["workflow_id"])
    if not wf:
        raise ValueError("Workflow definition not found")

    steps = {s["id"]: s for s in wf.get("steps", [])}
    current_step = steps.get(run["current_step_id"])
    if not current_step:
        raise ValueError(f"Step '{run['current_step_id']}' not found in workflow")

    run["history"].append({
        "step_id": run["current_step_id"],
        "action": action,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })

    next_step_id = None
    if run["state"] == "waiting_manual":
        next_step_id = current_step.get("on_complete")
    elif run["state"] == "waiting_decision":
        choice = action.get("choice", "")
        options = current_step.get("options", [])
        for opt in options:
            if opt.get("label") == choice or opt.get("id") == choice:
                next_step_id = opt.get("next")
                break

    if not next_step_id or next_step_id == "done":
        run["state"] = "completed"
        run["current_step_id"] = None
        run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_run(run)
        return {"run_id": run_id, "state": "completed"}

    run["current_step_id"] = next_step_id
    run["state"] = "running"
    run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _save_run(run)

    threading.Thread(target=_execute_run, args=(run_id,), daemon=True).start()
    return {"run_id": run_id, "state": run["state"], "current_step_id": next_step_id}


def cancel_run(run_id: str) -> dict:
    with _runs_lock:
        run = _active_runs.get(run_id)

    if not run:
        run = _load_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")

    if run["state"] in TERMINAL_STATES:
        return {"run_id": run_id, "state": run["state"]}

    run["state"] = "cancelled"
    run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    active_meta = dict(run.get("active_scheduler") or {})
    if active_meta:
        active_meta["state"] = "cancel_requested"
        run["active_scheduler"] = active_meta
    with _runs_lock:
        _active_runs[run_id] = run
        active_native = _active_scheduler_jobs.get(run_id)
        cancel_claimed = bool(active_native and not active_native.get("cancel_dispatched"))
        if cancel_claimed and active_native:
            active_native["cancel_dispatched"] = True
    _save_run(run)

    cancel_error = ""
    cancel_dispatched = False
    provider = str(active_meta.get("provider") or "")
    scheduler_ref = str(active_meta.get("scheduler_ref") or "")
    try:
        if active_native and cancel_claimed:
            active_native["scheduler"].cancel(active_native["handle"])
            cancel_dispatched = True
        elif active_native:
            cancel_dispatched = True
        elif provider and scheduler_ref:
            # The web process may have restarted while native work continued.
            # Rebuild the provider from the persisted workflow step and cancel
            # the exact native reference rather than merely changing UI state.
            from schedulers import JobHandle, build_scheduler

            wf = get_workflow(str(run.get("workflow_id") or "")) or {}
            step = next(
                (
                    item for item in wf.get("steps", [])
                    if str(item.get("id") or "") == str(active_meta.get("step_id") or "")
                ),
                {},
            )
            conn, profile = _resolve_step_connection(step)
            sched = build_scheduler(provider, connection=conn, profile=profile)
            sched.cancel(JobHandle(job_id=scheduler_ref, kind=provider))
            cancel_dispatched = True
    except Exception as exc:
        if active_native:
            with _runs_lock:
                active_native["cancel_dispatched"] = False
        cancel_error = str(exc)
        _logger.exception("Workflow %s child cancellation failed", run_id)

    run["cancel"] = {
        "provider": provider,
        "scheduler_ref": scheduler_ref,
        "dispatched": cancel_dispatched,
        "error": cancel_error,
        "requested_at": run["updated_at"],
    }
    _save_run(run)
    return {
        "run_id": run_id,
        "state": "cancelled",
        "provider": provider,
        "scheduler_ref": scheduler_ref,
        "cancel_dispatched": cancel_dispatched,
        **({"cancel_error": cancel_error} if cancel_error else {}),
    }


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------


def _find_next_step_in_sequence(steps_map: dict, steps_list: list[dict], current_step_id: str) -> str | None:
    """Find the next step in the ordered list after the current one."""
    for i, s in enumerate(steps_list):
        if s.get("id") == current_step_id and i + 1 < len(steps_list):
            return steps_list[i + 1].get("id")
    return None


def _execute_run(run_id: str) -> None:
    """Execute workflow steps sequentially until hitting a manual/decision/terminal step."""
    with _runs_lock:
        run = _active_runs.get(run_id)
    if not run:
        return

    wf = get_workflow(run["workflow_id"])
    if not wf:
        run["state"] = "failed"
        run["error"] = "Workflow definition not found"
        _save_run(run)
        return

    steps = {s["id"]: s for s in wf.get("steps", [])}

    while True:
        if run["state"] == "cancelled":
            _save_run(run)
            return

        step_id = run.get("current_step_id")
        if not step_id or step_id == "done":
            run["state"] = "completed"
            run["current_step_id"] = None
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            return

        step = steps.get(step_id)
        if not step:
            run["state"] = "failed"
            run["error"] = f"Step '{step_id}' not found"
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            return

        run["state"] = "running"
        run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_run(run)

        step_type = step.get("type", "manual")

        if step_type == "manual":
            run["state"] = "waiting_manual"
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            return

        if step_type == "decision":
            run["state"] = "waiting_decision"
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            return

        if step_type == "automated":
            success, output = _execute_automated_step(run, step)
            _eval_meta = run.get("evaluations", {}).get(step_id, {})
            scheduler_meta = dict(run.get("step_schedulers", {}).get(step_id, {}))
            run["history"].append({
                "step_id": step_id,
                "type": "automated",
                "success": success,
                "output_snippet": _get_output_snippet(output),
                "output_file": f"{run['run_id']}/{step_id}.log",
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **({"message": _eval_meta["message"]} if _eval_meta.get("message") else {}),
                **({"prompt": _eval_meta["prompt"]} if _eval_meta.get("prompt") else {}),
                **({"scheduler": scheduler_meta} if scheduler_meta else {}),
                **({"cancelled": True} if run.get("state") == "cancelled" else {}),
            })
            # Cancellation can race with the blocking scheduler wait. Never let
            # the wait result overwrite the durable cancelled terminal state.
            if run.get("state") == "cancelled":
                run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_run(run)
                return
            if not success:
                run["had_failure"] = True
            # ``continue_on_failure`` (workflow-level): keep walking the sequence past a
            # failed automated step so every check runs; the overall run is then marked
            # failed if any step failed. Absent -> unchanged fail-fast behavior.
            cont = bool(wf.get("continue_on_failure"))
            next_id = step.get("on_success") if success else step.get("on_failure")
            if not next_id and (success or cont):
                next_id = _find_next_step_in_sequence(steps, wf.get("steps", []), step_id)
            if not next_id or next_id == "done":
                if run.get("had_failure"):
                    run["state"] = "failed"
                else:
                    run["state"] = "completed" if success else "failed"
                run["current_step_id"] = None
                run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_run(run)
                return
            run["current_step_id"] = next_id
            continue

        if step_type == "delay":
            delay_sec = int(step.get("timeout", step.get("delay", 5)))
            output = f"[Delay] Waiting {delay_sec} seconds...\n"
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            time.sleep(delay_sec)
            if run.get("state") == "cancelled":
                _save_run(run)
                return
            output += f"[Delay] Completed after {delay_sec}s."
            _save_step_output(run["run_id"], step_id, output)
            run["history"].append({
                "step_id": step_id,
                "type": "delay",
                "success": True,
                "output_snippet": output,
                "output_file": f"{run['run_id']}/{step_id}.log",
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            next_id = step.get("on_success", step.get("on_complete"))
            if not next_id:
                next_id = _find_next_step_in_sequence(steps, wf.get("steps", []), step_id)
            if not next_id or next_id == "done":
                run["state"] = "completed"
                run["current_step_id"] = None
                run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_run(run)
                return
            run["current_step_id"] = next_id
            continue

        if step_type == "ai_query":
            ai_output = _execute_ai_step(run, step)
            run["history"].append({
                "step_id": step_id,
                "type": "ai_query",
                "success": True,
                "output_snippet": _get_output_snippet(ai_output),
                "output_file": f"{run['run_id']}/{step_id}.log",
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            next_id = step.get("on_success", step.get("on_complete"))
            if not next_id or next_id == "done":
                run["state"] = "completed"
                run["current_step_id"] = None
                run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_run(run)
                return
            run["current_step_id"] = next_id
            continue

        run["state"] = "failed"
        run["error"] = f"Unknown step type: {step_type}"
        run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_run(run)
        return


def _resolve_step_connection(step: dict) -> tuple[Any, str]:
    """Resolve a per-step (connection, profile) from the step's ``connection`` block.

    Returns ``(None, profile)`` to use the app's global connection via
    ``host_exec.run_on_host(profile=...)`` (default, unchanged behavior), or a
    dedicated ``transport`` connection when the step overrides sudo. Semantics:
      * ``profile: control|script`` -- route (default ``control``).
      * ``sudo: true``  -- run under sudo (implies the ``script`` route).
      * ``sudo: false`` -- never sudo (even on the ``script`` route).
      * ``sudo`` absent -- keep the global behavior (script route sudoes iff
        ``RACK_SCRIPT_SUDO`` is set).
    """
    spec = step.get("connection") or {}
    if not isinstance(spec, dict):
        return None, "control"
    profile = spec.get("profile") or "control"
    sudo = spec.get("sudo")
    if sudo is True:
        profile = "script"  # sudo implies the privileged route
    if sudo is None:
        return None, profile
    try:
        from transport import TransportConfig, build_connection

        cfg = TransportConfig.from_env()
        cfg.script_sudo = bool(sudo)
        return build_connection(cfg), profile
    except Exception:  # never let transport composition break execution
        _logger.debug("_resolve_step_connection: ignoring error", exc_info=True)
        return None, profile


def _run_step_command(step: dict, host_cmd: str, timeout: int):
    """Run *host_cmd* through the step's resolved connection (or the global one)."""
    conn, profile = _resolve_step_connection(step)
    if conn is not None:
        return conn.run(host_cmd, timeout=timeout, profile=profile)
    return host_exec.run_on_host(host_cmd, timeout=timeout, profile=profile)


def _apply_evaluation(run: dict, step: dict, returncode: int, stdout: str, stderr: str,
                      output: str) -> tuple[bool, str]:
    """Judge a step via its ``evaluation`` block (default: exit_code == 0).

    Persists extracted values on the run (``run['outputs']``) so later steps can
    reference them, and appends an evaluation summary to the step output.
    """
    ev = step.get("evaluation")
    if not ev:
        return returncode == 0, output
    import workflow_eval

    ok, outputs, detail = workflow_eval.evaluate(
        ev, returncode=returncode, stdout=stdout, stderr=stderr, outputs=run.get("outputs"),
    )
    run["outputs"] = outputs
    message, prompt = workflow_eval.resolve_messages(ev, ok, outputs)
    if detail:
        output = output + "\n--- evaluation ---\n" + "\n".join(detail) + \
            f"\nresult: {'PASS' if ok else 'FAIL'}\n"
    if message:
        output += f"message: {message}\n"
    if prompt and not ok:
        output += f"hint: {prompt}\n"
    # Persist result metadata so the UI and AI agents can explain/triage the step.
    step_id = step.get("id", "unknown")
    run.setdefault("evaluations", {})[step_id] = {
        "result": "pass" if ok else "fail",
        "message": message,
        "prompt": prompt,
    }
    return ok, output


def _scripts_dir() -> str:
    """Resolve the scripts dir (remote base when in SSH mode, else repo bin/scripts)."""
    base = os.environ.get("RACK_REMOTE_BASE_PATH", "").strip()
    return f"{base}/bin/scripts" if base else str(REPO_ROOT / "bin" / "scripts")


def _execute_scheduler_step(run: dict, step: dict) -> tuple[bool, str]:
    """Execute a step's ``scheduler`` block via the Scheduler abstraction.

    Builds a :class:`schedulers.JobSpec` from the block (substituting ``${param}``
    and ``${gpus_per_node}``), resolves the step's connection, submits via the
    selected backend (default ``slurm``), waits, and returns ``(success, output)``
    where output is the job's captured stdout (for evaluation).
    """
    from schedulers import JobSpec, build_scheduler

    params = {**run.get("outputs", {}), **run.get("params", {})}
    block = step.get("scheduler") or {}
    run_id = run["run_id"]
    step_id = step.get("id", "unknown")

    def sub(v):
        if not isinstance(v, str):
            return v
        for k, val in params.items():
            v = v.replace(f"${{{k}}}", str(val))
        v = v.replace("${gpus_per_node}", os.environ.get("GPUS_PER_NODE", "4"))
        v = v.replace("${log_dir}", os.environ.get("DEFAULT_LOG_DIR", ""))
        return v

    nodelist = (sub(block.get("nodelist") or "") or None)
    nodes = block.get("nodes")
    nodes = int(sub(nodes)) if isinstance(nodes, str) and sub(nodes) else (int(nodes) if nodes else None)
    if not nodes and nodelist:
        nodes = len([n for n in re.split(r"[,\s]+", nodelist) if n]) or None
    ntasks = block.get("ntasks_per_node")
    ntasks = sub(ntasks) if ntasks is not None else os.environ.get("GPUS_PER_NODE")
    ntasks = int(ntasks) if ntasks else None

    script = sub(block.get("script") or "") or None
    if script and not os.path.isabs(script):
        script = f"{_scripts_dir()}/{script}"
    command = sub(block.get("command") or "") or None
    output = sub(block.get("output") or "") or None

    requested_kind = str(block.get("kind") or "").strip().lower()
    effective_kind = requested_kind or (
        "slurm" if script and script.endswith(".sbatch") else scheduler_runtime.scheduler_kind()
    )
    if effective_kind == "pythoncm":
        explicitly_direct = block.get("pythoncm_direct") is True
        if script and not explicitly_direct:
            raise ValueError(
                f"workflow script {Path(script).name!r} requires scheduler.pythoncm_direct=true"
            )
        if command and not explicitly_direct:
            raise ValueError(
                "inline workflow commands require scheduler.pythoncm_direct=true in PythonCM mode"
            )

    # nccl_allreduce.sbatch (and peers) resolve lib/ via NVIS_SCRIPTS_DIR.  Keep
    # these values in the backend-neutral JobSpec env so PythonCM passes them to
    # ProgramRunner; retain sbatch --export for the Slurm provider.
    extra = list(block.get("extra_args") or [])
    job_env = {"NVIS_SCRIPTS_DIR": _scripts_dir()}
    exports = ["ALL", f"NVIS_SCRIPTS_DIR={job_env['NVIS_SCRIPTS_DIR']}"]
    if ntasks:
        job_env["GPUS_PER_NODE"] = str(ntasks)
        exports.append(f"GPUS_PER_NODE={ntasks}")
    if effective_kind != "pythoncm":
        extra.append(f"--export={','.join(exports)}")
    else:
        job_env["NVIS_PYTHONCM_DIRECT"] = "1"

    spec = JobSpec(
        name=block.get("name") or step_id,
        nodes=nodes,
        nodelist=nodelist,
        ntasks_per_node=ntasks,
        script=script,
        command=command,
        output=output,
        reservation=(sub(block.get("reservation") or "") or None),
        env=job_env,
        extra_args=extra,
        launch_mode=str(block.get("launch_mode") or "fanout"),
        execution_user=(
            str(block.get("execution_user") or "").strip()
            or os.environ.get("RACK_PYTHONCM_VALIDATION_USER", "cmods")
        ),
        timeout=int(step.get("timeout", block.get("timeout", 1800))),
    )

    conn, profile = _resolve_step_connection(step)
    try:
        sched = build_scheduler(
            effective_kind,
            connection=conn,
            profile=profile,
        )
        if run.get("state") == "cancelled":
            out = "[Scheduler cancelled] workflow was cancelled before native submission"
            _save_step_output(run_id, step_id, out)
            return False, out
        handle = sched.submit(spec)
        _logger.info("Workflow %s step %s submitted job %s via %s", run_id, step_id, handle.job_id, sched.kind)
        scheduler_meta = {
            "step_id": step_id,
            "provider": sched.kind,
            "scheduler_ref": str(handle.job_id),
            "state": "submitted",
            "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        run.setdefault("step_schedulers", {})[step_id] = scheduler_meta
        run["active_scheduler"] = dict(scheduler_meta)
        with _runs_lock:
            _active_scheduler_jobs[run_id] = {
                "scheduler": sched,
                "handle": handle,
                "step_id": step_id,
            }
        _save_run(run)
        # submit() itself can block. If cancellation won while it was waiting,
        # immediately kill the newly-created child before entering wait().
        if run.get("state") == "cancelled":
            try:
                with _runs_lock:
                    current = _active_scheduler_jobs.get(run_id) or {}
                    already_dispatched = bool(current.get("cancel_dispatched"))
                    if not already_dispatched:
                        current["cancel_dispatched"] = True
                if not already_dispatched:
                    sched.cancel(handle)
                scheduler_meta["state"] = "CANCELLED"
            finally:
                with _runs_lock:
                    _active_scheduler_jobs.pop(run_id, None)
                run["active_scheduler"] = None
                run.setdefault("step_schedulers", {})[step_id] = scheduler_meta
                _save_run(run)
            out = (
                f"[Scheduler cancelled] provider={sched.kind} "
                f"job={handle.job_id} before wait"
            )
            _save_step_output(run_id, step_id, out)
            return False, out
        try:
            result = sched.wait(handle, timeout=spec.timeout)
        finally:
            with _runs_lock:
                current = _active_scheduler_jobs.get(run_id)
                if current and current.get("handle") is handle:
                    _active_scheduler_jobs.pop(run_id, None)
        scheduler_meta["state"] = str(result.state)
        scheduler_meta["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run.setdefault("step_schedulers", {})[step_id] = scheduler_meta
        if str((run.get("active_scheduler") or {}).get("scheduler_ref") or "") == str(handle.job_id):
            run["active_scheduler"] = None
        _save_run(run)
        out = result.stdout or ""
        out += f"\n--- scheduler={sched.kind} job={result.job_id} state={result.state} ---\n"
        success, out = _apply_evaluation(run, step, result.returncode, result.stdout or "",
                                         result.stderr or "", out)
        _save_step_output(run_id, step_id, out)
        return success, out
    except Exception as exc:
        out = f"[Scheduler error] {exc}"
        _save_step_output(run_id, step_id, out)
        _logger.exception("Workflow %s scheduler step %s failed", run_id, step_id)
        return False, out


def _resolve_capability(cap: str, params: dict) -> tuple[str | None, dict, list[str]]:
    """Resolve a catalog Tool ``capability`` id to a command + param defaults.

    Returns ``(command, params_with_defaults, problems)``. The command is the
    tool's ``env_key`` (so it flows through the ``RACK_*_SCRIPT`` resolution) or a
    ``host:bash <scripts_dir>/<script>`` fallback. Required tool params missing from
    the merged params are reported as problems (step fails rather than mis-running).
    """
    try:
        from catalog.store import TABLE_TOOLS, get_repo
        tools = get_repo().list(TABLE_TOOLS)
    except Exception as exc:  # registry unavailable
        _logger.debug("_resolve_capability: ignoring error", exc_info=True)
        return None, params, [f"capability registry unavailable: {exc}"]
    tool = next((t for t in tools if t.get("id") == cap), None)
    if not tool:
        return None, params, [f"capability '{cap}' not found in tool registry"]
    merged = dict(params)
    for p in tool.get("params") or []:
        key = p.get("key")
        if key and key not in merged and p.get("default"):
            merged[key] = p["default"]
    missing = [
        p.get("key") for p in (tool.get("params") or [])
        if p.get("required") and not merged.get(p.get("key"))
    ]
    if missing:
        return None, merged, [f"capability '{cap}' missing required params: {missing}"]
    env_key = (tool.get("env_key") or "").strip()
    script = (tool.get("script") or "").strip()
    if env_key:
        return env_key, merged, []
    if script:
        return f"host:bash {_scripts_dir()}/{script}", merged, []
    return None, merged, [f"capability '{cap}' has no env_key or script"]


def _execute_resolve_step(run: dict, step: dict) -> tuple[bool, str]:
    """Resolve rack(s) → a real Slurm node list once, and store it as a run output.

    ``resolve`` block::

        resolve:
          racks: "${rack}"     # optional; default = the run's rack/target
          into: nodelist       # output name (default "nodelist")

    Downstream steps then reference ``${nodelist}`` (fan-out targets, etc.). Handles
    multi-rack selection ("rackA,rackB") by resolving each rack via live Slurm
    inventory and concatenating (de-duplicated, order-preserving).
    """
    params = {**run.get("outputs", {}), **(step.get("parameters") or {}), **run.get("params", {})}
    run_id = run["run_id"]
    step_id = step.get("id", "unknown")
    block = step.get("resolve") or {}
    into = (block.get("into") or "nodelist").strip() or "nodelist"

    def sub(v):
        if not isinstance(v, str):
            return v
        for k, val in params.items():
            v = v.replace(f"${{{k}}}", str(val))
        return v

    racks_raw = sub(block.get("racks") or "")
    if "$" in racks_raw:
        racks_raw = ""
    racks_raw = racks_raw or params.get("rack") or ""
    target = params.get("target") or ""

    all_nodes: list[str] = []
    try:
        import app as _app
        racks = [r for r in re.split(r"[,\s]+", racks_raw) if r]
        if racks:
            for rk in racks:
                # A single-rack launch may carry the operator's exact selected-node
                # override. Preserve it. A multi-rack launch cannot safely apply one
                # rack's override to every rack, so resolve each full rack instead.
                override = target if len(racks) == 1 and target else None
                nl = _app.validation_target_for_rack(rk, override) or ""
                all_nodes += [n for n in re.split(r"[,\s]+", nl) if n]
        elif target:
            # explicit hostlist/target override (no rack) — resolve/trust as-is
            nl = _app.validation_target_for_rack("", target) or target
            all_nodes += [n for n in re.split(r"[,\s]+", nl) if n]
    except Exception as exc:
        _logger.debug("_execute_resolve_step: ignoring error", exc_info=True)
        out = f"[Resolve error] {exc}"
        _save_step_output(run_id, step_id, out)
        return False, out

    seen: set[str] = set()
    nodes = [n for n in all_nodes if n and "$" not in n and not (n in seen or seen.add(n))]
    nodelist = ",".join(nodes)
    run.setdefault("outputs", {})[into] = nodelist

    n_racks = len([r for r in re.split(r"[,\s]+", racks_raw) if r])
    out = f"Resolved {n_racks or 'target'} rack(s) → {len(nodes)} node(s)\n{into}={nodelist}\n"
    _save_step_output(run_id, step_id, out)
    if not nodes:
        return False, out + "\n[Resolve] no nodes resolved from rack/target\n"
    return True, out


def _execute_slurm_fanout(
    nodes: list[str], cmd: str, *, timeout: int, sudo: bool, job_name: str,
    gpus_per_node: int = 0,
) -> list[Any]:
    """Run one task per exact node in a scheduler-visible Slurm allocation."""
    from platforms import get_platform
    from transport import NodeResult

    # Do not let one already-unschedulable node make srun reject the entire
    # multi-node audit.  Convert only states that Slurm itself declares
    # unavailable into exact-node structured failures; busy/allocated nodes are
    # left for srun and remain transport-inconclusive if no slot opens.
    plat = get_platform()
    unavailable: dict[str, dict[str, Any]] = {}
    try:
        target = ",".join(nodes)
        state_proc = host_exec.run_slurm_on_host(
            plat.sinfo_cmd(
                f"-N -h --nodes={shlex.quote(target)} -o '%N|%T|%E'"
            ),
            timeout=60,
        )
        if state_proc.returncode == 0:
            for line in (state_proc.stdout or "").splitlines():
                cols = line.split("|", 2)
                if len(cols) < 2:
                    continue
                host = cols[0].strip()
                state = cols[1].strip().lower().rstrip("*")
                reason = cols[2].strip() if len(cols) > 2 else ""
                if state.startswith(("down", "drain", "fail", "inval", "maint", "unknown")):
                    unavailable[host.casefold()] = {
                        "status": "fail",
                        "category": "control_plane",
                        "codes": ["slurm_node_unavailable"],
                        "reason": (
                            f"Slurm marks the node {state} before audit"
                            + (f": {reason}" if reason and reason.lower() != "none" else "")
                        )[:1000],
                        "warnings": [],
                        "metrics": {"slurm_state": state, "slurm_reason": reason},
                    }
    except Exception:
        # A failed preflight is not proof that any node is bad. Let srun provide
        # the normal inconclusive transport evidence instead.
        _logger.debug("Slurm audit availability preflight failed", exc_info=True)

    eligible = [node for node in nodes if node.casefold() not in unavailable]
    preflight_results = {
        node.casefold(): NodeResult(
            node=node,
            stdout=_DEEP_AUDIT_PREFIX + json.dumps(unavailable[node.casefold()]) + "\n",
            stderr="",
            returncode=2,
        )
        for node in nodes if node.casefold() in unavailable
    }
    if not eligible:
        return [preflight_results[node.casefold()] for node in nodes]

    execute = cmd
    if sudo:
        execute = "sudo -n -E bash -c " + shlex.quote(cmd)
    runner = f"""
node=$(hostname -s)
set +e
printf '__NVIS_JOB__%s|%s\\n' "$node" "${{SLURM_JOB_ID:-}}"
output=$({execute} 2>&1)
rc=$?
while IFS= read -r line; do
  printf '__NVIS_NODE__%s|%s\\n' "$node" "$line"
done <<< "$output"
printf '__NVIS_RC__%s|%s\\n' "$node" "$rc"
exit 0
""".strip()
    safe_name = re.sub(
        r"[^A-Za-z0-9_.-]", "-", job_name or "nvis-deep-audit"
    )[:96]
    args = [
        "--nodes", str(len(eligible)),
        "--nodelist", ",".join(eligible),
        "--ntasks", str(len(eligible)),
        "--ntasks-per-node", "1",
        "--distribution", "block:block",
        # Both Slurm options take optional arguments.  GNU getopt only binds an
        # optional long-option argument in ``--option=value`` form; with a
        # space, srun treats the standalone value (notably ``0``) as the task
        # executable and fails with ``execve(): 0: No such file or directory``.
        "--kill-on-bad-exit=0",
        "--immediate=60",
    ]
    if gpus_per_node > 0:
        # Slurm's device cgroup otherwise hides every GPU from the audit and a
        # healthy node is misclassified as gpu_count=0.  Keep this workflow
        # configurable because non-GPU fanout steps use the same engine path.
        args.append(f"--gpus-per-node={gpus_per_node}")
    args.extend([
        "--job-name", safe_name,
        "--label",
        "bash", "-c", runner,
    ])
    srun_args = " ".join(shlex.quote(part) for part in args)
    proc = host_exec.run_slurm_on_host(
        plat.srun_cmd(srun_args),
        timeout=max(timeout + 120, 180),
    )
    by_node: dict[str, dict[str, Any]] = {
        node.casefold(): {
            "node": node,
            "stdout": [],
            "rc": None,
            "scheduler_ref": "",
        }
        for node in eligible
    }
    for raw_line in (proc.stdout or "").splitlines():
        line = re.sub(r"^\s*\d+:\s*", "", raw_line, count=1)
        if line.startswith("__NVIS_NODE__"):
            tagged = line[len("__NVIS_NODE__"):]
            host, separator, payload = tagged.partition("|")
            record = by_node.get(host.strip().casefold())
            if separator and record is not None:
                record["stdout"].append(payload)
        elif line.startswith("__NVIS_RC__"):
            tagged = line[len("__NVIS_RC__"):]
            host, separator, value = tagged.partition("|")
            record = by_node.get(host.strip().casefold())
            if separator and record is not None:
                try:
                    record["rc"] = int(value.strip())
                except ValueError:
                    record["rc"] = 255
        elif line.startswith("__NVIS_JOB__"):
            tagged = line[len("__NVIS_JOB__"):]
            host, separator, value = tagged.partition("|")
            record = by_node.get(host.strip().casefold())
            if separator and record is not None:
                scheduler_ref = value.strip().split(".", 1)[0]
                if scheduler_ref.isdigit():
                    record["scheduler_ref"] = scheduler_ref
    global_error = (proc.stderr or "").strip()
    if proc.returncode and not global_error:
        global_error = (
            f"srun exited {proc.returncode} before all node results were collected"
        )
    results: list[NodeResult] = []
    for node in eligible:
        record = by_node[node.casefold()]
        rc = record["rc"] if isinstance(record.get("rc"), int) else 255
        stdout = "\n".join(record["stdout"])
        scheduler_ref = str(record.get("scheduler_ref") or "")
        if scheduler_ref:
            stdout = f"NVIS_SLURM_JOB_ID={scheduler_ref}\n" + stdout
        if stdout:
            stdout += "\n"
        stderr = global_error if rc == 255 and not stdout else ""
        results.append(
            NodeResult(node=node, stdout=stdout, stderr=stderr, returncode=rc)
        )
    by_result = {row.node.casefold(): row for row in results}
    by_result.update(preflight_results)
    return [by_result[node.casefold()] for node in nodes]


def _execute_fanout_step(run: dict, step: dict) -> tuple[bool, str]:
    """Execute a step's ``fanout`` block: run ``run`` across N nodes over the
    Connection chain, coalesce output, and evaluate.

    ``fanout`` shape (all optional except the target)::

        fanout:
          nodes: "${nodelist}"        # targets for the final (fanned) hop
          hops: [ {kind: ssh, host: "${head}", user: cmods}, ... ]  # shared prefix;
                                        # omitted -> the app's active control connection
          node: { kind: ssh, user: cmods, sudo: true }  # per-node hop template
          max_workers: 32

    The base connection is an N-level chain (``local -> [vpn] -> head -> …``); only
    the final ``node`` hop is expanded across ``nodes``. Gating is via the step's
    ``evaluation`` block on the grouped stdout (e.g. ``success_when: {stdout_empty: true}``).
    """
    import host_exec
    from transport import (
        HopSpec,
        NodeResult,
        build_chain,
        fanout,
        fanout_errors,
        group_outputs,
    )

    params = {**run.get("outputs", {}), **(step.get("parameters") or {}), **run.get("params", {})}
    params.setdefault("scripts_dir", _scripts_dir())
    run_id = run["run_id"]
    step_id = step.get("id", "unknown")
    block = step.get("fanout") or {}

    def sub(v):
        if not isinstance(v, str):
            return v
        for k, val in params.items():
            v = v.replace(f"${{{k}}}", str(val))
        v = v.replace("${gpus_per_node}", os.environ.get("GPUS_PER_NODE", "4"))
        return v

    def sub_hop(d: dict) -> dict:
        return {k: (sub(v) if isinstance(v, str) else v) for k, v in (d or {}).items()}

    # Resolve the node list: explicit fanout.nodes, then ${nodelist}, then the run's
    # rack/target via live Slurm inventory. An unresolved ``${...}`` (e.g. ${nodelist}
    # with no matching run param) is treated as empty so the rack fallback still fires.
    nodes_raw = sub(block.get("nodes") or "")
    if "$" in nodes_raw:
        nodes_raw = ""
    if not nodes_raw:
        nodes_raw = params.get("nodelist") or ""
    if not nodes_raw and (params.get("rack") or params.get("target")):
        try:
            import app as _app
            nodes_raw = _app.validation_target_for_rack(
                params.get("rack") or "", params.get("target") or None,
            ) or ""
        except Exception:
            _logger.debug("_execute_fanout_step: ignoring error", exc_info=True)
            nodes_raw = params.get("target") or ""
    # Drop empties and any residual unresolved ${...} placeholders. PythonCM
    # expands BCM bracket targets and validates every identity against PhysicalNode.
    if scheduler_runtime.is_pythoncm():
        requested = scheduler_runtime.expand_nodelist(nodes_raw)
        known = {name.casefold(): name for name in scheduler_runtime.node_names()}
        missing = [name for name in requested if name.casefold() not in known]
        if missing:
            out = "[Fanout error] node(s) not in PythonCM inventory: " + ", ".join(missing[:20])
            _save_step_output(run_id, step_id, out)
            return False, out
        nodes = [known[name.casefold()] for name in requested]
    else:
        nodes = [n for n in re.split(r"[,\s]+", nodes_raw) if n and "$" not in n]
    if not nodes:
        out = "[Fanout error] no target nodes resolved (set fanout.nodes or a rack/${nodelist})"
        _save_step_output(run_id, step_id, out)
        return False, out

    cmd = sub(str(step.get("run") or step.get("command") or ""))
    if not cmd.strip():
        out = "[Fanout error] step has no `run` command to fan out"
        _save_step_output(run_id, step_id, out)
        return False, out

    # Base connection: declared hop chain, else the app's active control connection.
    try:
        node_spec = HopSpec.from_dict(
            sub_hop(block.get("node") or {"kind": "ssh"})
        )
        timeout = int(step.get("timeout", block.get("timeout", 300)))
        requested_scheduler = str(block.get("scheduler") or "").strip().lower()
        use_pythoncm = (
            requested_scheduler == "pythoncm"
            or (not requested_scheduler and scheduler_runtime.is_pythoncm() and not block.get("hops"))
        )
        if use_pythoncm:
            from schedulers import JobSpec, build_scheduler

            scheduler = build_scheduler(
                "pythoncm", connection=host_exec.connection("control"), profile="control"
            )
            execution_user = (
                node_spec.user
                or os.environ.get("RACK_PYTHONCM_USER")
                or "root"
            )
            result = scheduler.run(
                JobSpec(
                    name=f"nvis-{run.get('workflow_id') or step_id}",
                    nodes=len(nodes),
                    nodelist=",".join(nodes),
                    command=cmd,
                    launch_mode="fanout",
                    execution_user=execution_user,
                    timeout=timeout,
                )
            )

            def sections(text: str) -> dict[str, str]:
                out_by_node: dict[str, list[str]] = {}
                current = ""
                for line in (text or "").splitlines():
                    match = re.fullmatch(r"===== (.+) =====", line.strip())
                    if match:
                        current = match.group(1).strip()
                        out_by_node.setdefault(current.casefold(), [])
                    elif current:
                        out_by_node[current.casefold()].append(line)
                return {key: "\n".join(value) for key, value in out_by_node.items()}

            stdout_by_node = sections(result.stdout)
            stderr_by_node = sections(result.stderr)
            rc_by_node = {
                str(row.get("node") or "").casefold(): row.get("returncode")
                for row in result.node_results
                if isinstance(row, dict)
            }
            results = [
                NodeResult(
                    node=node,
                    stdout=stdout_by_node.get(node.casefold(), ""),
                    stderr=stderr_by_node.get(node.casefold(), ""),
                    returncode=(
                        int(rc_by_node[node.casefold()])
                        if rc_by_node.get(node.casefold()) is not None
                        else result.returncode
                    ),
                )
                for node in nodes
            ]
        elif str(block.get("scheduler") or "").strip().lower() == "slurm":
            results = _execute_slurm_fanout(
                nodes,
                cmd,
                timeout=timeout,
                sudo=node_spec.sudo,
                job_name=f"nvis-{run.get('workflow_id') or step_id}",
                gpus_per_node=int(sub(block.get("gpus_per_node") or 0)),
            )
        else:
            hops = block.get("hops")
            if hops:
                base = build_chain([sub_hop(h) for h in hops])
            else:
                base = host_exec.connection("control")
            default_workers = int(
                os.environ.get("RACK_FANOUT_MAX_WORKERS", "8") or "8"
            )
            results = fanout(
                base,
                nodes,
                cmd,
                timeout=timeout,
                max_workers=int(block.get("max_workers", default_workers)),
                user=node_spec.user or None,
                key=node_spec.key,
                extra_opts=node_spec.extra_opts,
                connect_timeout=node_spec.connect_timeout,
                sudo=node_spec.sudo,
                retries=int(block.get("retries", 2)),
            )
    except Exception as exc:
        out = f"[Fanout error] {exc}"
        _save_step_output(run_id, step_id, out)
        _logger.exception("Workflow %s fanout step %s failed", run_id, step_id)
        return False, out

    grouped = group_outputs(results)
    errs = fanout_errors(results)
    output = grouped + (("\n--- errors ---\n" + errs) if errs else "")
    output += f"\n--- fanout: {len(nodes)} node(s), {sum(1 for r in results if r.returncode == 0)} ok ---\n"
    if step.get("issue_policy"):
        audit_summary, audit_output = _apply_node_issue_policy(run, step, results)
        run.setdefault("audits", {})[step_id] = audit_summary
        output += "\n--- node issue policy ---\n" + audit_output
    rc = 0 if (results and all(r.returncode == 0 for r in results)) else 1
    success, output = _apply_evaluation(run, step, rc, grouped, errs, output)
    _save_step_output(run_id, step_id, output)
    return success, output


_DEEP_AUDIT_PREFIX = "NVIS_DEEP_AUDIT_JSON="
_SLURM_JOB_PREFIX = "NVIS_SLURM_JOB_ID="


def _audit_payload(stdout: str) -> dict[str, Any] | None:
    """Return the final structured audit record in a node's stdout."""
    for line in reversed((stdout or "").splitlines()):
        if not line.startswith(_DEEP_AUDIT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(_DEEP_AUDIT_PREFIX):])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _audit_scheduler_ref(stdout: str) -> str:
    """Return the Slurm allocation id emitted by a scheduler fan-out runner."""
    for line in (stdout or "").splitlines():
        if not line.startswith(_SLURM_JOB_PREFIX):
            continue
        value = line[len(_SLURM_JOB_PREFIX):].strip().split(".", 1)[0]
        if value.isdigit():
            return value
    return ""


def _confirmed_scheduler_failure_payload(node: str, stderr: str) -> dict[str, Any] | None:
    """Turn an exact-node Slurm prolog failure into a structured issue.

    Most missing audit payloads are transport uncertainty and must remain
    inconclusive.  A Slurm line that explicitly names this node as the source of
    a prolog failure is different: the scheduler has confirmed that the node
    could not enter the allocation.  Matching the exact hostname is the safety
    boundary that prevents one failed prolog from draining every other requested
    node when ``srun`` repeats global stderr for the whole allocation.
    """
    node = (node or "").strip()
    detail = (stderr or "").strip()
    if not node or not detail:
        return None
    pattern = rf"(?im)\bProlog failure on node\s+{re.escape(node)}(?=\s|$)"
    if not re.search(pattern, detail):
        return None
    compact = re.sub(r"\s+", " ", detail)[:700]
    return {
        "status": "fail",
        "category": "control_plane",
        "codes": ["slurm_prolog_failure"],
        "reason": f"Slurm prolog/boot failure prevented the audit: {compact}",
        "warnings": [],
        "metrics": {},
    }


def _slurm_audit_reason(category: str, run_id: str, reason: str) -> str:
    """Specific, Slurm-safe comment for an automatically drained audit failure."""
    raw = (
        f"Drained for {category} deep audit - for validation {run_id[:8]} "
        f"due to {reason or 'confirmed node failure'}"
    )
    return re.sub(r"[^A-Za-z0-9 _.,:/()\-]", " ", raw)[:256].strip()


def _apply_node_issue_policy(
    run: dict, step: dict, results: list[Any]
) -> tuple[dict[str, Any], str]:
    """Record and drain only node-emitted, structured audit failures."""
    policy = step.get("issue_policy") or {}
    category = str(policy.get("category") or "hardware").strip().lower()
    drain_confirmed = bool(policy.get("drain_confirmed", True))
    rack = str((run.get("params") or {}).get("rack") or "").strip()
    run_id = str(run.get("run_id") or "")
    summary: dict[str, Any] = {
        "kind": category,
        "total": len(results),
        "passed": 0,
        "warning": 0,
        "failed": 0,
        "inconclusive": 0,
        "drained": 0,
        "drain_failed": 0,
        "nodes": [],
    }
    lines: list[str] = []

    for result in results:
        node = str(getattr(result, "node", "") or "").strip()
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        scheduler_ref = _audit_scheduler_ref(stdout)
        payload = _audit_payload(stdout)
        if payload is None:
            payload = _confirmed_scheduler_failure_payload(node, stderr)
        if payload is None:
            summary["inconclusive"] += 1
            row = {
                "node": node,
                "status": "inconclusive",
                "reason": (stderr or "structured audit result missing")[:1000],
                "scheduler_ref": scheduler_ref,
                "drained": False,
            }
            summary["nodes"].append(row)
            lines.append(f"{node}: INCONCLUSIVE - {row['reason']}")
            continue

        status = str(payload.get("status") or "inconclusive").strip().lower()
        if status == "pass":
            summary["passed"] += 1
        elif status == "warning":
            summary["warning"] += 1
        elif status == "fail":
            summary["failed"] += 1
        else:
            status = "inconclusive"
            summary["inconclusive"] += 1

        codes = [
            str(code).strip()
            for code in (payload.get("codes") or [])
            if str(code).strip()
        ]
        reason = str(payload.get("reason") or "").strip()
        issue_category = str(payload.get("category") or category).strip().lower()
        warnings = [
            str(warning).strip()
            for warning in (payload.get("warnings") or [])
            if str(warning).strip()
        ]
        row: dict[str, Any] = {
            "node": node,
            "status": status,
            "category": issue_category,
            "codes": codes,
            "reason": reason,
            "warnings": warnings,
            "metrics": (
                payload.get("metrics")
                if isinstance(payload.get("metrics"), dict)
                else {}
            ),
            "scheduler_ref": scheduler_ref,
            "drained": False,
            "slurm_reason": "",
            "drain_result": "",
        }

        if status == "fail":
            slurm_reason = _slurm_audit_reason(issue_category, run_id, reason)
            row["slurm_reason"] = slurm_reason
            drain_ok = False
            drain_text = "automatic drain disabled"
            if drain_confirmed and node:
                try:
                    import app as _app

                    result_obj = _app._run_scontrol_node_update(
                        node, ["state=drain", f"reason={slurm_reason}"]
                    )
                    drain_ok = bool(result_obj.get("ok"))
                    drain_text = str(
                        result_obj.get("stdout")
                        or result_obj.get("stderr")
                        or f"returncode={result_obj.get('returncode')}"
                    )[:2000]
                except Exception as exc:  # noqa: BLE001 - corrective action is recorded below
                    drain_text = f"{type(exc).__name__}: {exc}"[:2000]
            row["drained"] = drain_ok
            row["drain_result"] = drain_text
            if drain_ok:
                summary["drained"] += 1
            elif drain_confirmed:
                summary["drain_failed"] += 1

            try:
                import node_issues

                node_issues.record_issue(
                    node=node,
                    rack=rack or (node.rsplit("-", 1)[0] if "-" in node else node),
                    category=issue_category,
                    code=",".join(codes) or "validation_failure",
                    reason=reason or "confirmed node validation failure",
                    severity="failure",
                    source=str(run.get("workflow_id") or "deep-audit"),
                    validation_run_id=run_id,
                    scheduler_ref=scheduler_ref,
                    evidence=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    drained=drain_ok,
                    slurm_reason=slurm_reason,
                    drain_result=drain_text,
                    operator=f"workflow:{run_id[:8]}",
                )
            except Exception:
                _logger.exception(
                    "Workflow %s could not persist issue for %s", run_id, node
                )

        summary["nodes"].append(row)
        detail = reason or "; ".join(warnings) or "healthy"
        suffix = " - Slurm drained" if row["drained"] else ""
        if status == "fail" and drain_confirmed and not row["drained"]:
            suffix = f" - DRAIN FAILED: {row['drain_result']}"
        lines.append(f"{node}: {status.upper()} - {detail}{suffix}")

    lines.append(
        "summary: "
        f"pass={summary['passed']} warning={summary['warning']} "
        f"fail={summary['failed']} inconclusive={summary['inconclusive']} "
        f"drained={summary['drained']} drain_failed={summary['drain_failed']}"
    )
    return summary, "\n".join(lines) + "\n"


def _execute_inline_run(run: dict, step: dict, params: dict, script_text: str) -> tuple[bool, str]:
    """Execute a step's embedded ``run:`` batch script / command line.

    The script (single- or multi-line, incl. heredocs) is ``${param}``-substituted,
    base64-encoded, and piped to ``bash`` on the resolved connection so quoting is
    preserved verbatim over SSH. Success = exit 0 unless an ``evaluation`` block
    overrides it. This makes each step a self-contained "lego block" (e.g. one
    sysinfo check per step) with its own pass/fail.
    """
    import base64

    run_id = run["run_id"]
    step_id = step.get("id", "unknown")
    for key, val in params.items():
        script_text = script_text.replace(f"${{{key}}}", str(val))
    timeout = int(step.get("timeout", 300))
    b64 = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
    host_cmd = f"printf %s {b64} | base64 -d | bash"

    rack_norm: str | None = None
    if params.get("rack"):
        try:
            rack_norm = rack_locks.normalize_rack_id(params["rack"])
        except ValueError:
            rack_norm = None
    operator_label = f"workflow:{run_id[:8]}"
    claimed = False
    if rack_norm:
        try:
            rack_locks.upsert_claim(rack_norm, operator_label, note=f"workflow step {step_id}",
                                    ttl_sec=max(60, timeout + 60))
            claimed = True
        except PermissionError:
            pass
    try:
        proc = _run_step_command(step, host_cmd, timeout)
        out, err, returncode = proc.stdout or "", proc.stderr or "", proc.returncode
        output = out + ("\n--- stderr ---\n" + err if err else "")
        output += f"\n--- exit {returncode} ---\n"
        if not output.strip():
            output = f"[Command completed with exit code {returncode}]"
        success, output = _apply_evaluation(run, step, returncode, out, err, output)
        _save_step_output(run_id, step_id, output)
        return success, output
    except Exception as exc:
        output = f"[Error] {exc}"
        _save_step_output(run_id, step_id, output)
        _logger.exception("Workflow %s inline-run step %s failed", run_id, step_id)
        return False, output
    finally:
        if claimed and rack_norm:
            try:
                rack_locks.release_claim(rack_norm, operator_label)
            except (PermissionError, ValueError):
                pass


def _execute_automated_step(run: dict, step: dict) -> tuple[bool, str]:
    """Execute an automated step. Returns (success, output)."""
    if step.get("resolve"):
        return _execute_resolve_step(run, step)
    if step.get("scheduler"):
        return _execute_scheduler_step(run, step)
    if step.get("fanout"):
        return _execute_fanout_step(run, step)
    command = step.get("command", "")
    method = step.get("method", "GET").upper()
    # Merge extracted outputs + step.parameters (run params win) so a command can
    # reference ${extracted_name} from an earlier evaluation or a step-declared param.
    params = {**run.get("outputs", {}), **(step.get("parameters") or {}), **run.get("params", {})}
    step_id = step.get("id", "unknown")
    run_id = run["run_id"]

    # Expose ${scripts_dir} and ${nodelist} (resolved real Slurm hostnames) so
    # host:/capability steps can target a rack's nodes directly (e.g. a decomposed
    # per-check sysinfo step: `host:bash ${scripts_dir}/sysinfo.sh --check X ${nodelist}`).
    params.setdefault("scripts_dir", _scripts_dir())
    if params.get("rack") and not params.get("nodelist"):
        try:
            import app as _app

            _nl = _app.validation_target_for_rack(params["rack"], params.get("target") or None)
            if _nl:
                params["nodelist"] = _nl
                params.setdefault("target", _nl)
                run.setdefault("params", {})["nodelist"] = _nl  # persist for later steps
        except Exception:
            _logger.debug("_execute_automated_step: ignoring error", exc_info=True)

    # capability: resolve a catalog Tool id -> command (+ param defaults / validation).
    cap = step.get("capability")
    if cap:
        cap_cmd, params, cap_problems = _resolve_capability(cap, params)
        if cap_problems:
            out = "[Capability error] " + "; ".join(cap_problems)
            _save_step_output(run_id, step_id, out)
            return False, out
        command = cap_cmd

    # Inline batch script / command line embedded in the step (multi-line supported).
    if step.get("run"):
        return _execute_inline_run(run, step, params, str(step["run"]))

    for key, val in params.items():
        command = command.replace(f"${{{key}}}", str(val))

    output = ""

    if command.startswith("/api/"):
        _logger.info("Workflow step %s: internal API call %s %s", step_id, method, command)
        output = f"[Internal API call]\n{method} {command}\nStatus: OK (simulated)"
        _save_step_output(run_id, step_id, output)
        return True, output

    if command.startswith("host:"):
        host_cmd = command[5:].strip()
        for key, val in params.items():
            host_cmd = host_cmd.replace(f"${{{key}}}", str(val))
        timeout = int(step.get("timeout", 120))

        # Identify rack: explicit step field wins; otherwise first match in the substituted command.
        rack_raw = step.get("rack") or params.get("rack") or ""
        if not rack_raw:
            m = _WORKFLOW_RACK_RE.search(host_cmd)
            if m:
                rack_raw = m.group(0)
        rack_norm: str | None = None
        if isinstance(rack_raw, str) and rack_raw.strip():
            try:
                rack_norm = rack_locks.normalize_rack_id(rack_raw)
            except ValueError:
                rack_norm = None

        operator_label = f"workflow:{run_id[:8]}"
        claimed = False
        if rack_norm:
            try:
                rack_locks.upsert_claim(
                    rack_norm,
                    operator_label,
                    note=f"workflow step {step_id}",
                    ttl_sec=max(60, timeout + 60),
                )
                claimed = True
            except PermissionError as exc:
                _logger.info(
                    "Workflow %s step %s skipped rack-lock acquire (held by other): %s",
                    run_id, step_id, exc,
                )

        try:
            proc = _run_step_command(step, host_cmd, timeout)
            out, err = proc.stdout or "", proc.stderr or ""
            output = out + ("\n--- stderr ---\n" + err if err else "")
            if not output.strip():
                output = f"[Command completed with exit code {proc.returncode}]"
            success, output = _apply_evaluation(run, step, proc.returncode, out, err, output)
            _save_step_output(run_id, step_id, output)
            return success, output
        except Exception as exc:
            output = f"[Error] {exc}"
            _save_step_output(run_id, step_id, output)
            _logger.exception("Workflow automated step failed")
            return False, output
        finally:
            if claimed and rack_norm:
                try:
                    rack_locks.release_claim(rack_norm, operator_label)
                except (PermissionError, ValueError) as exc:
                    _logger.debug(
                        "Workflow %s step %s rack-lock release: %s",
                        run_id, step_id, exc,
                    )

    # Known tool env key (e.g. RACK_SYSINFO_SCRIPT) — resolve path and execute
    if command.startswith("RACK_") and command.endswith("_SCRIPT"):
        import os as _os
        from pathlib import Path as _Path
        script_dir = _os.environ.get("RACK_REMOTE_BASE_PATH", "").strip()
        default_scripts = (_Path(script_dir) / "bin" / "scripts") if script_dir else None
        _SCRIPT_DEFAULTS = {
            "RACK_SYSINFO_SCRIPT": "sysinfo.sh",
            "RACK_VALIDATION_SCRIPT": "run_singlerack.sh",
            "RACK_HW_CHECK_SCRIPT": "hw_check_pdsh.sh",
            "RACK_NVSWITCH_AUDIT_SCRIPT": "nvswitches_rack_audit.sh",
            "RACK_NEMO_XRACK_SCRIPT": "run-nemo-xrack.sh",
            "RACK_LLAMA_SCRIPT": "run-llama.sh",
            "RACK_IMEX_CHECK_SCRIPT": "imex_check.sh",
        }
        # Broaden defaults from the catalog Tool registry (env_key -> script basename),
        # so any registered tool is runnable from a workflow, not just the seven above.
        try:
            from catalog.store import TABLE_TOOLS
            from catalog.store import get_repo as _get_repo

            for _t in _get_repo().list(TABLE_TOOLS):
                ek = (_t.get("env_key") or "").strip()
                sc = (_t.get("script") or "").strip()
                if ek and sc:
                    _SCRIPT_DEFAULTS.setdefault(ek, sc)
        except Exception:
            _logger.debug("_execute_automated_step: ignoring error", exc_info=True)
        resolved = _os.environ.get(command, "").strip()
        if not resolved and default_scripts and command in _SCRIPT_DEFAULTS:
            resolved = str(default_scripts / _SCRIPT_DEFAULTS[command])
        if not resolved:
            output = f"[Tool: {command}]\nResolved path: (not configured: {command})\nStatus: SKIPPED"
            _save_step_output(run_id, step_id, output)
            return False, output
        host_cmd = f"bash {resolved}"
        target = params.get("target", "")
        rack_for_target = params.get("rack") or step.get("rack") or ""
        if rack_for_target:
            # Resolve to REAL node hostnames from live Slurm inventory (same path the
            # sysinfo/hw-check endpoints use), so pdsh/ssh targets resolvable hosts
            # (e.g. bos01-b05-11-s1-dgx-05-c01) instead of the -ct[NN] naming template
            # (bos01-b05-ct01), which is not a real hostname on every site.
            try:
                import app as _app

                # Only honor a *plain* host-list override; a range template (…-ct[NN])
                # is the buggy default and must not defeat inventory resolution.
                override = target if (target and "[" not in target) else None
                target = _app.validation_target_for_rack(rack_for_target, override)
            except Exception as exc:
                _logger.warning(
                    "Workflow %s step %s: node-target resolution failed (%s); using CT template",
                    run_id, step_id, exc,
                    exc_info=True,
                )
                if not target:
                    ct_range = _os.environ.get("RACK_VALIDATION_CT_RANGE", "01-18").strip() or "01-18"
                    target = f"{rack_for_target}-ct[{ct_range}]"
        if target:
            host_cmd += f" {target}"
        timeout = int(step.get("timeout", 300))

        rack_raw = params.get("rack", "")
        rack_norm_t: str | None = None
        if rack_raw:
            try:
                rack_norm_t = rack_locks.normalize_rack_id(rack_raw)
            except ValueError:
                pass
        operator_label_t = f"workflow:{run_id[:8]}"
        claimed_t = False
        if rack_norm_t:
            try:
                rack_locks.upsert_claim(rack_norm_t, operator_label_t, note=f"workflow step {step_id}", ttl_sec=max(60, timeout + 60))
                claimed_t = True
            except PermissionError:
                pass

        try:
            proc = _run_step_command(step, host_cmd, timeout)
            out, err = proc.stdout or "", proc.stderr or ""
            output = out + ("\n--- stderr ---\n" + err if err else "")
            output += f"\n--- exit {proc.returncode} ---\n"
            if not output.strip():
                output = f"[Command completed with exit code {proc.returncode}]"
            success, output = _apply_evaluation(run, step, proc.returncode, out, err, output)
            _save_step_output(run_id, step_id, output)
            return success, output
        except Exception as exc:
            output = f"[Error] {exc}"
            _save_step_output(run_id, step_id, output)
            _logger.exception("Workflow tool step failed")
            return False, output
        finally:
            if claimed_t and rack_norm_t:
                try:
                    rack_locks.release_claim(rack_norm_t, operator_label_t)
                except (PermissionError, ValueError):
                    pass

    output = f"[No-op] command={command!r}\nStep completed without execution."
    _save_step_output(run_id, step_id, output)
    _logger.info("Workflow step %s: no-op (command=%r)", step_id, command)
    return True, output


def _execute_ai_step(run: dict, step: dict) -> str:
    """Execute an AI query step using the chat completion API."""
    step_id = step.get("id", "unknown")
    run_id = run["run_id"]
    prompt = step.get("command", step.get("name", "Analyze the current state"))

    try:
        import ai_chat
        if ai_chat.is_ai_enabled():
            messages = [
                {"role": "system", "content": "You are analyzing workflow execution results. Be concise and actionable."},
                {"role": "user", "content": prompt},
            ]
            response = ai_chat.chat_completion(messages)
        else:
            response = f"[AI Demo Response]\nAnalysis for: {prompt}\n\nAll checks passed. No issues detected."
    except Exception as exc:
        response = f"[AI Error] {exc}"
        _logger.exception("AI step %s failed", step_id)

    _save_step_output(run_id, step_id, response)
    return response


# ---------------------------------------------------------------------------
# Mermaid generation from steps
# ---------------------------------------------------------------------------


def generate_mermaid_from_steps(steps: list[dict]) -> str:
    """Auto-generate a mermaid flowchart from workflow steps."""
    lines = ["graph TD"]
    for step in steps:
        sid = step.get("id", "unknown")
        name = step.get("name", sid)
        stype = step.get("type", "manual")

        if stype == "decision":
            node_def = f'    {sid}{{"{name}"}}'
        elif stype == "automated":
            node_def = f'    {sid}["{name}"]'
        else:
            node_def = f'    {sid}(["{name}"])'
        lines.append(node_def)

    for step in steps:
        sid = step.get("id", "unknown")
        stype = step.get("type", "manual")

        if stype == "decision":
            for opt in step.get("options", []):
                target = opt.get("next", "done")
                label = opt.get("label", "")
                lines.append(f'    {sid} -->|"{label}"| {target}')
        else:
            if step.get("on_success"):
                lines.append(f'    {sid} -->|"success"| {step["on_success"]}')
            if step.get("on_failure"):
                lines.append(f'    {sid} -->|"failure"| {step["on_failure"]}')
            if step.get("on_complete"):
                lines.append(f"    {sid} --> {step['on_complete']}")

    return "\n".join(lines)
