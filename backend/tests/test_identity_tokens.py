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


def _forge(claims: dict, key: str | None = SECRET, algorithm: str | None = T.ALGORITHM) -> str:
    """Hand-build a token, bypassing `mint`, to test what `verify` refuses.

    Uses the derived key so a forgery differs from a real token *only* in the property under test —
    signing with the raw secret would make every case fail for the wrong reason.
    """
    signing = None if algorithm is None else (T.signing_key() if key == SECRET else key)
    return jwt.encode(claims, signing, algorithm=algorithm)


def _good_claims(**over) -> dict:
    now = int(time.time())
    base = {
        "iss": T.issuer(),
        "aud": T.AUDIENCE,
        "sub": "user:admin",
        "iat": now,
        "exp": now + 300,
        "jti": "abc123",
        "typ": T.TOKEN_ACCESS,
    }
    base.update(over)
    return base


class TestTheHappyPath:
    def test_a_minted_access_token_verifies(self):
        claims = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        assert claims["sub"] == "user:admin"
        assert claims["typ"] == T.TOKEN_ACCESS

    def test_the_three_token_types_are_distinct_and_all_verify(self):
        """FR-5's id/access split, plus FR-5b's refresh."""
        for kind in (T.TOKEN_ID, T.TOKEN_ACCESS, T.TOKEN_REFRESH):
            assert T.verify(T.mint("user:admin", kind), kind)["typ"] == kind

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

    def test_no_grant_rides_inside_a_token(self):
        """The claim the raised lifetimes rest on, so it is asserted rather than assumed.

        If a group, role or scope were ever added to the payload, a long-lived access token would
        start carrying a stale authorisation — and the reasoning for a 1-hour (or 7-day) default
        would silently stop holding. This fails at that moment instead.
        """
        claims = T.verify(T.mint("user:admin", T.TOKEN_ACCESS), T.TOKEN_ACCESS)
        for grant in ("groups", "roles", "scope", "scopes", "perms", "permissions", "policy"):
            assert grant not in claims, (
                f"{grant!r} now rides in the token; the lifetime rationale in tokens.py assumes no "
                "grant is embedded and must be revisited"
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

    def test_the_defaults_are_an_hour_and_a_day(self, monkeypatch):
        monkeypatch.delenv("RACK_WEB_ACCESS_TTL_SECONDS", raising=False)
        monkeypatch.delenv("RACK_WEB_REFRESH_TTL_SECONDS", raising=False)
        assert T.access_ttl() == 60 * 60
        assert T.refresh_ttl() == 24 * 60 * 60

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

    # Forging an HS512 token with our HS256-sized key is the point of the test; PyJWT rightly warns
    # that 32 bytes is short for SHA-512. Silenced here only so a deliberate forgery does not train
    # anyone to ignore that warning elsewhere.
    @pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
    def test_a_different_algorithm_is_refused(self):
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(), algorithm="HS512"), T.TOKEN_ACCESS)

    def test_the_pinned_algorithm_is_symmetric_and_named(self):
        assert T.ALGORITHM == "HS256"

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
        for claim in T.REQUIRED_CLAIMS:
            claims = _good_claims()
            del claims[claim]
            with pytest.raises(T.TokenError, match="token rejected"):
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

    def test_a_token_from_another_deployment_is_refused(self, monkeypatch):
        """This — not the `iss` string — is what actually separates two installs, since `iss` and
        `aud` are product constants."""
        foreign = T.mint("user:admin", T.TOKEN_ACCESS)
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", OTHER_SECRET)
        with pytest.raises(T.TokenError):
            T.verify(foreign, T.TOKEN_ACCESS)

    def test_a_rotated_secret_invalidates_existing_tokens(self, monkeypatch):
        """The key is deliberately not cached, so rotation takes effect without a restart."""
        before = T.mint("user:admin", T.TOKEN_ACCESS)
        assert T.verify(before, T.TOKEN_ACCESS)
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", OTHER_SECRET)
        with pytest.raises(T.TokenError):
            T.verify(before, T.TOKEN_ACCESS)

    def test_no_secret_is_a_refusal_not_a_default_key(self, monkeypatch):
        monkeypatch.delenv("RACK_WEB_SECRET_KEY", raising=False)
        with pytest.raises(T.TokenError, match="RACK_WEB_SECRET_KEY"):
            T.mint("user:admin", T.TOKEN_ACCESS)


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
        assert {"sub", "jti", "iat", "typ", "exp"} <= set(seen[0])

    def test_the_checker_runs_only_after_signature_and_claim_checks(self):
        """A checker must never see attacker-controlled claims from an unverified token."""
        seen: list[dict] = []
        T.set_revocation_checker(seen.append)
        with pytest.raises(T.TokenError):
            T.verify(_forge(_good_claims(), key=OTHER_SECRET), T.TOKEN_ACCESS)
        assert seen == [], "revocation checker was handed claims from a token that failed validation"

    def test_a_revoked_token_raises_a_distinguishable_error(self):
        """`TokenRevoked` is a `TokenError`, so callers can stay uniform while the audit trail can
        tell 'was valid, now withdrawn' from 'never valid'."""
        assert issubclass(T.TokenRevoked, T.TokenError)
