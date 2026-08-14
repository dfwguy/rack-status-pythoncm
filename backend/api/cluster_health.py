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
"""Cluster-link reachability for the SPA connection banner.

Thin wrapper over the transport's own reachability cache. The cache lives in ``host_exec`` on
purpose: reachability is a property of the connection, not of this endpoint, and the whole app
reads it.
"""
from __future__ import annotations

import logging

import host_exec
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

# Empty prefix: these routes carry the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("cluster_health", __name__, url_prefix="")


@bp.route("/api/cluster-health", methods=["GET"])
def api_cluster_health():
    """Cheap, cached cluster-link reachability for the SPA connection banner.

    ``?force=1`` re-probes immediately (used by the "Reconnect now" action).
    """
    force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes")
    try:
        info = host_exec.reachability_info(force=force)
        return jsonify({"ok": True, **info})
    except Exception as e:
        _logger.debug("api_cluster_health: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/cluster-health/reconnect", methods=["POST"])
def api_cluster_health_reconnect():
    """Force an immediate reachability re-probe (drops the cached result first)."""
    try:
        host_exec.invalidate_reachability()
        info = host_exec.reachability_info(force=True)
        return jsonify({"ok": True, **info})
    except Exception as e:
        _logger.debug("api_cluster_health_reconnect: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
