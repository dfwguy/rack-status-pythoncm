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
"""The two `.well-known` endpoints: discovery and JWKS (FR-5, FR-6, FR-6a, FR-6b).

**These are the only unauthenticated routes this phase adds, and that is a requirement rather than
an accident** (FR-6b). OIDC obliges both to be reachable without a credential — a client cannot
authenticate before it knows how to. They are on the auth bypass list for the same reason
`auth.login` is, and the bar for a third entry from this phase is that it must be impossible to
serve any other way.

Nothing here is secret. The discovery document is a description of endpoints, and the JWKS carries
public keys whose entire purpose is publication. What must never appear is private key material,
which `SigningKey.public_jwk` structurally cannot emit and `test_oidc_endpoints` asserts against the
rendered bytes as well as the mapping.

`url_prefix=""` because `.well-known` paths are fixed by specification at the root and cannot be
spelled as a module-derived prefix.
"""

from __future__ import annotations

import throttle
from flask import Blueprint, jsonify
from identity import keys as keys_mod
from identity import oidc

bp = Blueprint("oidc", __name__, url_prefix="")

#: Matches the guidance we publish for consumers, which is Microsoft's for Entra rather than one we
#: invented: cache per `kid`, refresh about hourly. Long enough that a client is not refetching on
#: every verification, short enough that an emergency rotation propagates within the hour.
_CACHE_CONTROL = "public, max-age=3600"


def _disabled():
    """404 when the provider is off.

    Absent rather than empty, and this is the half-switched state AC-30 forbids. An empty JWKS
    served by a deployment signing HS256 tells a client "this issuer has no keys", which it will
    cache — so it is a worse answer than "no such endpoint", which is simply true.
    """
    return jsonify({"error": "the OIDC provider is not enabled on this deployment"}), 404


@bp.get("/.well-known/openid-configuration")
@throttle.throttle(120, rationale="memoised document; a client fetches it at cold start. Generous because refusing discovery breaks configuration rather than degrading it.")
def openid_configuration():
    """OIDC Discovery 1.0 §4. Advertises only what this build actually serves (FR-5)."""
    if not oidc.enabled():
        return _disabled()
    try:
        document = oidc.discovery_document()
    except oidc.OIDCConfigError as exc:
        # The provider is on but misconfigured. 500 with the reason, because this is our fault and
        # a client can do nothing about it — and silence here would present as an unreachable
        # provider, which sends someone debugging the network instead of the configuration.
        return jsonify({"error": str(exc)}), 500
    response = jsonify(document)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response


@bp.get("/.well-known/jwks.json")
@throttle.throttle(120, rationale="in-memory key set. Consumers are told to refresh hourly, so 2/s is far above real use.")
def jwks():
    """The JWK Set: the public half of every currently valid verification key (FR-6)."""
    if not oidc.enabled():
        return _disabled()

    document = keys_mod.key_set().public_jwks()
    if not document["keys"]:
        # Should be unreachable — startup bootstraps a key whenever the provider is on — so this is
        # a guard against that wiring being lost, not an expected path.
        #
        # 503 and explicitly NOT cached, because an empty JWKS is the worst possible 200: it tells a
        # client "this issuer has no keys", which is a *positive* statement it will cache for the
        # full hour, and every token we mint in that window is rejected as unverifiable. A 503 says
        # "ask again", which is the truth. Minting here instead is not an option: a read that writes
        # invalidates the cache above it (NFR-2a).
        return jsonify({
            "error": "no signing key is available; the provider is enabled but not ready",
        }), 503

    response = jsonify(document)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response
