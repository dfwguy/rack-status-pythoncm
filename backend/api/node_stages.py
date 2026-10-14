# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Per-node Stage 1 / Stage 2: ``GET /api/node-stages``.

The logic is the top-level ``node_stages`` module; this file is the route. It reads the same
canonical cluster snapshot ``/api/status`` serves -- its ``nodes`` inventory and its
``node_stage_logs`` -- narrowed by ``?rack=`` or ``?node=``; with neither, every node is answered.
"""
from __future__ import annotations

import logging

import node_stages
from audit.actions import action
from flask import Blueprint, jsonify, request
from paths import _resolve_log_dir
from rack_status_engine import get_cluster_snapshot

_logger = logging.getLogger(__name__)

bp = Blueprint("node_stages", __name__, url_prefix="")


@bp.route("/api/node-stages", methods=["GET"])
@action(get="rack:list")
def api_node_stages():
    """Stage 1 and Stage 2 for each node: ``{ok, nodes: [{node, rack, stage1, stage2}], ...}``.

    ``wait=1`` waits for a first snapshot and for the bring-up verdicts (about a minute on a
    large fleet) instead of answering 503 / ``stage2: unknown`` -- for scripts, not pages.
    """
    # The same warm-up /api/status does: after a restart nothing else may have asked yet.
    from api.dashboard import (
        _ensure_rack_status_background_thread,
        _start_cmsh_rack_list_warm_thread,
    )

    _start_cmsh_rack_list_warm_thread()
    _ensure_rack_status_background_thread()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    wait = (request.args.get("wait") or "").strip().lower() in ("1", "true", "yes")
    try:
        payload = get_cluster_snapshot(log_dir, wait=wait).payload or {}
    except Exception as e:
        _logger.warning("node stages: snapshot unavailable", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    inventory = payload.get("nodes")
    if not isinstance(inventory, list):
        resp = jsonify({"ok": False, "error": "node inventory is still loading; retry shortly"})
        resp.headers["Retry-After"] = "30"
        return resp, 503
    rack = (request.args.get("rack") or "").strip()
    node = (request.args.get("node") or "").strip()
    pairs = [
        (str(n.get("id") or ""), str(n.get("rack") or ""))
        for n in inventory
        if isinstance(n, dict) and n.get("id")
        and (not rack or str(n.get("rack") or "") == rack)
        and (not node or str(n.get("id")) == node)
    ]
    node_logs = payload.get("node_stage_logs")
    busy = {
        str(n.get("id")) for n in inventory
        if isinstance(n, dict) and n.get("id") and node_stages.scheduler_busy(n.get("scheduler_state"))
    }
    try:
        out = node_stages.node_stages(
            pairs, node_logs if isinstance(node_logs, dict) else {}, wait_for_bringup=wait,
            busy_hosts=busy,
        )
    except Exception as e:
        _logger.warning("node stages failed", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **out})
