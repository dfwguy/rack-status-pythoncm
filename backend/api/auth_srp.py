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
"""SRP login, session renewal and logout — FR-4, FR-4g, FR-5, FR-5a, FR-5b, FR-6.

**The password never reaches this process.** SRP-6a is a two-leg exchange in which the client proves
knowledge of the password without sending it, so it cannot appear in a proxy log, a memory dump or a
crash report. That is the whole reason SRP was kept over an argon2id verifier behind TLS: TLS
protects the wire, and this protects everything the wire terminates into.

The two legs, and why it is two rather than three: the client must send `A` alongside the login,
because the server cannot compute `B` without it. A first draft of the SRP module tried to answer
with a salt and `B` before `A` arrived, which the maths does not allow.

    POST /api/auth/srp/begin   {login, A}  -> {token, salt, B}
    POST /api/auth/srp/finish  {token, M}  -> {HAMK, user}  + Set-Cookie

**An unknown login is answered exactly like a known one.** `srp.begin` runs a decoy path with a
reproducible synthetic salt when the login does not exist, and this module must not undo that by
branching in a way the response reveals — not in the status code, not in the body, not in the
presence of a field. `finish` then fails identically for a bad proof and for a decoy. This is FR-4b,
and it is why there is no `404` anywhere below.

**Tokens travel in `HttpOnly` cookies (FR-5a).** The browser gets no readable copy, so an XSS cannot
exfiltrate a session. A programmatic client can still send `Authorization: Bearer`, which is why the
gate accepts both.
"""

from __future__ import annotations

import logging

import core
from audit.actions import action
from flask import Blueprint, jsonify, make_response, request
from identity import principals, revocation, srp, tokens
from identity.models import IdentityError, canonical_login
from identity.rate_limit import RateLimiter
from identity.store import IdentityStore

from . import _common

_logger = logging.getLogger("rack_web.api.auth_srp")

bp = Blueprint("auth_srp", __name__, url_prefix="/api/auth")

#: One limiter for the whole module, keyed by resolved client address. Module-level because the
#: bucket state must outlive a request; `--workers 1` is already required by the SRP challenge
#: table, so per-process is per-deployment here.
_begin_limiter = RateLimiter()

#: Cookie names. `nvis_` prefixed so they cannot collide with the legacy Flask `session` cookie
#: while both exist during the FR-18 transition.
ACCESS_COOKIE = "nvis_access"
REFRESH_COOKIE = "nvis_refresh"

#: The refresh cookie is only ever sent to the renewal and logout routes. Scoping it narrows the
#: surface on which the longest-lived credential is presented — an XSS cannot read it either way,
#: but a confused-deputy request to an unrelated endpoint will not carry it.
REFRESH_COOKIE_PATH = "/api/auth"


def _uniform_failure():
    """The single failure response for every authentication outcome.

    Deliberately one function so a future edit cannot make one path distinguishable from another.
    Wrong password, unknown login, expired challenge and replayed token must be indistinguishable;
    the moment they are not, the endpoint is a user-enumeration oracle.
    """
    return jsonify({"ok": False, "error": "authentication failed"}), 401


def _tls_active() -> bool:
    """Whether the connection reached us over TLS, honouring a terminating proxy.

    `request.is_secure` alone is wrong behind a TLS-terminating reverse proxy: the hop into the app
    is plain HTTP, so `Secure` would be dropped and the cookie would travel unencrypted on a
    subsequent plain request. Werkzeug populates `wsgi.url_scheme` from `X-Forwarded-Proto` only
    when a ProxyFix is installed, so the header is consulted directly.
    """
    if request.is_secure:
        return True
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
    return forwarded == "https"


