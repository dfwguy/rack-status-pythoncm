"""Workflow Engine — YAML template CRUD, state machine, and execution engine.

Workflows are defined as YAML files in var/workflows/ with mermaid diagrams
and step metadata. Execution state is persisted as JSON in var/workflows/runs/.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

import host_exec

_logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
WORKFLOWS_DIR = Path(
    os.environ.get("RACK_WORKFLOWS_DIR", "") or str(APP_DIR / "var" / "workflows")
)
RUNS_DIR = WORKFLOWS_DIR / "runs"

_runs_lock = threading.Lock()
_active_runs: dict[str, dict] = {}


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
    data.setdefault("version", 1)
    data.setdefault("steps", [])
    data.setdefault("mermaid", "")

    path = WORKFLOWS_DIR / f"{wf_id}.yaml"
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


def _save_run(run: dict) -> None:
    _ensure_dirs()
    run_id = run["run_id"]
    path = RUNS_DIR / f"{run_id}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(run, f, indent=2, default=str)
    tmp.replace(path)


def _load_run(run_id: str) -> dict | None:
    path = RUNS_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
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
    run: dict[str, Any] = {
        "run_id": run_id,
        "workflow_id": workflow_id,
        "workflow_name": wf.get("name", workflow_id),
        "state": "pending",
        "current_step_id": steps[0]["id"],
        "params": params or {},
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


def list_runs() -> list[dict]:
    _ensure_dirs()
    results = []
    with _runs_lock:
        for run in _active_runs.values():
            results.append({
                "run_id": run["run_id"],
                "workflow_id": run["workflow_id"],
                "workflow_name": run.get("workflow_name", ""),
                "state": run["state"],
                "current_step_id": run.get("current_step_id"),
                "started_at": run.get("started_at"),
                "updated_at": run.get("updated_at"),
            })

    for f in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        run_id = f.stem
        if any(r["run_id"] == run_id for r in results):
            continue
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
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
    with _runs_lock:
        _active_runs[run_id] = run
    _save_run(run)
    return {"run_id": run_id, "state": "cancelled"}


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------


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
            success = _execute_automated_step(run, step)
            run["history"].append({
                "step_id": step_id,
                "type": "automated",
                "success": success,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            next_id = step.get("on_success") if success else step.get("on_failure")
            if not next_id or next_id == "done":
                run["state"] = "completed" if success else "failed"
                run["current_step_id"] = None
                run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _save_run(run)
                return
            run["current_step_id"] = next_id
            continue

        if step_type == "ai_query":
            run["state"] = "waiting_manual"
            run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_run(run)
            return

        run["state"] = "failed"
        run["error"] = f"Unknown step type: {step_type}"
        run["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_run(run)
        return


def _execute_automated_step(run: dict, step: dict) -> bool:
    """Execute an automated step. Returns True on success, False on failure."""
    command = step.get("command", "")
    method = step.get("method", "GET").upper()
    params = run.get("params", {})

    for key, val in params.items():
        command = command.replace(f"${{{key}}}", str(val))

    if command.startswith("/api/"):
        _logger.info("Workflow step %s: internal API call %s %s", step.get("id"), method, command)
        return True

    if command.startswith("host:"):
        host_cmd = command[5:].strip()
        for key, val in params.items():
            host_cmd = host_cmd.replace(f"${{{key}}}", str(val))
        try:
            timeout = int(step.get("timeout", 120))
            proc = host_exec.run_on_host(host_cmd, timeout=timeout)
            return proc.returncode == 0
        except Exception as exc:
            _logger.error("Workflow automated step failed: %s", exc)
            return False

    _logger.info("Workflow step %s: no-op (command=%r)", step.get("id"), command)
    return True


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
