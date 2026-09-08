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
"""The OIDC provider endpoints (FR-10 .. FR-14).

``/authorize``, ``/token``, ``/userinfo``, ``/introspect`` and ``/revoke``. All behind the provider
flag; all 404 when it is off.

TWO ERROR VOCABULARIES, ON PURPOSE
-----------------------------------
``/authorize`` either **redirects** an OAuth error back to a *validated* redirect URI, or renders it
directly. Which one is not a style choice: an error may only be sent to a URI that has already been
confirmed as registered for the named client. Redirecting an error to an unvalidated URI is the open
redirect the exact-match rule exists to prevent -- so a bad `client_id` or an unregistered
`redirect_uri` is answered *to the browser*, and everything after that point is redirected.

``/token`` and the rest return RFC 6749 §5.2 JSON error bodies.

WHAT IS NOT HERE
----------------
No consent screen. Clients are operator-registered and first-party (no dynamic registration), so
there is no third party for the user to consent to. If an external relying party is ever registered,
consent belongs here and its absence becomes a defect rather than a simplification.
"""

from __future__ import annotations

import hmac
import logging
from datetime import datetime
from urllib.parse import urlencode

import jwt
import throttle
from audit.actions import action
from flask import Blueprint, g, jsonify, redirect, request
from identity import oidc, principals, tokens
from identity import oidc_clients as clients_mod
from identity import oidc_codes as codes_mod
from identity import revocation as revocation_mod
from identity import store as identity_store
from identity.models import IdentityError

_logger = logging.getLogger(__name__)

bp = Blueprint("oidc_provider", __name__, url_prefix="/oidc")


def _disabled():
    return jsonify({"error": "the OIDC provider is not enabled on this deployment"}), 404


def _oauth_error(code: str, description: str, status: int = 400):
    """RFC 6749 §5.2. `error` is a fixed vocabulary; `error_description` is for a human."""
    return jsonify({"error": code, "error_description": description}), status


def _current_principal() -> str | None:
    """The signed-in operator, or None.

    Read from ``g``, which the auth gate populates after verifying the token -- never from the
    request. A principal taken from anything the caller controls would let an authorization request
    mint a code for somebody else.
    """
    return getattr(g, "auth_principal", None)


# --- /authorize -------------------------------------------------------------------------------


@bp.get("/authorize")
@throttle.throttle(30, rationale="mints a code into a bounded table, so a flood evicts legitimate codes before they can be redeemed. An interactive authorization is a human clicking a button; 30/min is far above that.")
def authorize():
    """Authorization endpoint (FR-11). Issues a code to a registered client for a signed-in user."""
    if not oidc.enabled():
        return _disabled()

    # Before the client lookup, the redirect validation and the code generation -- the point of
    # shedding early is that the expensive work never starts.
    client_id = (request.args.get("client_id") or "").strip()
    redirect_uri = (request.args.get("redirect_uri") or "").strip()

    # Everything up to here is answered to the BROWSER, not redirected: until the client and its
    # redirect URI are both confirmed, there is no URI we are willing to send anything to.
    try:
        client = clients_mod.get(client_id)
    except clients_mod.ClientError:
        return _oauth_error("invalid_request", "unknown client_id")
    if not redirect_uri or not client.permits_redirect(redirect_uri):
        return _oauth_error(
            "invalid_request",
            "redirect_uri is not registered for this client (it is compared exactly)",
        )

    state = request.args.get("state") or ""

    def _redirect_error(code: str, description: str):
        params = {"error": code, "error_description": description}
        if state:
            # Echoed unmodified. It is the client's CSRF defence and only works if returned verbatim.
            params["state"] = state
        return redirect(f"{redirect_uri}?{urlencode(params)}", code=302)

    if (request.args.get("response_type") or "").strip() != "code":
        return _redirect_error("unsupported_response_type", "only response_type=code is supported")

    principal = _current_principal()
    if not principal:
        # Not a redirect to a login page: this deployment's login is the SPA, and inventing a
        # redirect chain here would be guessing at a flow nobody has specified.
        return _oauth_error("login_required", "no authenticated session", status=401)

    try:
        entry = codes_mod.store().issue(
            client_id=client.client_id,
            principal=principal,
            redirect_uri=redirect_uri,
            code_challenge=(request.args.get("code_challenge") or "").strip(),
            code_challenge_method=(request.args.get("code_challenge_method") or "").strip(),
            nonce=request.args.get("nonce"),
            auth_time=getattr(g, "auth_time", None),
            # Honoured rather than discarded. The granted set is pinned to the code here, so what
            # `/userinfo` later releases is decided by this authorization request and not by
            # whatever a token-endpoint caller asks for afterwards.
            scope=" ".join(oidc.parse_scope(request.args.get("scope"))),
        )
    except codes_mod.CodeError as exc:
        return _redirect_error("invalid_request", str(exc))

    params = {"code": entry.code}
    if state:
        params["state"] = state
    return redirect(f"{redirect_uri}?{urlencode(params)}", code=302)


