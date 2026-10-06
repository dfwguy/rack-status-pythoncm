# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""JWT mint/verify: the algorithm is pinned, the claims are demanded, revocation is fail-closed.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Covers FR-4c (algorithm confusion), FR-4d (our own tokens' issuer/audience/expiry), FR-5 (the
id/access split), FR-5b (revocable renewal) and FR-6 (no grant honoured without a revocation check).

The forgery attempts carry the weight. "A minted token verifies" would pass against an
implementation that also accepted `alg: none`, a token with no expiry, another deployment's key, or
a refresh token used as an access token — so each of those is written as a test that must fail to
authenticate.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

import jwt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import tokens as T

SECRET = "0" * 64
OTHER_SECRET = "f" * 64


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", SECRET)
    monkeypatch.delenv("RACK_WEB_TOKEN_ISSUER", raising=False)
    T.reset_for_tests()
    T.allow_all_for_tests()
    yield
    T.reset_for_tests()


#: Sentinel meaning "sign this the way the product does". Distinct from an explicit key, which the
#: algorithm-confusion cases pass deliberately.
REAL_KEY = object()


def _forge(claims: dict, key: object = REAL_KEY, algorithm: str | None = None) -> str:
    """Hand-build a token, bypassing `mint`, to test what `verify` refuses.

    **Defaults to the product's real ES256 key and `kid`, which it did not until 2026-09-10.** It
    used to sign HS256 with the HKDF secret — correct while that signed JWTs, and wrong ever since
    the provider became unconditional: `verify` refuses an HS256 token for having no `kid` before it
    reads any claim, so a case asserting "the wrong audience is refused" was really asserting "a
    token with no kid is refused".

    Pass `key=` and `algorithm=` explicitly to build a genuinely wrong-key or wrong-algorithm
    token — that is what the algorithm-confusion cases want, and they should say so rather than
    getting it as a side effect of the default.
    """
    from identity import keys as keys_mod

    if key is REAL_KEY:
        real = keys_mod.key_set().ensure()
        return jwt.encode(
            claims,
            real.private,
            algorithm=algorithm or keys_mod.ALGORITHM,
            headers={"kid": real.kid},
        )
    return jwt.encode(claims, key, algorithm=algorithm)


def _good_claims(**over) -> dict:
    now = int(time.time())
    base = {
        "iss": T.issuer(),
        "aud": T.AUDIENCE,
        "sub": "user:admin",
        "iat": now,
        "exp": now + 300,
        "jti": "abc123",
        T.TYPE_CLAIM: T.TOKEN_ACCESS,
    }
    base.update(over)
    return base


