# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Native metrics for the SPA: PromQL -> VictoriaMetrics -> Chart.js-shaped JSON.

Why this exists
---------------
The observability views originally embedded Grafana in ``<iframe>``s. That costs a second tunnel, a
second login, a Grafana security relaxation (``allow_embedding``), and it still renders as a page
inside a page. Rack Status already ships Chart.js (``react-chartjs-2``, see AnalyticsView), so the
data just needs to arrive as JSON.

VictoriaMetrics speaks the Prometheus query API, so this is a thin, read-only proxy plus a small
catalog of named charts. No new query language, no new datastore.

Endpoints
    GET /api/metrics/catalog                     the named charts the UI can render
    GET /api/metrics/chart/<chart_id>?...        one chart, Chart.js-ready {labels, datasets}
    GET /api/metrics/query?q=<promql>            instant query passthrough
    GET /api/metrics/query_range?q=&minutes=     range query passthrough
    GET /api/metrics/health                      upstream reachable? series count?

Scoping: every chart accepts ``rack``/``node``/``su`` and injects them as PromQL label matchers.
Values are validated against a conservative pattern before interpolation - they land inside a
PromQL string literal, so an unescaped quote would let a caller rewrite the query.

Config
    RACK_METRICS_URL       default http://host.docker.internal:8429  (VictoriaMetrics)
    RACK_METRICS_TIMEOUT   default 12 (seconds)
    RACK_METRICS_DISABLED  1 -> register nothing
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://host.docker.internal:8429"
# Label values are interpolated into a PromQL string literal. Anything outside this set could
# terminate the literal and inject arbitrary query text, so reject rather than escape.
MAX_LINES = 12  # more overlapping lines than this is unreadable and the legend swamps the chart
_SAFE_LABEL_VALUE = re.compile(r"^[A-Za-z0-9._:*|-]{1,128}$")

metrics_query_bp = Blueprint("metrics_query", __name__)


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def base_url() -> str:
    return _env("RACK_METRICS_URL", _DEFAULT_URL).rstrip("/")


def is_disabled() -> bool:
    return _env("RACK_METRICS_DISABLED").lower() in ("1", "true", "yes", "on")


def _pythoncm_primary() -> bool:
    return _env("RACK_SCHEDULER", "pythoncm").lower() == "pythoncm"


def _timeout() -> int:
    try:
        return max(2, int(_env("RACK_METRICS_TIMEOUT", "12")))
    except ValueError:
        return 12


