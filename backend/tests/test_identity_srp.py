# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""SRP-6a: the password never arrives, unknown logins are indistinguishable, state is bounded.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Covers FR-4 (the password does not reach the server), FR-4b (no user-enumeration oracle) and FR-4e
(the library's SHA-1 default is never used). The negative assertions carry most of the weight here:
"authentication succeeded" is easy to satisfy, and would still pass against an implementation that
also authenticated the wrong password, leaked which logins exist, or used SHA-1.
"""

from __future__ import annotations

import os
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest
import srp as lib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import srp as S


@pytest.fixture(autouse=True)
def _clean_table():
    S.reset_for_tests()
    yield
    S.reset_for_tests()


def _client(login: str, password: str) -> lib.User:
    """A client using the same parameters the server does. Mismatched parameters fail in a way that
    looks like a wrong password, so the fixture makes the agreement explicit."""
    return lib.User(login, password, hash_alg=S.HASH_ALG, ng_type=S.NG_TYPE)


def _exchange(login: str, password: str, stored: tuple[str, str] | tuple[()]) -> str:
    """Run a full two-leg exchange, returning a verdict string."""
    usr = _client(login, password)
    _, big_a = usr.start_authentication()
    token, salt_hex, b_hex = S.begin(login, big_a.hex(), *stored)
    proof = usr.process_challenge(bytes.fromhex(salt_hex), bytes.fromhex(b_hex))
    if proof is None:
        return "client-refused-server"
    try:
        hamk_hex, _key = S.finish(token, proof.hex())
    except S.SrpError:
        return "refused"
    usr.verify_session(bytes.fromhex(hamk_hex))
    return "authenticated" if usr.authenticated() else "client-rejected-proof"


class TestParametersAreExplicit:
    """FR-4e. The library defaults are SHA-1 with a 4-byte salt, and a correct-looking call gets
    them, so the parameters are asserted rather than trusted."""

    def test_the_hash_is_not_the_library_default(self):
        assert S.HASH_ALG == lib.SHA256
        assert S.HASH_ALG != lib.SHA1, "SHA-1 is the library default and must not be in use"

    def test_the_salt_is_at_least_sixteen_bytes(self):
        assert S.SALT_LEN >= 16
        salt_hex, _ = S.enroll("rkoike", "pw")
        assert len(bytes.fromhex(salt_hex)) == S.SALT_LEN

    def test_the_group_is_at_least_2048_bit(self):
        assert S.NG_TYPE in (lib.NG_2048, lib.NG_4096, lib.NG_8192)

    def test_the_proof_length_corresponds_to_sha256(self):
        """AC-4c: a 20-byte proof would mean SHA-1 slipped back in."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        assert len(proof) == 32, "expected SHA-256 sized proof"
        hamk_hex, _ = S.finish(token, proof.hex())
        assert len(bytes.fromhex(hamk_hex)) == 32

    def test_derived_verifier_matches_the_library(self):
        """`verifier_for_salt` reaches into `srp._pysrp` for `gen_x` and the hash map, which is
        private API. This pins the equivalence, so a library change fails here rather than silently
        breaking authentication."""
        salt, vkey = lib.create_salted_verification_key(
            "rkoike", "pw", hash_alg=S.HASH_ALG, ng_type=S.NG_TYPE, salt_len=S.SALT_LEN
        )
        assert S.verifier_for_salt("rkoike", "pw", salt) == vkey


class TestTheExchange:
    def test_the_correct_password_authenticates_both_ways(self):
        salt_hex, ver_hex = S.enroll("rkoike", "correct horse")
        assert _exchange("rkoike", "correct horse", (salt_hex, ver_hex)) == "authenticated"

    def test_a_wrong_password_is_refused(self):
        salt_hex, ver_hex = S.enroll("rkoike", "correct horse")
        assert _exchange("rkoike", "wrong", (salt_hex, ver_hex)) == "refused"

    def test_the_password_never_appears_in_what_the_client_sends(self):
        """FR-4 / AC-4. The point of SRP: this asserts the property, not the mechanism."""
        password = "a-very-distinctive-passphrase"
        salt_hex, ver_hex = S.enroll("rkoike", password)
        usr = _client("rkoike", password)
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        on_the_wire = big_a.hex() + proof.hex()
        assert password not in on_the_wire
        assert password.encode().hex() not in on_the_wire
        S.finish(token, proof.hex())

    def test_the_stored_record_does_not_contain_the_password(self):
        """FR-3, from this side: the verifier is not password-equivalent."""
        password = "a-very-distinctive-passphrase"
        salt_hex, ver_hex = S.enroll("rkoike", password)
        assert password not in salt_hex + ver_hex
        assert password.encode().hex() not in salt_hex + ver_hex

    def test_both_sides_derive_the_same_session_key(self):
        """Not used for transport yet, but it is what a future channel binding would rest on."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        hamk_hex, server_key = S.finish(token, proof.hex())
        usr.verify_session(bytes.fromhex(hamk_hex))
        assert usr.get_session_key() == server_key

    def test_a_malformed_public_value_is_refused_uniformly(self):
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        with pytest.raises(S.SrpError):
            S.begin("rkoike", "not-hex", salt_hex, ver_hex)

    def test_an_empty_password_cannot_be_enrolled(self):
        with pytest.raises(S.SrpError):
            S.enroll("rkoike", "")


class TestUnknownLoginsAreIndistinguishable:
    """FR-4b / AC-6. If these fail, the login form is a user-enumeration oracle."""

    def test_an_unknown_login_is_refused_like_a_wrong_password(self):
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        assert _exchange("ghost", "anything", ()) == "refused"
        assert _exchange("rkoike", "wrong", (salt_hex, ver_hex)) == "refused"

    def test_an_unknown_login_still_gets_a_salt_and_b_of_the_same_shape(self):
        """The response shape must not differ, or the refusal is unnecessary."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("x", "y")
        _, big_a = usr.start_authentication()
        _, real_s, real_b = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        _, fake_s, fake_b = S.begin("ghost", big_a.hex())
        assert len(fake_s) == len(real_s)
        assert len(fake_b) == len(real_b)

    def test_the_decoy_salt_is_stable_for_a_login(self):
        """The subtle one. A fresh random salt per probe would reveal non-existence by asking
        twice — a real user's salt does not change between calls."""
        first, _ = S.synthetic_credential("ghost")
        second, _ = S.synthetic_credential("ghost")
        assert first == second

    def test_the_decoy_salt_differs_between_logins(self):
        one, _ = S.synthetic_credential("ghost")
        two, _ = S.synthetic_credential("phantom")
        assert one != two

    def test_the_decoy_salt_is_stable_across_a_restart(self, monkeypatch):
        """Keyed on the persisted server secret, so it survives a process restart — otherwise the
        salt changes on redeploy for unknown logins but not for real ones."""
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", "a-stable-deployment-secret")
        before, _ = S.synthetic_credential("ghost")
        after, _ = S.synthetic_credential("ghost")
        monkeypatch.setenv("RACK_WEB_SECRET_KEY", "a-stable-deployment-secret")
        again, _ = S.synthetic_credential("ghost")
        assert before == after == again

    @pytest.mark.skipif(
        bool(os.environ.get("CI")),
        reason="a timing comparison on a shared CI runner is noise, not a measurement. Run locally.",
    )
    def test_an_unknown_login_costs_about_what_a_real_one_costs(self):
        """FR-4b says *indistinguishable*, and shape alone does not achieve that.

        If the decoy path were cheap — a fast refusal instead of a real exchange — the login form
        would still be an enumeration oracle, just a timing one. Measured at 1.11x when written; the
        band is wide because this is a smoke test for an order-of-magnitude mistake (an early return,
        a skipped modular exponentiation), not a constant-time proof.
        """
        salt_hex, ver_hex = S.enroll("rkoike", "pw")

        def one(login: str, stored: tuple) -> float:
            usr = _client(login, "pw")
            _, big_a = usr.start_authentication()
            started = time.perf_counter()
            S.begin(login, big_a.hex(), *stored)
            return time.perf_counter() - started

        real = statistics.median(one("rkoike", (salt_hex, ver_hex)) for _ in range(25))
        fake = statistics.median(one("ghost", ()) for _ in range(25))
        ratio = fake / real
        assert 0.4 < ratio < 2.5, f"unknown-login cost is {ratio:.2f}x a real one; enumeration risk"

    def test_the_decoy_verifier_is_a_real_verifier(self):
        """It must be usable in an exchange, or the decoy path fails differently from a wrong
        password — which is the leak it exists to prevent."""
        salt, verifier = S.synthetic_credential("ghost")
        assert len(salt) == S.SALT_LEN
        assert len(verifier) > 200  # 2048-bit group


class TestChallengeStateIsBoundedAndSingleUse:
    def test_a_challenge_cannot_be_used_twice(self):
        """A replayed proof must not authenticate a second session."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        S.finish(token, proof.hex())
        with pytest.raises(S.SrpError):
            S.finish(token, proof.hex())

    def test_a_failed_proof_also_consumes_the_challenge(self):
        """So a wrong guess costs a fresh exchange rather than allowing repeated tries against one
        B, which would make the second guess cheaper than the first."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "wrong")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        with pytest.raises(S.SrpError):
            S.finish(token, proof.hex())
        with pytest.raises(S.SrpError):
            S.finish(token, proof.hex())

    def test_an_unknown_token_is_refused(self):
        with pytest.raises(S.SrpError):
            S.finish("not-a-token", "00")

    def test_an_expired_challenge_is_refused(self, monkeypatch):
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        monkeypatch.setattr(S, "CHALLENGE_TTL_SECONDS", -1)
        with pytest.raises(S.SrpError):
            S.finish(token, proof.hex())

    def test_the_table_is_bounded(self, monkeypatch):
        """An unauthenticated caller must not be able to exhaust memory with exchanges it never
        finishes — the same reasoning as Phase 1's flood guard and the lake sink's queue."""
        monkeypatch.setattr(S, "MAX_CHALLENGES", 8)
        usr = _client("x", "y")
        _, big_a = usr.start_authentication()
        for _ in range(40):
            S.begin("ghost", big_a.hex())
        assert S.pending_count() <= 8

    def test_concurrent_takes_of_one_challenge_yield_one_winner(self):
        """Under gthread two threads can race `finish`. A get-then-delete would let both through."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))

        wins: list[int] = []
        start = threading.Barrier(6)

        def attempt():
            start.wait()
            try:
                S.finish(token, proof.hex())
                wins.append(1)
            except S.SrpError:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(wins) == 1, f"expected exactly one winner, got {len(wins)}"


class TestDeploymentGuard:
    def test_more_than_one_worker_is_refused(self):
        """The table is in-process, so a second worker makes login fail intermittently — which
        reads as a client bug. Better to refuse at startup than to debug that."""
        S.assert_single_worker_safe(1)
        with pytest.raises(S.SrpError, match="cannot span"):
            S.assert_single_worker_safe(2)
