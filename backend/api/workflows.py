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
"""Workflow templates and workflow runs.

A thin HTTP surface over ``workflow_engine``, which owns the state machine, persistence and the
long-job runner. Every route here is parse-validate-delegate; no workflow logic lives in this
module, and none should.
"""
from __future__ import annotations

import logging

import workflow_engine
from audit.actions import action
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
# Two prefixes live here on purpose -- /api/workflows (templates) and /api/workflow-runs
# (instances) are one domain, and splitting them would put a run's launch and its state in
# different modules.
bp = Blueprint("workflows", __name__, url_prefix="")


@bp.route("/api/workflows", methods=["GET"])
@action(get="workflow:list")
def api_workflows_list():
    """List available workflow templates."""
    return jsonify({"ok": True, "workflows": workflow_engine.list_workflows()})


@bp.route("/api/workflows/<workflow_id>", methods=["GET"])
@action(get="workflow:read")
def api_workflow_get(workflow_id: str):
    """Get a specific workflow template."""
    wf = workflow_engine.get_workflow(workflow_id)
    if not wf:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404
    return jsonify({"ok": True, "workflow": wf})


@bp.route("/api/workflows", methods=["POST"])
@action("workflow:save")
def api_workflow_save():
    """Create or update a workflow template."""
    body = request.get_json(silent=True) or {}
    if not body.get("name"):
        return jsonify({"ok": False, "error": "name is required"}), 400
    try:
        wf = workflow_engine.save_workflow(body)
        return jsonify({"ok": True, "workflow": wf})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        _logger.debug("api_workflow_save: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


@bp.route("/api/workflows/<workflow_id>", methods=["DELETE"])
@action("workflow:delete")
def api_workflow_delete(workflow_id: str):
    """Delete a workflow template."""
    deleted = workflow_engine.delete_workflow(workflow_id)
    if not deleted:
        return jsonify({"ok": False, "error": "Workflow not found"}), 404
    return jsonify({"ok": True})


@bp.route("/api/workflows/<workflow_id>/run", methods=["POST"])
@action("workflow:run")
def api_workflow_run(workflow_id: str):
    """Start a workflow execution instance."""
    body = request.get_json(silent=True) or {}
    params = body.get("params")
    try:
        result = workflow_engine.start_workflow_run(workflow_id, params)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.route("/api/workflows/generate-mermaid", methods=["POST"])
@action("workflow:generate")
def api_workflow_generate_mermaid():
    """Generate mermaid diagram from workflow steps."""
    body = request.get_json(silent=True) or {}
    steps = body.get("steps", [])
    if not steps:
        return jsonify({"ok": False, "error": "steps array is required"}), 400
    # Truthiness alone let a non-list through: `{"steps": 1}` passed the check above and reached
    # `for step in steps`, raising TypeError inside the generator and 500ing on a client's bad
    # request. A wrong type is the caller's error, so it gets a 400 like the missing case.
    if not isinstance(steps, list):
        return jsonify({"ok": False, "error": "steps must be an array"}), 400
    mermaid = workflow_engine.generate_mermaid_from_steps(steps)
    return jsonify({"ok": True, "mermaid": mermaid})


@bp.route("/api/workflow-runs", methods=["GET"])
@action(get="workflow:list")
def api_workflow_runs_list():
    """List active and recent workflow runs."""
    return jsonify({"ok": True, "runs": workflow_engine.list_runs()})


@bp.route("/api/workflow-runs/<run_id>", methods=["GET"])
@action(get="workflow:read")
def api_workflow_run_state(run_id: str):
    """Get current state of a workflow run."""
    run = workflow_engine.get_run_state(run_id)
    if not run:
        return jsonify({"ok": False, "error": "Run not found"}), 404
    return jsonify({"ok": True, "run": run})


@bp.route("/api/workflow-runs/<run_id>/advance", methods=["POST"])
@action("workflow:advance")
def api_workflow_run_advance(run_id: str):
    """Advance a workflow (for manual/decision steps)."""
    body = request.get_json(silent=True) or {}
    try:
        result = workflow_engine.advance_run(run_id, body)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.route("/api/workflow-runs/<run_id>/cancel", methods=["POST"])
@action("workflow:cancel")
def api_workflow_run_cancel(run_id: str):
    """Cancel a running workflow."""
    try:
        result = workflow_engine.cancel_run(run_id)
        return jsonify({"ok": True, **result})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@bp.route("/api/workflow-runs/<run_id>/output/<step_id>", methods=["GET"])
@action(get="workflow:read")
def api_workflow_run_output(run_id: str, step_id: str):
    """Get the full output log for a specific step in a workflow run."""
    output = workflow_engine.get_step_output(run_id, step_id)
    if output is None:
        return jsonify({"ok": False, "error": "Output not found"}), 404
    return jsonify({"ok": True, "output": output, "step_id": step_id, "run_id": run_id})
