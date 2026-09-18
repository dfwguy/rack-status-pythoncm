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
"""Minting and verifying in OIDC mode — Phase 2.5, FR-1, FR-3, FR-4a, FR-4b, FR-7, FR-8, FR-19.

Two things this file is really for.

**That "off" means identical, not similar** (AC-28). The flag adds a second path through the
authentication gate, and the way that goes wrong is a subtle drift in the default configuration that
nobody notices until it is deployed. The strongest statement of that is not in this file at all —
it is that `test_identity_tokens.py` and `test_identity_token_invalidation.py` pass **unchanged**.

**That publishing a verification key creates a new attack, and we refuse it.** Under HS256 an
attacker had no key material to work with. From this phase on, `alg: HS256` signed with our own
published public key as the HMAC secret is a token we handed them the ingredients for. PyJWT refuses
to *encode* that, which guards signing and says nothing about verification — so
`TestTheAttackThePublishedKeyCreates` builds the token by hand to actually reach `verify`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from identity import keys as keys_mod
from identity import oidc, revocation
from identity import tokens as T

ISSUER = "https://rack.example.com"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def oidc_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", "test-secret-for-the-legacy-path")
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "oidc"))
    monkeypatch.setenv("RACK_WEB_OIDC_ENABLED", "1")
    monkeypatch.setenv("RACK_WEB_OIDC_ISSUER", ISSUER)
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    revocation.install()
    yield
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()


@pytest.fixture
def legacy_mode(monkeypatch):
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", "test-secret-for-the-legacy-path")
    monkeypatch.delenv("RACK_WEB_OIDC_ENABLED", raising=False)
    keys_mod.reset_for_tests()
    oidc.reset_for_tests()
    revocation.install()
    yield
    keys_mod.reset_for_tests()


def _unverified(token: str) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


class TestTheTokenShape:
    """Was `TestTheTwoModesAreDistinct`. There is one mode as of 2026-09-08, so the six tests that
    described the HS256 side — no `kid`, the `typ` claim, the old audience, the opaque issuer, and
    cross-mode refusal — asserted a state the product can no longer be in. What remains is the shape
    every token now has."""

    """FR-19. Off is today; on is the provider. Nothing in between."""

    def test_oidc_mode_signs_es256_with_a_kid(self, oidc_mode):
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        header = jwt.get_unverified_header(token)
        assert header["alg"] == "ES256"
        assert header["kid"] == keys_mod.key_set().ensure().kid

    def test_oidc_mode_carries_token_use_and_not_typ(self, oidc_mode):
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS))
        assert claims["token_use"] == "access"
        assert "typ" not in claims

    def test_the_audience_is_the_launch_name(self, oidc_mode):
        """The product rename rides the cutover, which is what makes it free.

        `aud` is a protocol constant: renaming it refuses every token carrying the old value. The
        ES256 cutover already ends every session, so tying the rename to the same flag costs one
        logout instead of two. Renaming it independently would have meant a second mass logout for
        a change no operator can see.
        """
        assert _unverified(T.mint("user:admin", T.TOKEN_ACCESS))["aud"] == "nvis-greenlight"

    def test_the_issuer_becomes_a_url_in_oidc_mode(self, oidc_mode):
        assert _unverified(T.mint("user:admin", T.TOKEN_ACCESS))["iss"] == ISSUER


class TestRoundTrip:
    def test_a_minted_token_verifies(self, oidc_mode):
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        assert T.verify(token, T.TOKEN_ACCESS)["sub"] == "user:admin"

    @pytest.mark.parametrize("kind", [T.TOKEN_ID, T.TOKEN_ACCESS, T.TOKEN_REFRESH])
    def test_every_token_kind_round_trips(self, oidc_mode, kind):
        assert T.verify(T.mint("user:admin", kind), kind)["sub"] == "user:admin"

    def test_type_confusion_is_still_refused(self, oidc_mode):
        """The property must survive the rename from `typ` to `token_use`, not be lost in it."""
        refresh = T.mint("user:admin", T.TOKEN_REFRESH)
        with pytest.raises(T.TokenError, match="token type"):
            T.verify(refresh, T.TOKEN_ACCESS)

    def test_the_public_key_alone_verifies_a_token(self, oidc_mode):
        """FR-1, and the whole point of the phase: verifiable without the minting secret."""
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        public = keys_mod.key_set().ensure().private.public_key()

        claims = jwt.decode(
            token, public, algorithms=["ES256"], audience=T.AUDIENCE_OIDC, issuer=ISSUER
        )
        assert claims["sub"] == "user:admin"


class TestClaims:
    """FR-7, FR-8."""

    def test_auth_time_defaults_to_now_when_not_supplied(self, oidc_mode):
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS))
        assert "auth_time" in claims
        assert claims["auth_time"] == pytest.approx(claims["iat"])

    def test_auth_time_is_preserved_when_passed_through(self, oidc_mode):
        """FR-8, and the one that is invisible when wrong.

        A refreshed token whose `auth_time` is "now" claims the user authenticated a moment ago
        when they may have signed in weeks earlier. The value is well-formed either way, so every
        `max_age` check a relying party makes is answered with a plausible lie.
        """
        original = 1_800_000_000.0
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS, auth_time=original))
        assert claims["auth_time"] == original
        assert claims["iat"] != original, "iat must still be mint time, not auth time"

    def test_the_access_and_refresh_tokens_of_one_login_agree_on_auth_time(self, oidc_mode):
        """One authentication event, one timestamp — and this failed when first measured.

        Letting each `mint` stamp its own "now" is the obvious implementation and it is wrong: the
        two calls are milliseconds apart, so the pair disagrees, refresh propagates the *refresh*
        token's value, and `auth_time` creeps forward on every renewal. A whole-second assertion
        would have called the original 3 ms drift equal, which is why this compares exactly.
        """
        shared = 1_800_000_000.5
        access = _unverified(T.mint("user:admin", T.TOKEN_ACCESS, auth_time=shared))
        refresh = _unverified(T.mint("user:admin", T.TOKEN_REFRESH, auth_time=shared))

        assert access["auth_time"] == refresh["auth_time"] == shared

    def test_auth_time_survives_a_chain_of_renewals(self, oidc_mode):
        """Renewal is automatic and hourly, so any per-renewal error compounds over a session."""
        origin = 1_800_000_000.25
        carried = origin
        for _ in range(5):
            carried = _unverified(
                T.mint("user:admin", T.TOKEN_ACCESS, auth_time=carried)
            )["auth_time"]

        assert carried == origin

    def test_scope_is_on_the_access_and_refresh_tokens_but_not_the_id_token(self, oidc_mode):
        """The refresh token carries it deliberately, and this test previously asserted it did not.

        RFC 6749 §6 forbids a refresh from widening scope, and these tokens are self-contained --
        there is no server-side grant record to consult -- so a refresh token that cannot recall its
        own grant leaves the endpoint able only to guess or to widen. `scope` stays off the **ID**
        token because OIDC does not define it there; an ID token describes an authentication, not an
        authorization.
        """
        assert _unverified(T.mint("user:admin", T.TOKEN_ACCESS))["scope"] == "openid"
        assert _unverified(T.mint("user:admin", T.TOKEN_REFRESH))["scope"] == "openid"
        assert "scope" not in _unverified(T.mint("user:admin", T.TOKEN_ID))

    def test_scope_defaults_to_openid_when_the_caller_names_none(self, oidc_mode):
        """`mint` is called from places that have no grant to speak of (the SRP login path), and the
        narrowest scope is the right default for them."""
        assert _unverified(T.mint("user:admin", T.TOKEN_ACCESS))["scope"].split() == ["openid"]

    def test_a_named_scope_is_carried_verbatim(self, oidc_mode):
        minted = T.mint("user:admin", T.TOKEN_ACCESS, scope="openid profile email")
        assert _unverified(minted)["scope"] == "openid profile email"

    def test_a_caller_cannot_override_token_use(self, oidc_mode):
        with pytest.raises(T.TokenError, match="token_use"):
            T.mint("user:admin", T.TOKEN_ACCESS, claims={"token_use": "id"})


class TestKidSelection:
    """FR-3, FR-4a."""

    def test_a_token_signed_by_a_demoted_key_still_verifies(self, oidc_mode):
        """The property that makes rotation non-disruptive rather than a flag day."""
        old_token = T.mint("user:admin", T.TOKEN_ACCESS)
        keys_mod.key_set().rotate()

        assert T.verify(old_token, T.TOKEN_ACCESS)["sub"] == "user:admin"
        new_token = T.mint("user:admin", T.TOKEN_ACCESS)
        assert jwt.get_unverified_header(new_token)["kid"] != jwt.get_unverified_header(old_token)["kid"]

    def test_an_unknown_kid_is_refused(self, oidc_mode):
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        parts = token.split(".")
        parts[0] = _b64(json.dumps({"alg": "ES256", "kid": "not-a-real-key"}).encode())
        with pytest.raises(T.TokenError, match="unknown or retired"):
            T.verify(".".join(parts), T.TOKEN_ACCESS)

    def test_a_missing_kid_is_refused_rather_than_falling_back(self, oidc_mode):
        """No "try the current key" fallback.

        That fallback would re-admit exactly the tokens a rotation exists to retire, and it would
        do so silently.
        """
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        parts = token.split(".")
        parts[0] = _b64(json.dumps({"alg": "ES256"}).encode())
        with pytest.raises(T.TokenError, match="no `kid`"):
            T.verify(".".join(parts), T.TOKEN_ACCESS)


class TestTheAttackThePublishedKeyCreates:
    """FR-4b. New in this phase, because until now there was no key to hand an attacker.

    Built by hand rather than with `jwt.encode`, which refuses HS256 with an asymmetric key. That
    refusal protects *signing*; it says nothing about whether our `verify` would accept such a
    token, and it is our verify that is under test.
    """

    def _forge(self, claims: dict, secret: bytes, kid: str, alg: str = "HS256") -> str:
        header = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
        payload = _b64(json.dumps(claims).encode())
        sig = _b64(hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest())
        return f"{header}.{payload}.{sig}"

    def test_hs256_signed_with_our_own_public_key_is_refused(self, oidc_mode):
        from cryptography.hazmat.primitives import serialization

        key = keys_mod.key_set().ensure()
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS))
        pem = key.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

        with pytest.raises(T.TokenError):
            T.verify(self._forge(claims, pem, key.kid), T.TOKEN_ACCESS)

    def test_hs256_signed_with_the_published_jwk_is_refused(self, oidc_mode):
        """The JWKS is what an attacker actually fetches, so try that exact material too."""
        key = keys_mod.key_set().ensure()
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS))
        published = json.dumps(key.public_jwk()).encode()

        with pytest.raises(T.TokenError):
            T.verify(self._forge(claims, published, key.kid), T.TOKEN_ACCESS)

    def test_alg_none_is_refused(self, oidc_mode):
        key = keys_mod.key_set().ensure()
        claims = _unverified(T.mint("user:admin", T.TOKEN_ACCESS))
        header = _b64(json.dumps({"alg": "none", "kid": key.kid}).encode())
        payload = _b64(json.dumps(claims).encode())

        with pytest.raises(T.TokenError):
            T.verify(f"{header}.{payload}.", T.TOKEN_ACCESS)

    def test_a_tampered_payload_is_refused(self, oidc_mode):
        token = T.mint("user:operator", T.TOKEN_ACCESS)
        head, _payload, sig = token.split(".")
        elevated = _unverified(token) | {"sub": "user:admin"}

        with pytest.raises(T.TokenError):
            T.verify(f"{head}.{_b64(json.dumps(elevated).encode())}.{sig}", T.TOKEN_ACCESS)