# --- /token -----------------------------------------------------------------------------------


def _authenticated_client(form) -> clients_mod.Client | None:
    """Resolve and, for a confidential client, authenticate the caller."""
    client_id = (form.get("client_id") or "").strip()
    if not client_id and request.authorization:
        client_id = request.authorization.username or ""
    try:
        client = clients_mod.get(client_id)
    except clients_mod.ClientError:
        return None

    if client.is_confidential:
        supplied = form.get("client_secret") or (
            request.authorization.password if request.authorization else None
        )
        if not supplied or not hmac.compare_digest(str(supplied), client.client_secret or ""):
            return None
    return client


@bp.post("/token")
@throttle.throttle(60, rationale="mints three ES256 tokens (~0.03ms each) plus a PKCE digest -- cheap, but it is the credential-issuing endpoint and brute-forcing a code is the threat.")
@action("oidc.token:grant")
def token():
    """Token endpoint (FR-10). `authorization_code` and `refresh_token` grants."""
    if not oidc.enabled():
        return _disabled()

    form = request.form or {}
    grant_type = (form.get("grant_type") or "").strip()

    client = _authenticated_client(form)
    if client is None:
        return _oauth_error("invalid_client", "unknown client, or client authentication failed", 401)

    if grant_type == "authorization_code":
        return _grant_authorization_code(form, client)
    if grant_type == "refresh_token":
        return _grant_refresh_token(form, client)
    return _oauth_error(
        "unsupported_grant_type",
        "supported grant types are authorization_code and refresh_token",
    )


def _grant_authorization_code(form, client: clients_mod.Client):
    code = (form.get("code") or "").strip()
    try:
        entry = codes_mod.store().redeem(
            code=code,
            client_id=client.client_id,
            redirect_uri=(form.get("redirect_uri") or "").strip(),
            code_verifier=(form.get("code_verifier") or "").strip(),
        )
    except codes_mod.CodeError as exc:
        return _oauth_error("invalid_grant", str(exc))

    # Access token first: `at_hash` binds the id token to it, so it must already exist.
    access = tokens.mint(
        entry.principal, tokens.TOKEN_ACCESS, auth_time=entry.auth_time, scope=entry.scope
    )
    id_token = tokens.mint(
        entry.principal,
        tokens.TOKEN_ID,
        auth_time=entry.auth_time,
        claims=_id_token_claims(entry, client) | {"at_hash": oidc.at_hash(access)},
    )
    refresh = tokens.mint(
        entry.principal, tokens.TOKEN_REFRESH, auth_time=entry.auth_time, scope=entry.scope
    )

    # Recorded so a replay of this code can withdraw them (FR-11a). Done after minting, so a
    # failure part-way leaves nothing claiming to exist that does not.
    codes_mod.store().record_minted(
        code, [_peek(t) for t in (id_token, access, refresh)]
    )

    return jsonify({
        "access_token": access,
        "id_token": id_token,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": tokens.access_ttl(),
        "scope": entry.scope,
    })


