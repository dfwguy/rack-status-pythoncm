# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""HTTP API for the configuration-driven ClusterKit product workflow."""
from __future__ import annotations

from pathlib import Path

import clusterkit_service
import host_exec
import paths
import validation_activities
from clusterkit_attempts import SubmissionBusyError
from clusterkit_context import PinnedLocalConnection, PinnedPlatform, capture
from clusterkit_planner import PlanningError, SlurmInventory
from clusterkit_profile import resolve_profile
from flask import Blueprint, Response, current_app, jsonify, request
from platforms import get_platform
from schedulers import build_scheduler

from api._common import operator_from_request

bp = Blueprint("clusterkit_modular", __name__, url_prefix="/api/clusterkit")
clusterkit_bp = bp  # Import compatibility for the original transferred backend module.


def _secret() -> object:
    return current_app.secret_key


def _runtime():
    roles, roots = host_exec.read_only_roles()
    scripts = roots.get("scripts") or str(paths.REPO_ROOT / "bin" / "scripts")
    context = capture(scripts, roles.select("control"), get_platform())
    for role in ("scripts", "logs"):
        capture(scripts, roles.select(role), get_platform())
    return context, PinnedLocalConnection(context), PinnedPlatform(context)


def _reader(connection, platform):
    return SlurmInventory(connection, platform)


def _scheduler(connection, platform):
    return build_scheduler("slurm", connection=connection, platform=platform, profile="control")


def _ttl():
    from config_env import read_only_values
    try:
        value = read_only_values(("RACK_CLUSTERKIT_PREVIEW_TTL_SEC",))["RACK_CLUSTERKIT_PREVIEW_TTL_SEC"]
        return max(30, min(clusterkit_service.MAX_PREVIEW_TTL_SECONDS, int(value or "300")))
    except (TypeError, ValueError):
        return clusterkit_service.DEFAULT_PREVIEW_TTL_SECONDS


def _error(message: object, status: int):
    return jsonify({"ok": False, "error": str(message)}), status


@bp.get("/readiness")
def clusterkit_readiness():
    result = clusterkit_service.readiness(resolve_profile())
    try:
        context, _, _ = _runtime()
        result.update(execution_context_ready=True, execution_context=context)
    except (OSError, RuntimeError, ValueError) as exc:
        result.update(execution_context_ready=False, execution_context_errors=[str(exc)])
    return jsonify({"ok": True, **result})


@bp.get("/inventory")
def clusterkit_inventory():
    try:
        _, connection, platform = _runtime()
        snapshot = clusterkit_service.inventory(resolve_profile(), _reader(connection, platform))
        return jsonify({"ok": True, "inventory": snapshot})
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 409)


@bp.post("/preview")
def clusterkit_preview():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("A JSON ClusterKit run request is required", 400)
    try:
        context, connection, platform = _runtime()
        result = clusterkit_service.preview(
            body,
            resolve_profile(),
            _reader(connection, platform),
            scripts_dir=context["scripts_directory"],
            context=context,
            secret=_secret(),
            ttl_seconds=_ttl(),
        )
        return jsonify({"ok": True, **result})
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 409)


@bp.post("/runs")
def clusterkit_submit():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _error("Explicit confirm=true and a preview confirmation token are required", 400)
    if set(body) - {"confirm", "confirmation_token"}:
        return _error("Unknown confirmation fields", 400)
    try:
        context, connection, platform = _runtime()
        result = clusterkit_service.submit_confirmed(
            body.get("confirmation_token"),
            resolve_profile(),
            _reader(connection, platform),
            _scheduler(connection, platform),
            validation_activities,
            scripts_dir=context["scripts_directory"],
            context=context,
            secret=_secret(),
            operator=operator_from_request(),
        )
        return jsonify(result), 202
    except (clusterkit_service.DuplicateSubmissionError, SubmissionBusyError) as exc:
        return _error(exc, 409)
    except clusterkit_service.ConfirmationError as exc:
        return _error(exc, 409)
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 500)


def _activity_or_404(activity_id: str, context):
    activity = validation_activities.get_activity(activity_id)
    if activity is None:
        try:
            activity = clusterkit_service.recover_activity(activity_id, resolve_profile(), validation_activities, context=context)
        except KeyError:
            return None
    if not isinstance(activity, dict) or activity.get("tool") != "clusterkit" \
            or activity.get("source") != "clusterkit-api":
        return None
    clusterkit_service.require_activity_context(activity, context)
    return activity


@bp.get("/runs/<activity_id>")
def clusterkit_run(activity_id: str):
    try:
        context, connection, platform = _runtime()
        if _activity_or_404(activity_id, context) is None:
            return _error("Unknown modular ClusterKit activity", 404)
        result = clusterkit_service.reconcile(activity_id, validation_activities,
            _scheduler(connection, platform), context=context)
        return jsonify({"ok": True, **result})
    except KeyError as exc:
        return _error(exc, 404)
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(exc, 409)


@bp.get("/runs/<activity_id>/artifacts")
def clusterkit_artifacts(activity_id: str):
    try:
        context, _, _ = _runtime()
        activity = _activity_or_404(activity_id, context)
        if activity is None:
            return _error("Unknown modular ClusterKit activity", 404)
        return jsonify({"ok": True, "artifacts": clusterkit_service.artifacts(activity)})
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(exc, 409)


@bp.get("/runs/<activity_id>/artifacts/<path:name>")
def clusterkit_artifact(activity_id: str, name: str):
    try:
        context, _, _ = _runtime()
        activity = _activity_or_404(activity_id, context)
        if activity is None:
            return _error("Unknown modular ClusterKit activity", 404)
        data, content_type = clusterkit_service.artifact(activity, name)
        return Response(data, content_type=content_type,
                        headers={"Content-Disposition": f'attachment; filename="{Path(name).name}"'})
    except FileNotFoundError:
        return _error("Artifact not found", 404)
    except (OSError, RuntimeError) as exc:
        return _error(exc, 409)
    except ValueError as exc:
        return _error(exc, 400)
