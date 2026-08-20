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
"""Validation activities: the durable answer to "what ran on this rack, and what came of it".

The read/admin surface over ``validation_activities`` -- the registry that outlives the process that
launched a run, which is the only reason a handover question can be answered at all (citizen 8).

Multirack lives here rather than in its own module because a multirack job IS an activity:
``multirack_jobs`` is a compat shim over the same registry, so splitting them would put two views of one
table in two places and invite them to disagree.

These routes are deliberately thin. The work is elsewhere and belongs there:

* ``activity_hydration`` reconciles a stored activity against Slurm and its result log, which is why an
  activity launched before a restart still finishes correctly instead of being stuck at "running".
* ``_refresh_validation_activities_lazy`` triggers that reconciliation on READ rather than on a timer.
  The registry is the system of record, not a poller; hydrating lazily means an operator who never opens
  the page costs nothing, and one who does gets current state.
* ``api_validation_activities_adopt`` exists because runs get launched outside the dashboard. Adopting an
  external log is what stops the ledger claiming a rack was idle while somebody was soaking it from a
  terminal.

``api_validation_activity_corrective_action`` records what an operator DID about a failure. That is the
field an acceptance pack needs and no scheduler can supply.
"""
from __future__ import annotations

import logging
import os
import subprocess

import native_status
import validation_activities
from activity_hydration import (
    _hydrate_activity_task_logs,
    _task_needs_slurm_hydration,
)
from audit.actions import action
from config_env import _eget
from flask import Blueprint, Response, jsonify, request
from naming import (
    _multirack_physical_rack_ids,
    _normalize_rack_id,
    multirack_display_rack_from_basename,
)
from paths import _resolve_log_dir
from rack_status_engine import _probe_log_json_via_rack_status
from slurm_query import _resolve_slurm_job_stdout_path
from validation_launch import _multirack_campaign_snapshot

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("activities", __name__, url_prefix="")