def _grant_refresh_token(form, client: clients_mod.Client):
    presented = (form.get("refresh_token") or "").strip()
    try:
        claims = tokens.verify(presented, tokens.TOKEN_REFRESH)
    except tokens.TokenError:
        return _oauth_error("invalid_grant", "the refresh token is not valid")

    principal = str(claims.get("sub") or "")
    if not _subject_still_enabled(principal):
        return _oauth_error("invalid_grant", "the subject is no longer able to authenticate")

    # Never wider than the original grant (RFC 6749 §6). The scope comes off the presented refresh
    # token, so a client asking for more here narrows rather than escalates.
    granted = oidc.narrow_scope(claims.get("scope"), form.get("scope"))
    access = tokens.mint(
        principal,
        tokens.TOKEN_ACCESS,
        auth_time=claims.get("auth_time"),
        scope=" ".join(granted),
    )
    return jsonify({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": tokens.access_ttl(),
        "scope": " ".join(granted),
    })


def _id_token_claims(entry, client: clients_mod.Client) -> dict:
    """`azp` and the echoed `nonce` (FR-7, FR-9)."""
    extra: dict = {"azp": client.client_id}
    if entry.nonce is not None:
        # Verbatim, and treated as an opaque string. It is the client's replay defence and only
        # works if it comes back exactly as sent.
        extra["nonce"] = entry.nonce
    return extra


def _peek(token_str: str) -> dict:
    """Claims of a token we just minted, without re-verifying it.

    Safe here and nowhere else: this token was produced by `mint` three lines ago, so there is
    nothing to authenticate. Anywhere a token arrives from outside, it goes through `verify`.
    """
    return jwt.decode(token_str, options={"verify_signature": False})


def _subject_still_enabled(principal: str) -> bool:
    try:
        _kind, login = principals.parse_principal(principal)
    except (IdentityError, ValueError):
        return False
    store = None
    try:
        store = identity_store.IdentityStore()
        user = store.get_user(login)
        return user is not None and not user.disabled
    except Exception:
        _logger.exception("oidc: identity store unavailable during grant")
        return False
    finally:
        if store is not None:
            store.close()


# --- /userinfo --------------------------------------------------------------------------------


def _bearer_claims():
    """Claims of the presented access token, or None.

    Access tokens only. An id token presented here is refused (FR-12a): it is a claims carrier
    handed to the browser, frequently logged by clients, and was never meant to be a credential.
    """
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        return tokens.verify(header[7:].strip(), tokens.TOKEN_ACCESS)
    except tokens.TokenError:
        return None


@bp.get("/userinfo")
@throttle.throttle(120, rationale="verification plus an identity-store read, so it touches disk where introspection does not.")
def userinfo():
    """UserInfo endpoint (FR-12). The bearer's own claims, and nobody else's."""
    if not oidc.enabled():
        return _disabled()

    claims = _bearer_claims()
    if claims is None:
        resp = jsonify({"error": "invalid_token"})
        resp.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        return resp, 401

    principal = str(claims.get("sub") or "")
    # The subject comes from the *token*, never from a query parameter. Accepting a caller-supplied
    # subject here would turn this into a directory that any authenticated user could read.
    try:
        _kind, login = principals.parse_principal(principal)
    except (IdentityError, ValueError):
        return jsonify({"error": "invalid_token"}), 401

    store = None
    try:
        store = identity_store.IdentityStore()
        user = store.get_user(login)
    except Exception:
        _logger.exception("oidc: identity store unavailable at userinfo")
        return jsonify({"error": "server_error"}), 500
    finally:
        if store is not None:
            store.close()

    if user is None or user.disabled:
        return jsonify({"error": "invalid_token"}), 401

    return jsonify(_claims_for_scope(principal, user, str(claims.get("scope") or "")))


