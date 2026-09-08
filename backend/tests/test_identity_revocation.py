# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Revocation: denials are never stale, and the cache cannot make them so (FR-6, FR-6a, NFR-3).

Author: Rafael M. Koike & Cursor Agent ("Ada")

The invariant under test is *grants may be stale, denials may not*. Most of these assert a token
stops working, and the cache tests exist because a cache is the one thing that can quietly turn a
denial into a grant — which is exactly the failure that would not show up in a functional test.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import revocation as R
from identity import store as identity_store
from identity import tokens as T
from identity.models import User


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    monkeypatch.delenv("RACK_WEB_TOKEN_ISSUER", raising=False)
    identity_store.reset_for_tests()
    T.reset_for_tests()
    R.reset_for_tests()
    R.install()
    identity_store.store().create_user(User(login="alice", display_name="Alice"))
    identity_store.store().create_user(User(login="bob", display_name="Bob"))
    yield
    identity_store.reset_for_tests()
    T.reset_for_tests()
    R.reset_for_tests()


def _token(login: str = "alice", kind: str = T.TOKEN_ACCESS) -> str:
    return T.mint(f"user:{login}", kind)


def _verifies(token: str, kind: str = T.TOKEN_ACCESS) -> bool:
    try:
        T.verify(token, kind)
        return True
    except T.TokenError:
        return False


class TestInstallation:
    def test_install_satisfies_the_fail_closed_gate(self):
        """FR-6a: `tokens.verify` refuses everything until a checker is registered, and this is the
        module that registers one."""
        T.reset_for_tests()
        assert T.revocation_checker_registered() is False
        assert not _verifies(_token())
        R.install()
        assert T.revocation_checker_registered() is True
        assert _verifies(_token())


class TestRevokeBySubject:
    def test_a_revoked_subject_is_refused(self):
        token = _token("bob")
        assert _verifies(token)
        R.revoke_subject("user:bob", "offboarded")
        assert not _verifies(token)

    def test_revoking_one_subject_leaves_others_alone(self):
        alice, bob = _token("alice"), _token("bob")
        R.revoke_subject("user:bob")
        assert _verifies(alice)
        assert not _verifies(bob)

    def test_a_revoked_subject_cannot_get_a_new_token_honoured_either(self):
        """Subject revocation outlives the token that was live when it happened — otherwise a
        disabled user simply logs in again."""
        R.revoke_subject("user:bob")
        assert not _verifies(_token("bob"))

    def test_restoring_a_subject_works(self):
        R.revoke_subject("user:bob")
        R.restore_subject("user:bob")
        assert _verifies(_token("bob"))

    def test_the_reason_reaches_the_error(self):
        """So the audit trail records *why*, not just that a refusal happened."""
        R.revoke_subject("user:bob", "offboarded 2026-08-25")
        with pytest.raises(T.TokenRevoked, match="offboarded"):
            T.verify(_token("bob"), T.TOKEN_ACCESS)

    def test_a_malformed_principal_cannot_be_revoked(self):
        """Guards against a revocation row nobody's token will ever match — a control that silently
        does nothing is worse than a missing one."""
        with pytest.raises(Exception):  # noqa: B017 - principals raises its own type
            R.revoke_subject("not-a-principal")


class TestRevokeOneToken:
    def test_a_revoked_token_is_refused(self):
        token = _token("alice", T.TOKEN_REFRESH)
        R.revoke_token(T.verify(token, T.TOKEN_REFRESH), "leaked")
        assert not _verifies(token, T.TOKEN_REFRESH)

    def test_the_users_other_tokens_survive(self):
        """FR-5b's "the renewal path is itself revocable" is only useful at this granularity: burning
        a leaked refresh token must not log the user out of everything."""
        burn = _token("alice", T.TOKEN_REFRESH)
        keep = _token("alice", T.TOKEN_ACCESS)
        R.revoke_token(T.verify(burn, T.TOKEN_REFRESH), "leaked")
        assert not _verifies(burn, T.TOKEN_REFRESH)
        assert _verifies(keep)

    def test_claims_without_a_jti_are_refused_loudly(self):
        with pytest.raises(ValueError, match="no jti"):
            R.revoke_token({"sub": "user:alice", "exp": time.time() + 60})

    def test_expired_revocation_rows_are_pruned(self):
        """Unbounded growth on a hot-path table. Pruning is safe because an expired token is refused
        by `verify` on its own merits, so the row stops meaning anything."""
        store = identity_store.store()
        store.revoke_token("long-gone", expires_at=time.time() - 10, reason="old")
        store.revoke_token("still-live", expires_at=time.time() + 600, reason="current")
        tokens_table = store.revocation_snapshot()["tokens"]
        assert "long-gone" not in tokens_table
        assert "still-live" in tokens_table


