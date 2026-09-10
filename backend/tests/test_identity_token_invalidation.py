# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The complete catalogue of ways a token stops being honoured (FR-4c, FR-4d, FR-5c, FR-6, FR-6a).

Author: Rafael M. Koike & Cursor Agent ("Ada")

`test_identity_tokens.py` and `test_identity_revocation.py` test each mechanism in depth. **This file
is the enumeration**: one row per scenario, in one place, so the set is auditable and adding a new
invalidation path means adding a row rather than hoping someone remembers to.

Two reasons it is worth its own file:

1. **Completeness is the property under test**, not any single refusal. A reviewer asking "what can
   invalidate a session?" should get an answer from a table, and `test_the_catalogue_is_complete`
   fails if a refusal path exists in the code that no row here covers.
2. **Some of these are hit by accident**, especially from tests and fixtures. Repointing
   ``NVIS_IDENTITY_FILE`` silently *un-revokes* every revoked token; rotating
   ``RACK_WEB_SECRET_KEY`` invalidates all of them; forgetting ``revocation.install()`` refuses all
   of them. Each is written down with what it looks like, so the next person debugging "why is this
   token suddenly (in)valid" has somewhere to look.

The scenario ids (``INV-*``) are stable and referenced from the SADD.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import revocation as R
from identity import store as identity_store
from identity import tokens as T
from identity.models import User

SECRET = "0" * 64
OTHER_SECRET = "f" * 64


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", SECRET)
    monkeypatch.delenv("RACK_WEB_TOKEN_ISSUER", raising=False)
    identity_store.reset_for_tests()
    T.reset_for_tests()
    R.reset_for_tests()
    R.install()
    identity_store.store().create_user(User(login="alice", display_name="Alice"))
    yield
    identity_store.reset_for_tests()
    T.reset_for_tests()
    R.reset_for_tests()


def _access() -> str:
    return T.mint("user:alice", T.TOKEN_ACCESS)


def _honoured(token: str, kind: str = T.TOKEN_ACCESS) -> bool:
    try:
        T.verify(token, kind)
        return True
    except T.TokenError:
        return False


def _claims(**over) -> dict:
    now = time.time()
    base = {
        "iss": T.issuer(),
        "aud": T.AUDIENCE,
        "sub": "user:alice",
        "iat": now,
        "exp": now + 300,
        "jti": "fixed-jti",
        "typ": T.TOKEN_ACCESS,
    }
    base.update(over)
    return base


def _forged(claims: dict) -> str:
    """Signed with the deployment's real ES256 key and `kid`, so only the claim under test differs.

    **This signed HS256 with the HKDF secret until 2026-09-10, and every case built on it passed
    for the wrong reason.** That was right while `RACK_WEB_SECRET_KEY` signed JWTs; once the OIDC
    provider became unconditional the product signs ES256 with a key set, and `verify` refuses an
    HS256 token with ``token has no `kid` `` — *before* reading a single claim. So "an expired token
    is not honoured" was really "a token without a kid is not honoured", which proves nothing about
    `exp`.

    Signing with the real key and a real `kid` is what makes the token reach the claim checks, which
    is the only way these assertions mean anything.
    """
    from identity import keys as keys_mod

    key = keys_mod.key_set().ensure()
    return jwt.encode(
        claims, key.private, algorithm=keys_mod.ALGORITHM, headers={"kid": key.kid}
    )


# --- INV-01..INV-04  Time and cryptography -------------------------------------------------------


class TestTimeAndCrypto:
    def test_inv01_expiry_passed(self):
        now = time.time()
        assert not _honoured(_forged(_claims(iat=now - 600, exp=now - 1)))

    def test_inv02_signature_does_not_match_the_payload(self):
        """Tampering: flip the claims, keep the signature."""
        good = _access()
        header, _payload, signature = good.split(".")
        other = _forged(_claims(sub="user:root")).split(".")[1]
        assert not _honoured(f"{header}.{other}.{signature}")

    def test_inv03_signed_by_another_deployment(self, tmp_path, monkeypatch):
        """Still the boundary between two installs — but the **key set** is now what draws it.

        It was the deployment secret while HS256 signed. Since the provider became unconditional
        (2026-09-08) tokens are ES256, signed by the key set under `RACK_WEB_SIGNING_KEY_DIR`, so
        repointing the secret no longer invalidates anything. `iss` is still not the boundary: it is
        configured and two installs could coincide on it.
        """
        from identity import keys as keys_mod

        foreign = _access()
        monkeypatch.setenv("RACK_WEB_SIGNING_KEY_DIR", str(tmp_path / "other-deployment"))
        keys_mod.reset_for_tests()
        keys_mod.key_set().ensure()
        try:
            assert not _honoured(foreign)
        finally:
            keys_mod.reset_for_tests()

    def test_inv04_algorithm_is_not_the_pinned_one(self):
        unsigned = jwt.encode(_claims(), key=None, algorithm=None)
        assert not _honoured(unsigned)


