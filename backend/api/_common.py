# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Flask-dependent helpers shared across API modules.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``core.py`` deliberately imports no Flask, so anything that builds a response cannot live there.
This module can, and the loader skips it: underscore-prefixed modules are helpers, not endpoints.

Importable from ``app.py`` too, without a cycle -- ``app`` already imports the ``api`` package, and
nothing under ``api`` imports ``app``.
"""

from __future__ import annotations

import logging
import os
import re
import shlex

import core
import logging_config as lc
import rack_locks
from flask import g, jsonify, request, send_file
from naming import _normalize_rack_id
from node_targets import validation_target_for_rack
from paths import REPO_ROOT, _resolve_log_dir, _script_exists, _sysinfo_script_path

_logger = logging.getLogger(__name__)

_SPA_DIR = REPO_ROOT / "static" / "spa"

# A client route ("/compute/nodes") must fall back to index.html, but a *build artifact*
# must not: returning HTML for a missing .js makes the browser report a MIME-type error
# ("Expected a JavaScript-or-Wasm module script … responded with text/html") and renders a
# blank page, pointing at the SPA instead of at the deploy that dropped the file. These are
# requests for a hashed filename nobody types, so a miss is always a broken deploy.
# Both the `/<path>` and `/spa/<path>` routes arrive here with a leading "assets/".
#
# `icons/` is here for the same reason but a worse failure: a browser that gets HTML for a
# manifest icon does not report anything, it just drops the icon, and dropping an icon makes the
# app quietly non-installable. Loud beats silent even though nothing renders blank.
_SPA_ARTIFACT_PREFIXES = ("assets/", "icons/")
# The web manifest is the same class of never-typed install asset, but an exact filename rather
# than a directory.
_SPA_ARTIFACT_NAMES = ("manifest.webmanifest",)
# An /api/ path that reached the catch-all has no route: the endpoint does not exist in THIS
# build. Answering it with index.html gives the caller HTML at status 200, so a half-deployed
# backend looks like a feature returning no data instead of a missing endpoint. Verified
# 2026-08-03: the Validation activities panel read "0 tracked" for exactly this reason, with
# the frontend deployed and its backend not.
_SPA_API_PREFIX = "api/"


def serve_spa_asset(path: str = "index.html"):
    """Serve a built SPA asset; fall back to index.html for client routes only.

    Shared rather than owned by the catch-all routes because the login route serves the same shell:
    the login screen *is* the React app, so /login and / must return byte-identical HTML or the two
    disagree about which bundle to load.
    """
    spa_file = _SPA_DIR / path
    if spa_file.is_file():
        return send_file(spa_file)
    if path.startswith(_SPA_ARTIFACT_PREFIXES) or path in _SPA_ARTIFACT_NAMES:
        _logger.error("SPA artifact missing: %s — index.html and assets/ are out of sync", path)
        return jsonify({"ok": False, "error": f"SPA asset not found: {path}"}), 404
    if path.startswith(_SPA_API_PREFIX):
        _logger.error("no such API endpoint: /%s — frontend and backend are out of step", path)
        return jsonify({"ok": False, "error": f"no such API endpoint: /{path}"}), 404

    index = _SPA_DIR / "index.html"
    if not index.is_file():
        # Falling through to send_file here would hand off a file this function has already
        # established is absent, so the operator gets a 500 and a stack trace for what is really
        # a deployment that never had a frontend built into it. Say that instead: the backend is
        # healthy and answering, the bundle is what is missing.
        _logger.error("SPA shell missing: %s — the frontend was not built into this deployment",
                      index)
        body = (
            "<!doctype html><title>Frontend not built</title>"
            "<h1>Frontend not built</h1>"
            "<p>The backend is running, but <code>static/spa/index.html</code> is missing from "
            "this deployment, so there is no UI to serve.</p>"
            "<p>Build it with <code>npm run build</code> in <code>frontend/</code>, or rebuild "
            "the image so <code>static/spa/</code> is populated. "
            "<code>bin/verify-deploy.sh</code> checks for exactly this.</p>"
        )
        return body, 503, {"Content-Type": "text/html; charset=utf-8"}
    return send_file(index)


def history_unavailable_response():
    """The 503 returned when the run-history store is absent or disabled."""
    return jsonify({
        "ok": False,
        "error": "run history is unavailable (results_history not installed or disabled)",
    }), 503


def arg_or_body(name: str):
    """One parameter from either the JSON body or the query string, body first.

    The same endpoint is called both ways: the SPA POSTs a body, and an operator debugging by hand
    passes a query parameter. Body wins so an explicit POST is never silently overridden by a stale
    query string on the same URL.
    """
    payload = request.get_json(silent=True)
    if isinstance(payload, dict) and payload.get(name) is not None:
        return payload.get(name)
    return request.args.get(name)


def requested_nodes() -> list[str]:
    """Node list from a JSON body (preferred) or a ``?nodes=`` query.

    The body is the real channel: a cluster-sized list does not fit in a URL -- 591 node names is
    a ~12 KB query and gunicorn rejects any request line over 4094 bytes with a bare 400. The
    query form is kept for hand-run curl against a handful of nodes.
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("nodes") if isinstance(body, dict) else None
    if isinstance(raw, list):
        return [str(n).strip() for n in raw if str(n).strip()]
    return [n.strip() for n in (request.args.get("nodes") or "").split(",") if n.strip()]


