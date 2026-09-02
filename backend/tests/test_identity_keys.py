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
"""The signing key set — Phase 2.5, FR-1..FR-4f.

Every test takes an explicit ``now``. The whole design is about dates that are months apart, so a
suite that can only observe "now" would verify the arithmetic at exactly one point on the timeline —
and the interesting points are the cutoff and the moment just before it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from identity import keys as K

MONTH = 30 * 24 * 60 * 60
T0 = 1_800_000_000.0


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    d = tmp_path / "oidc"
    monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(d))
    K.reset_for_tests()
    yield d
    K.reset_for_tests()


@pytest.fixture
def month_tokens(monkeypatch):
    """A 30-day refresh ceiling — the Phase 2 default, and today's real ``T_max``."""
    monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(MONTH))


def _set(key_dir, validity=None):
    return K.KeySet(key_dir, validity_seconds=validity)


class TestBootstrap:
    """FR-2, FR-18a: generate on first use, reuse afterwards."""

    def test_a_key_is_minted_on_first_use(self, key_dir, month_tokens):
        key = _set(key_dir).ensure(now=T0)
        assert key.kid
        assert (key_dir / f"{key.kid}.pem").is_file()
        assert (key_dir / "keys.json").is_file()

    def test_a_second_call_reuses_rather_than_minting(self, key_dir, month_tokens):
        first = _set(key_dir).ensure(now=T0)
        second = _set(key_dir).ensure(now=T0 + 60)
        assert first.kid == second.kid
        assert len(list(key_dir.glob("*.pem"))) == 1

    def test_the_key_survives_a_restart(self, key_dir, month_tokens):
        """A fresh KeySet is what a process restart looks like from here."""
        original = _set(key_dir).ensure(now=T0)
        reloaded = _set(key_dir).ensure(now=T0 + MONTH)
        assert reloaded.kid == original.kid
        assert reloaded.private.private_numbers() == original.private.private_numbers()


class TestPermissions:
    """FR-2a. Owner-only at every observable moment, including across a rewrite."""

    def _mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_the_private_key_is_owner_only(self, key_dir, month_tokens):
        key = _set(key_dir).ensure(now=T0)
        assert self._mode(key_dir / f"{key.kid}.pem") == 0o600

    def test_permissions_survive_several_writes(self, key_dir, month_tokens):
        """Three rotations, not one.

        A single-write test passes on the broken implementation — the classic form of this bug only
        appears when a file is *replaced*, because `os.replace` installs the temp file's inode and
        a temp created with a plain `open()` carries the umask.
        """
        ks = _set(key_dir)
        for i in range(3):
            ks.rotate(now=T0 + i * 60)
        pems = list(key_dir.glob("*.pem"))
        assert len(pems) == 3  # rotate() mints each time; nothing bootstraps a fourth
        for pem in pems:
            assert self._mode(pem) == 0o600, f"{pem.name} is {oct(self._mode(pem))}"

    def test_the_metadata_and_directory_are_not_world_readable(self, key_dir, month_tokens):
        _set(key_dir).ensure(now=T0)
        assert self._mode(key_dir / "keys.json") == 0o600
        assert self._mode(key_dir) == 0o700

    def test_a_hostile_umask_does_not_widen_the_key(self, key_dir, month_tokens):
        """The umask is the actual source of the classic 0644, so set a bad one and re-check."""
        old = os.umask(0)
        try:
            key = _set(key_dir).ensure(now=T0)
            assert self._mode(key_dir / f"{key.kid}.pem") == 0o600
        finally:
            os.umask(old)


