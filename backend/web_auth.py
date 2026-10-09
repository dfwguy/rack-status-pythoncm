# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Dashboard authentication mechanism: modes, credential verification, session state.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The *mechanism*, deliberately separate from both the endpoints that use it (``api/auth.py``) and the
gate that enforces it (``_require_dashboard_auth`` in ``app.py``). One module answers "how does this
app decide who you are", which is the question the coming authorization work has to build on.

One mechanism, not two. :func:`authenticated_principal` verifies a signed access token -- the
``nvis_access`` cookie first, then an ``Authorization: Bearer`` header -- and nothing else. The
shared-token and PAM paths were **deleted** on 2026-09-01 (FR-18), not deprecated; the note above
granting nothing was the worse option to leave standing.

Authentication has no off switch. There is no configuration input that disables it,
and it turns the gate off entirely rather than selecting a weaker credential.

Not an endpoint module and not under ``api/``: it imports Flask only for ``session``, and nothing here
builds a response.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlsplit

from config_env import _env_int

_logger = logging.getLogger(__name__)


#: The cookie a browser carries the access token in. Duplicated from ``api/auth_srp`` rather than
#: imported: the dependency runs ``app.py -> api/<domain>.py -> core.py`` in one direction only
#: (core-designs.mdc §0), and the gate importing a route module would invert it.
ACCESS_COOKIE = "nvis_access"

#: The cookie carrying the refresh token. Duplicated from ``api/auth_srp`` for the same reason.
REFRESH_COOKIE = "nvis_refresh"

#: The routes whose credential is the **refresh token**, and the only places the gate accepts one.
#:
#: Renewal exists for the moment the access token has gone, so a gate that demands a live access
#: token before renewal can run demands the one credential renewal replaces. That is how these two
#: routes behaved until 2026-10-09: the SPA renews on a timer ahead of expiry and never noticed,
#: while `nvrs` renews after a 401 and was refused every time — a 7-day refresh token that lasted
#: one hour. Logout was worse, silently: past the hour it was refused before it could revoke the
#: refresh token, so "logged out" left a renewable credential behind.
#:
#: **Accepted here rather than exempted.** Listing these in :data:`AUTH_BYPASS_ENDPOINTS` would also
#: have worked, but would make them reachable with no credential at all and audit every renewal as
#: `anonymous`. This keeps every request authenticated, records the real subject, and throttles per
#: principal — the same shape as flask-jwt-extended's ``@jwt_required(refresh=True)``.
#:
#: **Only here, and only from the cookie.** A refresh token honoured on any other route would be a
#: 7-day access token, which is the type confusion ``tokens.verify``'s mandatory ``expect_type``
#: exists to prevent (FR-5c). And never from ``Authorization``: the browser cannot read its HttpOnly
#: refresh cookie to put it in a header, and the cookie's ``Path=/api/auth`` keeps the longest-lived
#: credential off every other request and out of header logs.
REFRESH_CREDENTIAL_ENDPOINTS: frozenset[str] = frozenset({
    "auth_srp.refresh_session",
    "auth_srp.logout_session",
})


def bearer_token(headers: object) -> str:
    """The token from an ``Authorization: Bearer`` header, or empty.

    Programmatic clients — CI, ``curl``, the deploy scripts — cannot hold a cookie jar, so the
    header is their path. A browser uses the ``HttpOnly`` cookie instead (FR-5a). Both end at the
    same verification, so there is one notion of a valid caller rather than two.
    """
    try:
        raw = (headers.get("Authorization") or "").strip()  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        return ""
    scheme, _, value = raw.partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


def authenticated_principal(headers: object, cookies: object = None) -> str:
    """The verified principal for this request, or ``""`` when there is none.

    **The principal comes from a signed token and from nowhere else** (FR-16). Not from
    ``X-Rack-Operator``, which is self-reported display metadata; not from a query parameter. A
    principal the caller can choose turns the activity ledger and the rack locks into decoration,
    and turns authorization into a suggestion.

    Returns a plain string rather than a ``(principal, method)`` tuple, and that is a deliberate
    guard: a tuple is *always truthy*, so the natural ``if principal:`` at the call site would admit
    every anonymous request. The suite runs with the gate disabled, so no existing test would have
    caught it — this shape makes the mistake unavailable.

    Cookie before header. The order matters only for a client sending both, and preferring the
    cookie means a stale ``Authorization`` left by a fetch wrapper cannot displace a live session.
    """
    return str(verified_claims(headers, cookies).get("sub") or "").strip()