class TestTheHappyPath:
    def test_a_minted_access_token_verifies(self):
        claims = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        assert claims["sub"] == "user:admin"
        assert claims[T.TYPE_CLAIM] == T.TOKEN_ACCESS

    def test_the_three_token_types_are_distinct_and_all_verify(self):
        """FR-5's id/access split, plus FR-5b's refresh."""
        for kind in (T.TOKEN_ID, T.TOKEN_ACCESS, T.TOKEN_REFRESH):
            assert T.verify(T.mint("user:admin", kind), kind)[T.TYPE_CLAIM] == kind

    def test_the_access_token_is_shorter_lived_than_the_refresh_token(self):
        """FR-5b: a short credential renewable by a longer one that can itself be withdrawn.

        This asserts the *relationship* and the ceilings, not a specific default. It used to pin
        ``<= 15 * 60`` on the reasoning that the access TTL bounds how stale a withdrawn grant can
        be (FR-6) — and that reasoning does not describe what was built. `mint` puts no grant in the
        payload: membership and policy are read from the store on every request and the revocation
        record is consulted on every verify, so neither is bounded by this TTL. What it bounds is a
        stolen token nobody has revoked. The defaults were raised on that basis.
        """
        access = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        refresh = T.verify(T.mint("user:admin", T.TOKEN_REFRESH), T.TOKEN_REFRESH)
        access_life = access["exp"] - access["iat"]
        refresh_life = refresh["exp"] - refresh["iat"]

        assert refresh_life > access_life
        assert access_life <= T.MAX_ACCESS_TTL_SECONDS
        assert refresh_life <= T.MAX_REFRESH_TTL_SECONDS

    def test_no_policy_grant_rides_inside_a_token(self):
        """The claim the raised lifetimes rest on, narrowed once — and the narrowing is the point.

        **`scope` is now embedded, deliberately.** OIDC requires it: `/userinfo` releases claims by
        the scope the token was granted, and a refresh must be able to recall its original grant to
        avoid widening (RFC 6749 §6). So the original assertion — that *nothing* grant-shaped is in
        the payload — stopped being true when the provider became unconditional, and it was right to
        fail: it exists to force this paragraph to be written rather than let the change pass.

        What the lifetime reasoning actually needs is unchanged, because `scope` is not an
        authorization decision. Policy is resolved per request against the attached documents, so a
        withdrawn grant takes effect on the next call regardless of token age. `scope` only bounds
        *claim release* at `/userinfo`, and its staleness is bounded by the access TTL — the same
        bound revocation gives everything else.

        `groups`, `roles` and `permissions` must still never appear: those *would* be authorization
        decisions frozen into a long-lived token, which is precisely what the 1-hour default assumes
        cannot happen.
        """
        claims = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        for grant in ("groups", "roles", "scopes", "perms", "permissions", "policy"):
            assert grant not in claims, (
                f"{grant!r} now rides in the token. Unlike `scope`, that is an authorization "
                f"decision frozen into a token the product keeps alive for an hour by default, so "
                f"the lifetime rationale in tokens.py must be revisited before this lands."
            )

    def test_every_token_has_a_unique_id(self):
        """`jti` is what lets one refresh token be revoked without revoking the user."""
        seen = {T.verify(T.mint("user:a", T.TOKEN_REFRESH), T.TOKEN_REFRESH)["jti"] for _ in range(8)}
        assert len(seen) == 8