class TestThumbprintKid:
    """FR-3. RFC 7638, so a verifier can derive it rather than trust our label."""

    def test_the_kid_is_the_rfc7638_thumbprint(self, key_dir, month_tokens):
        key = _set(key_dir).ensure(now=T0)
        jwk = key.public_jwk()
        # Recomputed here the way an external verifier would, from the published JWK alone.
        canonical = json.dumps(
            {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        expected = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).decode().rstrip("=")
        assert key.kid == expected == jwk["kid"]

    def test_the_canonicalisation_reproduces_the_rfc_worked_example(self):
        """RFC 7638 §3.1's own vector — the only *external* check available here.

        Every other thumbprint test compares our output against our own code, which cannot detect a
        consistent misreading of the spec: a wrong-but-stable `kid` passes all of them while
        agreeing with nobody. The RFC publishes an RSA example with a stated result, and the parts
        that matter are shared with the EC path — canonical JSON with no whitespace, members sorted,
        SHA-256, base64url without padding. If any of those four is wrong this fails.

        RSA rather than EC because the RFC does not publish an EC vector; the key is the RFC's, not
        one of ours, and it is used only to exercise the encoding.
        """
        rfc_jwk = {
            "e": "AQAB",
            "kty": "RSA",
            "n": (
                "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1"
                "L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4"
                "QyQ5v-65YGjQR0_FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbO"
                "pbISD08qNLyrdkt-bFTWhAI4vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csF"
                "Cur-kEgU8awapJzKnqDKgw"
            ),
        }
        payload = json.dumps(rfc_jwk, separators=(",", ":"), sort_keys=True).encode()
        computed = K._b64u(hashlib.sha256(payload).digest())

        assert computed == "NzbLsXh8uDCcd-6MNwXF4W_7noWXFZAfHkxZsRGC9Xs"

    def test_the_kid_is_stable_across_reloads(self, key_dir, month_tokens):
        first = _set(key_dir).ensure(now=T0).kid
        K.reset_for_tests()
        assert _set(key_dir).ensure(now=T0 + 1).kid == first

    def test_distinct_keys_get_distinct_kids(self, key_dir, month_tokens):
        ks = _set(key_dir)
        kids = {ks.rotate(now=T0 + i).kid for i in range(5)}
        assert len(kids) == 5

    def test_metadata_claiming_the_wrong_kid_is_refused(self, key_dir, month_tokens):
        """A renamed or edited key must not be served under a kid it does not hash to.

        If it were, the JWKS would advertise a kid that does not match the key, and every external
        verifier would look up that kid and fail — a break that looks like our signing is wrong.
        """
        key = _set(key_dir).ensure(now=T0)
        (key_dir / f"{key.kid}.pem").rename(key_dir / "forged.pem")
        meta = json.loads((key_dir / "keys.json").read_text())
        meta["keys"][0]["kid"] = "forged"
        (key_dir / "keys.json").write_text(json.dumps(meta))
        K.reset_for_tests()

        with pytest.raises(K.KeyError_, match="thumbprint"):
            _set(key_dir).ensure(now=T0 + 1)


class TestTheDerivedSigningCutoff:
    """FR-4c, FR-4d1 — the arithmetic the whole design rests on."""

    @pytest.mark.parametrize(
        ("validity_months", "t_max_months", "expect_cutoff_months"),
        [
            (18, 6, 12),   # the feature lead's own example
            (18, 1, 17),   # today's real ceiling
            (12, 6, 6),
            (24, 12, 12),
        ],
    )
    def test_the_cutoff_is_not_after_minus_t_max(
        self, key_dir, monkeypatch, validity_months, t_max_months, expect_cutoff_months
    ):
        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(t_max_months * MONTH))
        key = _set(key_dir, validity_months * MONTH).ensure(now=T0)
        cutoff = key.signing_cutoff(t_max_months * MONTH)
        assert cutoff == pytest.approx(T0 + expect_cutoff_months * MONTH)

    def test_the_default_validity_is_eighteen_months(self):
        assert K.DEFAULT_VALIDITY_SECONDS == 18 * MONTH

    def test_raising_the_refresh_ceiling_moves_the_cutoff_earlier(self, key_dir, monkeypatch):
        """FR-4d1. The regression a hard-coded T_max would cause, and it would be silent.

        Nothing errors if the cutoff stops tracking the ceiling — tokens simply begin outliving
        their signing key, and only start failing weeks later.

        Both values sit under the refresh ceiling's own 30-day maximum; see
        ``test_t_max_is_bounded_by_the_refresh_ceilings_own_maximum`` for why that matters.
        """
        week = 7 * 24 * 60 * 60
        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(week))
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        narrow = key.signing_cutoff(K._t_max())

        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(MONTH))
        wide = key.signing_cutoff(K._t_max())

        assert wide == pytest.approx(narrow - (MONTH - week))

    def test_t_max_is_bounded_by_the_refresh_ceilings_own_maximum(self, monkeypatch):
        """Why the FR-4d refusal cannot fire in a real deployment today — and must still exist.

        ``refresh_ttl()`` clamps to ``MAX_REFRESH_TTL_SECONDS`` (30 days), so ``T_max`` is
        structurally bounded far below an 18-month key: the invariant holds by construction rather
        than by configuration discipline. The brief's "6-month refresh token" illustrates the
        *formula*; it is not currently reachable, and asking for it yields 30 days.

        The guard stays because that bound is a Phase 2 constant, not a law. Raise the ceiling
        toward six months and this test is the one that says the key validity must move with it.
        """
        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(6 * MONTH))
        assert K._t_max() == 30 * 24 * 60 * 60
        assert K._t_max() < K.DEFAULT_VALIDITY_SECONDS


