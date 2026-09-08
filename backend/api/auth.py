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

import logging

import throttle
from audit.actions import action
from flask import Blueprint, jsonify, redirect, request, session

from ._common import serve_spa_asset as _serve_spa_asset

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
# The blueprint NAME, however, does change the endpoint names -- see the docstring.
_logger = logging.getLogger("rack_web.api.auth")

bp = Blueprint("auth", __name__, url_prefix="")


@throttle.throttle(60, rationale="serves the SPA shell; cheap, but anonymous.")
@bp.route("/login", methods=["GET"])
def login():
    """Serve the SPA shell; the React LoginPage drives the SRP exchange.

    An already-authenticated caller is redirected away, and "authenticated" now means a valid access
    token rather than a Flask session — otherwise a stale session cookie would bounce a signed-out
    operator off the one page they need.
    """
    import web_auth

    if web_auth.authenticated_principal(request.headers, request.cookies):
        return redirect("/")
    return _serve_spa_asset("index.html")


@bp.route("/logout", methods=["POST"])
@action("auth:logout", variant="form")
def logout():
    session.clear()
    return redirect("/")


@throttle.throttle(120, rationale="one token verification. The SPA calls it on mount and after every renewal, so it must clear normal use comfortably.")
@bp.route("/api/auth-check")
def api_auth_check():
    """Whether this request carries a valid credential. 401 when it does not.

    Reads the **token**, matching what the gate enforces. It used to answer from the Flask session,
    and after the gate moved to tokens that produced the worst possible shape: this endpoint said
    200 while every other route said 401, so the SPA rendered a logged-in shell over a dashboard
    where nothing loaded and nothing explained why.
    """
    import web_auth

    principal = web_auth.authenticated_principal(request.headers, request.cookies)
    if not principal:
        return jsonify({"authenticated": False}), 401

    # `must_change_password` is reported here, not only at login. The SPA re-checks on every mount,
    # so without it a page reload silently clears the flag and the operator walks past the prompt —
    # which is the whole of FR-10a defeated by pressing F5.
    must_change = False
    try:
        from identity import principals
        from identity.store import IdentityStore

        kind, login = principals.parse_principal(principal)
        if kind == principals.KIND_USER:
            store = IdentityStore()
            try:
                user = store.get_user(login)
                must_change = bool(user is not None and user.must_change_password)
            finally:
                store.close()
    except Exception:
        # A store hiccup must not lock out a caller whose token is perfectly valid; the worst case
        # is one missed prompt, and the next login re-reports it.
        _logger.exception("auth-check: could not read the password-change flag")

    return jsonify({
        "authenticated": True,
        "user": principal,
        "must_change_password": must_change,
    })


# --- Removed 2026-09-01: POST /api/login (FR-18) ---------------------------------------------
#
# The shared-token and PAM login endpoint is gone. `POST /api/auth/srp/begin` and `/finish` replace
# it: the password is never transmitted, so it cannot appear in a proxy log or a crash report.
#
# **Deleted rather than left returning an error.** Once the gate moved to tokens this route still
# answered `200 {"ok": true}` and set a Flask session nothing honoured — an operator logged in, got
# a dashboard shell, and every panel returned 401 with nothing explaining why. A route that succeeds
# and grants nothing is worse than one that is absent, because the failure appears far from its
# cause. A 410 was considered and rejected for the same reason: the SPA no longer calls it, so the
# only readers would be scripts, and a hard 404 with the path simply gone is the clearer signal.
#
# Migration for a caller that used it: authenticate over SRP and send the resulting access token as
# `Authorization: Bearer <token>`, or hold the `HttpOnly` cookie the exchange sets. See
# `docs/API_RESPONSE_CODES.md` and the login flow in `frontend/src/lib/srpClient.ts`.


@bp.route("/api/logout", methods=["POST"])
@action("auth:logout")
def api_logout():
    """JSON logout endpoint for the React SPA."""
    session.clear()
    return jsonify({"ok": True})
