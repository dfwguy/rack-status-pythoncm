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
"""Node issue punchcard rollups and in-SPA issue history.

The endpoints; the store itself is the top-level ``node_issues`` module. ``import node_issues``
below resolves to that one, not to this file -- Python 3 imports are absolute, and ``backend/`` is
on ``sys.path``, so the two coexist as ``node_issues`` and ``api.node_issues``.
"""
from __future__ import annotations

import logging

import core
import node_issues
from audit.actions import action
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("node_issues", __name__, url_prefix="")


@bp.route("/api/node-issues", methods=["GET"])
@action(get="node.issues:read")
def api_node_issues():
    """Node issue punchcard rollups and full in-SPA issue history."""
    rack = (request.args.get("rack") or "").strip()
    node = (request.args.get("node") or "").strip()
    sync_error = ""
    if rack and core.truthy_arg(request.args.get("sync")):
        try:
            node_issues.sync_punchcard(rack)
        except Exception as exc:  # noqa: BLE001 - stale local history remains useful offline
            sync_error = str(exc)
            _logger.warning("punchcard sync failed: %s", exc)
    payload = node_issues.query(rack=rack, node=node)
    return jsonify({
        "ok": True,
        **payload,
        **({"sync_error": sync_error} if sync_error else {}),
    })


@bp.route("/api/node-issues/sync-punchcard", methods=["POST"])
@action("node.issues:sync")
def api_node_issues_sync_punchcard():
    """Force-refresh the server-side Google punchcard baseline for this cluster."""
    body = request.get_json(silent=True) or {}
    rack = str(body.get("rack") or "").strip()
    if not rack:
        return jsonify({"ok": False, "error": "rack is required"}), 400
    try:
        result = node_issues.sync_punchcard(rack, force=True)
    except Exception as exc:  # noqa: BLE001 - external Sheets API boundary
        _logger.warning("forced punchcard sync failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    return jsonify(result)