def verified_claims(headers: object, cookies: object = None) -> dict:
    """Every claim from this request's verified access token, or ``{}`` when there is none.

    **A dict, and specifically not a tuple.** ``authenticated_principal`` returns a bare string
    because a ``(principal, method)`` tuple is *always truthy*, so the natural ``if principal:`` at
    a call site would admit every anonymous request. An empty dict is falsy, so ``if claims:``
    carries the same protection while giving the caller the rest of the token.

    That matters because a claim other than ``sub`` is sometimes load-bearing. ``auth_time`` is the
    case that forced this to exist: the OIDC authorization endpoint has to stamp an issued code with
    **when the operator actually authenticated**, and it was reading ``g.auth_time``, which nothing
    ever set — so `CodeStore.issue` fell back to "now" and every id token from the code flow claimed
    the user had just signed in. A relying party's ``max_age`` check was then answered with a
    plausible, well-formed lie. Discarding the claims here is what made that unavoidable.
    """
    from identity import tokens  # local: keeps identity off the import path of a bare web_auth

    candidates: list[str] = []
    try:
        cookie_value = cookies.get(ACCESS_COOKIE) if cookies is not None else None  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        cookie_value = None
    if cookie_value:
        candidates.append(str(cookie_value))
    header_value = bearer_token(headers)
    if header_value:
        candidates.append(header_value)

    for raw in candidates:
        try:
            claims = tokens.verify(raw, tokens.TOKEN_ACCESS)
        except tokens.TokenError:
            # Every refusal reason is equivalent here — expired, revoked, forged, wrong type.
            # INV-01..INV-19 hold the taxonomy; saying which one applied would tell a caller whether
            # they hold a real token that is merely stale.
            continue
        except Exception:
            _logger.exception("token verification raised; treating as unauthenticated")
            continue
        if str(claims.get("sub") or "").strip():
            return dict(claims)
    return {}


def verified_refresh_claims(endpoint: str | None, cookies: object = None) -> dict:
    """The verified refresh-token claims for a route in :data:`REFRESH_CREDENTIAL_ENDPOINTS`, or ``{}``.

    The gate asks this only after :func:`verified_claims` found no access token. Every other
    endpoint gets ``{}`` without the cookie being read, so a refresh token can never stand in for
    an access token anywhere else. Verified as ``TOKEN_REFRESH`` — signature, issuer, audience,
    expiry, type and revocation, exactly as the route itself verifies it — so this admits nothing
    the route would not.
    """
    if endpoint not in REFRESH_CREDENTIAL_ENDPOINTS:
        return {}
    from identity import tokens  # local, as in verified_claims

    try:
        raw = cookies.get(REFRESH_COOKIE) if cookies is not None else None  # type: ignore[union-attr]
    except (AttributeError, TypeError):
        raw = None
    if not raw:
        return {}
    try:
        claims = tokens.verify(str(raw), tokens.TOKEN_REFRESH)
    except tokens.TokenError:
        return {}  # uniform, for the reason given in verified_claims
    except Exception:
        _logger.exception("refresh token verification raised; treating as unauthenticated")
        return {}
    return dict(claims) if str(claims.get("sub") or "").strip() else {}


# --- Removed 2026-09-01: shared-token and PAM authentication (FR-18) ------------------------------
#
# `_auth_mode`, `_auth_token`, `_pam_service`, `_pam_verify`, `_token_verify` and `_session_ok` are
# gone, along with `RACK_WEB_AUTH_MODE`, `RACK_WEB_AUTH_TOKEN`, `RACK_WEB_PAM_SERVICE` and the
# vendored `rack_web/pam_*` bindings. Every caller authenticates through `authenticated_principal`
# above, which verifies a signed access token and nothing else.
#
# **They were deleted rather than deprecated**, and the reason is specific: after the gate moved to
# tokens, `POST /api/login` still answered `200 {"ok": true}` and set a Flask session the gate no
# longer honoured. An operator logged in, saw a dashboard shell, and watched every panel return 401
# with nothing on screen explaining why. A credential path that succeeds and grants nothing is worse
# than an absent one, because the failure surfaces far from its cause.
#
# Authentication cannot be switched off. `RACK_WEB_AUTH_DISABLED` and the latch that kept it off
# real deployments were both removed on 2026-09-08: it was exported by `start.sh` on macOS and
# hard-assigned by 25 test modules, and since the environment is process-global and pytest imports
# every test module during collection, one assignment turned the gate off for the whole suite. The
# product's own tests never authenticated (SUTP G-10). No guard fixes a switch whose problem is
# that it works; `identity/testing.py` replaces it by issuing real credentials.


