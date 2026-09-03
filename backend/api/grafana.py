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
"""Grafana surfaces: the deeplinks catalogue and the live 5-minute rack charts.

Two routes and no helpers of their own -- the mechanics live in ``grafana_embed``, which is shared with
parser-results and settings so the four surfaces cannot disagree about which dashboard, which host filter
or which timezone a chart uses.

Deeplinks are advertised rather than assumed: ``_grafana_deeplinks_enabled`` means a deployment with no
Grafana simply reports none, instead of the UI offering links that 404.
"""
from __future__ import annotations

import logging

from audit.actions import action
from flask import Blueprint, jsonify, request
from grafana_embed import (
    _grafana_deeplink_dashboards,
    _grafana_deeplinks_enabled,
    _grafana_live_5m_embed_context,
    _grafana_url_timezone_param,
    _grafana_var_host,
    _grafana_var_rack,
    _rack_web_grafana_port,
    _rack_web_grafana_uri,
    _sanitize_display_timezone_query,
)
from naming import _normalize_rack_id
from paths import _resolve_log_dir

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("grafana", __name__, url_prefix="")


@bp.route("/api/rack-grafana-live-charts", methods=["GET"])
@action(get="observability:read")
def api_rack_grafana_live_charts():
    """Grafana solo context for GPU thermal + power, last 5 minutes, hosts = rack CT expand."""
    try:
        _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log_tz_q = _sanitize_display_timezone_query(request.args.get("display_timezone"))
    g_embed = _grafana_live_5m_embed_context(rk, log_timestamp_tz=log_tz_q)
    pdisp = _grafana_url_timezone_param()
    if isinstance(g_embed, dict) and g_embed.get("display_timezone"):
        pdisp = str(g_embed["display_timezone"])
    return jsonify(
        {
            "ok": True,
            "rack": rk,
            "grafana_uri": _rack_web_grafana_uri(),
            "grafana_port": _rack_web_grafana_port(),
            "grafana_embed": g_embed,
            "parser_display_timezone": pdisp,
        }
    )


@bp.route("/api/grafana/deeplinks")
@action(get="observability:read")
def api_grafana_deeplinks():
    """Config for 'Open in Grafana' deep-links (full dashboards scoped by rack/host, opened in a new tab).

    Unlike the /d-solo/ parser embeds, these need no embedding config — they link to the existing
    BCM/NMC dashboards. Response: ``enabled``, ``base`` (may be empty -> resolve to same host),
    ``port``, ``var_rack`` / ``var_host`` (template var names), ``timezone`` and ``dashboards``.
    """
    return jsonify(
        {
            "ok": True,
            "enabled": _grafana_deeplinks_enabled(),
            "base": _rack_web_grafana_uri(),
            "port": _rack_web_grafana_port(),
            "var_rack": _grafana_var_rack(),
            "var_host": _grafana_var_host(),
            "timezone": _grafana_url_timezone_param(),
            "dashboards": _grafana_deeplink_dashboards(),
        }
    )