# ---------------------------------------------------------------------------------------------
# chart catalog - each entry is one panel the SPA can draw natively
# ---------------------------------------------------------------------------------------------
# ``expr`` gets ``{sel}`` replaced by the caller's label selector (possibly empty).
CHARTS: dict[str, dict[str, Any]] = {
    # Every chart is a TIME SERIES. A bar of "current value per rack" answers nothing you can act
    # on; a line over time shows drift, ramps and outages. `split_by` names the label whose values
    # become one line each (and the legend entries).
    "gpu_temp_by_rack": {
        "group": "gpu",
        "title": "GPU Core Temp by rack — max per rack over time",
        "unit": "°C",
        "kind": "line",
        "range": True,
        "expr": "max by (rack) (DCGM_FI_DEV_GPU_TEMP{sel})",
        "split_by": "rack",
    },
    "gpu_temp_trend": {
        "group": "gpu",
        "title": "GPU Core Temp — cluster max vs mean",
        "unit": "°C",
        "kind": "line",
        "range": True,
        "series": {
            "cluster max": "max(DCGM_FI_DEV_GPU_TEMP{sel})",
            "cluster mean": "avg(DCGM_FI_DEV_GPU_TEMP{sel})",
        },
    },
    "gpu_temp_hottest_hosts": {
        "group": "gpu",
        "title": "GPU Core Temp — 8 hottest hosts over time",
        "unit": "°C",
        "kind": "line",
        "range": True,
        # topk INSIDE a range query re-picks the winners at every step -> 26 hosts with broken,
        # sparse lines. Instead pick the hottest hosts ONCE (instant), then plot only those.
        "select_expr": "topk(8, max by (Hostname) (DCGM_FI_DEV_GPU_TEMP{sel}))",
        "select_label": "Hostname",
        "expr": "max by (Hostname) (DCGM_FI_DEV_GPU_TEMP{sel})",
        "split_by": "Hostname",
    },
    "gpu_power_by_rack": {
        "group": "gpu",
        "title": "GPU Power Draw by rack — total per rack over time",
        "unit": "W",
        "kind": "line",
        "range": True,
        "expr": "sum by (rack) (DCGM_FI_DEV_POWER_USAGE{sel})",
        "split_by": "rack",
    },
    "gpu_power_trend": {
        "group": "gpu",
        "title": "GPU Power Draw — cluster total",
        "unit": "W",
        "kind": "line",
        "range": True,
        "series": {"cluster total": "sum(DCGM_FI_DEV_POWER_USAGE{sel})"},
    },
    # --- 699 W power-cap detection -------------------------------------------------
    # DCGM field 164 (ENFORCED_POWER_LIMIT) is the ONLY thing that shows a cap: the GB300
    # rating is 1400 W, a half-fed power domain enforces exactly 699 W, and
    # `nvidia-smi --query-gpu=power.limit` still reports 1400 on an affected GPU.
    # `< 1400` means a rack/host only produces a series while it is actually capped, so a
    # line vanishing IS the fix landing — which is what we want to see over time.
    "gpu_power_cap_racks": {
        "group": "gpu",
        "title": "GPU power cap — capped GPUs per rack (enforced < 1400 W)",
        "unit": "GPUs",
        "kind": "line",
        "range": True,
        "expr": "count by (rack) (DCGM_FI_DEV_ENFORCED_POWER_LIMIT{sel} < 1400)",
        "split_by": "rack",
    },
    "gpu_power_cap_nodes": {
        "group": "gpu",
        "title": "GPU power cap — capped GPUs per node (pick a rack in the filter)",
        "unit": "GPUs",
        "kind": "line",
        "range": True,
        # Unfiltered this is ~38 lines, which is legible but busy; scoped to one rack it is
        # the 1-4 trays that actually need attention. Hostname (not node) so it matches the
        # `node` query param, which _selector maps onto the Hostname label.
        "expr": "count by (Hostname) (DCGM_FI_DEV_ENFORCED_POWER_LIMIT{sel} < 1400)",
        "split_by": "Hostname",
    },
    "gpu_util_by_rack": {
        "group": "gpu",
        "title": "GPU Utilisation by rack — mean per rack over time",
        "unit": "%",
        "kind": "line",
        "range": True,
        "expr": "avg by (rack) (DCGM_FI_DEV_GPU_UTIL{sel})",
        "split_by": "rack",
    },
    "gpu_util_trend": {
        "group": "gpu",
        "title": "GPU Utilisation — cluster mean",
        "unit": "%",
        "kind": "line",
        "range": True,
        "series": {"cluster mean": "avg(DCGM_FI_DEV_GPU_UTIL{sel})"},
    },
    "nodes_reporting": {
        "group": "infra",
        "title": "Hosts reporting DCGM",
        "unit": "hosts",
        "kind": "line",
        "range": True,
        "series": {"hosts": "count(count by (Hostname) (DCGM_FI_DEV_GPU_TEMP{sel}))"},
    },
    # ---- infrastructure / platform (verified non-empty 2026-07-28) -------------------------
    "slurm_nodes": {
        "group": "infra",
        "title": "Slurm nodes — total / idle / allocated / down",
        "unit": "nodes",
        "kind": "line",
        "range": True,
        # Retained for explicitly Slurm-backed deployments. Nscale/PythonCM
        # hides it from the UI catalog because `bcm_devices` is the native
        # equivalent and does not depend on a Slurm exporter.
        "provider": "slurm",
        "series": {
            "total": "sum(slurm_nodes_total{sel})",
            "idle": "sum(slurm_nodes_idle{sel})",
            "allocated": "sum(slurm_nodes_allocated{sel})",
            "down": "sum(slurm_nodes_down{sel})",
        },
    },
    "bcm_devices": {
        "group": "infra",
        "title": "BCM inventory by device type",
        "unit": "devices",
        "kind": "line",
        "range": True,
        "expr": "sum by (type) (bcm_devices_by_type)",
        "split_by": "type",
    },
    "exporter_health": {
        "group": "infra",
        "title": "Exporter health — scrape up/down by job",
        "unit": "up",
        "kind": "line",
        "range": True,
        "expr": "max by (job) (up)",
        "split_by": "job",
    },
    "active_alerts": {
        "group": "events",
        "title": "Active alerts by name (vmalert)",
        "unit": "firing",
        "kind": "line",
        "range": True,
        "expr": "sum by (alertname) (ALERTS)",
        "split_by": "alertname",
    },
    # ---- GPU detail ------------------------------------------------------------------------
    "gpu_mem_by_rack": {
        "group": "gpu",
        "title": "GPU framebuffer used by rack",
        "unit": "MiB",
        "kind": "line",
        "range": True,
        "expr": "avg by (rack) (DCGM_FI_DEV_FB_USED{sel})",
        "split_by": "rack",
    },
    "gpu_sm_clock_by_rack": {
        "group": "gpu",
        "title": "GPU SM clock by rack",
        "unit": "MHz",
        "kind": "line",
        "range": True,
        "expr": "avg by (rack) (DCGM_FI_DEV_SM_CLOCK{sel})",
        "split_by": "rack",
    },
    "gpu_hbm_temp_by_rack": {
        "group": "gpu",
        "title": "GPU HBM memory temp by rack — max",
        "unit": "\u00b0C",
        "kind": "line",
        "range": True,
        "expr": "max by (rack) (DCGM_FI_DEV_MEMORY_TEMP{sel})",
        "split_by": "rack",
    },
    "nvlink_bw_by_rack": {
        "group": "gpu",
        "title": "NVLink bandwidth by rack",
        "unit": "KiB/s",
        "kind": "line",
        "range": True,
        "expr": "sum by (rack) (DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL{sel})",
        "split_by": "rack",
    },
}


