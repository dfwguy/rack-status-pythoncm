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
"""OIDC provider configuration: the flag, the issuer, and the discovery document (FR-5, FR-19).

THE FLAG (FR-19)
----------------
``RACK_WEB_OIDC_ENABLED`` selects the whole provider surface **as one unit** — asymmetric signing,
``kid`` headers, JWKS and discovery — because the dangerous state is not on or off but *half*. A
deployment signing HS256 while advertising a JWKS publishes a verification key that verifies
nothing, and every external consumer then fails in a way that looks like our bug rather than a
misconfiguration. There is deliberately no separate switch for the endpoints.

Off is the default and means **identical to today**, not merely similar: HS256, no ``kid``, and the
endpoints absent rather than empty. Turning it on ends every live session, which is why it is a
scheduled cutover rather than a runtime toggle — flipping it back does not restore service, it signs
everyone out a second time.

THE ISSUER IS A URL HERE, AND IT WAS NOT BEFORE
-----------------------------------------------
``tokens.issuer()`` returns the opaque string ``nvis-rack-status``. OIDC Discovery 1.0 §4.3 requires
``issuer`` to be **the URL the discovery document was fetched from**, and clients enforce that: a
non-URL issuer fails validation in every conforming library. So enabling the provider requires
``RACK_WEB_OIDC_ISSUER`` to be set to the deployment's own HTTPS URL.

**It is configured, never derived from the request.** Deriving it from the ``Host`` header would be
convenient and would hand an attacker control of the issuer of our tokens — they set a header, we
mint a token claiming to come from their origin, and a relying party that trusts that origin accepts
it. The value is a security boundary, so it comes from configuration.

This is also why the flag refuses rather than warns when the issuer is missing: a provider that
advertises the wrong issuer is worse than one that is switched off, because the failure surfaces at
the client and looks like the client's fault.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

#: Everything the provider surface needs, in one switch. See the module docstring.
# `FLAG_ENV = "RACK_WEB_OIDC_ENABLED"` was removed on 2026-09-10. It survived the flag itself
# by two days, read only to interpolate the variable name into two startup log messages -- so a
# failed start told the operator to check something that no longer exists. A named constant for
# a retired switch is also an invitation to branch on it again.
ISSUER_ENV = "RACK_WEB_OIDC_ISSUER"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

#: Which scope releases which claims, per OpenID Connect Core 1.0 §5.4.
#:
#: **The single source of truth for both directions.** `claims_supported` in the discovery document
#: and the release decision in `/userinfo` are both derived from this map, so the document cannot
#: advertise a claim the endpoint withholds — the same reason the policy JSON Schema is exported from
#: the pydantic model rather than written twice.
#:
#: `address` is absent because the record has no address, and `gender`/`birthdate`/`picture`/
#: `website`/`nickname` because an internal cluster-operations tool has no use for them and each is
#: PII the product would then own.
SCOPE_CLAIMS: dict[str, tuple[str, ...]] = {
    "openid": ("sub",),
    "profile": (
        "name",
        "given_name",
        "family_name",
        "preferred_username",
        "zoneinfo",
        "locale",
        "updated_at",
    ),
    "email": ("email", "email_verified"),
    "phone": ("phone_number", "phone_number_verified"),
}

#: Scopes a client may ask for. `openid` is mandatory in an OIDC request, so it is always granted.
SUPPORTED_SCOPES: tuple[str, ...] = tuple(SCOPE_CLAIMS)

#: What a request that names no scope at all is granted.
#:
#: A compatibility choice, not a rule. `/authorize` used to discard `scope` entirely and `/userinfo`
#: returned `name` and `preferred_username` unconditionally — both of which §5.4 assigns to
#: `profile`. Gating them without this default would silently stop returning them to every caller
#: that predates scope handling, so an absent `scope` reproduces the previous response exactly. A
#: request that names its scopes gets precisely those.
DEFAULT_SCOPE: tuple[str, ...] = ("openid", "profile")


def parse_scope(raw: object) -> tuple[str, ...]:
    """Normalise a space-delimited scope string into the granted set.

    Unsupported scopes are dropped rather than refused (RFC 6749 §3.3 permits either, and a hard
    refusal makes a client that asks for one harmless extra scope unable to log in at all). `openid`
    is always present: this endpoint only speaks OIDC.
    """
    requested = [s for s in str(raw or "").replace(",", " ").split() if s]
    if not requested:
        return DEFAULT_SCOPE
    granted = [s for s in SUPPORTED_SCOPES if s in requested]
    if "openid" not in granted:
        granted.insert(0, "openid")
    return tuple(granted)


def narrow_scope(granted: object, requested: object) -> tuple[str, ...]:
    """The scope for a refreshed token: never wider than what was originally granted (RFC 6749 §6).

    Without this a client holding an `openid` refresh token could ask for `openid profile email` and
    be handed attributes its authorization request never covered — a privilege escalation that looks
    like an ordinary renewal in every log.
    """
    have = tuple(s for s in SUPPORTED_SCOPES if s in str(granted or "").split())
    if not have:
        have = ("openid",)
    asked = [s for s in str(requested or "").replace(",", " ").split() if s]
    if not asked:
        return have
    kept = tuple(s for s in have if s in asked)
    return kept or ("openid",)


class OIDCConfigError(Exception):
    """The provider is enabled but cannot be configured coherently."""


def enabled() -> bool:
    """Whether the OIDC provider surface is active.

    **`RACK_WEB_OIDC_ENABLED` was retired on 2026-09-08** and setting it does nothing. The flag
    selected the provider *as one unit*, so a deployment that left it unset ran a materially
    different product: HS256 tokens with no `kid`, a non-standard audience, no `auth_time`, no
    `scope`, and a login that issued no id token at all. Two token shapes meant two verification
    paths, and the one nobody ran locally was the one customers had.

    Kept as a function rather than deleted because thirteen call sites read it, and the honest
    reading of those sites is "the provider is present" — not "the flag is set".
    """
    return True


def _derived_issuer() -> str:
    """This deployment's issuer, inferred from the bind address.

    One implementation, shared with `startup_gate._ensure_oidc_issuer()`, so the value the gate
    announces is the value tokens actually carry. Two derivations would drift, and the drift surfaces
    as a client rejecting a token whose `iss` looks correct in our log.
    """
    port = (os.environ.get("RACK_WEB_PORT") or os.environ.get("PORT") or "5000").strip()
    host = (os.environ.get("RACK_WEB_PUBLIC_HOST") or "localhost").strip()
    return f"https://{host}:{port}"


def issuer_url() -> str:
    """The deployment's issuer URL. Raises when enabled without a usable one.

    Validated rather than accepted: OIDC clients compare this string against the URL they fetched
    discovery from, so a value with a trailing slash, a query, or an ``http`` scheme produces a
    mismatch at the client with no error on our side. Catching it here costs one startup check;
    catching it at the client costs somebody a day.
    """
    raw = (os.environ.get(ISSUER_ENV) or "").strip()
    if not raw:
        # **Derived, not refused.** Making it required the moment the provider became unconditional
        # took `import app` down with exit 78 everywhere — every deployment and 36 test modules —
        # for a value only a *third-party* client ever compares. While this product's own SPA is the
        # only reader, the token is verified by the process that minted it, so a self-consistent
        # issuer suffices.
        #
        # Still never from the request: a `Host` header is attacker-controlled, so deriving per
        # request would let a caller choose the issuer of our tokens (Discovery 1.0 §4.3). This
        # reads configuration, and `startup_gate` warns once at boot when it falls back.
        return _derived_issuer()
    parsed = urlparse(raw)
    # Localhost is the exception that matters: operators reach the dashboard through an SSH tunnel,
    # so http://localhost:PORT is a legitimate issuer for a tunnelled deployment and refusing it
    # would make the provider unusable in the way it is actually deployed.
    tunnelled = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not tunnelled:
        raise OIDCConfigError(
            f"{ISSUER_ENV}={raw!r} must use https (http is permitted only for localhost, which "
            f"is how an operator reaches a tunnelled deployment)"
        )
    if not parsed.netloc:
        raise OIDCConfigError(f"{ISSUER_ENV}={raw!r} has no host")
    if parsed.query or parsed.fragment:
        raise OIDCConfigError(
            f"{ISSUER_ENV}={raw!r} must not carry a query or fragment; clients compare the issuer "
            f"by exact string against the URL they fetched discovery from"
        )
    # A trailing slash is a different string to a client doing an exact comparison, and it is the
    # single most common way an issuer mismatch happens. Normalised rather than refused, because
    # refusing over a slash would be pedantry when the intent is unambiguous.
    return raw.rstrip("/")


_doc_cache: tuple[str, dict[str, object]] | None = None
_doc_hits = 0
_doc_lock = threading.Lock()


def discovery_document() -> dict[str, object]:
    """The OIDC discovery document, describing **only what is implemented**.

    Advertising an endpoint we do not serve is worse than omitting it: a client configures itself
    from this document and then fails at the point of use, far from the cause. So this grows as the
    phase does — the entries below are the ones that exist today, and each later increment adds its
    own alongside the route.

    Cached on the issuer, because it is a pure function of configuration and is fetched on every
    client cold start. The cache is a plain memo with **no write anywhere**: a read path that writes
    changes a file mtime, and if that mtime is part of a cache key the cache invalidates itself and
    never hits — measured in Phase 2 while every functional test passed.
    """
    global _doc_cache, _doc_hits
    issuer = issuer_url()
    with _doc_lock:
        if _doc_cache is not None and _doc_cache[0] == issuer:
            _doc_hits += 1
            return _doc_cache[1]
        document: dict[str, object] = {
            "issuer": issuer,
            "jwks_uri": f"{issuer}/.well-known/jwks.json",
            "authorization_endpoint": f"{issuer}/oidc/authorize",
            "token_endpoint": f"{issuer}/oidc/token",
            "userinfo_endpoint": f"{issuer}/oidc/userinfo",
            "introspection_endpoint": f"{issuer}/oidc/introspect",
            "revocation_endpoint": f"{issuer}/oidc/revoke",
            "id_token_signing_alg_values_supported": ["ES256"],
            "subject_types_supported": ["public"],
            # Every scope here now gates a real claim-release decision at `/userinfo`, which is the
            # condition for advertising it: a scope that appears in a token and gates nothing must
            # not be presented as though it did.
            "scopes_supported": list(SUPPORTED_SCOPES),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            # S256 only. `plain` is advertised by many providers and protects nothing against an
            # intercepted code, so declining to offer it is the point rather than an omission.
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
            # Protocol claims, then every claim some scope can release -- derived from SCOPE_CLAIMS
            # so the document cannot advertise something `/userinfo` withholds.
            "claims_supported": [
                "iss", "aud", "sub", "exp", "iat", "jti",
                "token_use", "auth_time", "azp", "nonce", "scope",
                *sorted({c for claims in SCOPE_CLAIMS.values() for c in claims} - {"sub"}),
            ],
        }
        _doc_cache = (issuer, document)
        return document


def cache_hits() -> int:
    """How many times the memo served. Exposed so a test can prove the cache is not inert.

    Without this, a staleness test proves nothing: a cache that never hits cannot go stale, and the
    Phase 2 revocation cache was in exactly that state — invalidated by its own reads, hit count
    zero, every functional test green.
    """
    return _doc_hits


def reset_for_tests() -> None:
    global _doc_cache, _doc_hits
    with _doc_lock:
        _doc_cache = None
        _doc_hits = 0


def at_hash(access_token: str) -> str:
    """OIDC Core 1.0 §3.1.3.6: the **left-most half** of the SHA-256 of the access token.

    The half is the part that gets missed — a full digest is a plausible-looking value that no
    conforming client accepts, and the mismatch surfaces at the client as a rejected id token with
    nothing visibly wrong on our side.

    SHA-256 because the id token is signed with ES256: the hash tracks the signing algorithm, so
    this must move if that does.

    Lives here rather than in `api/oidc_provider.py`, where it started, because the local SRP login
    issues an id token too and the dependency runs `app.py -> api/<domain>.py -> core.py` in one
    direction only (core-designs.mdc §0) — one `api/` module importing another would invert it.
    """
    digest = hashlib.sha256(access_token.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest[: len(digest) // 2]).decode("ascii").rstrip("=")