_APP_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")


def operator_from_request() -> str:
    """Who is performing this operation, for the durable record (FR-16).

    **The authenticated principal wins; the header can no longer override it.** This used to read
    ``X-Rack-Operator`` unconditionally — a client-supplied string — and stamp it onto rack lock
    claims and validation activities. It was correctly never trusted for *authorization*, but that
    was never the whole job: those records are the evidence for "who locked this rack" and "who ran
    this on that tray", and an operator label anyone could set with a curl flag made that
    unprovable. A handover reading the ledger could not tell a real claim from a typed one.

    Now that the gate verifies a token and publishes the subject on ``g``, the label comes from
    there and is unforgeable while the gate is on.

    **The header fallback is gone too, as of 2026-09-10.** It survived for exactly one case,
    ``RACK_WEB_AUTH_DISABLED``, where there was no principal to be had — and that switch was removed
    on 2026-09-08, which made the fallback unreachable on any gated route while leaving a
    client-supplied string able to name the operator in a durable record if it ever became reachable
    again. Keeping a forgeable path alive for a mode that no longer exists is the opposite of what
    FR-16 asked for.

    All 47 call sites across 12 api modules route through here, so this is the one place the rule
    lives. Returns a bare login (``rkoike``), not the ``user:rkoike`` principal rendering: the label
    is displayed to operators and stored beside free-text history, and nothing compares it.
    """
    try:
        principal = getattr(g, "auth_principal", "") or ""
    except RuntimeError:
        principal = ""

    if principal:
        # `user:rkoike` -> `rkoike`. An unexpected shape is kept whole rather than dropped: a label
        # we cannot parse is still better evidence than falling through to the header.
        _, _, login = principal.partition(":")
        return rack_locks.sanitize_operator(login or principal)

    # No principal. Two ways to get here, and neither should produce a label:
    #
    #   * a worker thread, which has no request context at all -- this used to be a `RuntimeError`
    #     branch around the header read, and the header was the only reason the try/except existed;
    #   * a gated route somehow reaching this without `g.auth_principal`, which would be a defect.
    #
    # An empty label is the honest answer for both. It is also what makes the record evidence: a
    # blank operator says "we do not know", where `X-Rack-Operator` said whatever the caller typed.
    return ""


