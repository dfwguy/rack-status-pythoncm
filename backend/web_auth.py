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

Two modes, chosen by :func:`_auth_mode`. PAM authenticates against *this machine's* accounts, so it
is only meaningful when the app runs on the target host; a container talking to a remote head node has
no such mapping, which is why a remote target defaults to a shared token instead.

PAM verification prefers in-process ctypes and falls back to a subprocess helper under a Python whose
ctypes actually works -- a module-loaded Python on a cluster login node frequently cannot load it, and
the error that produces is opaque, so the fallback raises RuntimeError naming
``RACK_WEB_PAM_HELPER_PYTHON``.

Not an endpoint module and not under ``api/``: it imports Flask only for ``session``, and nothing here
builds a response.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import urlsplit

import host_exec
from config_env import _env_int, _subprocess_env
from core import truthy_env as _truthy_env
from flask import session
from paths import APP_DIR

_logger = logging.getLogger(__name__)


def _auth_enabled() -> bool:
    """Whether the gate is enforced.

    ``RACK_WEB_AUTH_DISABLED`` survives FR-18's removal of the legacy credentials as an explicit
    development and test switch (FR-18a), and only that: it no longer selects a login method, it
    turns the gate off entirely. FR-18c retires it once the suite authenticates through a real
    credential facility instead of bypassing — until then some 2,500 tests depend on it.
    """
    return not _truthy_env("RACK_WEB_AUTH_DISABLED")


#: The cookie a browser carries the access token in. Duplicated from ``api/auth_srp`` rather than
#: imported: the dependency runs ``app.py -> api/<domain>.py -> core.py`` in one direction only
#: (core-designs.mdc §0), and the gate importing a route module would invert it.
ACCESS_COOKIE = "nvis_access"


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
        subject = str(claims.get("sub") or "").strip()
        if subject:
            return subject
    return ""


def _auth_mode() -> str:
    """Return ``pam`` or ``token``.  Defaults to ``token`` when inside a container (SSH mode)."""
    explicit = (os.environ.get("RACK_WEB_AUTH_MODE") or "").strip().lower()
    if explicit in ("pam", "token"):
        return explicit
    # PAM authenticates against *this* machine's accounts, so it is only meaningful
    # when the app runs on the target host; a remote target has no such mapping.
    return "pam" if host_exec.exec_kind() == "local" else "token"


def _auth_token() -> str | None:
    """Token string when RACK_WEB_AUTH_MODE=token."""
    t = (os.environ.get("RACK_WEB_AUTH_TOKEN") or "").strip()
    return t or None


def _auth_user() -> str:
    return (os.environ.get("RACK_WEB_AUTH_USER") or "root").strip() or "root"


def _pam_service() -> str:
    return (os.environ.get("RACK_WEB_PAM_SERVICE") or "login").strip() or "login"


def _session_lifetime() -> timedelta:
    h = _env_int("RACK_WEB_SESSION_HOURS", 8)
    h = max(h, 1)
    h = min(h, 24 * 30)
    return timedelta(hours=h)


def _session_ok() -> bool:
    return session.get("rack_web_auth") == "1"


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
    "auth.api_login",
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
    if not _auth_enabled():
        return True
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


def _pam_helper_python_exe() -> str:
    return (os.environ.get("RACK_WEB_PAM_HELPER_PYTHON") or "/usr/bin/python3").strip() or "/usr/bin/python3"


def _pam_verify_subprocess(username: str, password: str, service: str) -> bool:
    """Authenticate via pam_helper.py under a Python that can load ctypes (e.g. /usr/bin/python3)."""
    exe = _pam_helper_python_exe()
    helper = APP_DIR / "rack_web" / "pam_helper.py"
    if not helper.is_file():
        raise RuntimeError(f"Internal error: missing {helper}")
    payload = json.dumps({"user": username, "password": password, "service": service})
    try:
        proc = subprocess.run(
            [exe, str(helper)],
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=90,
            env=_subprocess_env(),
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"PAM helper interpreter not found: {exe!r}. Set RACK_WEB_PAM_HELPER_PYTHON to a "
            "Python whose ctypes module works (often /usr/bin/python3), or recreate the venv with "
            "that interpreter instead of a mismatched module-loaded Python."
        ) from None
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("PAM authentication subprocess timed out") from e
    out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
    err_tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(
            f"PAM helper process failed (exit {proc.returncode}). "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    if not out:
        raise RuntimeError(
            "PAM helper produced no output. "
            f"Try RACK_WEB_PAM_HELPER_PYTHON=/usr/bin/python3. Stderr: {err_tail[-1200:]}"
        )
    line = out.splitlines()[-1].strip()
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"PAM helper returned invalid JSON ({line[:200]!r}): {e}") from e
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return bool(data.get("ok"))


def _token_verify(password: str) -> bool:
    """Verify password against RACK_WEB_AUTH_TOKEN (simple shared-secret)."""
    token = _auth_token()
    if not token:
        return False
    return secrets.compare_digest(password, token)


def _pam_verify(username: str, password: str, service: str) -> bool:
    if password is None:
        return False
    if _auth_mode() == "token":
        return _token_verify(password)
    try:
        from rack_web.pam_ctypes import authenticate as pam_authenticate
    except (ImportError, OSError):
        return _pam_verify_subprocess(username, password, service)
    return bool(pam_authenticate(username, password, service=service))