def _set_token_cookies(resp, access: str, refresh: str | None = None) -> None:
    """Attach the session cookies (FR-5a).

    `Secure` is conditional on TLS rather than always-on for one reason: set unconditionally, the
    cookie is silently dropped by the browser on a plain-HTTP deployment and login appears to
    succeed while every subsequent request is anonymous — a failure that reads as "the app logs me
    straight out again" and has no error anywhere.
    """
    secure = _tls_active()
    resp.set_cookie(
        ACCESS_COOKIE, access,
        max_age=tokens.ACCESS_TTL_SECONDS,
        httponly=True, secure=secure, samesite="Lax", path="/",
    )
    if refresh is not None:
        resp.set_cookie(
            REFRESH_COOKIE, refresh,
            max_age=tokens.REFRESH_TTL_SECONDS,
            httponly=True, secure=secure, samesite="Lax", path=REFRESH_COOKIE_PATH,
        )


def _clear_token_cookies(resp) -> None:
    resp.delete_cookie(ACCESS_COOKIE, path="/")
    resp.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _client_key() -> str:
    """Rate-limit key: the resolved client address.

    `core.resolve_client_addr` is the same resolver the audit chokepoint uses, so a forwarded
    address is believed here exactly when it is believed there — one trusted-proxy policy, not two.
    Using the raw peer instead would put every operator behind one NAT in a single bucket.
    """
    addr, _trust = core.resolve_client_addr(
        request.remote_addr,
        request.headers.get("X-Forwarded-For"),
        _common.trusted_networks(),
    )
    return addr or "unknown"


@bp.route("/srp/begin", methods=["POST"])
@action("auth.srp:begin")
def srp_begin():
    """Leg 1. Returns the salt and the server's public value `B`.

    The rate-limit check is the FIRST thing this function does, before the store is touched and
    before `srp.begin` runs its 4096-bit exponentiation. That ordering is FR-4g's actual
    requirement — shedding after the expensive work protects nothing that matters.
    """
    allowed, retry_after = _begin_limiter.check(_client_key())
    if not allowed:
        resp = jsonify({"ok": False, "error": "too many authentication attempts"})
        resp.headers["Retry-After"] = str(max(1, int(retry_after + 0.5)))
        return resp, 429

    body = request.get_json(silent=True) or {}
    login_raw = str(body.get("login") or "").strip()
    a_hex = str(body.get("A") or "").strip()
    if not login_raw or not a_hex:
        return _uniform_failure()

    try:
        login = canonical_login(login_raw)
    except IdentityError:
        # A malformed login is answered like an unknown one: telling the caller their input was
        # rejected for *shape* still separates "could exist" from "could not".
        login = login_raw

    salt_hex = ""
    verifier_hex = ""
    store = None
    try:
        store = IdentityStore()
        user = store.get_user(login)
        # A disabled account takes the decoy path too. Answering differently would confirm the
        # login exists, and FR-6's implicit revocation already refuses its tokens.
        if user is not None and not user.disabled and user.has_credential:
            salt_hex = user.srp_salt
            verifier_hex = user.srp_verifier
    except Exception:
        _logger.exception("srp begin: identity store unavailable")
        return _uniform_failure()
    finally:
        if store is not None:
            store.close()

    try:
        challenge, salt_out, b_hex = srp.begin(login, a_hex, salt_hex, verifier_hex)
    except srp.SrpError:
        return _uniform_failure()
    except Exception:
        _logger.exception("srp begin: unexpected failure")
        return _uniform_failure()

    return jsonify({"ok": True, "token": challenge, "salt": salt_out, "B": b_hex})