class TestConfigurableLifetimes:
    """Defaults, ceilings, and the fact that a ceiling constrains every path to a lifetime.

    Raised from 15 min / 12 h to 1 h / 1 day on 2026-09-01, with per-deployment ceilings of 7 days
    and 30 days. The interesting cases are not the defaults — they are what happens when a
    deployment asks for more than the ceiling, and whether the shortest path around the ceiling
    ignores it.
    """

    def test_the_defaults_are_an_hour_and_a_week(self, monkeypatch):
        monkeypatch.delenv("RACK_WEB_ACCESS_TTL_SECONDS", raising=False)
        monkeypatch.delenv("RACK_WEB_REFRESH_TTL_SECONDS", raising=False)
        assert T.access_ttl() == 60 * 60
        # The refresh token is the session's source of truth, so its default is the shipped session
        # length — one week since 2026-10-06, up from one day.
        assert T.refresh_ttl() == 7 * 24 * 60 * 60

    @pytest.mark.parametrize(
        ("env", "reader", "requested", "expected"),
        [
            ("RACK_WEB_ACCESS_TTL_SECONDS", "access_ttl", 3 * 24 * 3600, 3 * 24 * 3600),
            ("RACK_WEB_REFRESH_TTL_SECONDS", "refresh_ttl", 14 * 24 * 3600, 14 * 24 * 3600),
        ],
    )
    def test_a_value_within_the_ceiling_is_honoured(
        self, monkeypatch, env, reader, requested, expected
    ):
        monkeypatch.setenv(env, str(requested))
        assert getattr(T, reader)() == expected

    @pytest.mark.parametrize(
        ("env", "reader", "ceiling_attr"),
        [
            ("RACK_WEB_ACCESS_TTL_SECONDS", "access_ttl", "MAX_ACCESS_TTL_SECONDS"),
            ("RACK_WEB_REFRESH_TTL_SECONDS", "refresh_ttl", "MAX_REFRESH_TTL_SECONDS"),
        ],
    )
    def test_a_value_above_the_ceiling_is_clamped_not_obeyed(
        self, monkeypatch, caplog, env, reader, ceiling_attr
    ):
        """Clamped rather than refused: a too-generous TTL should not take startup down.

        But it must be *said*, because a security parameter quietly disagreeing with its
        configuration is worse than either value — the operator would believe the number they set.
        """
        ceiling = getattr(T, ceiling_attr)
        monkeypatch.setenv(env, str(ceiling * 12))
        with caplog.at_level(logging.WARNING):
            assert getattr(T, reader)() == ceiling
        assert env in caplog.text and "ceiling" in caplog.text

    @pytest.mark.parametrize("garbage", ["banana", "", "  ", "1e5", "-0x10"])
    def test_an_unparseable_value_falls_back_to_the_default(self, monkeypatch, garbage):
        monkeypatch.setenv("RACK_WEB_ACCESS_TTL_SECONDS", garbage)
        assert T.access_ttl() == T.ACCESS_TTL_SECONDS

    def test_a_below_floor_value_is_raised_to_a_minute(self, monkeypatch):
        """A 1-second token would make the product unusable while looking configured."""
        monkeypatch.setenv("RACK_WEB_ACCESS_TTL_SECONDS", "1")
        assert T.access_ttl() == 60

    @pytest.mark.parametrize(
        ("token_type", "ceiling_attr"),
        [
            (T.TOKEN_ACCESS, "MAX_ACCESS_TTL_SECONDS"),
            (T.TOKEN_ID, "MAX_ACCESS_TTL_SECONDS"),
            (T.TOKEN_REFRESH, "MAX_REFRESH_TTL_SECONDS"),
        ],
    )
    def test_an_explicit_override_cannot_exceed_the_ceiling(self, token_type, ceiling_attr):
        """The path around the ceiling is capped too, or the ceiling is decorative.

        `mint(ttl_seconds=...)` bypasses the environment entirely, so without this a caller could
        mint a decade-long token in a deployment configured for one hour.
        """
        ceiling = getattr(T, ceiling_attr)
        with pytest.raises(T.TokenError, match="ceiling"):
            T.mint("user:admin", token_type, ttl_seconds=ceiling + 1)

        # One second under the ceiling is still fine, so the boundary is not off by one.
        assert T.mint("user:admin", token_type, ttl_seconds=ceiling - 1)

    def test_the_mint_reads_the_configured_value_not_the_constant(self, monkeypatch):
        """Wiring test: the accessors exist, and `mint` actually consults them.

        Without this the constants could be raised, the accessors written, and `mint` left reading
        the module constant — every test above would pass and no deployment could configure
        anything.
        """
        monkeypatch.setenv("RACK_WEB_ACCESS_TTL_SECONDS", str(2 * 3600))
        claims = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        assert round(claims["exp"] - claims["iat"]) == 2 * 3600