def client_id_from_request() -> str:
    """Stable browser id from the ``X-Rack-Client-Id`` header (also accepted as
    ``?client_id=`` for backward compatibility with the existing useRackStatus poll)."""
    try:
        raw = request.headers.get("X-Rack-Client-Id", "") or request.args.get("client_id", "")
    except RuntimeError:
        return ""
    if not isinstance(raw, str):
        return ""
    cid = raw.strip()[:80]
    return cid if _APP_CLIENT_ID_RE.fullmatch(cid) else ""


def parse_sysinfo_request(body: dict) -> tuple[dict | None, tuple[object, int] | None]:
    """Validate sysinfo POST body. On success returns (prep, None); prep has log_dir, rack, target, display_cmd, cmd."""
    if os.environ.get("RACK_SYSINFO_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return None, (jsonify({"ok": False, "error": "run-sysinfo disabled (RACK_SYSINFO_DISABLED)"}), 403)
    log_raw = body.get("log_dir")
    try:
        log_dir = _resolve_log_dir(str(log_raw).strip() if log_raw else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    rack = (body.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    tgt_ov = body.get("target")
    if tgt_ov is not None and not isinstance(tgt_ov, str):
        return None, (jsonify({"ok": False, "error": "target must be a string"}), 400)
    try:
        target = validation_target_for_rack(rack, tgt_ov if isinstance(tgt_ov, str) else None)
    except ValueError as e:
        return None, (jsonify({"ok": False, "error": str(e)}), 400)
    script = _sysinfo_script_path()
    if not _script_exists(script):
        return None, (jsonify({"ok": False, "error": f"sysinfo script not found: {script}"}), 500)
    cmd = ["bash", str(script), target]
    display_cmd = "./" + script.name + " " + shlex.quote(target)
    return (
        {
            "log_dir": log_dir,
            "rack": rk,
            "target": target,
            "display_cmd": display_cmd,
            "cmd": cmd,
        },
        None,
    )


# ---------------------------------------------------------------------------
# Client address provenance (FR-8, NFR-14) -- the Flask half of core.resolve_client_addr
# ---------------------------------------------------------------------------

#: Deploy-time only, read straight from the process environment rather than through the settings
#: ladder: widening a trust list is a privilege change, so an authenticated dashboard operator must
#: not be able to do it at runtime (NFR-5). Same exception already taken for
#: ``RACK_WEB_ALLOWED_ORIGINS`` and ``RACK_WEB_TLS_*``.
TRUSTED_PROXIES = lc.register_setting(lc.Setting("RACK_WEB_TRUSTED_PROXIES", ""))

_trusted_cache: tuple[str, tuple] | None = None
_refusals_reported = False


def trusted_networks() -> tuple:
    """The accepted trusted-proxy networks, parsing once and reporting refusals once.

    Empty by default, which is the safe default: with no trusted proxy configured, no forwarded
    header is ever believed.
    """
    global _trusted_cache, _refusals_reported
    raw = str(TRUSTED_PROXIES.resolve())
    if _trusted_cache is not None and _trusted_cache[0] == raw:
        return _trusted_cache[1]

    accepted, refusals = core.parse_trusted_proxies(raw)
    if refusals and not _refusals_reported:
        _refusals_reported = True
        for reason in refusals:
            _logger.warning("RACK_WEB_TRUSTED_PROXIES: refusing %s", reason)
    _trusted_cache = (raw, accepted)
    return accepted


def client_addr() -> tuple[str, str]:
    """``(src_ip, src_ip_trust)`` for the request in flight.

    Operators normally reach this dashboard through an SSH tunnel, so the peer is usually the
    tunnel endpoint rather than the workstation. That is a real limit of the field and is
    documented rather than papered over; what the label prevents is the *worse* outcome of
    recording an attacker-supplied address as though it were verified.
    """
    return core.resolve_client_addr(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
        trusted_networks(),
    )


def reset_trusted_cache_for_tests() -> None:
    global _trusted_cache, _refusals_reported
    _trusted_cache = None
    _refusals_reported = False
