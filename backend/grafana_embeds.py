# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Flask blueprint serving the 1:1 observability-mockup -> real-Grafana embed map.

This is the server side of "replace the static observability mockups with our real
Grafana". It reads the canonical map produced from the RECON-B audit
(``deploy-secrets/grafana_embed_map.json``) and hands the SPA fully-resolved
``/d-solo/`` iframe URLs, so the Metrics / Logs / Events / Infrastructure views can stop
rendering generated sine waves and fabricated tables.

Routes (registered under ``/api/observability``)::

    GET  /api/observability/dashboards          whole map + resolved embed URLs
    GET  /api/observability/dashboards/<view>   one view's sections
    GET  /api/observability/health              can the backend reach Grafana?

All three are covered by the app-level ``_require_dashboard_auth`` before_request hook in
``app.py`` (any ``/api/`` path requires a session unless ``RACK_WEB_AUTH_DISABLED=1``), so
this module adds no auth of its own.

Why there are TWO base URLs
---------------------------
**The browser resolves the iframe, not the server.** The operator reaches Rack Status
through a Teleport tunnel on :5056, so a server-side ``http://localhost:3001`` is useless to
their browser -- localhost there is their laptop.

* ``RACK_GRAFANA_BASE_URL``   -- server-facing. Used ONLY for reachability checks from
  inside the container. Default ``http://localhost:3001``.
* ``RACK_GRAFANA_PUBLIC_URL`` -- browser-facing origin baked into the iframe ``src``.
  Default is EMPTY on purpose: an empty value tells the SPA to resolve the origin the same
  way ``frontend/src/lib/grafanaLinks.ts::resolveGrafanaBase`` and
  ``ParserGrafanaEmbeds.tsx`` already do -- same protocol+hostname as the browser's current
  page, plus ``RACK_GRAFANA_PUBLIC_PORT`` (default 3001). Under a Teleport tunnel that
  resolves to the tunnelled host, which is what you want *provided :3001 is tunnelled too*.

Every panel is returned with BOTH an absolute ``embed_url`` (built from the public base when
one is known) and a base-relative ``embed_path``. The frontend should prefer ``embed_path``
joined onto its own resolved base when ``public_base`` is empty -- identical to the existing
``resolveGrafanaBase(grafanaUri, grafanaPort)`` pattern. Do not invent a third scheme.

