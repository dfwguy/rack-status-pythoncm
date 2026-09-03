# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable run and metric history: queries over the SQLite/Parquet store.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Moved out of ``app.py``. The blueprint uses ``url_prefix=""`` so every route keeps the exact
path it always had -- a migration must not change a URL, and the route snapshot proves it.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# Alias kept because TRACK-A's integration note documents this spelling.

from __future__ import annotations

import logging

import core
import results_history
from audit.actions import action
from flask import Blueprint, jsonify, request

from api._common import history_unavailable_response

_logger = logging.getLogger("rack_web.api.history")

bp = Blueprint("history", __name__, url_prefix="")

_HISTORY_FILTER_KEYS = (
    "workload_kind", "kind", "rack", "job_id", "status", "run_id",
    "since", "until", "search", "source_log_path", "has_metric",
)


def _history_filters_from_args() -> dict[str, object]:
    """Query-string -> ``results_history`` filter mapping (repeatable params become lists)."""
    out: dict[str, object] = {}
    for key in _HISTORY_FILTER_KEYS:
        vals = [v.strip() for v in request.args.getlist(key) if v and v.strip()]
        if not vals:
            continue
        out[key] = vals[0] if len(vals) == 1 else vals
    return out


@bp.route("/api/history/runs")
@action(get="history:list")
def api_history_runs():
    """Paged run history. Filters: workload_kind, rack, job_id, status, since, until, search."""
    if not core.history_enabled():
        return history_unavailable_response()
    try:
        limit = max(1, min(int(request.args.get("limit") or 100), 1000))
        offset = max(0, int(request.args.get("offset") or 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit/offset must be integers"}), 400
    filters = _history_filters_from_args()
    include_metrics = core.truthy_arg(request.args.get("metrics"), default=True)
    order = "asc" if str(request.args.get("order") or "").lower().startswith("a") else "desc"
    try:
        runs = results_history.list_runs(
            filters, limit, offset, order=order, include_metrics=include_metrics
        )
        total = results_history.count_runs(filters)
    except Exception as e:  # noqa: BLE001
        _logger.warning("history runs query failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify(
        {"ok": True, "total": total, "limit": limit, "offset": offset,
         "filters": filters, "runs": runs}
    )


@bp.route("/api/history/runs/<path:run_id>")
@action(get="history:read")
def api_history_run(run_id: str):
    """One run with every metric row (series points included)."""
    if not core.history_enabled():
        return history_unavailable_response()
    try:
        run = results_history.get_run(run_id)
    except Exception as e:  # noqa: BLE001
        _logger.warning("history run lookup failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    if not run:
        return jsonify({"ok": False, "error": "run not found", "run_id": run_id}), 404
    return jsonify({"ok": True, "run": run})


@bp.route("/api/history/metrics")
@action(get="history:read")
def api_history_metrics():
    """``?metric=<name>`` -> the trend series. Without ``metric`` -> the available names."""
    if not core.history_enabled():
        return history_unavailable_response()
    name = (request.args.get("metric") or request.args.get("name") or "").strip()
    filters = _history_filters_from_args()
    try:
        if not name:
            return jsonify(
                {"ok": True, "filters": filters, "metrics": results_history.metric_names(filters)}
            )
        include_series = core.truthy_arg(request.args.get("series"), default=False)
        limit = max(1, min(int(request.args.get("limit") or 5000), 200000))
        points = results_history.metric_series(
            name, filters, limit=limit, include_series_points=include_series
        )
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "limit must be an integer"}), 400
    except Exception as e:  # noqa: BLE001
        _logger.warning("history metric series failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "metric": name.lower(), "filters": filters,
                    "count": len(points), "points": points})


@bp.route("/api/history/metric-series")
@action(get="history:read")
def api_history_metric_series():
    """Alias of ``/api/history/metrics`` that requires ``?metric=``."""
    if not (request.args.get("metric") or request.args.get("name") or "").strip():
        return jsonify({"ok": False, "error": "metric is required"}), 400
    return api_history_metrics()


@bp.route("/api/history/summary")
@action(get="history:read")
def api_history_summary():
    """Fleet baseline (count / median / p05 / p95 / latest per metric) + store health."""
    if not core.history_enabled():
        return history_unavailable_response()
    kind = (request.args.get("workload_kind") or request.args.get("kind") or "").strip() or None
    filters = _history_filters_from_args()
    filters.pop("workload_kind", None)
    filters.pop("kind", None)
    try:
        summary = results_history.summary_stats(kind, filters or None)
        stats = results_history.db_stats()
    except Exception as e:  # noqa: BLE001
        _logger.warning("history summary failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "summary": summary, "db": stats})