def _session_lifetime() -> timedelta:
    """Kept for the Flask session cookie's own lifetime, which still carries CSRF and flash state.

    It no longer bounds *authentication* — an access token's TTL does that (`tokens.ACCESS_TTL_SECONDS`),
    and a refresh token's bounds how long a session can be renewed without a password.
    """
    h = _env_int("RACK_WEB_SESSION_HOURS", 8)
    h = max(h, 1)
    h = min(h, 24 * 30)
    return timedelta(hours=h)


def _safe_next(raw: str | None) -> str:
    s = (raw or "").strip().split("#", 1)[0]
    if not s.startswith("/") or s.startswith("//"):
        return ""
    return s


# ---------------------------------------------------------------------------
# Below the Flask router: the same gate, reachable from WSGI middleware.
#
# ``@app.before_request`` only runs for requests Flask routes. ``/ws/terminal`` is served by
# middleware wrapping ``app.wsgi_app`` (``api/terminal.py``), so it never saw the gate at all --
# the CRITICAL finding of the 2026-08 assessment, where anonymous WebSocket upgrades were
# accepted while ``GET /api/status`` correctly returned 401 on the same server.
#
# Evaluating the credential needs the Flask app, to open a request context and unseal the signed
# session cookie. But neither this module nor anything under ``api/`` may import ``app``, or the
# import cycle that ``backend/api/`` exists to remove comes back. So ``app.py`` registers its
# implementation here at import time, exactly as it registers
# ``config_env.set_materialize_hook`` (``core-designs.mdc`` §5).
# ---------------------------------------------------------------------------

#: Protection space named in the `WWW-Authenticate` challenge. One realm: there is one gate.
AUTH_REALM = "nvis-rack-status"