class TestTheInvariantThatProtectsOutstandingTokens:
    """FR-4c. The property that makes a long-lived key safe: nothing it signed dies early."""

    def test_a_token_minted_just_before_the_cutoff_is_still_verifiable_at_its_expiry(
        self, key_dir, month_tokens
    ):
        """AC-6b — the case a "does rotation work" test misses.

        Such a test mints a token *after* rotating and naturally passes. The token at risk is the
        last one the outgoing key signed, at the final instant it was allowed to sign.
        """
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        t_max = MONTH

        last_signable = key.signing_cutoff(t_max) - 1
        assert key.can_sign(t_max, now=last_signable)

        token_expiry = last_signable + t_max
        assert key.can_verify(now=token_expiry), (
            "a token signed at the last legal instant outlives its key's ability to verify it"
        )

    def test_the_key_stops_signing_before_it_stops_verifying(self, key_dir, month_tokens):
        key = _set(key_dir, 18 * MONTH).ensure(now=T0)
        cutoff = key.signing_cutoff(MONTH)
        assert not key.can_sign(MONTH, now=cutoff + 1)
        assert key.can_verify(now=cutoff + 1)

    def test_a_validity_shorter_than_t_max_is_refused_at_startup(self, key_dir, monkeypatch):
        """FR-4d. The residual error the derivation cannot design away.

        The cutoff would land in the past, so the key could never sign anything and the deployment
        could not mint a session at all.
        """
        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(MONTH))
        short = 10 * 24 * 60 * 60  # ten days, under the 30-day T_max
        with pytest.raises(K.KeyError_) as exc:
            _set(key_dir, short).ensure(now=T0)
        message = str(exc.value)
        assert str(short) in message and str(MONTH) in message, (
            "the refusal must name both values, or it is a puzzle rather than a fix"
        )

    def test_the_default_gives_more_than_a_year_of_signing_at_the_real_ceiling(
        self, key_dir, month_tokens
    ):
        """The feature lead's floor: "at least 1 year + extra time for the last issued token".

        18-month validity against today's 1-month ceiling is 17 months of signing. Asserted rather
        than assumed, because it is a property of two independently configurable numbers and either
        could move.
        """
        key = _set(key_dir).ensure(now=T0)
        signing_period = key.signing_cutoff(K._t_max()) - T0
        assert signing_period >= K.MIN_INTENDED_SIGNING_PERIOD_SECONDS
        assert signing_period == pytest.approx(17 * MONTH)

    def test_a_short_signing_period_warns_but_still_works(self, key_dir, month_tokens, caplog):
        """Warned, not refused — the severity is the point.

        Such a key is not unsafe: the cutoff derivation still guarantees every token verifies for
        its full life. It just rotates constantly, which is the exact thing a long-lived key exists
        to avoid. Refusing to boot over a working configuration would be the wrong response;
        silence would let the intent decay unnoticed.
        """
        with caplog.at_level(logging.WARNING):
            key = _set(key_dir, 3 * MONTH).ensure(now=T0)

        assert key.kid, "a short signing period must still produce a usable key"
        assert "below the intended" in caplog.text
        assert key.can_verify(now=T0 + 2 * MONTH)

    def test_no_warning_at_the_default(self, key_dir, month_tokens, caplog):
        """Guards the other direction: a floor that always fires is noise nobody reads."""
        with caplog.at_level(logging.WARNING):
            _set(key_dir).ensure(now=T0)
        assert "below the intended" not in caplog.text

    def test_rotation_is_refused_on_the_same_grounds(self, key_dir, monkeypatch):
        """The check belongs on every minting path, not only the bootstrap one."""
        monkeypatch.setenv("RACK_WEB_REFRESH_TTL_SECONDS", str(MONTH))
        with pytest.raises(K.KeyError_):
            _set(key_dir, 10 * 24 * 60 * 60).rotate(now=T0)