def _selector(args: Any) -> str:
    """Build a PromQL label selector from rack/node/su query params."""
    parts: list[str] = []
    for param, label in (("rack", "rack"), ("node", "Hostname"), ("su", "su")):
        raw = (args.get(param) or "").strip()
        if not raw:
            continue
        if not _SAFE_LABEL_VALUE.match(raw):
            raise ValueError(f"invalid {param} value")
        op = "=~" if ("*" in raw or "|" in raw) else "="
        val = raw.replace("*", ".*") if op == "=~" else raw
        parts.append(f'{label}{op}"{val}"')
    return "{" + ",".join(parts) + "}" if parts else ""


def _vm(path: str, params: dict[str, str]) -> dict[str, Any]:
    url = f"{base_url()}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_timeout()) as resp:
        return json.loads(resp.read())


def _instant(expr: str) -> list[dict[str, Any]]:
    return (_vm("/api/v1/query", {"query": expr}).get("data") or {}).get("result") or []


def _range(expr: str, minutes: int, step: str) -> list[dict[str, Any]]:
    import time

    now = int(time.time())
    params = {"query": expr, "start": str(now - minutes * 60), "end": str(now), "step": step}
    return (_vm("/api/v1/query_range", params).get("data") or {}).get("result") or []


@metrics_query_bp.route("/api/metrics/catalog")
def api_metrics_catalog():
    """Optional ?group=gpu|infra|events narrows the catalog so each view asks for its own charts."""
    want = (request.args.get("group") or "").strip()
    return jsonify({
        "ok": True,
        "upstream": base_url(),
        "group": want or "all",
        "charts": [
            {"id": cid, "title": c["title"], "unit": c["unit"], "kind": c["kind"],
             "group": c.get("group") or "other", "range": bool(c.get("range"))}
            for cid, c in CHARTS.items()
            if (not want or (c.get("group") or "other") == want)
            and not (_pythoncm_primary() and c.get("provider") == "slurm")
        ],
        "scheduler": "pythoncm" if _pythoncm_primary() else "slurm",
    })


@metrics_query_bp.route("/api/metrics/health")
def api_metrics_health():
    out: dict[str, Any] = {"ok": False, "upstream": base_url(), "error": None}
    if is_disabled():
        out["error"] = "disabled"
        return jsonify(out)
    try:
        res = _instant("count(count by (Hostname) (DCGM_FI_DEV_GPU_TEMP))")
        out["dcgm_hosts"] = int(float(res[0]["value"][1])) if res else 0
        out["ok"] = True
    except Exception as exc:
        _logger.debug("api_metrics_health: ignoring error", exc_info=True)
        out["error"] = str(exc)
    return jsonify(out)