#: Endpoints the gate does **not** protect. The login routes, the auth probe the SPA polls before it
#: has a session, and Flask's static handler.
#:
#: It lives here rather than in `app.py` because two other places legitimately need it — the gate
#: itself, and the OpenAPI generator, which must know whether an operation can return 401 — and
#: `backend/openapi.py` reaching into `app` for it was a deferred `import app` inside a function
#: body, which `test_no_deferred_app_lookups.py` correctly rejected. Auth policy belongs with the
#: auth mechanism; keeping the only copy here is what stops the spec drifting from the gate.
#:
#: Adding to this list makes an endpoint reachable with no credential. That is almost never right —
#: `api_login` is in it because it is how a credential is obtained in the first place.
AUTH_BYPASS_ENDPOINTS: tuple[str, ...] = (
    "auth.login",
    # The documentation site, and this is a **product decision rather than a concession**: the
    # documentation is public information, it is the same text anybody can read in the
    # repository, and gating it bought nothing while costing a great deal.
    #
    # What it cost is worth recording, because the gated version looked correct at every step.
    # `/docs/` is a server route, so a plain navigation never passes through the SPA's fetch
    # layer and never gets its transparent refresh — the link worked immediately after signing
    # in and showed the login page an hour later. Then, past that, an administrators policy
    # written before the `docs` resource type existed refused administrators outright, rendering
    # `{"error":"not authorized","ok":false,"reason":"policy"}` into the viewport. Two failure
    # modes, both confusing, guarding text that is not secret.
    #
    # **The bar for this list is "safe because", not "convenient because".** These two routes
    # serve bytes from `static/docs`, a Sphinx build artifact: no request body, no query
    # parameters, no store, no credential read, no `Set-Cookie`, and every path resolved through
    # `send_from_directory`, which refuses to escape the directory. There is nothing here for an
    # unauthenticated caller to reach that the documentation does not already tell them.
    # `test_docs_are_public.py` asserts each of those claims, so the entry cannot outlive them.
    #
    # The one risk that *is* real is same-origin content injection — anything served from this
    # origin can script against it — which is why the answer is "static files built from our own
    # sources" rather than "user-supplied content", and why the docs responses relax framing to
    # `SAMEORIGIN` rather than dropping the header.
    "docs_site.serve_docs",
    # `GET /api/about` — product name and build version, and nothing else. It exists so the
    # public documentation can name the build it describes without an authenticated call: the
    # only endpoint carrying the version was `/api/app-stats`, which is gated, so a public page
    # was reaching into the auth system for one string.
    #
    # Against this list's bar: no request body, no query parameters, no store read of any kind
    # (one pure function), no credential read, no `Set-Cookie`. Every field it returns is already
    # public on a deployment that serves the documentation. What it must never carry — cluster
    # identity, customer, project, hostnames, counts, configuration, paths — is enumerated in the
    # module docstring and asserted by `test_about_is_public.py`, which fails on an added key, so
    # this entry cannot outlive the claims that justify it.
    "about.api_about",
    # `auth.api_login` is gone with FR-18. Its removal from this tuple matters as much as its
    # removal from the router: an entry naming a route that no longer exists is dead weight that
    # reads as an intentional exemption to the next person auditing this list.
    "auth.api_auth_check",
    # The SRP exchange is how a caller obtains a credential, so it cannot require one. Both legs
    # are listed: leg 2 presents a challenge token, not a session, and gating it would make login
    # impossible rather than merely awkward.
    #
    # These are the only additions this phase makes to the bypass set, and the bar for a fourth is
    # high — an entry here is a route reachable by anyone who can reach the port. `/refresh` is
    # deliberately NOT here: it authenticates with the refresh cookie and must be refused without
    # one.
    "auth_srp.srp_begin",
    "auth_srp.srp_finish",
    # Phase 2.5 (FR-6b). OIDC obliges both to be reachable without a credential — a client cannot
    # authenticate before it has discovered how to, and cannot verify a token before it has the
    # key. Neither carries anything secret: one describes endpoints, the other publishes keys whose
    # purpose is publication. Both 404 unless the provider is enabled, so on a default deployment
    # these entries name routes that answer nothing.
    #
    # These are the ONLY additions Phase 2.5 makes to this set, which is itself a requirement
    # rather than a coincidence: everything else the provider serves takes a credential.
    "oidc.openid_configuration",
    "oidc.jwks",
    # The OAuth endpoints, and these are a DIFFERENT CATEGORY to everything above.
    #
    # The entries above are genuinely unauthenticated: anyone who can reach the port gets an answer.
    # These four are not — they authenticate by a mechanism this gate does not implement. `/token`,
    # `/introspect` and `/revoke` authenticate the *client* (secret or HTTP Basic); `/userinfo`
    # authenticates the *bearer* of an access token. The gate only understands the dashboard session
    # cookie, so leaving them gated would refuse a correctly-credentialled OAuth client before it
    # could present anything.
    #
    # **A bypass entry is only safe because the route authenticates for itself**, which is exactly
    # the kind of claim that rots. `test_oidc_provider.py` asserts each of these four refuses an
    # unauthenticated caller, so the entry cannot outlive the check that justifies it.
    #
    # `/authorize` is deliberately NOT here: it needs a signed-in operator, which is precisely what
    # this gate is for.
    "oidc_provider.token",
    "oidc_provider.userinfo",
    "oidc_provider.introspect",
    "oidc_provider.revoke",
    "static",
)


def www_authenticate(credential_presented: bool) -> str:
    """The `WWW-Authenticate` value for a 401.

    RFC 9110 §11.6.1 makes this header **mandatory** on a 401, and the gate omitted it — a
    conformance gap that also costs real behaviour: a client, proxy or API-tooling chain has nothing
    to tell it *how* to authenticate, and generic HTTP clients cannot distinguish "you must
    authenticate" from an opaque refusal.

    `Bearer` because the credential is a JWT once Phase 2 lands (a browser carries it in an
    `HttpOnly` cookie; a programmatic client sends `Authorization: Bearer`).

    ``credential_presented`` selects between the two RFC 6750 §3 forms, and **this does not leak
    anything**: the caller already knows whether it sent a credential. What it must *not* do is say
    which of the invalidation causes applied — expired, revoked, malformed and wrong-audience all map
    to the single `invalid_token`, which is exactly the uniformity FR-4b wants, so no
    `error_description` is emitted.
    """
    if credential_presented:
        return f'Bearer realm="{AUTH_REALM}", error="invalid_token"'
    return f'Bearer realm="{AUTH_REALM}"'


