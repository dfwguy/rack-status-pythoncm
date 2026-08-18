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
"""Node bring-up status, read from the bring-up automation tree when it is present.

The ``bringup`` module is an optional integration: on a cluster without that tree these routes
answer ``enabled: false`` (config) or 404 (node/fleet) rather than failing, so the SPA can show the
panel as unavailable instead of erroring.

``/api/bringup/fleet`` accepts POST because the node list travels in a JSON body -- a cluster-sized
list does not fit in a URL, and gunicorn rejects a request line over 4094 bytes with a bare 400.
"""
from __future__ import annotations

import re
import subprocess

from core import truthy_env as _truthy_env
from flask import Blueprint, jsonify, request

from ._common import requested_nodes as _requested_nodes

try:  # optional integration; absent on clusters without the bring-up tree
    import bringup as _bringup
except ImportError:
    _bringup = None  # type: ignore[assignment]

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("bringup", __name__, url_prefix="")


@bp.route("/api/bringup/config")
def api_bringup_config():
    """Discovery: whether Travis's bring-up automation tree is present, and where."""
    if _truthy_env("RACK_STATUS_DEMO"):
        return jsonify({"ok": True, "enabled": True, "present": True, "home": "/home/cmods/bringup"})
    if _bringup is None:
        return jsonify({"ok": True, "enabled": False, "present": False, "home": ""})
    try:
        return jsonify({"ok": True, **_bringup.config()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/bringup/node")
def api_bringup_node():
    """Per-node bring-up job summary (status+reason from status-check, metric from daily-report)."""
    node = (request.args.get("node") or "").strip()
    if not node or not re.match(r"^[A-Za-z0-9._-]+$", node):
        return jsonify({"ok": False, "error": "a valid node name is required"}), 400
    if _truthy_env("RACK_STATUS_DEMO"):
        data = _bringup.demo_node_summary(node) if _bringup else {"node": node, "jobs": []}
        return jsonify({"ok": True, "data": data})
    if _bringup is None or not _bringup.is_enabled():
        return jsonify({"ok": False, "error": "bring-up integration disabled or unavailable"}), 404
    try:
        data = _bringup.node_summary(node)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "bring-up report timed out"}), 504
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "data": data})


@bp.route("/api/bringup/fleet", methods=["GET", "POST"])
def api_bringup_fleet():
    """Bring-up state for every node — what a cluster heatmap and the daily report need.

    One request instead of one per node: the underlying fleet scripts are cheap because they
    share a verdict cache, so this is ~2 round trips for the whole cluster. POST so the node
    list travels as a JSON body; GET still works for a whole-cluster query with no list.
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        # Seed the synthetic fleet from the demo rack inventory so node names line up with
        # what the dashboard is already showing.
        data = _bringup.demo_fleet_summary(_requested_nodes()) if _bringup else {"nodes": []}
        return jsonify({"ok": True, "data": data})
    if _bringup is None or not _bringup.is_enabled():
        return jsonify({"ok": False, "error": "bring-up integration disabled or unavailable"}), 404
    try:
        data = _bringup.fleet_summary()
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "bring-up fleet report timed out"}), 504
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "data": data})