def _refresh_validation_activities_lazy(rows: list[dict]) -> None:
    """Best-effort evidence-ladder refresh for tasks that still need hydration."""
    if (_eget("RACK_VALIDATION_ACTIVITIES_REFRESH_DISABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    for rec in rows:
        aid = str(rec.get("activity_id") or rec.get("job_id") or "")
        if not aid:
            continue
        tasks = rec.get("tasks") or []
        if not any(_task_needs_slurm_hydration(t) for t in tasks if isinstance(t, dict)):
            # Still refresh rollup for legacy rows missing work_phase.
            if tasks and not rec.get("work_phase"):
                rec["work_phase"] = validation_activities.rollup_work_phase(
                    [t for t in tasks if isinstance(t, dict)]
                )
            continue
        _hydrate_activity_task_logs(aid, rec)


@bp.route("/api/multirack-status")
def api_multirack_status():
    """List multirack NEMO/NCCL logs: async sinfo/scontrol + log probes (same pattern as single-rack native)."""
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    # Always jsonify failures: an uncaught AttributeError (e.g. PurePosixPath.resolve in
    # ssh/container mode) becomes Flask's HTML 500 page, and the SPA surfaces
    # "Unexpected token '<!DOCTYPE' is not valid JSON" in Results & logs on disk.
    try:
        payload = native_status.build_multirack_status(
            log_dir,
            _probe_log_json_via_rack_status,
            multirack_display_rack_from_basename,
            _multirack_physical_rack_ids,
        )
    except Exception as e:
        _logger.exception("multirack-status failed for log_dir=%s", log_dir)
        return jsonify({"ok": False, "error": f"multirack-status failed: {e}"}), 500
    return jsonify(payload)


@bp.route("/api/multirack-campaign")
def api_multirack_campaign():
    try:
        return jsonify(_multirack_campaign_snapshot())
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        _logger.exception("multirack campaign snapshot failed")
        return jsonify({"ok": False, "active": False, "error": str(exc)}), 503


@bp.route("/api/multirack-jobs", methods=["GET"])
def api_multirack_jobs():
    """List launched multi-rack jobs from the durable state table (newest first).

    Persists across sessions / restarts, so every operator sees the same job list.
    Optional ``?log_dir=`` filters to jobs launched against that log directory.
    """
    log_dir_raw = (request.args.get("log_dir") or "").strip()
    log_dir_filter: str | None = None
    if log_dir_raw:
        try:
            log_dir_filter = str(_resolve_log_dir(log_dir_raw))
        except ValueError:
            log_dir_filter = log_dir_raw
    try:
        limit = int(request.args.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    jobs = validation_activities.list_activities(log_dir=log_dir_filter, scope="multirack", limit=limit)
    _refresh_validation_activities_lazy(jobs)
    return jsonify({"ok": True, "jobs": jobs})


@bp.route("/api/multirack-jobs/<job_id>", methods=["DELETE"])
@action("validation.activity:delete", variant="legacy")
def api_multirack_job_delete(job_id: str):
    """Dismiss (remove) a launched multi-rack job from the durable state table."""
    if validation_activities.delete_activity(job_id):
        return jsonify({"ok": True, "job_id": job_id})
    return jsonify({"ok": False, "error": "unknown job_id"}), 404


def _validation_activities_query_filters() -> tuple[str | None, str | None, str | None, str | None, int]:
    log_dir_raw = (request.args.get("log_dir") or "").strip()
    log_dir_filter: str | None = None
    if log_dir_raw:
        try:
            log_dir_filter = str(_resolve_log_dir(log_dir_raw))
        except ValueError:
            log_dir_filter = log_dir_raw
    rack = (request.args.get("rack") or "").strip() or None
    tool = (request.args.get("tool") or "").strip() or None
    scope = (request.args.get("scope") or "").strip() or None
    try:
        limit = int(request.args.get("limit", "100"))
    except (TypeError, ValueError):
        limit = 100
    return log_dir_filter, rack, tool, scope, limit


@bp.route("/api/validation-activities", methods=["GET"])
def api_validation_activities():
    """List validation activities (newest first) with optional rack/log_dir/tool/scope filters."""
    log_dir_filter, rack, tool, scope, limit = _validation_activities_query_filters()
    rows = validation_activities.list_activities(
        log_dir=log_dir_filter,
        rack=rack,
        tool=tool,
        scope=scope,
        limit=limit,
    )
    _refresh_validation_activities_lazy(rows)
    completion = validation_activities.completion_rollup(
        log_dir=log_dir_filter,
        rack=rack,
        tool=tool,
        scope=scope,
    )
    return jsonify({"ok": True, "activities": rows, "completion_rollup": completion})


@bp.route("/api/validation-activities/ledger", methods=["GET"])
def api_validation_activities_ledger():
    """Human-readable, append-preserving multi-rack run ledger."""
    return Response(
        validation_activities.read_multirack_ledger_markdown(),
        content_type="text/markdown; charset=utf-8",
    )


@bp.route("/api/validation-activities/<activity_id>", methods=["GET"])
def api_validation_activity_detail(activity_id: str):
    aid = (activity_id or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "activity_id is required"}), 400
    rec = validation_activities.get_activity(aid)
    if rec is None:
        return jsonify({"ok": False, "error": "unknown activity_id"}), 404
    _refresh_validation_activities_lazy([rec])
    rec = validation_activities.get_activity(aid) or rec
    return jsonify({"ok": True, "activity": rec})


@bp.route(
    "/api/validation-activities/<activity_id>/corrective-actions",
    methods=["POST"],
)
@action("validation.activity:annotate")
def api_validation_activity_corrective_action(activity_id: str):
    """Annotate an activity; corrective actions append and are never overwritten."""
    aid = (activity_id or "").strip()
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    issue = body.get("issue_summary")
    outcome = body.get("outcome_summary")
    if not any(str(value or "").strip() for value in (action, issue, outcome)):
        return jsonify(
            {
                "ok": False,
                "error": "action, issue_summary, or outcome_summary is required",
            }
        ), 400
    rec = validation_activities.annotate_activity(
        aid,
        issue_summary=issue,
        outcome_summary=outcome,
        corrective_action=action,
        corrective_action_result=body.get("result"),
        corrective_action_status=body.get("status"),
        operator=_operator_from_request() or body.get("operator") or "operator",
    )
    if rec is None:
        return jsonify({"ok": False, "error": "unknown activity_id"}), 404
    return jsonify({"ok": True, "activity_id": aid, "activity": rec})


@bp.route("/api/validation-activities/<activity_id>", methods=["DELETE"])
@action("validation.activity:delete")
def api_validation_activity_delete(activity_id: str):
    aid = (activity_id or "").strip()
    if validation_activities.delete_activity(aid):
        return jsonify({"ok": True, "activity_id": aid})
    return jsonify({"ok": False, "error": "unknown activity_id"}), 404


@bp.route("/api/validation-activities/adopt", methods=["POST"])
@action("validation.activity:adopt")
def api_validation_activities_adopt():
    """Register an external validation log under a new durable activity id."""
    body = request.get_json(silent=True) or {}
    path_raw = (body.get("path") or body.get("log_path") or "").strip()
    if not path_raw:
        return jsonify({"ok": False, "error": "path is required"}), 400
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack = (body.get("rack") or "").strip()
    try:
        if rack:
            rack = _normalize_rack_id(rack)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not rack:
        rack = multirack_display_rack_from_basename(os.path.basename(path_raw)) or ""
    try:
        log_path = _resolve_slurm_job_stdout_path(path_raw, str(log_dir))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    aid = validation_activities.adopt_log(
        str(log_path),
        log_dir=str(log_dir),
        rack=rack,
        operator=_operator_from_request() or "external",
    )
    if not aid:
        return jsonify({"ok": False, "error": "could not adopt log"}), 400
    rec = validation_activities.get_activity(aid)
    return jsonify({"ok": True, "activity_id": aid, "activity": rec})