class TestAlgorithmConfusion:
    """FR-4c. PyJWT honours the token's own `alg` header unless `algorithms=` is pinned, so these
    are the two classic forgeries."""

    def test_alg_none_is_refused(self):
        unsigned = jwt.encode(_good_claims(), key=None, algorithm=None)
        with pytest.raises(T.TokenError):
            T.verify(unsigned, T.TOKEN_ACCESS)

    def test_our_own_public_key_as_an_hmac_secret_is_refused(self):
        """**The attack publishing a JWKS creates**, and the reason `algorithms=` is pinned.

        An attacker takes our *public* ES256 key — which we serve deliberately at
        `/.well-known/jwks.json` — signs a token with it as an HMAC secret, and sets `alg: HS256`.
        A verifier that trusts the header's algorithm then validates a forgery against a value we
        handed out ourselves. Pinning `algorithms=[ES256]` refuses it, but only because it is
        written down.

        Previously spelled as `algorithm="HS512"` against a symmetric default, which tested PyJWT's
        algorithm list rather than this attack.
        """
        from cryptography.hazmat.primitives import serialization
        from identity import keys as keys_mod

        # Serialised from the key pair, because `SigningKey` exposes `private` and `public_jwk`
        # but no PEM. This is the exact byte string a client would take from our JWKS and is what
        # makes the attack real rather than notional.
        public_pem = keys_mod.key_set().ensure().private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        # Hand-rolled, because PyJWT refuses to *encode* with an asymmetric key as an HMAC secret
        # ("should not be used as an HMAC secret"). That guard is welcome but it is PyJWT's, and
        # asserting it would test the library rather than our verifier — an attacker writes the
        # bytes themselves. So we do too, and what is under test is our `algorithms=[ES256]` pin.
        import base64
        import hashlib
        import hmac

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        ours = keys_mod.key_set().ensure()
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": ours.kid}).encode())
        payload = b64(json.dumps(_good_claims()).encode())
        signing_input = header + b"." + payload
        signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()

        with pytest.raises(T.TokenError):
            T.verify(forged, T.TOKEN_ACCESS)

    def test_the_pinned_algorithm_is_asymmetric_and_named(self):
        """ES256, and asserted because the pin is the whole defence above. `T.ALGORITHM` is the
        retired HS256 constant and is deliberately not what verification uses."""
        from identity import keys as keys_mod

        assert keys_mod.ALGORITHM == "ES256"

    def test_an_unsigned_token_is_not_readable_as_valid(self):
        """Belt and braces: even a syntactically fine token with an empty signature fails."""
        header_payload = _forge(_good_claims()).rsplit(".", 1)[0]
        with pytest.raises(T.TokenError):
            T.verify(header_payload + ".", T.TOKEN_ACCESS)


class TestClaimValidation:
    """FR-4d, and the two silent PyJWT gaps measured on 2.13.0."""

    def test_a_token_with_no_expiry_is_refused(self):
        """The gap that matters most: PyJWT *accepts* a token with no `exp` and treats it as
        eternal. Only the explicit `require` list stops it."""
        claims = _good_claims()
        del claims["exp"]
        with pytest.raises(T.TokenError):
            T.verify(_forge(claims), T.TOKEN_ACCESS)

    def test_an_expired_token_is_refused(self):
        now = int(time.time())
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(iat=now - 600, exp=now - 1)), T.TOKEN_ACCESS)

    def test_a_wrong_issuer_is_refused(self):
        """PyJWT does not check `iss` at all unless `issuer=` is passed, so this asserts the kwarg
        is actually there."""
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(iss="somebody-else")), T.TOKEN_ACCESS)

    def test_a_wrong_audience_is_refused(self):
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(aud="some-other-service")), T.TOKEN_ACCESS)

    def test_each_required_claim_is_actually_required(self):
        """Table-driven so a claim dropped from `REQUIRED_CLAIMS` fails here rather than silently
        becoming optional."""
        # `_required_claims()`, not `REQUIRED_CLAIMS`: the constant still lists the retired `typ`
        # name, and the effective set swaps it for `token_use`. Iterating the constant asked for a
        # claim no token carries.
        for claim in T._required_claims():
            claims = _good_claims()
            del claims[claim]
            # The message is no longer always "token rejected": a hand-forged token carries no
            # `kid`, and under ES256 that is refused before the claim set is examined at all. Both
            # are refusals, which is what the test is about; pinning the wording would pin which
            # layer got there first.
            with pytest.raises(T.TokenError):
                T.verify(_forge(claims), T.TOKEN_ACCESS)

    def test_a_malformed_subject_is_refused_as_a_token_error(self):
        """FR-15a: the principal grammar has one implementation, and it applies on the way out too —
        so a token cannot deliver a `sub` that later code has to split by hand.

        Asserts the *type*, not just the failure: this caught the module leaking `PrincipalError`,
        which would have made a malformed subject a 500 for any caller correctly catching
        `TokenError`.
        """
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(sub="not-a-principal")), T.TOKEN_ACCESS)

    def test_a_caller_cannot_override_a_reserved_claim(self):
        """Otherwise `mint(..., claims={"exp": far_future})` is a self-service eternal token."""
        for reserved in T.REQUIRED_CLAIMS:
            with pytest.raises(T.TokenError, match="may not override"):
                T.mint("user:admin", T.TOKEN_ACCESS, claims={reserved: "x"})

    def test_a_malformed_principal_cannot_be_minted(self):
        with pytest.raises(T.TokenError):
            T.mint("not-a-principal", T.TOKEN_ACCESS)