Environment
-----------
``RACK_GRAFANA_BASE_URL``     server-side Grafana origin (default ``http://localhost:3001``)
``RACK_GRAFANA_PUBLIC_URL``   browser-side Grafana origin (default empty = same-host)
``RACK_GRAFANA_PUBLIC_PORT``  port used when the public URL is empty (default ``3001``)
``RACK_GRAFANA_EMBED_MAP``    path to grafana_embed_map.json (default: auto-discovered)
``RACK_GRAFANA_ORG_ID``       Grafana orgId query param (default ``1``)
``RACK_GRAFANA_THEME``        default embed theme: ``dark`` / ``light`` (default ``dark``)
``RACK_GRAFANA_FROM``         default time range start (default ``now-1h``)
``RACK_GRAFANA_TO``           default time range end (default ``now``)
``RACK_GRAFANA_REFRESH``      default auto-refresh, e.g. ``30s``; empty = none
``RACK_GRAFANA_TIMEZONE``     overrides the shared ``RACK_WEB_GRAFANA_TIMEZONE`` if set
``RACK_GRAFANA_USER`` / ``RACK_GRAFANA_PASSWORD``  basic-auth for the health probe only
``RACK_GRAFANA_TOKEN``        bearer token for the health probe (preferred over basic auth)
``RACK_GRAFANA_HEALTH_TIMEOUT``  seconds, default ``2.5``
``RACK_GRAFANA_EMBEDS_DISABLED`` set truthy to serve the map with ``enabled: false``

Nothing in this module raises out to Flask. A missing/corrupt map degrades to an empty map
with an ``error`` field; an unreachable Grafana degrades to ``reachable: false``.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

grafana_embeds_bp = Blueprint("grafana_embeds", __name__, url_prefix="/api/observability")

# Template variables any of the mapped dashboards actually declare. Verified against the
# live dashboard JSON on 2026-07-28. Anything outside this set is still accepted when sent
# explicitly as ``var-<name>``; Grafana ignores unknown vars harmlessly.
KNOWN_SCOPE_VARS = (
    "cluster",
    "datahall",
    "su",
    "rack",
    "node",
    "gpu",
    "switch",
    "interface",
    "role",
    "severity",
    "jobid",
    "type",
    "status",
    "category",
    "power_component",
    "log_search",
    "dgx_flavor",
)

_DEFAULT_MAP_FILENAME = "grafana_embed_map.json"


# --------------------------------------------------------------------------------------
# env helpers (kept local so this module never imports app.py -- avoids a circular import)
# --------------------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    try:
        return (os.environ.get(name) or "").strip() or default
    except Exception:  # pragma: no cover - defensive
        _logger.debug("_env: ignoring error", exc_info=True)
        return default


def _env_truthy(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _trim_slash(s: str) -> str:
    return (s or "").rstrip("/")


def server_base_url() -> str:
    """Grafana origin reachable from INSIDE the container (health checks only)."""
    explicit = _env("RACK_GRAFANA_BASE_URL")
    if explicit:
        return _trim_slash(explicit)
    # Stay consistent with the existing parser-embed scheme if the operator already set it.
    legacy = _env("RACK_WEB_GRAFANA_URI")
    if legacy:
        return _trim_slash(legacy)
    return "http://localhost:3001"


def public_base_url() -> str:
    """Grafana origin the BROWSER will use. Empty means 'same host as the SPA'."""
    explicit = _env("RACK_GRAFANA_PUBLIC_URL")
    if explicit:
        return _trim_slash(explicit)
    return ""


def public_port() -> str:
    return _env("RACK_GRAFANA_PUBLIC_PORT") or _env("RACK_WEB_GRAFANA_PORT") or "3001"


def _timezone_param() -> str:
    """Match ``app.py::_grafana_url_timezone_param`` semantics without importing app.py."""
    g = _env("RACK_GRAFANA_TIMEZONE") or _env("RACK_WEB_GRAFANA_TIMEZONE")
    if g:
        return g.lower() if g.lower() in ("browser", "utc") else g
    logtz = _env("RACK_PROMETHEUS_LOG_TIMESTAMP_TZ", "UTC")
    return "utc" if logtz.upper() in ("UTC", "GMT", "Z", "UCT") else logtz


# --------------------------------------------------------------------------------------
# map loading -- never raises
# --------------------------------------------------------------------------------------
_map_lock = threading.Lock()
_map_cache: dict[str, Any] | None = None
_map_cache_key: tuple[str, float, int] | None = None


def _candidate_map_paths() -> list[Path]:
    out: list[Path] = []
    explicit = _env("RACK_GRAFANA_EMBED_MAP")
    if explicit:
        out.append(Path(explicit))
    here = Path(__file__).resolve()
    repo_root = here.parent.parent  # backend/ -> repo root (== /app in the container)
    out.append(repo_root / "deploy-secrets" / _DEFAULT_MAP_FILENAME)
    out.append(here.parent / _DEFAULT_MAP_FILENAME)
    out.append(Path("/app/deploy-secrets") / _DEFAULT_MAP_FILENAME)
    out.append(Path("/opt/observability/nvis-rack-status-v3/deploy-secrets") / _DEFAULT_MAP_FILENAME)
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def _empty_map(error: str, searched: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 0,
        "views": {},
        "unmapped": [],
        "keep_as_is": [],
        "error": error,
        "searched_paths": searched,
    }


def load_map(force: bool = False) -> dict[str, Any]:
    """Load and cache the embed map. Returns an empty map with ``error`` on any failure."""
    global _map_cache, _map_cache_key
    searched: list[str] = []
    for path in _candidate_map_paths():
        searched.append(str(path))
        try:
            st = path.stat()
        except OSError:
            continue
        key = (str(path), st.st_mtime, st.st_size)
        with _map_lock:
            if not force and _map_cache is not None and _map_cache_key == key:
                return _map_cache
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            _logger.warning("grafana embed map at %s is unreadable: %s", path, exc, exc_info=True)
            return _empty_map(f"embed map unreadable at {path}: {exc}", searched)
        if not isinstance(data, dict) or not isinstance(data.get("views"), dict):
            return _empty_map(f"embed map at {path} has no 'views' object", searched)
        data.setdefault("unmapped", [])
        data.setdefault("keep_as_is", [])
        data["_source_path"] = str(path)
        with _map_lock:
            _map_cache = data
            _map_cache_key = key
        return data
    _logger.warning("grafana embed map not found; searched %s", searched)
    return _empty_map("embed map file not found", searched)


# --------------------------------------------------------------------------------------
# URL building
# --------------------------------------------------------------------------------------
def _collect_scope_vars(args: Any) -> list[tuple[str, str]]:
    """Pull ``var-*`` passthroughs plus the bare ``rack``/``node``/``su``/... conveniences.

    Explicit ``var-x`` always wins over the bare ``x`` form. Multi-valued params are
    preserved (Grafana repeats ``var-rack=a&var-rack=b`` for multi-select variables).
    """
    pairs: list[tuple[str, str]] = []
    explicit: set[str] = set()
    try:
        items = list(args.lists())  # werkzeug MultiDict
    except AttributeError:
        items = [(k, [v]) for k, v in dict(args or {}).items()]
    for key, values in items:
        if not key.startswith("var-"):
            continue
        name = key[4:].strip()
        if not name:
            continue
        explicit.add(name)
        for v in values:
            if str(v).strip():
                pairs.append((f"var-{name}", str(v)))
    for key, values in items:
        if key in explicit or key not in KNOWN_SCOPE_VARS:
            continue
        for v in values:
            if str(v).strip():
                pairs.append((f"var-{key}", str(v)))
    return pairs


class _UrlOpts:
    """Resolved per-request embed options."""

    __slots__ = ("frm", "kiosk", "org_id", "refresh", "scope", "theme", "timezone", "to")

    def __init__(self, args: Any = None):
        args = args if args is not None else {}
        get = args.get if hasattr(args, "get") else (lambda k, d=None: d)
        self.frm = str(get("from", "") or "").strip() or _env("RACK_GRAFANA_FROM", "now-1h")
        self.to = str(get("to", "") or "").strip() or _env("RACK_GRAFANA_TO", "now")
        theme = str(get("theme", "") or "").strip().lower() or _env("RACK_GRAFANA_THEME", "dark")
        self.theme = theme if theme in ("dark", "light", "system") else "dark"
        self.refresh = str(get("refresh", "") or "").strip() or _env("RACK_GRAFANA_REFRESH")
        self.timezone = str(get("timezone", "") or "").strip() or _timezone_param()
        self.org_id = _env("RACK_GRAFANA_ORG_ID", "1")
        kiosk_raw = str(get("kiosk", "") or "").strip().lower()
        self.kiosk = kiosk_raw not in ("0", "false", "no", "off")
        self.scope = _collect_scope_vars(args)


def _q(pairs: list[tuple[str, str]]) -> str:
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)


def panel_embed_path(uid: str, panel_id: Any, opts: _UrlOpts) -> str:
    """``/d-solo/{uid}/_?panelId=..&from=..&to=..&theme=..&kiosk`` (+ orgId, tz, var-*)."""
    pairs: list[tuple[str, str]] = [
        ("orgId", opts.org_id),
        ("panelId", str(panel_id)),
        ("from", opts.frm),
        ("to", opts.to),
        ("theme", opts.theme),
        ("timezone", opts.timezone),
    ]
    if opts.refresh:
        pairs.append(("refresh", opts.refresh))
    pairs.extend(opts.scope)
    qs = _q(pairs)
    if opts.kiosk:
        qs += "&kiosk"
    return f"/d-solo/{urllib.parse.quote(str(uid))}/_?{qs}"


def dashboard_path(uid: str, opts: _UrlOpts, solo_free: bool = False) -> str:
    """``/d/{uid}/_?kiosk&theme={theme}`` (+ orgId, time range, tz, var-*).

    ``solo_free=True`` drops ``kiosk`` so the URL is suitable for an "Open in Grafana"
    link in a new tab (chrome + variable pickers visible), matching the intent of
    ``frontend/src/lib/grafanaLinks.ts::buildGrafanaDeepLink``.
    """
    pairs: list[tuple[str, str]] = [
        ("orgId", opts.org_id),
        ("from", opts.frm),
        ("to", opts.to),
        ("theme", opts.theme),
        ("timezone", opts.timezone),
    ]
    if opts.refresh:
        pairs.append(("refresh", opts.refresh))
    pairs.extend(opts.scope)
    qs = _q(pairs)
    base = f"/d/{urllib.parse.quote(str(uid))}/_?{qs}"
    if opts.kiosk and not solo_free:
        base += "&kiosk"
    return base


def _abs(pub_base: str, path: str) -> str:
    return f"{pub_base}{path}" if pub_base else ""


# --------------------------------------------------------------------------------------
# map -> resolved payload
# --------------------------------------------------------------------------------------
def _resolve_section(section: dict[str, Any], pub_base: str, opts: _UrlOpts) -> dict[str, Any]:
    uid = str(section.get("uid") or "")
    titles = section.get("panel_titles") or {}
    panels: list[dict[str, Any]] = []
    for pid in section.get("panel_ids") or []:
        path = panel_embed_path(uid, pid, opts)
        panels.append(
            {
                "panel_id": pid,
                "panel_title": titles.get(str(pid)) or titles.get(pid) or "",
                "embed_path": path,
                "embed_url": _abs(pub_base, path),
                # The SPA's EmbedPanel type is {id, title, url} and it prefers `panels` over
                # `panel_ids`. Without these aliases every frame renders "Panel undefined" with an
                # empty src. Kept alongside the snake_case keys so both shapes stay valid.
                "id": pid,
                "title": titles.get(str(pid)) or titles.get(pid) or "",
                "url": _abs(pub_base, path) or path,
            }
        )
    out = dict(section)
    out["panels"] = panels
    dash_path = dashboard_path(uid, opts)
    open_path = dashboard_path(uid, opts, solo_free=True)
    out["dashboard_embed_path"] = dash_path
    out["dashboard_embed_url"] = _abs(pub_base, dash_path)
    out["dashboard_open_path"] = open_path
    out["dashboard_open_url"] = _abs(pub_base, open_path)
    return out


def _resolve_view(view_key: str, view: dict[str, Any], pub_base: str, opts: _UrlOpts) -> dict[str, Any]:
    out = dict(view)
    out["view"] = view_key
    out["sections"] = [
        _resolve_section(s, pub_base, opts) for s in (view.get("sections") or []) if isinstance(s, dict)
    ]
    out["section_count"] = len(out["sections"])
    out["panel_count"] = sum(len(s["panels"]) for s in out["sections"])
    return out


def _view_aliases(data: dict[str, Any]) -> dict[str, str]:
    """Accept ``metrics``, ``obsMetrics``, ``/observability/metrics``, ``infra-ethernet``, ``ethernet``..."""
    alias: dict[str, str] = {}
    for key, view in (data.get("views") or {}).items():
        alias[key.lower()] = key
        alias[key.lower().replace("-", "_")] = key
        alias[key.lower().replace("-", "/")] = key
        route = str((view or {}).get("route") or "").strip()
        if route:
            alias[route.lower()] = key
            alias[route.lower().lstrip("/")] = key
        seg = str((view or {}).get("segment") or "").strip()
        if seg:
            alias.setdefault(seg.lower(), key)
    # viewIds used by frontend/src/.../routes.ts
    for vid, key in (
        ("obsmetrics", "metrics"),
        ("obslogs", "logs"),
        ("obsevents", "events"),
        ("infraethernet", "infra-ethernet"),
        ("infraibnvlink", "infra-ib-nvlink"),
        ("infrastorage", "infra-storage"),
    ):
        if key in (data.get("views") or {}):
            alias.setdefault(vid, key)
    return alias


def _config_block(pub_base: str, opts: _UrlOpts) -> dict[str, Any]:
    return {
        "enabled": not _env_truthy("RACK_GRAFANA_EMBEDS_DISABLED"),
        "public_base": pub_base,
        "public_port": public_port(),
        "public_base_note": (
            "Empty public_base means: resolve the Grafana origin in the BROWSER as "
            "`${window.location.protocol}//${window.location.hostname}:${public_port}` -- the same "
            "rule as frontend/src/lib/grafanaLinks.ts::resolveGrafanaBase. Join it with each "
            "panel's embed_path. Under the Teleport tunnel on :5056, port "
            f"{public_port()} must ALSO be tunnelled or the iframes will not load."
        ),
        "server_base": server_base_url(),
        "server_base_note": "Used only for backend reachability checks; NOT browser-resolvable.",
        "org_id": opts.org_id,
        "theme": opts.theme,
        "from": opts.frm,
        "to": opts.to,
        "refresh": opts.refresh,
        "timezone": opts.timezone,
        "kiosk": opts.kiosk,
        "scope_vars_applied": [f"{k}={v}" for k, v in opts.scope],
        "known_scope_vars": list(KNOWN_SCOPE_VARS),
    }


# --------------------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------------------
@grafana_embeds_bp.route("/dashboards", methods=["GET"])
def api_observability_dashboards():
    """Whole map with every panel resolved to an embed URL."""
    try:
        data = load_map(force=str(request.args.get("reload", "")).strip() in ("1", "true", "yes"))
        opts = _UrlOpts(request.args)
        pub = public_base_url()
        views = {k: _resolve_view(k, v, pub, opts) for k, v in (data.get("views") or {}).items() if isinstance(v, dict)}
        payload = {
            "ok": True,
            "config": _config_block(pub, opts),
            "grafana": data.get("grafana", {}),
            "schema_version": data.get("schema_version"),
            "generated": data.get("generated"),
            "source_path": data.get("_source_path"),
            "views": views,
            "view_keys": list(views.keys()),
            "unmapped": data.get("unmapped", []),
            "keep_as_is": data.get("keep_as_is", []),
            "embed_blocker": data.get("embed_blocker"),
            "totals": {
                "views": len(views),
                "sections": sum(v["section_count"] for v in views.values()),
                "panels": sum(v["panel_count"] for v in views.values()),
                "unmapped": len(data.get("unmapped", []) or []),
            },
        }
        if data.get("error"):
            payload["ok"] = False
            payload["error"] = data["error"]
            payload["searched_paths"] = data.get("searched_paths", [])
        return jsonify(payload)
    except Exception as exc:  # pragma: no cover - the route must never 500
        _logger.exception("observability dashboards route failed")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}", "views": {}, "unmapped": []}), 200


