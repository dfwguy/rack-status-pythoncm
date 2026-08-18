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
"""Wire the curated agent tool set to the Flask app's internal functions.

Reaches ``app`` only at call time, through the :class:`_AppViews` facade, so there is no import
cycle: the route module imports :mod:`agent.loop`, and the agent modules reach back into ``app``
once it is fully initialized. The facade also resolves views that have moved into
``backend/api/`` blueprints.

Read tools call builders/helpers directly or via :func:`_invoke_view` (a nested Flask test
request context) so route logic — including ``RACK_*_DISABLED`` gates and body parsing — is
reused verbatim. Action tools always go through :func:`_invoke_view` against the real POST
view so behavior is identical to the UI.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import job_registry
import workflow_engine
from node_targets import _validation_ct_inner

from .tools import AgentTool, register

_logger = logging.getLogger(__name__)

_registered = False
_lock = threading.Lock()


class _AppViews:
    """Attribute access over ``app.py`` that also finds views migrated into ``backend/api/``.

    Routes are being extracted from ``app.py`` into blueprints, where the view is no longer an
    attribute of the ``app`` module — ``api_slurm_squeue`` and ``api_workflow_run`` already moved.
    Resolving a miss through Flask's ``view_functions`` (endpoint names are ``<blueprint>.<func>``)
    keeps the tool wiring pointed at the real route wherever it currently lives, so the next
    extraction cannot silently unregister a tool.
    """

    def __getattr__(self, name: str) -> Any:
        import app as _app_mod

        try:
            return getattr(_app_mod, name)
        except AttributeError:
            pass
        for endpoint, view in _app_mod.app.view_functions.items():
            if endpoint == name or endpoint.rsplit(".", 1)[-1] == name:
                return view
        raise AttributeError(f"app has no attribute or view function named {name!r}")


_app = _AppViews()


def _invoke_view(
    view: Callable,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    query: dict | None = None,
    path_args: tuple = (),
    operator: str = "",
) -> dict[str, Any]:
    """Call a Flask view function under a nested test request context and normalize.

    Reuses the route's own parsing/gating/response. Returns the view's JSON as a dict
    with ``ok`` and ``_status`` filled in.
    """
    import app as _app

    headers = {}
    if operator:
        headers["X-Rack-Operator"] = operator
    kwargs: dict[str, Any] = {"method": method, "headers": headers}
    if json_body is not None:
        kwargs["json"] = json_body
    if query:
        kwargs["query_string"] = query
    with _app.app.test_request_context("/agent-tool", **kwargs):
        resp = view(*path_args)
    status = 200
    if isinstance(resp, tuple):
        resp, status = resp[0], (resp[1] if len(resp) > 1 else 200)
    data: Any = None
    try:
        data = resp.get_json()
    except Exception:  # a non-JSON response is normalized below, not an error
        _logger.debug("_invoke_view: response was not JSON", exc_info=True)
        data = None
    if not isinstance(data, dict):
        text = ""
        try:
            text = resp.get_data(as_text=True)[:2000]
        except Exception:  # streamed/odd responses still get summarized
            _logger.debug("_invoke_view: could not read response body", exc_info=True)
            text = str(resp)[:2000]
        data = {"ok": status < 400, "raw": text}
    data.setdefault("ok", status < 400)
    data["_status"] = status
    return data


def register_all() -> None:
    """Idempotently register the curated tool set. Safe to call on every request."""
    global _registered
    with _lock:
        if _registered:
            return

        # ---------------------------------------------------------------- reads
        register(AgentTool(
            name="get_cluster_overview",
            description=(
                "Deployed cluster facts: compute rack count, tray/node count, estimated GPUs, "
                "validation summary (done/fail/ongoing/pending), cluster/customer/product line, "
                "BCM/Slurm versions and a live squeue snapshot. Use for 'how big is the cluster' "
                "and overall health questions."
            ),
            category="read",
            handler=lambda a: _invoke_view(_app.api_cluster_overview),
        ))

        def _rack_status(_a: dict) -> dict:
            data = _invoke_view(_app.api_status)
            rows = data.get("rows") or []
            keep = ("rack", "nemo", "stage1", "stage2", "last_job", "sinfo", "ct_down_unk")
            return {
                "ok": bool(data.get("ok", True)),
                "summary": data.get("summary"),
                "updated_at": data.get("updated_at"),
                "cluster_reachable": data.get("cluster_reachable"),
                "racks": [{k: r.get(k) for k in keep} for r in rows if isinstance(r, dict)],
            }

        register(AgentTool(
            name="get_rack_status",
            description=(
                "Per-rack validation table: each rack's NEMO/stage1(NCCL)/stage2 state, last Slurm "
                "job id, sinfo node-state counts, and down/unknown trays. Use to see which racks "
                "passed/failed or are running."
            ),
            category="read",
            handler=_rack_status,
        ))

        register(AgentTool(
            name="list_squeue",
            description="Current Slurm queue (squeue): running and pending jobs with nodes and reasons.",
            category="read",
            handler=lambda a: _invoke_view(_app.api_slurm_squeue),
        ))

        def _recent_jobs(a: dict) -> dict:
            q: dict[str, Any] = {}
            if a.get("limit"):
                q["limit"] = int(a["limit"])
            if a.get("state"):
                q["state"] = str(a["state"])
            return _invoke_view(_app.api_slurm_sacct_recent, query=q or None)

        register(AgentTool(
            name="list_recent_jobs",
            description=(
                "Recently finished Slurm jobs from sacct (job id, name, nodes, elapsed, end time, "
                "state, exit code). squeue drops jobs the instant they end; use this to inspect a "
                "job that just completed or failed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "max jobs (1-50)", "default": 10},
                    "state": {"type": "string", "description": "Slurm state filter, e.g. COMPLETED, FAILED, or FINISHED for all terminal states"},
                },
            },
            category="read",
            handler=_recent_jobs,
        ))

        register(AgentTool(
            name="get_job_detail",
            description="scontrol show job <id>: full detail for one Slurm job (state, nodes, reason, StdOut path).",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            category="read",
            handler=lambda a: _invoke_view(_app.api_slurm_scontrol_show_job, query={"job_id": str(a.get("job_id", ""))}),
        ))

        def _job_log(a: dict) -> dict:
            jid = str(a.get("job_id", "")).strip()
            ld = str(a.get("log_dir", "")).strip()
            pq: dict[str, Any] = {"job_id": jid}
            if ld:
                pq["log_dir"] = ld
            p = _invoke_view(_app.api_slurm_job_log_path, query=pq)
            path = p.get("path")
            if not path:
                return {"ok": False, "error": p.get("error") or "no StdOut path for job", "detail": p}
            cq: dict[str, Any] = {"path": path}
            if ld:
                cq["log_dir"] = ld
            c = _invoke_view(_app.api_slurm_job_stdout_content, query=cq)
            content = str(c.get("content") or "")
            return {
                "ok": bool(c.get("ok", True)),
                "path": path,
                "size_bytes": c.get("size_bytes"),
                "too_large": c.get("too_large"),
                "content_tail": content[-6000:],
            }

        register(AgentTool(
            name="get_job_log",
            description="Resolve a Slurm job's stdout path and return the tail of its log. Use to evaluate why a job passed or failed.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}, "log_dir": {"type": "string"}},
                "required": ["job_id"],
            },
            category="read",
            handler=_job_log,
        ))

        def _parser_results(a: dict) -> dict:
            q: dict[str, Any] = {}
            for k in ("rack", "job_id", "log_file", "log_dir"):
                if a.get(k):
                    q[k] = str(a[k])
            return _invoke_view(_app.api_parser_results, query=q or None)

        register(AgentTool(
            name="get_parser_results",
            description=(
                "Parsed NCCL/NeMo metrics for a rack's latest (or given) run: NCCL peak busbw (GB/s), "
                "Nemotron/Llama training throughput windows. Use to evaluate benchmark/training results."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "rack": {"type": "string"},
                    "job_id": {"type": "string"},
                    "log_file": {"type": "string"},
                },
                "required": ["rack"],
            },
            category="read",
            handler=_parser_results,
        ))

        register(AgentTool(
            name="get_rack_locks",
            description="Advisory rack locks: which racks are busy (active jobs or manual operator claims).",
            category="read",
            handler=lambda a: _invoke_view(_app.api_rack_locks_get),
        ))

        register(AgentTool(
            name="list_workflows",
            description="List available workflow templates (id, name, description) that can be run.",
            category="workflow",
            handler=lambda a: {"ok": True, "workflows": workflow_engine.list_workflows()},
        ))

        def _get_workflow_run(a: dict) -> dict:
            run = workflow_engine.get_run_state(str(a.get("run_id", "")))
            if not run:
                return {"ok": False, "error": "run not found"}
            return {"ok": True, "run": run}

        register(AgentTool(
            name="get_workflow_run",
            description=(
                "State of a workflow run: current step, per-step pass/fail evaluations, extracted "
                "metrics, and whether it is waiting for a manual/decision step. Use to monitor and "
                "evaluate a workflow the agent started."
            ),
            parameters={
                "type": "object",
                "properties": {"run_id": {"type": "string"}},
                "required": ["run_id"],
            },
            category="workflow",
            handler=_get_workflow_run,
        ))

        def _action_status(a: dict) -> dict:
            jid = str(a.get("job_id", "")).strip()
            if not jid:
                return {"ok": False, "error": "job_id is required"}
            # Straight from job_registry, not through _app: these are shared objects that module owns,
            # and reaching them via app only worked while app happened to re-export them. It stopped
            # working the moment the launch routes moved and the unused imports went with them.
            registries = [
                (job_registry._reset_jobs_lock, job_registry._reset_jobs),
                (job_registry._validation_jobs_lock, job_registry._validation_jobs),
                (job_registry._stress_jobs_lock, job_registry._stress_jobs),
                (job_registry._xrack_jobs_lock, job_registry._xrack_jobs),
                (job_registry._llama_jobs_lock, job_registry._llama_jobs),
            ]
            for lock, jobs in registries:
                with lock:
                    row = jobs.get(jid)
                    if row:
                        out = {k: v for k, v in row.items() if k not in ("stdout", "stderr")}
                        out["stdout_tail"] = (row.get("stdout") or "")[-4000:]
                        out["stderr_tail"] = (row.get("stderr") or "")[-2000:]
                        return {"ok": True, **out}
            run = workflow_engine.get_run_state(jid)
            if run:
                return {"ok": True, "kind": "workflow", "run": run}
            return {"ok": False, "error": "unknown or expired job_id/run_id"}

        register(AgentTool(
            name="get_action_status",
            description=(
                "Poll the status of an async action or workflow the agent launched, by its job_id/run_id. "
                "Returns phase (starting/running/done/error/timeout), return code, and log tails."
            ),
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            category="read",
            handler=_action_status,
        ))

        # ------------------------------------------------------------- actions
        register(AgentTool(
            name="scancel_job",
            description="Cancel a Slurm job (scancel). Requires approval.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}, "rack": {"type": "string"}},
                "required": ["job_id"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_SLURM_SCANCEL_DISABLED",
            handler=lambda a: _invoke_view(
                _app.api_slurm_scancel,
                method="POST",
                json_body={"job_id": str(a.get("job_id", "")), "rack": str(a.get("rack", ""))},
                operator=a.get("_operator", ""),
            ),
        ))

        register(AgentTool(
            name="drain_rack",
            description="Drain all Slurm nodes in a rack (scontrol update state=drain). Requires approval.",
            parameters={
                "type": "object",
                "properties": {"rack": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["rack"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_SLURM_NODES_STATE_DISABLED",
            handler=lambda a: _invoke_view(
                _app.api_slurm_drain,
                method="POST",
                json_body={"rack": str(a.get("rack", "")), **({"reason": str(a["reason"])} if a.get("reason") else {})},
                operator=a.get("_operator", ""),
            ),
        ))

        register(AgentTool(
            name="undrain_rack",
            description="Return a rack's Slurm nodes to service (scontrol update state=idle). Requires approval.",
            parameters={
                "type": "object",
                "properties": {"rack": {"type": "string"}},
                "required": ["rack"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_SLURM_NODES_STATE_DISABLED",
            handler=lambda a: _invoke_view(
                _app.api_slurm_undrain,
                method="POST",
                json_body={"rack": str(a.get("rack", ""))},
                operator=a.get("_operator", ""),
            ),
        ))

        def _gpu_reset(a: dict) -> dict:
            rk = str(a.get("rack", "")).strip()
            specs = a.get("specs")
            if not specs and rk:
                inner = _validation_ct_inner()
                specs = [f"{rk}-ct[{inner}]"]
            body = {"specs": specs or [], "rack": rk}
            return _invoke_view(_app.api_gpu_reset_async, method="POST", json_body=body, operator=a.get("_operator", ""))

        register(AgentTool(
            name="gpu_reset",
            description=(
                "Reset GPUs on a rack (nvidia-smi -r via the reset script). Returns a job_id to poll "
                "with get_action_status. Requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "rack": {"type": "string", "description": "rack id; expands to <rack>-ct[<inner>]"},
                    "specs": {"type": "array", "items": {"type": "string"}, "description": "explicit tray specs (optional; overrides rack)"},
                },
                "required": ["rack"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_GPU_RESET_DISABLED",
            handler=_gpu_reset,
        ))

        def _run_validation(a: dict) -> dict:
            body: dict[str, Any] = {"rack": str(a.get("rack", ""))}
            for k in ("target", "log_dir"):
                if a.get(k):
                    body[k] = str(a[k])
            return _invoke_view(_app.api_run_validation_async, method="POST", json_body=body, operator=a.get("_operator", ""))

        register(AgentTool(
            name="run_validation",
            description="Launch single-rack validation (async). Returns a job_id to poll with get_action_status. Requires approval.",
            parameters={
                "type": "object",
                "properties": {"rack": {"type": "string"}, "target": {"type": "string"}},
                "required": ["rack"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_VALIDATION_DISABLED",
            handler=_run_validation,
        ))

        def _run_nccl(a: dict) -> dict:
            body: dict[str, Any] = {"rack": str(a.get("rack", ""))}
            for k in ("target", "log_dir"):
                if a.get(k):
                    body[k] = str(a[k])
            return _invoke_view(_app.api_run_nccl_fullrack_async, method="POST", json_body=body, operator=a.get("_operator", ""))

        register(AgentTool(
            name="run_nccl_fullrack",
            description="Launch the Stage-1 full-rack NCCL all_reduce benchmark (async). Returns a job_id. Requires approval.",
            parameters={
                "type": "object",
                "properties": {"rack": {"type": "string"}, "target": {"type": "string"}},
                "required": ["rack"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_NCCL_FULLRACK_DISABLED",
            handler=_run_nccl,
        ))

        def _run_nemo(a: dict) -> dict:
            racks = a.get("racks")
            if isinstance(racks, str):
                racks = [r.strip() for r in racks.split(",") if r.strip()]
            body: dict[str, Any] = {"racks": racks or []}
            if a.get("log_dir"):
                body["log_dir"] = str(a["log_dir"])
            return _invoke_view(_app.api_run_nemo_xrack_async, method="POST", json_body=body, operator=a.get("_operator", ""))

        register(AgentTool(
            name="run_nemo_xrack",
            description="Launch multi-rack NeMo training across the given racks (async). Returns a job_id. Requires approval.",
            parameters={
                "type": "object",
                "properties": {"racks": {"type": "array", "items": {"type": "string"}}},
                "required": ["racks"],
            },
            category="action",
            side_effect=True,
            disabled_env="RACK_NEMO_XRACK_DISABLED",
            handler=_run_nemo,
        ))

        def _run_workflow(a: dict) -> dict:
            wid = str(a.get("workflow_id", "")).strip()
            params = a.get("params") if isinstance(a.get("params"), dict) else {}
            return _invoke_view(
                _app.api_workflow_run,
                method="POST",
                json_body={"params": params},
                path_args=(wid,),
                operator=a.get("_operator", ""),
            )

        register(AgentTool(
            name="run_workflow",
            description=(
                "Start a workflow template by id with optional params (e.g. rack, target). Returns a "
                "run_id to monitor with get_workflow_run. Requires approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "params": {"type": "object", "description": "e.g. {\"rack\": \"bos01-b06\"}"},
                },
                "required": ["workflow_id"],
            },
            category="workflow",
            side_effect=True,
            handler=_run_workflow,
        ))

        def _advance_workflow(a: dict) -> dict:
            rid = str(a.get("run_id", "")).strip()
            body: dict[str, Any] = {}
            if a.get("choice"):
                body["choice"] = str(a["choice"])
            if a.get("completed") is not None:
                body["completed"] = bool(a["completed"])
            return _invoke_view(
                _app.api_workflow_run_advance,
                method="POST",
                json_body=body,
                path_args=(rid,),
                operator=a.get("_operator", ""),
            )

        register(AgentTool(
            name="advance_workflow_run",
            description="Advance a workflow run that is waiting on a manual step (completed=true) or decision (choice=<label>). Requires approval.",
            parameters={
                "type": "object",
                "properties": {
                    "run_id": {"type": "string"},
                    "completed": {"type": "boolean"},
                    "choice": {"type": "string"},
                },
                "required": ["run_id"],
            },
            category="workflow",
            side_effect=True,
            handler=_advance_workflow,
        ))

        _registered = True