class TestTokenTypeConfusion:
    """FR-5. Not a formality: the refresh token lives 48x longer than the access token, so
    presenting one as the other would authorise requests for half a day."""

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self):
        with pytest.raises(T.TokenError, match="token type"):
            T.verify(T.mint("user:admin", T.TOKEN_REFRESH), T.TOKEN_ACCESS)

    def test_an_id_token_is_not_accepted_as_an_access_token(self):
        with pytest.raises(T.TokenError, match="token type"):
            T.verify(T.mint("user:admin", T.TOKEN_ID), T.TOKEN_ACCESS)

    def test_an_access_token_is_not_accepted_as_a_refresh_token(self):
        """The direction that would otherwise let an access token mint fresh ones forever."""
        with pytest.raises(T.TokenError, match="token type"):
            T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_REFRESH)

    def test_the_expected_type_has_no_default(self):
        """A default would make type confusion an omission rather than a decision."""
        with pytest.raises(TypeError):
            T.verify(T.mint("user:admin", T.TOKEN_ACCESS))  # type: ignore[call-arg]

    def test_an_unknown_type_is_refused_at_both_ends(self):
        with pytest.raises(T.TokenError):
            T.mint("user:admin", "superuser")
        with pytest.raises(T.TokenError):
            T.verify(T.mint("user:admin", T.TOKEN_ACCESS), "superuser")


class TestTheSigningKey:
    def test_the_key_is_derived_not_the_deployment_secret(self):
        """So a leaked JWT key is not a Flask session forgery key (and vice versa)."""
        assert T.signing_key() != SECRET.encode()
        assert len(T.signing_key()) == T.KEY_BYTES

    def test_the_derivation_is_stable(self):
        assert T.signing_key() == T.signing_key()

    def test_a_different_deployment_secret_yields_a_different_key(self, monkeypatch):
        ours = T.signing_key()
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", OTHER_SECRET)
        assert T.signing_key() != ours

    def test_the_secret_no_longer_signs_jwts(self, monkeypatch):
        """**The change that retiring HS256 made, and the one most likely to mislead a reader.**

        Three tests stood here asserting that `RACK_WEB_SECRET_KEY` separates deployments, that
        rotating it invalidates outstanding tokens, and that its absence refuses a mint. All three
        were true of HS256 and none is true now: the ES256 key set signs, and the secret protects the
        Flask session and the token vault instead. So INV-17 is no longer a *JWT* hazard — repointing
        the secret leaves tokens verifying, which is exactly the opposite of what the traps note said
        while HS256 existed.

        `signing_key()` itself still works and is still tested above; it simply is not what signs a
        token.
        """
        before = T.mint("user:admin", T.TOKEN_ACCESS)
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", OTHER_SECRET)
        assert T.verify(before, T.TOKEN_ACCESS)["sub"] == "user:admin"

    def test_a_token_from_another_deployment_is_refused(self, tmp_path, monkeypatch):
        """Deployment separation, asserted against the mechanism that now provides it.

        Still not the `iss` string — that is configured and could coincide. A different key set is
        what makes two installs' tokens mutually unusable.
        """
        from identity import keys as keys_mod

        foreign = T.mint("user:admin", T.TOKEN_ACCESS)

        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "other-deployment"))
        keys_mod.reset_for_tests()
        keys_mod.key_set().ensure()
        try:
            with pytest.raises(T.TokenError):
                T.verify(foreign, T.TOKEN_ACCESS)
        finally:
            keys_mod.reset_for_tests()

    def test_key_rotation_does_not_invalidate_outstanding_tokens(self):
        """The deliberate opposite of the HS256 behaviour, so it is worth pinning.

        Rotation is append-only: the incumbent is demoted to verify-only rather than removed, so a
        token minted before a rotation keeps working until it expires. That is what makes rotation a
        routine operation instead of a mass logout — and it is why revocation, not rotation, is the
        tool for withdrawing a session.
        """
        from identity import keys as keys_mod

        before = T.mint("user:admin", T.TOKEN_ACCESS)
        keys_mod.key_set().rotate()
        assert T.verify(before, T.TOKEN_ACCESS)["sub"] == "user:admin"

    def test_a_missing_key_set_is_generated_not_refused(self, tmp_path, monkeypatch):
        """`ensure()` mints on demand, so there is no "no key" state to refuse — the same posture as
        the self-signed certificate the startup gate generates."""
        from identity import keys as keys_mod

        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "fresh"))
        keys_mod.reset_for_tests()
        try:
            assert T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        finally:
            keys_mod.reset_for_tests()