@metrics_query_bp.route("/api/metrics/chart/<chart_id>")
def api_metrics_chart(chart_id: str):
    """One chart, already shaped for Chart.js: {labels: [...], datasets: [{label, data}]}."""
    chart = CHARTS.get(chart_id)
    if not chart:
        return jsonify({"ok": False, "error": f"unknown chart {chart_id!r}"}), 404
    if is_disabled():
        return jsonify({"ok": False, "error": "metrics disabled"}), 503
    try:
        sel = _selector(request.args)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        minutes = max(1, min(1440, int(request.args.get("minutes") or 60)))
    except ValueError:
        minutes = 60
    step = request.args.get("step") or f"{max(15, minutes * 60 // 120)}s"

    try:
        if chart.get("range"):
            labels: list[str] = []
            datasets: list[dict[str, Any]] = []

            # A single expression whose result splits into one line per label value
            # (e.g. one line per rack). Every series must be sampled onto the SAME x-axis, so
            # points are indexed by timestamp and re-emitted against the union of timestamps —
            # a rack that started reporting late gets nulls (gaps), not a shifted line.
            split = chart.get("split_by")
            if split:
                expr_sel = sel
                sel_expr = chart.get("select_expr")
                if sel_expr:
                    # Two-step: resolve the interesting series now, then pin the range query to
                    # them so every line is continuous across the whole window.
                    lbl = chart.get("select_label") or split
                    chosen = [
                        str(r.get("metric", {}).get(lbl) or "")
                        for r in _instant(sel_expr.replace("{sel}", sel))
                    ]
                    chosen = [c for c in chosen if c and _SAFE_LABEL_VALUE.match(c)]
                    if chosen:
                        pin = f'{lbl}=~"{"|".join(chosen)}"'
                        expr_sel = (sel[:-1] + "," + pin + "}") if sel else "{" + pin + "}"
                series = _range(chart["expr"].replace("{sel}", expr_sel), minutes, step)
                by_name: dict[str, dict[str, float | None]] = {}
                stamps: set[str] = set()
                for s in series:
                    name = str(s.get("metric", {}).get(split) or "?")
                    pts = {str(int(float(ts))): _num(v) for ts, v in (s.get("values") or [])}
                    by_name[name] = pts
                    stamps |= set(pts)
                labels = sorted(stamps, key=int)
                # Busiest series first so the legend leads with what matters; cap the line count
                # because 20+ overlapping lines is unreadable and the legend swamps the chart.
                ordered = sorted(
                    by_name.items(),
                    key=lambda kv: -max((v for v in kv[1].values() if v is not None), default=0),
                )[:MAX_LINES]
                datasets = [
                    {"label": name, "data": [pts.get(ts) for ts in labels]}
                    for name, pts in ordered
                ]
                return jsonify({"ok": True, "id": chart_id, "title": chart["title"],
                                "unit": chart["unit"], "kind": chart["kind"],
                                "selector": sel, "split_by": split,
                                "series_total": len(by_name),
                                "labels": labels, "datasets": datasets})

            for name, tmpl in (chart.get("series") or {}).items():
                series = _range(tmpl.replace("{sel}", sel), minutes, step)
                if not series:
                    datasets.append({"label": name, "data": []})
                    continue
                points = series[0].get("values") or []
                if not labels:
                    labels = [str(int(float(ts))) for ts, _ in points]
                datasets.append({"label": name,
                                 "data": [_num(v) for _, v in points]})
            return jsonify({"ok": True, "id": chart_id, "title": chart["title"],
                            "unit": chart["unit"], "kind": chart["kind"],
                            "selector": sel, "labels": labels, "datasets": datasets})

        rows = _instant(chart["expr"].replace("{sel}", sel))
        key = chart.get("label_by") or "instance"
        pairs = [(str(r.get("metric", {}).get(key) or "?"), _num(r["value"][1])) for r in rows]
        pairs.sort(key=lambda p: (p[1] is None, -(p[1] or 0)))
        return jsonify({"ok": True, "id": chart_id, "title": chart["title"],
                        "unit": chart["unit"], "kind": chart["kind"], "selector": sel,
                        "labels": [p[0] for p in pairs],
                        "datasets": [{"label": chart["title"], "data": [p[1] for p in pairs]}]})
    except urllib.error.URLError as exc:
        return jsonify({"ok": False, "error": f"metrics upstream unreachable: {exc}"}), 502
    except Exception as exc:
        _logger.debug("api_metrics_chart: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN/Inf are not valid JSON; Chart.js renders null as a gap, which is the honest rendering.
    return f if math.isfinite(f) else None


@metrics_query_bp.route("/api/metrics/query")
def api_metrics_query():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    try:
        return jsonify({"ok": True, "result": _instant(q)})
    except Exception as exc:
        _logger.debug("api_metrics_query: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 502


@metrics_query_bp.route("/api/metrics/query_range")
def api_metrics_query_range():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    try:
        minutes = max(1, min(1440, int(request.args.get("minutes") or 60)))
    except ValueError:
        minutes = 60
    try:
        step = request.args.get("step") or f"{max(15, minutes * 60 // 120)}s"
        return jsonify({"ok": True, "result": _range(q, minutes, step)})
    except Exception as exc:
        _logger.debug("api_metrics_query_range: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 502
