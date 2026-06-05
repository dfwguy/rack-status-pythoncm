"""Workflow Engine — YAML template CRUD, state machine, and execution engine.

Workflows are defined as YAML files in var/workflows/ with mermaid diagrams
and step metadata. Execution state is persisted as JSON in var/workflows/runs/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

import host_exec
import rack_locks

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
    tmp = path.with_suffix(".tmp")
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
            run["history"].append({
                "step_id": step_id,
                "type": "automated",
                "success": success,
                "output_snippet": _get_output_snippet(output),
                "output_file": f"{run['run_id']}/{step_id}.log",
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            next_id = step.get("on_success") if success else step.get("on_failure")
            if not next_id and success:
                next_id = _find_next_step_in_sequence(steps, wf.get("steps", []), step_id)
            if not next_id or next_id == "done":
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


def _execute_automated_step(run: dict, step: dict) -> tuple[bool, str]:
    """Execute an automated step. Returns (success, output)."""
    command = step.get("command", "")
    method = step.get("method", "GET").upper()
    params = run.get("params", {})
    step_id = step.get("id", "unknown")
    run_id = run["run_id"]

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
            proc = host_exec.run_on_host(host_cmd, timeout=timeout)
            output = ""
            if proc.stdout:
                output += proc.stdout
            if proc.stderr:
                output += "\n--- stderr ---\n" + proc.stderr
            if not output.strip():
                output = f"[Command completed with exit code {proc.returncode}]"
            _save_step_output(run_id, step_id, output)
            return proc.returncode == 0, output
        except Exception as exc:
            output = f"[Error] {exc}"
            _save_step_output(run_id, step_id, output)
            _logger.error("Workflow automated step failed: %s", exc)
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
        resolved = _os.environ.get(command, "").strip()
        if not resolved and default_scripts and command in _SCRIPT_DEFAULTS:
            resolved = str(default_scripts / _SCRIPT_DEFAULTS[command])
        if not resolved:
            output = f"[Tool: {command}]\nResolved path: (not configured: {command})\nStatus: SKIPPED"
            _save_step_output(run_id, step_id, output)
            return False, output
        host_cmd = f"bash {resolved}"
        target = params.get("target", "")
        if not target and params.get("rack"):
            ct_range = _os.environ.get("RACK_VALIDATION_CT_RANGE", "01-18").strip() or "01-18"
            target = f"{params['rack']}-ct[{ct_range}]"
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
            proc = host_exec.run_on_host(host_cmd, timeout=timeout)
            output = ""
            if proc.stdout:
                output += proc.stdout
            if proc.stderr:
                output += "\n--- stderr ---\n" + proc.stderr
            output += f"\n--- exit {proc.returncode} ---\n"
            if not output.strip():
                output = f"[Command completed with exit code {proc.returncode}]"
            _save_step_output(run_id, step_id, output)
            return proc.returncode == 0, output
        except Exception as exc:
            output = f"[Error] {exc}"
            _save_step_output(run_id, step_id, output)
            _logger.error("Workflow tool step failed: %s", exc)
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
        _logger.error("AI step %s failed: %s", step_id, exc)

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
