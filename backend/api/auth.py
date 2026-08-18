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
"""Login, logout and session state -- the endpoints only.

The mechanism (modes, PAM/token verification, session predicates) is ``web_auth.py``; the enforcement
is ``_require_dashboard_auth`` in ``app.py``. Three files, three jobs, so the coming authorization
work has one place to change per concern.

**These routes carry a hard constraint.** ``/api/login`` and ``/api/auth-check`` must be reachable
WITHOUT a session -- they are how a session is obtained and how the SPA discovers it has none. That
exemption is granted by endpoint name in ``app._AUTH_BYPASS_ENDPOINTS``, and because this is a
blueprint the names are qualified: ``auth.login``, ``auth.api_login``, ``auth.api_auth_check``. Rename
the blueprint or a function without updating that tuple and nobody can log in, with no error anywhere;
``test_web_auth_gate.py`` asserts every entry resolves to a real endpoint for exactly that reason.

``/login`` returns the SPA shell rather than a server-rendered form: the login screen is a React page,
so it must serve the same ``index.html`` as ``/`` or the two load different bundles.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, request, session
from web_auth import (
    _auth_enabled,
    _auth_user,
    _pam_service,
    _pam_verify,
    _session_ok,
)

from ._common import serve_spa_asset as _serve_spa_asset

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
# The blueprint NAME, however, does change the endpoint names -- see the docstring.
bp = Blueprint("auth", __name__, url_prefix="")


@bp.route("/login", methods=["GET"])
def login():
    """Redirect to SPA — the React LoginPage handles authentication."""
    if not _auth_enabled() or _session_ok():
        return redirect("/")
    return _serve_spa_asset("index.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


@bp.route("/api/auth-check")
def api_auth_check():
    """Check if the current session is authenticated. Returns 401 if not."""
    if not _auth_enabled():
        return jsonify({"authenticated": True, "user": _auth_user(), "auth_disabled": True})
    if _session_ok():
        return jsonify({"authenticated": True, "user": _auth_user()})
    return jsonify({"authenticated": False}), 401


@bp.route("/api/login", methods=["POST"])
def api_login():
    """JSON login endpoint for the React SPA. Accepts {"password": "..."}."""
    if not _auth_enabled():
        return jsonify({"ok": True, "user": _auth_user(), "auth_disabled": True})
    data = request.get_json(silent=True) or {}
    pw = str(data.get("password") or "")
    au = _auth_user()
    psvc = _pam_service()
    try:
        ok = _pam_verify(au, pw, psvc)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": f"PAM error: {e}"}), 500
    if ok:
        session.clear()
        session.permanent = True
        session["rack_web_auth"] = "1"
        return jsonify({"ok": True, "user": au})
    return jsonify({"ok": False, "error": "Invalid password"}), 401


@bp.route("/api/logout", methods=["POST"])
def api_logout():
    """JSON logout endpoint for the React SPA."""
    session.clear()
    return jsonify({"ok": True})
