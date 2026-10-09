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

The logic is the top-level ``node_stages`` module; this file is the route. Nodes come from the
same inventory ``/api/status`` serves, narrowed by ``?rack=`` or ``?node=``; with neither, every
node is answered (a fleet sweep reads it that way, once).
"""
from __future__ import annotations

import logging

import native_status
import node_stages
from audit.actions import action
from flask import Blueprint, jsonify, request
from paths import _resolve_log_dir

_logger = logging.getLogger(__name__)

bp = Blueprint("node_stages", __name__, url_prefix="")


@bp.route("/api/node-stages", methods=["GET"])
@action(get="rack:list")
def api_node_stages():
    """Stage 1 and Stage 2 for each node: ``{ok, nodes: [{node, rack, stage1, stage2}], ...}``.

    ``wait=1`` blocks until the bring-up verdicts are loaded (about a minute on a large fleet)
    instead of answering ``stage2: unknown`` with ``stage2_loading`` -- for scripts, not pages.
    """
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    inventory = native_status.node_inventory_for_log_dir(log_dir)
    if inventory is None:
        return jsonify({"ok": False, "error": "node inventory not loaded yet; open the dashboard or retry"}), 503
    rack = (request.args.get("rack") or "").strip()
    node = (request.args.get("node") or "").strip()
    wanted = {node} if node else set()
    pairs = [
        (str(n.get("id") or ""), str(n.get("rack") or ""))
        for n in inventory
        if isinstance(n, dict) and n.get("id")
        and (not rack or str(n.get("rack") or "") == rack)
        and (not wanted or str(n.get("id")) in wanted)
    ]
    wait = (request.args.get("wait") or "").strip().lower() in ("1", "true", "yes")
    try:
        payload = node_stages.node_stages(log_dir, pairs, wait_for_bringup=wait)
    except Exception as e:
        _logger.warning("node stages failed", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, **payload})