def credential_presented(headers: object, cookies: object = None) -> bool:
    """Whether the request carried *something* it believed was a credential.

    Deliberately generous — an `Authorization` header or a session cookie both count — because the
    only consequence is which of two equally non-disclosing challenge forms is returned. For the same
    reason it never raises: failing to build a header is not worth turning a 401 into a 500.
    """
    try:
        if (getattr(headers, "get", lambda _k: None)("Authorization") or "").strip():
            return True
        return bool(cookies is not None and ("session" in cookies or "nvis_token" in cookies))
    except (AttributeError, TypeError):
        return False


_wsgi_authenticator: Callable[[dict], bool] | None = None


def set_wsgi_authenticator(fn: Callable[[dict], bool] | None) -> None:
    """Register how a raw WSGI environ is checked for a dashboard credential."""
    global _wsgi_authenticator
    _wsgi_authenticator = fn


def wsgi_environ_authenticated(environ: dict) -> bool:
    """Whether a raw WSGI request carries a valid dashboard credential.

    **Fails closed.** If no authenticator was registered, an enabled gate denies rather than
    falls open: a missing wire is a deployment mistake, and the alternative is an endpoint that
    reaches ``pty.openpty()`` and then ``ssh`` with the app's cluster key. ``test_wsgi_auth_hook``
    asserts the wire exists so the closed default is never what production relies on.
    """
    fn = _wsgi_authenticator
    if fn is None:
        _logger.error("no WSGI authenticator registered; denying (app.py should register one)")
        return False
    try:
        return bool(fn(environ))
    except Exception:
        _logger.warning("WSGI credential check failed; denying", exc_info=True)
        return False


def _allowed_origins() -> tuple[str, ...]:
    """Extra browser origins permitted to open a WebSocket, from ``RACK_WEB_ALLOWED_ORIGINS``.

    Comma-separated, e.g. ``https://nvis.example:8443,https://localhost:5156``.

    Deliberately read from the **process environment only**, not through ``config_env._eget``.
    That is the one exception to ``core-designs.mdc`` §5 in this module, and it is not an
    oversight: ``_eget`` consults ``job_env_overrides.json``, which the dashboard's own Settings
    API writes, so an authenticated operator could widen their own origin allowlist -- and until
    the authorization work lands, every operator is equally privileged. The rule exists to stop a
    feature working on one cluster and not another; an origin allowlist describes how a browser
    reaches *this deployment* and is identical for every cluster the container serves, so the
    rationale does not apply while the escalation risk does. Same treatment as
    ``RACK_WEB_COOKIE_SECURE``, which is likewise deploy-time only.
    """
    raw = os.environ.get("RACK_WEB_ALLOWED_ORIGINS") or ""
    return tuple(part.strip().rstrip("/") for part in raw.split(",") if part.strip())


def wsgi_origin_allowed(environ: dict) -> bool:
    """Cross-origin guard for a WebSocket handshake.

    A browser cannot be made to forge ``Origin`` on a WebSocket upgrade, which is what makes this
    the decisive control against a page the operator merely *visits* opening a terminal in their
    name. It is load-bearing rather than belt-and-braces because **cookies are not port-scoped**:
    anything else served from ``localhost`` -- another dev server, a compromised local tool --
    shares the dashboard's cookie jar with ``localhost:5156``, and ``SameSite=Lax`` does not
    separate them. TLS does not help there either.

    Compared on host *and port*, not scheme. The port is the part that catches the same-host
    attacker above, while comparing schemes would break the plain-HTTP-to-HTTPS transition and any
    deployment where a proxy terminates TLS -- for no gain, since a same-host origin on a
    different scheme is not a boundary cookies respect anyway.

    No ``Origin`` header means a non-browser client (curl, a test, an operator's script). That is
    not the threat this addresses, and such a caller still has to pass
    :func:`wsgi_environ_authenticated`, so it is allowed through.
    """
    origin = (environ.get("HTTP_ORIGIN") or "").strip().rstrip("/")
    if not origin:
        return True
    if origin in _allowed_origins():
        return True
    host = (environ.get("HTTP_HOST") or "").strip().lower()
    if not host:
        return False
    try:
        origin_host = urlsplit(origin).netloc.strip().lower()
    except ValueError:
        return False
    return bool(origin_host) and origin_host == host
