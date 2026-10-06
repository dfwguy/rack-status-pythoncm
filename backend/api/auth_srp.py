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
import time

import core
import throttle
from audit.actions import action
from flask import Blueprint, g, jsonify, make_response, request
from identity import oidc, principals, revocation, srp, tokens
from identity.models import IdentityError, canonical_login
from identity.store import IdentityStore

from . import _common

_logger = logging.getLogger("rack_web.api.auth_srp")

bp = Blueprint("auth_srp", __name__, url_prefix="/api/auth")

#: One limiter for the whole module, keyed by resolved client address. Module-level because the
#: bucket state must outlive a request; `--workers 1` is already required by the SRP challenge
#: table, so per-process is per-deployment here.

#: Cookie names. `nvis_` prefixed so they cannot collide with the legacy Flask `session` cookie
#: while both exist during the FR-18 transition.
ACCESS_COOKIE = "nvis_access"
REFRESH_COOKIE = "nvis_refresh"

#: The refresh cookie is only ever sent to the renewal and logout routes. Scoping it narrows the
#: surface on which the longest-lived credential is presented — an XSS cannot read it either way,
#: but a confused-deputy request to an unrelated endpoint will not carry it.
REFRESH_COOKIE_PATH = "/api/auth"


def _current_login() -> str:
    """The signed-in user's login, or empty. Read from the gate, never re-verified here.

    This used to fall back to ``web_auth.authenticated_principal(...)`` when ``g`` was empty, and
    the fallback existed for one reason: ``RACK_WEB_AUTH_DISABLED``, where the gate did not run.
    That switch was removed on 2026-09-08, and the fallback became unreachable — the only callers
    are ``/password/begin`` and ``/password/finish``, neither of which is in
    ``web_auth.AUTH_BYPASS_ENDPOINTS``, so the gate has always populated ``g`` before they execute.

    Removed rather than re-explained, because a second verification path is not merely dead weight:
    it contradicts a rule the product states elsewhere. ``audit.request_hook._principal`` reads the
    subject the gate *already verified* and says why — "one verification per request means the trail
    cannot disagree with the gate about who the caller was". A route that verifies again could
    disagree, and the one that changes passwords is a poor place to find out.

    Still fail-closed: no principal on ``g`` means no login, and the password routes refuse.
    """
    principal = str(getattr(g, "auth_principal", "") or "")
    if not principal:
        return ""
    try:
        kind, login = principals.parse_principal(principal)
    except principals.PrincipalError:
        return ""
    return login if kind == principals.KIND_USER else ""


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
        max_age=tokens.access_ttl(),
        httponly=True, secure=secure, samesite="Lax", path="/",
    )
    if refresh is not None:
        resp.set_cookie(
            REFRESH_COOKIE, refresh,
            max_age=tokens.refresh_ttl(),
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
@throttle.throttle(10, rationale="~52ms of CPU per call: a 4096-bit SRP exponentiation that FR-4b makes unavoidable for every caller. One worker saturates a core at ~19 req/s, so this is the one route where the limit is the only thing between an anonymous caller and the CPU.")
@action("auth.srp:begin")
def srp_begin():
    """Leg 1. Returns the salt and the server's public value `B`.

    **Shedding happens before this function is entered**, in `app._enforce_request_throttle`, so the
    4096-bit exponentiation below is never reached by a refused caller. That ordering is FR-4g's
    actual requirement — shedding after the expensive work protects nothing that matters.

    This route used to carry its own `RateLimiter`. It was removed when the global throttle landed:
    two limiters on one route consume separately, so the effective allowance was half of either
    declared value, and neither number described the behaviour. The limit now lives in the
    `@throttle.throttle` declaration above, where the rationale sits next to it.
    """
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
@throttle.throttle(20, rationale="also SRP math; 20 rather than 10 because one login is a begin AND a finish, plus room for a retry.")
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
    # One authentication event, one `auth_time` — sampled once and given to both tokens.
    #
    # Letting each `mint` stamp its own "now" is the obvious form and it is wrong. The two calls are
    # milliseconds apart, so the access and refresh tokens disagree about when the user
    # authenticated; refresh then propagates the *refresh* token's value and `auth_time` creeps
    # forward by a few milliseconds on every renewal. Measured, not theorised — a millisecond
    # comparison caught a 3 ms drift that a whole-second assertion would have called equal, and
    # hourly renewal over a long session compounds it.
    authenticated_at = time.time()
    access = tokens.mint(principal, tokens.TOKEN_ACCESS, auth_time=authenticated_at)
    refresh = tokens.mint(principal, tokens.TOKEN_REFRESH, auth_time=authenticated_at)

    body = {
        "ok": True,
        # The server's own proof. A client that does not check it has authenticated itself to an
        # unauthenticated server, which is half of what mutual authentication is for.
        "HAMK": hamk_hex,
        "user": {"login": login, "display_name": display, "principal": principal},
        "must_change_password": must_change,
        # Control-plane metadata the SPA holds alongside the (cookie-sealed) token: when this access
        # token expires, so the client can renew ahead of a 401 from its very first request. Derived
        # from the same `authenticated_at` the token was minted with.
        "access_expires_at": authenticated_at + tokens.access_ttl(),
    }

    id_token = _mint_id_token(principal, access, authenticated_at)
    if id_token is not None:
        body["id_token"] = id_token

    resp = make_response(jsonify(body))
    _set_token_cookies(resp, access, refresh)
    return resp


def _mint_id_token(principal: str, access: str, authenticated_at: float) -> str | None:
    """The id token for a local login (FR-5), or ``None`` in legacy mode.

    **Three tokens, which is what the requirement asked for and what a Cognito-shaped client
    expects.** The login used to mint only access and refresh, so FR-5's "id token and access token"
    was unmet while `tokens.py` supported `TOKEN_ID` perfectly well — the login handler simply never
    called it.

    **It goes in the response body, not a cookie, and that is the standard's intent.** An id token
    is a statement *to the client* about who signed in; a client that cannot read it has been sent
    nothing. The access and refresh tokens are the opposite — they are credentials, so they stay
    `HttpOnly` where script cannot reach them.

    That split is only safe because an id token authorizes nothing here: `verify` demands an exact
    token type and the gate asks for `TOKEN_ACCESS`, so presenting this as a bearer is refused.
    `test_id_token_is_not_a_credential.py` asserts exactly that, and it is what makes readable
    delivery a considered choice rather than a leak.

    `at_hash` binds it to the access token issued in the same breath (OIDC Core §3.1.3.6), so a
    client can tell the pair apart from an id token replayed beside a different access token.

    Returns ``None`` in legacy HS256 mode rather than raising: an id token that no `kid` identifies
    and no JWKS describes is not usable by a conforming client, and a first-party SPA reads its user
    from the `user` object above regardless. Callers must treat the field as optional.
    """
    if not oidc.enabled():
        return None
    try:
        return tokens.mint(
            principal,
            tokens.TOKEN_ID,
            auth_time=authenticated_at,
            claims={"at_hash": oidc.at_hash(access)},
        )
    except tokens.TokenError:
        # A login that succeeded must not fail because an *optional* token could not be minted —
        # the session cookies are already the credential. Logged, not raised.
        _logger.warning("srp finish: could not mint an id token", exc_info=True)
        return None


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

    # The new access token's absolute expiry, returned so the SPA can pre-check validity before its
    # *next* request and renew ahead of a 401 rather than after one. Computed from the same clock and
    # TTL the mint uses; the SPA applies a skew margin, so sub-millisecond drift is immaterial.
    access_expires_at = time.time() + tokens.access_ttl()
    resp = make_response(
        jsonify({"ok": True, "principal": principal, "access_expires_at": access_expires_at})
    )
    # `auth_time` carries forward from the refresh token rather than being re-stamped (FR-8).
    #
    # This is the whole reason the parameter exists. A renewed access token whose `auth_time` is
    # "now" asserts the user authenticated a moment ago, when the SRP exchange may have been weeks
    # earlier — and with silent hourly renewal, "weeks earlier" is the normal case. The value is
    # well-formed either way, so a relying party asking `max_age` gets a plausible answer that is
    # simply untrue, and nothing anywhere logs a problem.
    _set_token_cookies(
        resp,
        tokens.mint(principal, tokens.TOKEN_ACCESS, auth_time=claims.get("auth_time")),
    )
    return resp


@bp.route("/password/begin", methods=["POST"])
@action("auth.password:begin")
def password_change_begin():
    """Leg 1 of a password change: prove you know the *current* one.

    **Being logged in is not enough to change a password.** A session that has been stolen — a
    borrowed laptop, a token lifted before `HttpOnly` was in place — could otherwise be used to
    lock the real owner out of their own account, silently and permanently. Requiring the current
    password means possession of a session is not possession of the account.

    This is the login exchange again, deliberately: same two legs, same primitives, no second
    implementation of SRP to keep in step with the first. The difference is that the login is taken
    from the verified token rather than from the request, so a caller can only re-prove *their own*
    password.
    """
    login = _current_login()
    if not login:
        return _uniform_failure()

    body = request.get_json(silent=True) or {}
    a_hex = str(body.get("A") or "").strip()
    if not a_hex:
        return _uniform_failure()

    store = None
    try:
        store = IdentityStore()
        user = store.get_user(login)
        if user is None or user.disabled or not user.has_credential:
            return _uniform_failure()
        salt_hex, verifier_hex = user.srp_salt, user.srp_verifier
    except Exception:
        _logger.exception("password begin: identity store unavailable")
        return _uniform_failure()
    finally:
        if store is not None:
            store.close()

    try:
        challenge, salt_out, b_hex = srp.begin(login, a_hex, salt_hex, verifier_hex)
    except srp.SrpError:
        return _uniform_failure()
    return jsonify({"ok": True, "token": challenge, "salt": salt_out, "B": b_hex})


@bp.route("/password/finish", methods=["POST"])
@action("auth.password:change")
def password_change_finish():
    """Leg 2: verify the current-password proof, then install the new verifier.

    **The new password never reaches the server either.** The client derives the new salt and
    verifier locally and sends only those, exactly as enrolment does — so a password change has the
    same disclosure properties as a login, and the server still holds nothing reversible (FR-3).

    On success every existing session is revoked. That is the point of changing a password after a
    suspected compromise: leaving the attacker's token valid for the rest of its lifetime would make
    the change cosmetic. The caller is logged out too, which is the honest consequence — "log out
    everywhere" includes here.
    """
    login = _current_login()
    if not login:
        return _uniform_failure()

    body = request.get_json(silent=True) or {}
    challenge = str(body.get("token") or "").strip()
    m_hex = str(body.get("M") or "").strip()
    new_salt = str(body.get("new_salt") or "").strip()
    new_verifier = str(body.get("new_verifier") or "").strip()
    if not (challenge and m_hex and new_salt and new_verifier):
        return _uniform_failure()

    if not _is_hex(new_salt) or not _is_hex(new_verifier):
        # The values go straight into the credential store; a non-hex string there would be a
        # credential nobody can ever authenticate against, i.e. a lockout written by the client.
        return jsonify({"ok": False, "error": "malformed credential"}), 400

    try:
        proved_login, _hamk, _key = srp.finish(challenge, m_hex)
    except srp.SrpError:
        return _uniform_failure()

    if proved_login != login:
        # The challenge belongs to a different account than the token. Cannot happen through the
        # routes above; refusing anyway, because the alternative is changing someone else's
        # password on the strength of a proof about your own.
        _logger.error("password change: challenge login %r != token login %r", proved_login, login)
        return _uniform_failure()

    store = None
    try:
        store = IdentityStore()
        user = store.get_user(login)
        if user is None or user.disabled:
            return _uniform_failure()
        store.update_user(
            user.edited(
                srp_salt=new_salt,
                srp_verifier=new_verifier,
                must_change_password=False,
            )
        )
    except Exception:
        _logger.exception("password change: could not write the new credential")
        return jsonify({"ok": False, "error": "could not change password"}), 500
    finally:
        if store is not None:
            store.close()

    try:
        revocation.revoke_all_for(principals.user_principal(login))
    except Exception:
        _logger.exception("password change: sessions were not revoked")

    resp = make_response(jsonify({"ok": True, "reauthenticate": True}))
    _clear_token_cookies(resp)
    return resp


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return bool(value)


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