@bp.route("/srp/finish", methods=["POST"])
@action("auth.srp:finish")
def srp_finish():
    """Leg 2. Verifies the client's proof and, on success, issues the session cookies."""
    body = request.get_json(silent=True) or {}
    challenge = str(body.get("token") or "").strip()
    m_hex = str(body.get("M") or "").strip()
    if not challenge or not m_hex:
        return _uniform_failure()

    try:
        # The login comes back from the challenge this server minted in leg 1 — never from the
        # request body. A caller that supplied it again here could prove one account's password and
        # name another.
        login, hamk_hex, _session_key = srp.finish(challenge, m_hex)
    except srp.SrpError:
        return _uniform_failure()
    except Exception:
        _logger.exception("srp finish: unexpected failure")
        return _uniform_failure()

    store = None
    try:
        store = IdentityStore()
        user = store.get_user(login)
        if user is None or user.disabled:
            # The decoy path can verify a proof against a synthetic credential; there is no user
            # behind it, and this is where that must not become a session.
            return _uniform_failure()
        must_change = user.must_change_password
        display = user.display_name
    except Exception:
        _logger.exception("srp finish: identity store unavailable")
        return _uniform_failure()
    finally:
        if store is not None:
            store.close()

    principal = principals.user_principal(login)
    access = tokens.mint(principal, tokens.TOKEN_ACCESS)
    refresh = tokens.mint(principal, tokens.TOKEN_REFRESH)

    resp = make_response(jsonify({
        "ok": True,
        # The server's own proof. A client that does not check it has authenticated itself to an
        # unauthenticated server, which is half of what mutual authentication is for.
        "HAMK": hamk_hex,
        "user": {"login": login, "display_name": display, "principal": principal},
        "must_change_password": must_change,
    }))
    _set_token_cookies(resp, access, refresh)
    return resp


@bp.route("/refresh", methods=["POST"])
@action("auth:refresh")
def refresh_session():
    """Renew the access token without a password (FR-5b).

    The renewal path is itself revocable: `tokens.verify` consults the revocation record, so a
    revoked subject cannot refresh its way past a withdrawal. That is what makes a short access TTL
    a bound on staleness rather than an inconvenience.
    """
    presented = request.cookies.get(REFRESH_COOKIE) or ""
    if not presented:
        return _uniform_failure()
    try:
        claims = tokens.verify(presented, tokens.TOKEN_REFRESH)
    except tokens.TokenError:
        return _uniform_failure()

    principal = str(claims.get("sub") or "")
    try:
        kind, login = principals.parse_principal(principal)
    except principals.PrincipalError:
        return _uniform_failure()
    if kind != principals.KIND_USER:
        return _uniform_failure()

    store = None
    try:
        store = IdentityStore()
        user = store.get_user(login)
        if user is None or user.disabled:
            return _uniform_failure()
    except Exception:
        _logger.exception("refresh: identity store unavailable")
        return _uniform_failure()
    finally:
        if store is not None:
            store.close()

    resp = make_response(jsonify({"ok": True, "principal": principal}))
    _set_token_cookies(resp, tokens.mint(principal, tokens.TOKEN_ACCESS))
    return resp


@bp.route("/logout", methods=["POST"])
@action("auth:logout_session")
def logout_session():
    """End this session: revoke the presented tokens by `jti`, then clear the cookies.

    Clearing the cookies alone would be theatre — the tokens stay valid until they expire, and a
    copy taken beforehand still works. Revoking by `jti` is what makes logout mean something for a
    stateless token, and it is why FR-6's fast path exists.
    """
    for cookie, expected in ((ACCESS_COOKIE, tokens.TOKEN_ACCESS),
                             (REFRESH_COOKIE, tokens.TOKEN_REFRESH)):
        raw = request.cookies.get(cookie) or ""
        if not raw:
            continue
        try:
            claims = tokens.verify(raw, expected)
        except tokens.TokenError:
            # Already invalid; nothing to revoke, and this is not an error worth reporting to a
            # caller who is on their way out.
            continue
        try:
            # Whole claims, not a bare jti: the store needs `exp` from the token itself so the
            # revocation row can be pruned once the token would have expired anyway.
            revocation.revoke_token(claims, reason="logout")
        except ValueError:
            # No jti — nothing to revoke individually. Not an error on the way out.
            continue
        except Exception:
            _logger.exception("logout: could not revoke %s", cookie)

    resp = make_response(jsonify({"ok": True}))
    _clear_token_cookies(resp)
    return resp
