# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""HTTP API for the configuration-driven ClusterKit product workflow."""
from __future__ import annotations

import os
from pathlib import Path

import clusterkit_service
import host_exec
import validation_activities
from api._common import operator_from_request
from clusterkit_planner import PlanningError, SlurmInventory
from clusterkit_profile import resolve_profile
from flask import Blueprint, Response, current_app, jsonify, request
from platforms import get_platform
from schedulers import build_scheduler

clusterkit_bp = Blueprint("clusterkit_modular", __name__, url_prefix="/api/clusterkit")
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "bin" / "scripts"


def _secret() -> object:
    return current_app.secret_key


def _connection():
    return host_exec.connection("control")


def _reader(connection=None) -> SlurmInventory:
    return SlurmInventory(connection or _connection(), get_platform())


def _scheduler(connection=None):
    return build_scheduler("slurm", connection=connection or _connection(),
                           platform=get_platform(), profile="control")


def _ttl() -> int:
    try:
        return max(30, min(clusterkit_service.MAX_PREVIEW_TTL_SECONDS,
                           int(os.environ.get("RACK_CLUSTERKIT_PREVIEW_TTL_SEC", "300"))))
    except ValueError:
        return clusterkit_service.DEFAULT_PREVIEW_TTL_SECONDS


def _error(message: object, status: int):
    return jsonify({"ok": False, "error": str(message)}), status


@clusterkit_bp.get("/readiness")
def clusterkit_readiness():
    return jsonify({"ok": True, **clusterkit_service.readiness(resolve_profile())})


@clusterkit_bp.get("/inventory")
def clusterkit_inventory():
    try:
        snapshot = clusterkit_service.inventory(resolve_profile(), _reader())
        return jsonify({"ok": True, "inventory": snapshot})
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 409)


@clusterkit_bp.post("/preview")
def clusterkit_preview():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("A JSON ClusterKit run request is required", 400)
    try:
        result = clusterkit_service.preview(
            body,
            resolve_profile(),
            _reader(),
            scripts_dir=str(SCRIPTS_DIR),
            secret=_secret(),
            ttl_seconds=_ttl(),
        )
        return jsonify({"ok": True, **result})
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 409)


@clusterkit_bp.post("/runs")
def clusterkit_submit():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm") is not True:
        return _error("Explicit confirm=true and a preview confirmation token are required", 400)
    try:
        connection = _connection()
        result = clusterkit_service.submit_confirmed(
            body.get("confirmation_token"),
            resolve_profile(),
            _reader(connection),
            _scheduler(connection),
            validation_activities,
            scripts_dir=str(SCRIPTS_DIR),
            secret=_secret(),
            operator=operator_from_request(),
        )
        return jsonify(result), 202
    except clusterkit_service.DuplicateSubmissionError as exc:
        return _error(exc, 409)
    except clusterkit_service.ConfirmationError as exc:
        return _error(exc, 409)
    except (OSError, RuntimeError, ValueError, PlanningError) as exc:
        return _error(exc, 500)


def _activity_or_404(activity_id: str):
    activity = validation_activities.get_activity(activity_id)
    if not isinstance(activity, dict) or activity.get("tool") != "clusterkit" \
            or activity.get("source") != "clusterkit-api":
        return None
    return activity


@clusterkit_bp.get("/runs/<activity_id>")
def clusterkit_run(activity_id: str):
    if _activity_or_404(activity_id) is None:
        return _error("Unknown modular ClusterKit activity", 404)
    try:
        result = clusterkit_service.reconcile(activity_id, validation_activities, _scheduler())
        return jsonify({"ok": True, **result})
    except KeyError as exc:
        return _error(exc, 404)
    except (OSError, RuntimeError, ValueError) as exc:
        return _error(exc, 409)


@clusterkit_bp.get("/runs/<activity_id>/artifacts")
def clusterkit_artifacts(activity_id: str):
    activity = _activity_or_404(activity_id)
    if activity is None:
        return _error("Unknown modular ClusterKit activity", 404)
    try:
        return jsonify({"ok": True, "artifacts": clusterkit_service.artifacts(activity)})
    except OSError as exc:
        return _error(exc, 409)


@clusterkit_bp.get("/runs/<activity_id>/artifacts/<path:name>")
def clusterkit_artifact(activity_id: str, name: str):
    activity = _activity_or_404(activity_id)
    if activity is None:
        return _error("Unknown modular ClusterKit activity", 404)
    try:
        data, content_type = clusterkit_service.artifact(activity, name)
        return Response(data, content_type=content_type,
                        headers={"Content-Disposition": f'attachment; filename="{Path(name).name}"'})
    except FileNotFoundError:
        return _error("Artifact not found", 404)
    except OSError as exc:
        return _error(exc, 409)
    except ValueError as exc:
        return _error(exc, 400)
