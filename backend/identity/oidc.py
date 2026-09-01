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

import logging
import os
import threading
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

#: Everything the provider surface needs, in one switch. See the module docstring.
FLAG_ENV = "RACK_WEB_OIDC_ENABLED"
ISSUER_ENV = "RACK_WEB_OIDC_ISSUER"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class OIDCConfigError(Exception):
    """The provider is enabled but cannot be configured coherently."""


def enabled() -> bool:
    """Whether the OIDC provider surface is active.

    Read per call rather than captured at import: a captured flag makes the value depend on import
    order, which in tests is the difference between a suite that passes and one that passes
    depending on which file ran first.
    """
    return (os.environ.get(FLAG_ENV) or "").strip().lower() in _TRUTHY


def issuer_url() -> str:
    """The deployment's issuer URL. Raises when enabled without a usable one.

    Validated rather than accepted: OIDC clients compare this string against the URL they fetched
    discovery from, so a value with a trailing slash, a query, or an ``http`` scheme produces a
    mismatch at the client with no error on our side. Catching it here costs one startup check;
    catching it at the client costs somebody a day.
    """
    raw = (os.environ.get(ISSUER_ENV) or "").strip()
    if not raw:
        raise OIDCConfigError(
            f"{FLAG_ENV} is on but {ISSUER_ENV} is not set. OIDC Discovery 1.0 §4.3 requires the "
            f"issuer to be the URL the discovery document is served from, and conforming clients "
            f"reject a token whose `iss` is not that URL. Set it to this deployment's own base URL, "
            f"e.g. https://rack.example.com — it cannot be derived from the request, because a "
            f"Host header is attacker-controlled and the issuer is a security boundary."
        )
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
            "id_token_signing_alg_values_supported": ["ES256"],
            "subject_types_supported": ["public"],
            # Only `openid` until Phase 3. A scope that appears in a token and gates nothing must
            # not be advertised as though it did.
            "scopes_supported": ["openid"],
            "response_types_supported": [],
            "grant_types_supported": [],
            "claims_supported": ["iss", "aud", "sub", "exp", "iat", "jti", "token_use"],
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