def _claims_for_scope(principal: str, user, scope: str) -> dict:
    """The claims this token's scope releases, and no others (§5.4).

    Built from `oidc.SCOPE_CLAIMS` rather than an inline list, so the discovery document's
    `claims_supported` and what this endpoint actually returns are derived from one table and cannot
    drift apart.

    Empty values are omitted rather than sent as `""`. A relying party cannot distinguish "we hold
    no family name" from "their family name is the empty string", and §5.3.2 permits omission, so
    saying nothing is the honest encoding.
    """
    available: dict[str, object] = {
        "sub": principal,
        "preferred_username": user.login,
        "name": user.display_name or user.login,
        "given_name": user.given_name,
        "family_name": user.family_name,
        "zoneinfo": user.zoneinfo,
        "locale": user.locale,
        "email": user.email,
        "email_verified": user.email_verified,
        "phone_number": user.phone_number,
        "phone_number_verified": user.phone_number_verified,
        # §5.1 defines this as a NumericDate -- seconds since the epoch -- while the record stores
        # an ISO-8601 string. Returning the stored form under this name would be well-formed and
        # wrong, so it is converted, and dropped entirely if it cannot be parsed.
        "updated_at": _numeric_date(user.updated_at),
    }

    granted = {s for s in str(scope or "").split()} or set(oidc.DEFAULT_SCOPE)
    released: dict[str, object] = {}
    for name in ("openid", *oidc.SCOPE_CLAIMS):
        if name != "openid" and name not in granted:
            continue
        for claim in oidc.SCOPE_CLAIMS.get(name, ()):
            value = available.get(claim)
            # `False` is a real answer for the two `*_verified` booleans and must survive; only
            # absent strings and unparseable dates are dropped.
            if value is None or value == "":
                continue
            released[claim] = value
    return released


def _numeric_date(iso: str) -> float | None:
    """An ISO-8601 stamp as OIDC's NumericDate, or ``None`` when it cannot be read."""
    text = str(iso or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# --- introspection and revocation ---------------------------------------------------------------


@bp.post("/introspect")
@throttle.throttle(120, rationale="one token verification; client-authenticated.")
@action("oidc.token:introspect")
def introspect():
    """RFC 7662. Client-authenticated, because otherwise it is a token-validity oracle (FR-13)."""
    if not oidc.enabled():
        return _disabled()

    form = request.form or {}
    client = _authenticated_client(form)
    if client is None:
        return _oauth_error("invalid_client", "introspection requires client authentication", 401)

    presented = (form.get("token") or "").strip()
    for kind in (tokens.TOKEN_ACCESS, tokens.TOKEN_REFRESH, tokens.TOKEN_ID):
        try:
            claims = tokens.verify(presented, kind)
        except tokens.TokenError:
            continue
        return jsonify({
            "active": True,
            "sub": claims.get("sub"),
            "token_use": kind,
            "exp": claims.get("exp"),
            "iat": claims.get("iat"),
            "jti": claims.get("jti"),
            "iss": claims.get("iss"),
            "scope": claims.get("scope"),
        })

    # RFC 7662 §2.2: an invalid token is `active: false`, not an error. Anything else would let a
    # caller distinguish "malformed" from "expired" from "revoked", which is more than they need.
    return jsonify({"active": False})


@bp.post("/revoke")
@throttle.throttle(120, rationale="one verification plus a store write; client-authenticated.")
@action("oidc.token:revoke")
def revoke():
    """RFC 7009 (FR-13a). Always 200, per §2.2."""
    if not oidc.enabled():
        return _disabled()

    form = request.form or {}
    client = _authenticated_client(form)
    if client is None:
        return _oauth_error("invalid_client", "revocation requires client authentication", 401)

    presented = (form.get("token") or "").strip()
    for kind in (tokens.TOKEN_REFRESH, tokens.TOKEN_ACCESS, tokens.TOKEN_ID):
        try:
            claims = tokens.verify(presented, kind)
        except tokens.TokenError:
            continue
        try:
            revocation_mod.revoke_token(claims, reason=f"revoked by client {client.client_id}")
        except Exception:
            _logger.exception("oidc: revocation failed")
            return jsonify({"error": "server_error"}), 500
        break

    # 200 whether or not the token was recognised. RFC 7009 §2.2 is explicit, and the reason is
    # sound: an unknown token is already in the state the caller asked for, and distinguishing the
    # two would turn this into the oracle that introspection deliberately authenticates.
    return jsonify({}), 200