@grafana_embeds_bp.route("/dashboards/<path:view>", methods=["GET"])
def api_observability_dashboards_view(view: str):
    """One view's sections. Accepts the map key, the route path, or the SPA viewId."""
    try:
        data = load_map()
        views = data.get("views") or {}
        alias = _view_aliases(data)
        key = alias.get(str(view or "").strip().lower()) or alias.get(
            str(view or "").strip().lower().lstrip("/")
        )
        if key is None or key not in views:
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": f"unknown view {view!r}",
                        "known_views": sorted(views.keys()),
                        "accepted_aliases": sorted(alias.keys()),
                    }
                ),
                404,
            )
        opts = _UrlOpts(request.args)
        pub = public_base_url()
        resolved = _resolve_view(key, views[key], pub, opts)
        unmapped = [u for u in (data.get("unmapped") or []) if key in str(u.get("view", "")).split(",")]
        keep = [k for k in (data.get("keep_as_is") or []) if str(k.get("view", "")) in (key, "all")]
        return jsonify(
            {
                "ok": True,
                "config": _config_block(pub, opts),
                "view": key,
                **{k: v for k, v in resolved.items() if k != "view"},
                "unmapped": unmapped,
                "keep_as_is": keep,
                "embed_blocker": data.get("embed_blocker"),
            }
        )
    except Exception as exc:  # pragma: no cover
        _logger.exception("observability dashboards/<view> route failed")
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 200