class TestLogOutEverywhere:
    """The `not_before` cutoff — and the bug it originally had."""

    def test_existing_tokens_stop_working(self):
        token = _token("alice")
        R.revoke_all_for("user:alice")
        assert not _verifies(token)

    def test_a_fresh_login_immediately_afterwards_is_accepted(self):
        """The regression test for a real defect. The first implementation pushed the cutoff a second
        into the future to avoid leaving a just-minted token valid, which then refused the user's own
        next login for up to a second — a password change looking like a broken account. Fixed by
        recording `iat` with sub-second precision instead."""
        R.revoke_all_for("user:alice")
        assert _verifies(_token("alice"))

    def test_repeated_cycles_never_leak_or_lock_out(self):
        """Runs the boundary many times, because a sub-second ordering bug shows up as *flakiness*
        rather than a failure — the shape of defect a single-shot test misses."""
        leaked = locked = 0
        for _ in range(120):
            stale = _token("alice")
            R.revoke_all_for("user:alice")
            if _verifies(stale):
                leaked += 1
            if not _verifies(_token("alice")):
                locked += 1
        assert (leaked, locked) == (0, 0)

    def test_it_does_not_affect_other_subjects(self):
        bob = _token("bob")
        R.revoke_all_for("user:alice")
        assert _verifies(bob)

    def test_an_explicit_moment_is_honoured(self):
        """So an operator can revoke as of a known past incident time."""
        token = _token("alice")
        R.revoke_all_for("user:alice", moment=time.time() + 300)
        assert not _verifies(token)


class TestImplicitRevocation:
    """Defence in depth: a disabled or deleted user must not keep working tokens even if nobody
    wrote a revocation row."""

    def test_a_disabled_user_is_refused(self):
        token = _token("bob")
        store = identity_store.store()
        store.update_user(store.get_user("bob").edited(disabled=True))
        assert not _verifies(token)

    def test_a_deleted_user_is_refused(self):
        token = _token("bob")
        identity_store.store().delete_user("bob")
        assert not _verifies(token)

    def test_a_deleted_user_reports_absence_rather_than_disablement(self):
        token = _token("bob")
        identity_store.store().delete_user("bob")
        with pytest.raises(T.TokenRevoked, match="no longer exists"):
            T.verify(token, T.TOKEN_ACCESS)


class TestTheCacheCannotStaleADenial:
    """NFR-3 wants this check cached; FR-6 caps staleness at 10 seconds. These are the tests that
    stop the cache from quietly turning a denial into a grant."""

    def test_a_revocation_in_this_process_takes_effect_immediately(self):
        token = _token("bob")
        assert _verifies(token)  # populates the cache
        R.revoke_subject("user:bob")
        assert not _verifies(token), "cache served a stale allow after an in-process revocation"

    def test_the_cache_is_actually_serving_reads(self):
        """Otherwise the staleness tests below prove nothing — a cache that never hits cannot go
        stale, and the NFR-3 budget would be unmet for a different reason."""
        _verifies(_token())
        before = identity_store.store().revocation_snapshot
        calls = []
        identity_store.store().revocation_snapshot = lambda: (calls.append(1), before())[1]
        try:
            for _ in range(20):
                R.snapshot()
        finally:
            identity_store.store().revocation_snapshot = before
        assert len(calls) <= 1, f"expected a cached read, saw {len(calls)} store reads"

    def test_a_write_from_another_process_is_noticed(self, tmp_path, monkeypatch):
        """The gap the generation counter alone leaves: it is a process-local integer, so an operator
        running a CLI against the same file would never bump it and the cache could serve a stale
        allow indefinitely. Simulated by writing the file behind the store's back."""
        token = _token("bob")
        assert _verifies(token)  # warm the cache

        # A separate store handle over the same path stands in for another process: it writes the
        # file without touching this process's generation counter.
        other = identity_store.IdentityStore(os.environ["NVIS_IDENTITY_FILE"])
        generation_before = identity_store.current_generation()
        other.revoke_subject("user:bob")
        other.close()
        monkeypatch.setattr(identity_store, "current_generation", lambda: generation_before)

        assert not _verifies(token), "a revocation written by another process was not observed"

    def test_the_cache_has_a_hard_age_ceiling_below_the_fr6_bound(self):
        assert R.MAX_CACHE_AGE_SECONDS < 10, "FR-6 requires a revocation to bite within 10 s"

    def test_the_age_ceiling_forces_a_refresh(self, monkeypatch):
        """Belt and braces for a filesystem whose mtime granularity hides a fast second write."""
        _verifies(_token())
        reads = []
        real = identity_store.store().revocation_snapshot
        identity_store.store().revocation_snapshot = lambda: (reads.append(1), real())[1]
        try:
            R.snapshot()
            R.snapshot()
            while_cached = len(reads)
            monkeypatch.setattr(R, "MAX_CACHE_AGE_SECONDS", -1.0)
            R.snapshot()
            after_expiry = len(reads)
        finally:
            identity_store.store().revocation_snapshot = real
        assert while_cached <= 1, "repeated reads were not served from cache"
        assert after_expiry > while_cached, "an expired cache entry was not refreshed"

    @pytest.mark.skipif(
        bool(os.environ.get("CI")),
        reason="a latency measurement on a shared CI runner is noise. Run locally.",
    )
    def test_the_check_fits_the_latency_budget(self):
        """NFR-3 gives the check 5 ms at p95. Phase 1 is why this is measured rather than assumed:
        `catalog_context.resolve()` read TinyDB per audited request at 4.74 ms and breached the audit
        budget until it was cached on the generation counter."""
        claims = T.verify(_token(), T.TOKEN_ACCESS)
        samples = []
        for _ in range(300):
            started = time.perf_counter()
            R.check(claims)
            samples.append((time.perf_counter() - started) * 1000)
        p95 = statistics.quantiles(samples, n=20)[-1]
        assert p95 < 5.0, f"revocation check p95 {p95:.3f} ms exceeds the 5 ms budget"