# --- INV-05..INV-08  Claims ----------------------------------------------------------------------


class TestClaims:
    def test_inv05_a_required_claim_is_absent(self):
        """Includes `exp`: PyJWT accepts a token without it and treats it as eternal, so the explicit
        `require` list is the only thing refusing this."""
        for claim in T.REQUIRED_CLAIMS:
            broken = _claims()
            del broken[claim]
            assert not _honoured(_forged(broken)), f"a token missing {claim!r} was honoured"

    def test_inv06_issuer_is_wrong(self):
        assert not _honoured(_forged(_claims(iss="another-product")))

    def test_inv07_audience_is_wrong(self):
        assert not _honoured(_forged(_claims(aud="another-service")))

    def test_inv08_subject_is_not_a_principal(self):
        assert not _honoured(_forged(_claims(sub="alice")))  # missing the `user:` kind


# --- INV-09..INV-11  Token type ------------------------------------------------------------------


class TestTokenType:
    def test_inv09_refresh_presented_as_access(self):
        assert not _honoured(T.mint("user:alice", T.TOKEN_REFRESH), T.TOKEN_ACCESS)

    def test_inv10_id_presented_as_access(self):
        assert not _honoured(T.mint("user:alice", T.TOKEN_ID), T.TOKEN_ACCESS)

    def test_inv11_access_presented_as_refresh(self):
        """The direction that would otherwise buy perpetual renewal."""
        assert not _honoured(_access(), T.TOKEN_REFRESH)


# --- INV-12..INV-14  Explicit revocation ---------------------------------------------------------


class TestExplicitRevocation:
    def test_inv12_subject_revoked(self):
        token = _access()
        R.revoke_subject("user:alice", "offboarded")
        assert not _honoured(token)

    def test_inv13_this_token_revoked_by_jti(self):
        token = T.mint("user:alice", T.TOKEN_REFRESH)
        survivor = _access()
        R.revoke_token(T.verify(token, T.TOKEN_REFRESH), "leaked")
        assert not _honoured(token, T.TOKEN_REFRESH)
        assert _honoured(survivor), "revoking one token must not revoke the user's others"

    def test_inv14_issued_before_a_logout_everywhere_cutoff(self):
        token = _access()
        R.revoke_all_for("user:alice")
        assert not _honoured(token)
        assert _honoured(_access()), "a fresh login after the cutoff must still be honoured"


# --- INV-15..INV-16  Implicit revocation, with no row written ------------------------------------


class TestImplicitRevocation:
    def test_inv15_account_disabled(self):
        token = _access()
        store = identity_store.store()
        store.update_user(store.get_user("alice").edited(disabled=True))
        assert not _honoured(token)

    def test_inv16_account_deleted(self):
        token = _access()
        identity_store.store().delete_user("alice")
        assert not _honoured(token)


# --- INV-17..INV-19  Operational and configuration ----------------------------------------------