def _grafana_request(url: str, timeout: float, method: str = "GET") -> tuple[int | None, dict[str, str], bytes, str]:
    """One best-effort HTTP call to Grafana. Returns (status, headers, body, error)."""
    req = urllib.request.Request(url, method=method)
    token = _env("RACK_GRAFANA_TOKEN")
    user = _env("RACK_GRAFANA_USER")
    pwd = os.environ.get("RACK_GRAFANA_PASSWORD") or ""
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    elif user and pwd:
        raw = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {raw}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(65536), ""
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(65536)
        except Exception:
            _logger.debug("_grafana_request: ignoring error", exc_info=True)
            body = b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, body, f"HTTP {exc.code}"
    except Exception as exc:
        _logger.debug("_grafana_request: ignoring error", exc_info=True)
        return None, {}, b"", f"{type(exc).__name__}: {exc}"


@grafana_embeds_bp.route("/health", methods=["GET"])
def api_observability_health():
    """Can the BACKEND reach Grafana? Short timeout, never raises."""
    base = server_base_url()
    timeout = _env_float("RACK_GRAFANA_HEALTH_TIMEOUT", 2.5)
    out: dict[str, Any] = {
        "ok": True,
        "server_base": base,
        "public_base": public_base_url(),
        "public_port": public_port(),
        "timeout_sec": timeout,
        "reachable": False,
        "grafana_version": None,
        "database": None,
        "dashboards_count": None,
        "authenticated": None,
        "embedding_allowed": None,
        "x_frame_options": None,
        "content_security_policy": None,
        "notes": [],
        "errors": [],
    }
    try:
        status, _headers, body, err = _grafana_request(f"{base}/api/health", timeout)
        if status is None:
            out["errors"].append(f"/api/health: {err}")
            out["notes"].append(
                "Grafana is not reachable from inside the rack-status container. If Grafana runs on "
                "the ObsVM host, the container needs host networking or a reachable host IP -- "
                "'localhost' inside the container is the container itself."
            )
        else:
            out["reachable"] = 200 <= int(status) < 400
            try:
                h = json.loads(body.decode("utf-8", "replace"))
                out["grafana_version"] = h.get("version")
                out["database"] = h.get("database")
            except Exception:
                _logger.debug("api_observability_health: ignoring error", exc_info=True)

        # Dashboard count (needs auth). Never fatal.
        status2, _h2, body2, err2 = _grafana_request(f"{base}/api/search?type=dash-db&limit=500", timeout)
        if status2 == 200:
            try:
                items = json.loads(body2.decode("utf-8", "replace"))
                if isinstance(items, list):
                    out["dashboards_count"] = len(items)
                    out["authenticated"] = True
            except Exception as exc:
                _logger.debug("api_observability_health: ignoring error", exc_info=True)
                out["errors"].append(f"/api/search parse: {exc}")
        elif status2 in (401, 403):
            out["authenticated"] = False
            out["notes"].append(
                "Grafana rejected the backend's credentials, so dashboards_count is unknown. Set "
                "RACK_GRAFANA_USER + RACK_GRAFANA_PASSWORD (or RACK_GRAFANA_TOKEN) to enable the "
                "count. This does NOT affect iframe embedding, which uses the browser's session."
            )
        elif status2 is None:
            out["errors"].append(f"/api/search: {err2}")

        # Iframe embedding headers -- the thing that actually decides whether embeds render.
        probe_uid = str(request.args.get("probe_uid", "gpu-health")).strip() or "gpu-health"
        status3, h3, _b3, err3 = _grafana_request(
            f"{base}/d-solo/{urllib.parse.quote(probe_uid)}/_?orgId=1&panelId=1", timeout
        )
        if status3 is not None:
            xfo = (h3.get("x-frame-options") or "").strip()
            csp = (h3.get("content-security-policy") or "").strip()
            out["x_frame_options"] = xfo or None
            out["content_security_policy"] = csp or None
            blocked = xfo.lower() in ("deny", "sameorigin") or "frame-ancestors 'none'" in csp.lower()
            out["embedding_allowed"] = not blocked
            if blocked:
                out["notes"].append(
                    f"BLOCKING: Grafana returns 'X-Frame-Options: {xfo}' -- no iframe from the Rack "
                    "Status SPA can render. Fix: add GF_SECURITY_ALLOW_EMBEDDING=true to the grafana "
                    "service in /opt/observability/nscaletx/platform/docker-compose.core.yml and "
                    "restart Grafana. (Not changed automatically -- platform owner's call.)"
                )
        else:
            out["errors"].append(f"/d-solo probe: {err3}")

        m = load_map()
        out["map_loaded"] = not m.get("error")
        out["map_source_path"] = m.get("_source_path")
        out["map_views"] = list((m.get("views") or {}).keys())
        if m.get("error"):
            out["errors"].append(f"embed map: {m['error']}")
    except Exception as exc:  # pragma: no cover - health must never raise
        _logger.exception("observability health route failed")
        out["ok"] = False
        out["errors"].append(f"{type(exc).__name__}: {exc}")
    return jsonify(out)
