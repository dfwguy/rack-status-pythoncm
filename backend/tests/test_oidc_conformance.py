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
"""Conformance: verify our tokens the way a stranger would (Phase 2.5, gap 4).

**This closes the most important gap in the phase.** Every other test verifies our tokens with
PyJWT — the same library that minted them — so a consistent misreading of the spec passes
everything while interoperating with nobody. That is not a hypothetical failure mode: it is exactly
what happened to the SRP implementation, where the library's non-RFC padding default produced a
protocol that worked perfectly against itself and would have failed against every browser client.

So nothing here uses PyJWT. A token is split on dots, base64url-decoded by hand, and its ECDSA
signature checked against the JWKS coordinates using raw `cryptography` primitives. The JOSE
encoding, the `kid` lookup and the signature format are all re-derived from the specifications
rather than borrowed from our own code.

**What this still cannot prove.** `cryptography` is the same library underneath PyJWT's ES256
implementation, so a defect in the P-256 primitive itself would be invisible to both. A genuinely
independent check needs a different language's implementation, which is what a real relying party
(Grafana) will provide the first time it is pointed at a deployment. Recorded rather than glossed:
this is a strong check, not a complete one.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric import utils as asym_utils
from identity import keys as keys_mod
from identity import oidc, revocation
from identity import tokens as T

ISSUER = "https://rack.example.com"


@pytest.fixture
def provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ISSUER)
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    revocation.install()
    # Bootstrap the key explicitly, as startup does. Relying on `mint()` to create it lazily works
    # only for tests that mint before they read — and the discovery test deliberately reads the
    # JWKS first, the way a client does, so it saw the empty-set 503 instead of a key.
    keys_mod.key_set().ensure()
    yield
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()


def b64u_decode(segment: str) -> bytes:
    """base64url with the padding a JWS omits (RFC 7515 §2)."""
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def public_key_from_jwk(jwk: dict) -> ec.EllipticCurvePublicKey:
    """Rebuild a P-256 public key from a JWK, per RFC 7518 §6.2.1.

    Coordinates are unsigned big-endian integers, base64url-encoded. Nothing here consults our own
    serialiser, which is the point — if we emit `x`/`y` incorrectly, this fails.
    """
    if jwk["kty"] != "EC" or jwk["crv"] != "P-256":
        raise AssertionError(f"unexpected key type {jwk['kty']}/{jwk['crv']}")
    x = int.from_bytes(b64u_decode(jwk["x"]), "big")
    y = int.from_bytes(b64u_decode(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()


def verify_like_a_relying_party(token: str, jwks: dict, *, issuer: str, audience: str) -> dict:
    """Validate a JWS the way a conforming client does, using no JWT library.

    Returns the claims, or raises AssertionError. Written as one function so the order of operations
    is visible: **signature first**, then claims. A verifier that reads claims before checking the
    signature is acting on attacker-controlled data.
    """
    parts = token.split(".")
    assert len(parts) == 3, "a JWS Compact Serialization has exactly three parts"
    header_b64, payload_b64, signature_b64 = parts

    header = json.loads(b64u_decode(header_b64))
    assert header["alg"] == "ES256", f"unexpected alg {header['alg']!r}"
    assert "kid" in header, "no kid, so the right key cannot be selected"

    matching = [k for k in jwks["keys"] if k["kid"] == header["kid"]]
    assert len(matching) == 1, f"kid {header['kid']!r} matched {len(matching)} keys in the JWKS"
    public = public_key_from_jwk(matching[0])

    # ES256 signatures in JOSE are the raw r||s concatenation (RFC 7518 §3.4), NOT the DER encoding
    # `cryptography` expects. Converting is the step a naive implementation gets wrong, and getting
    # it wrong looks exactly like a bad signature.
    raw = b64u_decode(signature_b64)
    assert len(raw) == 64, f"ES256 signature must be 64 bytes, got {len(raw)}"
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = asym_utils.encode_dss_signature(r, s)

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        public.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise AssertionError("signature did not verify") from exc

    claims = json.loads(b64u_decode(payload_b64))
    assert claims["iss"] == issuer, f"iss {claims['iss']!r} != {issuer!r}"
    aud = claims["aud"]
    assert audience in ([aud] if isinstance(aud, str) else aud), f"aud {aud!r} excludes {audience!r}"
    for required in ("sub", "exp", "iat", "jti"):
        assert required in claims, f"missing required claim {required!r}"
    return claims


class TestAStrangerCanVerifyOurTokens:
    """The entire purpose of the phase, checked without our own JWT library."""

    @pytest.mark.parametrize("kind", [T.TOKEN_ID, T.TOKEN_ACCESS, T.TOKEN_REFRESH])
    def test_every_token_kind_verifies_from_the_jwks_alone(self, provider, kind):
        token = T.mint("user:admin", kind)
        jwks = keys_mod.key_set().public_jwks()

        claims = verify_like_a_relying_party(
            token, jwks, issuer=ISSUER, audience=T.AUDIENCE_OIDC
        )
        assert claims["sub"] == "user:admin"
        assert claims["token_use"] == kind

    def test_the_signature_is_the_jose_raw_format_not_der(self, provider):
        """RFC 7518 §3.4. A DER-encoded signature is ~70 bytes and variable-length; JOSE requires a
        fixed 64. Emitting DER would fail in every conforming client and pass with libraries that
        accept both."""
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        raw = b64u_decode(token.split(".")[2])
        assert len(raw) == 64

    def test_the_kid_selects_exactly_one_key_after_a_rotation(self, provider):
        """With two keys published, an ambiguous or absent `kid` makes a client guess."""
        first = T.mint("user:admin", T.TOKEN_ACCESS)
        keys_mod.key_set().rotate()
        second = T.mint("user:admin", T.TOKEN_ACCESS)
        jwks = keys_mod.key_set().public_jwks()
        assert len(jwks["keys"]) == 2

        for token in (first, second):
            verify_like_a_relying_party(token, jwks, issuer=ISSUER, audience=T.AUDIENCE_OIDC)

    def test_a_tampered_payload_fails_the_independent_check_too(self, provider):
        """Guards the checker itself: a verifier that accepts anything proves nothing above."""
        token = T.mint("user:operator", T.TOKEN_ACCESS)
        header_b64, payload_b64, sig = token.split(".")
        claims = json.loads(b64u_decode(payload_b64)) | {"sub": "user:admin"}
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")

        with pytest.raises(AssertionError, match="signature did not verify"):
            verify_like_a_relying_party(
                f"{header_b64}.{forged}.{sig}", keys_mod.key_set().public_jwks(),
                issuer=ISSUER, audience=T.AUDIENCE_OIDC,
            )

    def test_the_at_hash_matches_what_a_client_computes(self, provider):
        """OIDC Core §3.1.3.6, recomputed from the spec rather than from our helper.

        The left-most **half** of the digest. A full digest is a plausible-looking value that every
        conforming client rejects, with nothing visibly wrong on our side.
        """
        access = T.mint("user:admin", T.TOKEN_ACCESS)
        digest = hashlib.sha256(access.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")

        # `at_hash` moved to `identity/oidc.py` when the local SRP login began issuing an id token
        # too: two `api/` modules need it now, and one importing the other would invert the
        # dependency direction (core-designs.mdc §0).
        from identity import oidc as oidc_mod

        assert oidc_mod.at_hash(access) == expected
        assert len(b64u_decode(expected)) == 16


class TestTheDiscoveryDocumentIsUsableByAStranger:
    def test_a_client_can_configure_itself_from_discovery_alone(self, provider):
        """Walks the document as a client would: read `jwks_uri`, fetch it, verify a token.

        No step consults our internal constants, so a document that is internally consistent but
        wrong about itself fails here.
        """
        from app import app

        app.config["TESTING"] = True
        with app.test_client() as c:
            doc = c.get("/.well-known/openid-configuration").get_json()
            assert doc["issuer"] == ISSUER

            jwks_path = doc["jwks_uri"].removeprefix(ISSUER)
            jwks = c.get(jwks_path).get_json()

        token = T.mint("user:admin", T.TOKEN_ACCESS)
        claims = verify_like_a_relying_party(
            token, jwks, issuer=doc["issuer"], audience=T.AUDIENCE_OIDC
        )
        assert claims["sub"] == "user:admin"

    def test_the_advertised_algorithm_is_the_one_actually_used(self, provider):
        from app import app

        app.config["TESTING"] = True
        with app.test_client() as c:
            advertised = c.get(
                "/.well-known/openid-configuration"
            ).get_json()["id_token_signing_alg_values_supported"]

        header = json.loads(b64u_decode(T.mint("user:admin", T.TOKEN_ID).split(".")[0]))
        assert header["alg"] in advertised