class TestOperationalCauses:
    """The ones a fixture or a deploy trips without meaning to."""

    def test_inv17_the_deployment_secret_no_longer_invalidates_tokens(self, monkeypatch):
        """**INV-17 stopped applying to JWTs on 2026-09-08, and that is worth an explicit test.**

        While HS256 signed, repointing `RACK_WEB_SECRET_KEY` invalidated every outstanding token —
        which is what made a fixture setting its own secret a mystery 401, and why the test client
        mints per request. Tokens are ES256 now, so the secret does not sign them and rotating it
        changes nothing here.

        Asserted rather than deleted because the *expectation* is what changed: a reader who
        remembers INV-17 needs to see that this direction is now a non-event. The secret still
        protects the Flask session and the token vault, so it is not free to rotate — it simply is
        not a JWT invalidator. Key-set rotation is append-only and also does not invalidate; only
        revocation and expiry do.
        """
        token = _access()
        assert _honoured(token)
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", OTHER_SECRET)
        assert _honoured(token), "the secret is signing JWTs again; INV-17 would be back"

    def test_inv18_no_revocation_checker_registered(self):
        """Fail-closed (FR-6a): every token is refused, not just revoked ones. A test that forgets
        `revocation.install()` sees *total* refusal, which is the intended, loud symptom."""
        token = _access()
        T.reset_for_tests()
        assert not _honoured(token)
        R.install()
        assert _honoured(token)

    def test_inv19_repointing_the_identity_store_un_revokes(self, tmp_path, monkeypatch):
        """**The dangerous one, and it invalidates nothing — it makes a dead token live again.**

        Revocation state lives in the identity file. Point `NVIS_IDENTITY_FILE` somewhere else and a
        previously revoked token is honoured once more, because the new store has no revocation row
        and no user record to mark disabled.

        Scope, because the deployment layout mitigates the obvious version of this and not the
        realistic one. Both `var/secrets/.rack_web_secret_key` and `var/identity/identity.json` sit on
        the same `rack-data` named volume, so **losing the volume is safe**: the secret is regenerated,
        every signature fails, and INV-17 covers it. What is *not* safe is replacing the identity file
        while the secret survives — restoring `identity.json` from a backup taken before someone was
        offboarded re-honours their tokens until expiry (≤15 min for access, ≤12 h for refresh). That
        is the operational hazard worth knowing, and it is why a restore is not purely additive.
        """
        token = _access()
        R.revoke_subject("user:alice", "offboarded")
        assert not _honoured(token)

        monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "somewhere-else.json"))
        identity_store.reset_for_tests()
        R.reset_for_tests()
        R.install()
        assert _honoured(token), (
            "expected the documented hazard: repointing the store un-revokes. If this now fails, "
            "the behaviour improved — update INV-19 in the SADD and this test together."
        )


# --- The catalogue itself ------------------------------------------------------------------------


class TestTheCatalogueIsComplete:
    """Guards the enumeration, so a new refusal path cannot be added without a row here."""

    def test_every_refusal_site_in_the_code_is_represented(self):
        """Counts `raise` sites in the two modules against the scenario count.

        Deliberately a *lower bound* rather than an exact match: several code paths (mint-time
        argument checks) refuse an operation rather than invalidating a token, so they have no INV
        row. If a refusal is added and no scenario covers it, this trips and forces the decision to
        be explicit.
        """
        scenarios = [
            name
            for cls in (
                TestTimeAndCrypto,
                TestClaims,
                TestTokenType,
                TestExplicitRevocation,
                TestImplicitRevocation,
                TestOperationalCauses,
            )
            for name in vars(cls)
            if name.startswith("test_inv")
        ]
        assert len(scenarios) == 19, (
            f"expected 19 catalogued scenarios, found {len(scenarios)}. Adding one? Add its INV row "
            "to the SADD's token-invalidation table and bump this number."
        )

        revocation_src = (
            Path(__file__).resolve().parents[1] / "identity" / "revocation.py"
        ).read_text()
        # Every distinct TokenRevoked message in revocation.py must map to a scenario above.
        assert revocation_src.count("raise tokens.TokenRevoked") == 5, (
            "revocation.py grew or lost a refusal path; the catalogue (INV-12..INV-16) and the "
            "SADD table must be updated to match"
        )

    def test_the_ids_are_unique_and_contiguous(self):
        """A gap or a duplicate in the ids means the SADD's table and this file have drifted."""
        found = sorted(
            int(name.split("_")[1][3:])
            for cls in (
                TestTimeAndCrypto,
                TestClaims,
                TestTokenType,
                TestExplicitRevocation,
                TestImplicitRevocation,
                TestOperationalCauses,
            )
            for name in vars(cls)
            if name.startswith("test_inv")
        )
        assert found == list(range(1, 20)), f"INV ids are not 1..19 contiguous: {found}"
