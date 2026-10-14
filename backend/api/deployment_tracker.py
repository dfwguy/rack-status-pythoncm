# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The deployment tracker: ``GET /api/deployment-tracker`` and ``GET /api/deployment-tracker/site.html``.

The dataset is ``deployment_tracker.build_dataset``; this file only gathers its inputs -- the
same canonical snapshot ``/api/status`` serves, with the same hierarchy and SU grouping merged
in, and the node stages ``/api/node-stages`` computes from it -- and hands the result to one of
the two renderers: JSON for the SPA, the self-contained static page for publishing.
"""
from __future__ import annotations

import logging

import deployment_tracker
import node_stages
from audit.actions import action
from flask import Blueprint, Response, jsonify, request
from paths import _resolve_log_dir
from rack_status_engine import get_cluster_snapshot

from api.dashboard import (
    _ensure_rack_status_background_thread,
    _merge_naming_into_status_payload,
    _merge_project_name_into_status_payload,
    _start_cmsh_rack_list_warm_thread,
)

_logger = logging.getLogger(__name__)

bp = Blueprint("deployment_tracker", __name__)


class _NotReady(Exception):
    """The node inventory has not loaded yet; the caller should retry."""


def _cluster_name() -> str:
    try:
        from catalog import context as catalog_context

        _project, cluster, _platform = catalog_context.resolve()
        return cluster.name if cluster is not None else ""
    except Exception:  # a label; the dataset is complete without it
        _logger.warning("deployment tracker: cluster name unavailable", exc_info=True)
        return ""


def _dataset() -> dict:
    # The same warm-up /api/status does: after a restart nothing else may have asked yet.
    _start_cmsh_rack_list_warm_thread()
    _ensure_rack_status_background_thread()
    log_dir = _resolve_log_dir(request.args.get("log_dir"))
    wait = (request.args.get("wait") or "").strip().lower() in ("1", "true", "yes")
    payload = dict(get_cluster_snapshot(log_dir, wait=wait).payload or {})
    inventory = payload.get("nodes")
    if not isinstance(inventory, list):
        raise _NotReady("node inventory is still loading; retry shortly")
    _merge_project_name_into_status_payload(payload)
    _merge_naming_into_status_payload(payload)
    pairs = [
        (str(n.get("id") or ""), str(n.get("rack") or ""))
        for n in inventory
        if isinstance(n, dict) and n.get("id")
    ]
    node_logs = payload.get("node_stage_logs")
    stages = node_stages.node_stages(
        pairs, node_logs if isinstance(node_logs, dict) else {}, wait_for_bringup=wait
    )
    return deployment_tracker.build_dataset(payload, stages, cluster_name=_cluster_name())


def _not_ready(e: Exception):
    resp = jsonify({"ok": False, "error": str(e)})
    resp.headers["Retry-After"] = "30"
    return resp, 503


@bp.route("", methods=["GET"])
@action(get="report.tracker:read")
def api_deployment_tracker():
    """The tracker dataset. ``wait=1`` waits for a first snapshot and the bring-up verdicts."""
    try:
        return jsonify({"ok": True, **_dataset()})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except _NotReady as e:
        return _not_ready(e)
    except Exception as e:
        _logger.warning("deployment tracker failed", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/site.html", methods=["GET"])
@action(get="report.tracker:read")
def api_deployment_tracker_site():
    """The static tracker page with the current dataset embedded, as one HTML file to publish."""
    try:
        body = deployment_tracker.render_site(_dataset())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except _NotReady as e:
        return _not_ready(e)
    except deployment_tracker.SiteBundleMissing as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        _logger.warning("deployment tracker site failed", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    return Response(
        body,
        mimetype="text/html",
        headers={"Content-Disposition": 'attachment; filename="deployment-tracker.html"'},
    )