class TestRevocationIsFailClosed:
    """FR-6. The design choice here is that an unregistered checker refuses everything."""

    def test_no_registered_checker_means_no_token_is_honoured(self):
        token = T.mint("user:admin", T.TOKEN_ACCESS)
        T.reset_for_tests()
        assert T.revocation_checker_registered() is False
        with pytest.raises(T.TokenError, match="no revocation checker"):
            T.verify(token, T.TOKEN_ACCESS)

    def test_minting_still_works_without_a_checker(self):
        """Deliberate: the gate is on honouring a token, not on issuing one, so bootstrap can mint
        the first admin credential before the revocation store is wired."""
        T.reset_for_tests()
        assert T.mint("user:admin", T.TOKEN_ACCESS)

    def test_the_checker_can_refuse_a_subject(self):
        T.set_revocation_checker(
            lambda c: (_ for _ in ()).throw(T.TokenRevoked("user disabled"))
            if c["sub"] == "user:bob"
            else None
        )
        assert T.verify(T.mint("user:alice", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        with pytest.raises(T.TokenRevoked):
            T.verify(T.mint("user:bob", T.TOKEN_ACCESS), T.TOKEN_ACCESS)

    def test_the_checker_receives_verified_claims_not_a_bare_subject(self):
        """So revocation can key on `jti` (one refresh token) or `iat` (everything issued before a
        password change) without changing this signature."""
        seen: list[dict] = []
        T.set_revocation_checker(seen.append)
        T.verify(T.mint("user:admin", T.TOKEN_REFRESH), T.TOKEN_REFRESH)
        assert {"sub", "jti", "iat", T.TYPE_CLAIM, "exp"} <= set(seen[0])

    def test_the_checker_runs_only_after_signature_and_claim_checks(self):
        """A checker must never see attacker-controlled claims from an unverified token."""
        seen: list[dict] = []
        T.set_revocation_checker(seen.append)
        # A different key pair, not a different HMAC secret: the point is a *valid-looking* token
        # whose signature does not verify, and since the product signs ES256 that means another
        # private key. Signed with our real `kid` so it gets past key lookup and fails at the
        # signature -- which is precisely the boundary this case is about.
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from identity import keys as keys_mod

        stranger = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        ours = keys_mod.key_set().ensure()
        forged = jwt.encode(
            _good_claims(), stranger, algorithm=keys_mod.ALGORITHM, headers={"kid": ours.kid}
        )
        with pytest.raises(T.TokenError):
            T.verify(forged, T.TOKEN_ACCESS)
        assert seen == [], "revocation checker was handed claims from a token that failed validation"

    def test_a_revoked_token_raises_a_distinguishable_error(self):
        """`TokenRevoked` is a `TokenError`, so callers can stay uniform while the audit trail can
        tell 'was valid, now withdrawn' from 'never valid'."""
        assert issubclass(T.TokenRevoked, T.TokenError)