class TestRotation:
    """FR-4, FR-4e, FR-4f. Appends; never invalidates what is outstanding."""

    def test_rotation_keeps_the_previous_key_as_a_verifier(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        old = ks.ensure(now=T0)
        new = ks.rotate(now=T0 + MONTH)

        assert new.kid != old.kid
        assert ks.verifier(old.kid, now=T0 + MONTH) is not None
        assert {k["kid"] for k in ks.public_jwks(now=T0 + MONTH)["keys"]} == {old.kid, new.kid}

    def test_the_newest_key_signs_after_a_rotation(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        ks.ensure(now=T0)
        rotated = ks.rotate(now=T0 + MONTH)
        assert ks.ensure(now=T0 + MONTH + 1).kid == rotated.kid

    def test_a_key_past_its_cutoff_rotates_rather_than_refusing(self, key_dir, month_tokens):
        """FR-4e. An expiry that refuses is an outage on a date chosen 18 months earlier."""
        ks = _set(key_dir, 18 * MONTH)
        first = ks.ensure(now=T0)
        after_cutoff = first.signing_cutoff(MONTH) + 1

        successor = ks.ensure(now=after_cutoff)

        assert successor.kid != first.kid
        assert successor.can_sign(MONTH, now=after_cutoff)
        assert ks.verifier(first.kid, now=after_cutoff) is not None, (
            "the outgoing key must keep verifying its outstanding tokens"
        )

    def test_steady_state_is_two_keys(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        first = ks.ensure(now=T0)
        ks.ensure(now=first.signing_cutoff(MONTH) + 1)
        assert len(ks.public_jwks(now=first.signing_cutoff(MONTH) + 1)["keys"]) == 2

    def test_a_fully_expired_key_is_retired(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        first = ks.ensure(now=T0)
        later = T0 + 19 * MONTH

        ks.ensure(now=later)

        assert ks.verifier(first.kid, now=later) is None
        assert not (key_dir / f"{first.kid}.pem").exists()
        assert first.kid not in {k["kid"] for k in ks.public_jwks(now=later)["keys"]}


class TestTheJwksNeverLeaksPrivateMaterial:
    """FR-6a. The one mistake in this phase that cannot be walked back."""

    PRIVATE_FIELDS = ("d", "p", "q", "dp", "dq", "qi")

    def test_no_private_field_appears_in_any_published_key(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        ks.ensure(now=T0)
        ks.rotate(now=T0 + MONTH)

        published = ks.public_jwks(now=T0 + MONTH)["keys"]
        assert len(published) == 2, "check every key, not just the first"
        for jwk in published:
            for field in self.PRIVATE_FIELDS:
                assert field not in jwk, f"{field!r} present in published key {jwk.get('kid')}"

    def test_the_serialised_jwks_does_not_contain_the_private_scalar(self, key_dir, month_tokens):
        """Asserted against the rendered JSON as well as the dict.

        A field can be absent from the mapping and still reach the wire through a nested object or
        a custom encoder, and it is the bytes on the wire that matter.
        """
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        secret = key.private.private_numbers().private_value
        size = (key.private.curve.key_size + 7) // 8
        encoded = base64.urlsafe_b64encode(secret.to_bytes(size, "big")).decode().rstrip("=")

        rendered = json.dumps(ks.public_jwks(now=T0))

        assert encoded not in rendered
        assert "BEGIN" not in rendered  # no PEM smuggled in either

    def test_the_published_key_verifies_a_signature_from_the_private_half(self, key_dir, month_tokens):
        """AC-9a. The JWKS must be *usable*, not merely free of secrets.

        Asserting only what the JWKS must not contain would be satisfied by an empty document.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import utils as asym_utils

        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        message = b"instance identity"
        signature = key.private.sign(message, ec.ECDSA(hashes.SHA256()))

        jwk = ks.public_jwks(now=T0)["keys"][0]
        pad = "=" * (-len(jwk["x"]) % 4)
        x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + pad), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + pad), "big")
        rebuilt = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()

        rebuilt.verify(signature, message, ec.ECDSA(hashes.SHA256()))  # raises on failure
        del asym_utils

    def test_every_published_key_carries_the_fields_a_client_needs(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        ks.ensure(now=T0)
        for jwk in ks.public_jwks(now=T0)["keys"]:
            assert {"kty", "crv", "alg", "use", "kid", "x", "y"} <= set(jwk)
            assert jwk["alg"] == "ES256"
            assert jwk["use"] == "sig"


class TestDamagedState:
    """What happens when the key directory is not what we left there.

    The distinction throughout is between damage we can survive and damage we must not paper over:
    a *missing* key means some tokens cannot be verified, which is bad; a *wrong* key means we would
    sign or publish something incorrect, which is worse.
    """

    def test_corrupt_metadata_is_refused_rather_than_silently_reset(self, key_dir, month_tokens):
        """Regenerating on unreadable metadata would discard keys whose tokens are still live."""
        _set(key_dir, 18 * MONTH).ensure(now=T0)
        (key_dir / "keys.json").write_text("{ not json")
        K.reset_for_tests()

        with pytest.raises(K.KeyError_, match="unreadable"):
            _set(key_dir, 18 * MONTH).ensure(now=T0 + 1)

    def test_a_metadata_row_whose_key_file_is_gone_is_skipped_not_fatal(
        self, key_dir, month_tokens, caplog
    ):
        """Deliberately survivable, unlike the corruption case above.

        The tokens that key signed cannot be verified whether we start or not, so refusing to boot
        would turn a partial loss into a total outage. It is logged, because silently serving a
        smaller key set than the metadata claims is how this goes unnoticed.
        """
        ks = _set(key_dir, 18 * MONTH)
        lost = ks.ensure(now=T0)
        kept = ks.rotate(now=T0 + 60)
        (key_dir / f"{lost.kid}.pem").unlink()
        K.reset_for_tests()

        with caplog.at_level(logging.WARNING):
            reloaded = _set(key_dir, 18 * MONTH)
            assert reloaded.ensure(now=T0 + 120).kid == kept.kid
        assert lost.kid in caplog.text

    def test_an_rsa_key_in_the_directory_is_refused(self, key_dir, month_tokens):
        """The module is ES256-only; an RSA key here would be signed with the wrong algorithm."""
        from cryptography.hazmat.primitives import serialization as ser
        from cryptography.hazmat.primitives.asymmetric import rsa

        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        (key_dir / f"{key.kid}.pem").write_bytes(
            rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
                encoding=ser.Encoding.PEM,
                format=ser.PrivateFormat.PKCS8,
                encryption_algorithm=ser.NoEncryption(),
            )
        )
        K.reset_for_tests()

        with pytest.raises(K.KeyError_, match="EC private key"):
            _set(key_dir, 18 * MONTH).ensure(now=T0 + 1)


class TestConfiguration:
    def test_an_unparseable_validity_falls_back_to_the_default(self, monkeypatch, caplog):
        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_VALIDITY_SECONDS", "eighteen months")
        with caplog.at_level(logging.WARNING):
            assert K._configured_validity() == K.DEFAULT_VALIDITY_SECONDS
        assert "not an integer" in caplog.text

    def test_the_directory_is_configurable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "elsewhere"))
        K.reset_for_tests()
        assert K.key_set().directory == tmp_path / "elsewhere"

    def test_the_process_key_set_follows_a_directory_change(self, tmp_path, monkeypatch):
        """Otherwise a test — or a reconfigured deployment — keeps serving the previous set."""
        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "a"))
        K.reset_for_tests()
        first = K.key_set()
        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "b"))
        assert K.key_set() is not first
        assert K.key_set().directory == tmp_path / "b"


class TestThePrivateKeyStaysOutOfEveryObservable:
    """NFR-3, AC-25a. Five named observables, because "never appears in a log line" is untestable.

    The draft requirement quantified over every future log statement, which no test can establish.
    These are the five places it would realistically leak, each of which a test can actually check.
    """

    def _secret_bytes(self, key):
        return key.private.private_numbers().private_value.to_bytes(32, "big")

    def test_it_is_not_in_the_jwks(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        rendered = json.dumps(ks.public_jwks(now=T0))
        assert K._b64u(self._secret_bytes(key)) not in rendered

    def test_it_is_not_in_the_repr_or_str_of_the_holder(self, key_dir, month_tokens):
        """A dataclass `repr` that dumps the key object is how a secret reaches a traceback."""
        key = _set(key_dir, 18 * MONTH).ensure(now=T0)
        secret = str(key.private.private_numbers().private_value)

        for rendering in (repr(key), str(key), repr(key.private), str(key.private)):
            assert secret not in rendering
            assert "BEGIN" not in rendering

    def test_it_is_not_in_the_pem_path_or_the_metadata(self, key_dir, month_tokens):
        """The PEM on disk holds it by necessity; the metadata index must not."""
        ks = _set(key_dir, 18 * MONTH)
        ks.ensure(now=T0)
        meta = (key_dir / "keys.json").read_text()
        assert "BEGIN" not in meta and "PRIVATE" not in meta

    def test_it_is_not_in_an_error_raised_on_a_bad_key_set(self, key_dir, month_tokens):
        """A message that quotes file contents to be helpful is a common leak."""
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        pem = (key_dir / f"{key.kid}.pem").read_text()
        (key_dir / f"{key.kid}.pem").rename(key_dir / "moved.pem")
        meta = json.loads((key_dir / "keys.json").read_text())
        meta["keys"][0]["kid"] = "moved"
        (key_dir / "keys.json").write_text(json.dumps(meta))
        K.reset_for_tests()

        with pytest.raises(K.KeyError_) as exc:
            _set(key_dir, 18 * MONTH).ensure(now=T0 + 1)
        assert "BEGIN" not in str(exc.value)
        assert pem.split("\n")[1] not in str(exc.value)


class TestTheCodeTableIsBounded:
    """NFR-5. A bound that is never exercised is a bound that may not hold."""

    def test_outstanding_codes_never_exceed_the_capacity(self):
        from identity import oidc_codes

        store = oidc_codes.CodeStore(capacity=16)
        _verifier, challenge = oidc_codes.verifier_for("seed")
        for i in range(200):
            store.issue(
                client_id="c", principal=f"user:u{i}", redirect_uri="https://x/cb",
                code_challenge=challenge, code_challenge_method="S256",
            )
        assert store.outstanding() <= 16

    def test_expired_codes_are_evicted_on_issue(self):
        """Eviction happens on a write path, never on a read -- a read that writes would defeat any
        cache above it, which is the Phase 2 revocation-cache lesson."""
        from identity import oidc_codes

        store = oidc_codes.CodeStore(ttl_seconds=60, capacity=1024)
        _v, challenge = oidc_codes.verifier_for("seed")
        store.issue(client_id="c", principal="user:a", redirect_uri="https://x/cb",
                    code_challenge=challenge, code_challenge_method="S256", now=1000.0)
        assert store.outstanding() == 1

        store.issue(client_id="c", principal="user:b", redirect_uri="https://x/cb",
                    code_challenge=challenge, code_challenge_method="S256", now=2000.0)
        assert store.outstanding() == 1, "the lapsed code should be gone, not merely unusable"


class TestReadsDoNotWrite:
    """NFR-2a. A read path that writes defeats any cache built over it (Phase 2, trap 2)."""

    def test_reading_the_jwks_does_not_touch_the_metadata_file(self, key_dir, month_tokens):
        ks = _set(key_dir, 18 * MONTH)
        ks.ensure(now=T0)
        meta = key_dir / "keys.json"
        before = meta.stat().st_mtime_ns

        for _ in range(5):
            ks.public_jwks(now=T0)
            ks.verifier("nope", now=T0)

        assert meta.stat().st_mtime_ns == before

    def test_a_lookup_for_an_expired_key_does_not_prune(self, key_dir, month_tokens):
        """Pruning belongs on write paths. A read that prunes changes the mtime, and the mtime is
        part of any cache key over this file — so the cache would invalidate itself on every read."""
        ks = _set(key_dir, 18 * MONTH)
        key = ks.ensure(now=T0)
        before = (key_dir / "keys.json").stat().st_mtime_ns

        assert ks.verifier(key.kid, now=T0 + 19 * MONTH) is None
        assert (key_dir / f"{key.kid}.pem").exists()
        assert (key_dir / "keys.json").stat().st_mtime_ns == before
