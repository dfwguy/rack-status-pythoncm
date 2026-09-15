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
import random
import statistics
import sys
import threading
import time
from pathlib import Path

import pytest
import srp as lib
from srp import _pysrp

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
        _login, hamk_hex, _key = S.finish(token, proof.hex())
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

    def test_the_group_is_not_a_weak_one(self):
        assert S.NG_TYPE in (lib.NG_4096, lib.NG_8192)

    def test_the_proof_length_corresponds_to_sha256(self):
        """AC-4c: a 20-byte proof would mean SHA-1 slipped back in."""
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("rkoike", "pw")
        _, big_a = usr.start_authentication()
        token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
        assert len(proof) == 32, "expected SHA-256 sized proof"
        _login, hamk_hex, _ = S.finish(token, proof.hex())
        assert len(bytes.fromhex(hamk_hex)) == 32

    def test_derived_verifier_matches_the_library(self):
        """`verifier_for_salt` reaches into `srp._pysrp` for `gen_x` and the hash map, which is
        private API. This pins the equivalence, so a library change fails here rather than silently
        breaking authentication."""
        salt, vkey = lib.create_salted_verification_key(
            "rkoike", "pw", hash_alg=S.HASH_ALG, ng_type=S.NG_TYPE, salt_len=S.SALT_LEN
        )
        assert S.verifier_for_salt("rkoike", "pw", salt) == vkey


class TestWireCompatibilityIsPinned:
    """RFC 5054 §2.6 padding, which the library defaults *off*.

    This class exists because the failure mode is uniquely expensive: a padding mismatch does not
    raise, it produces a proof that does not verify, so every login fails looking exactly like a
    wrong password. The browser client is in-repo TypeScript and mainstream JS SRP libraries follow
    the RFC, so the server has to as well.
    """

    def test_rfc5054_padding_is_enabled_on_the_active_backend(self):
        assert S.RFC5054_COMPAT is True
        active = getattr(lib, "_mod", None)
        assert active is not None
        assert active._rfc5054_compat is True, (
            f"{active.__name__} is the bound backend and its padding flag is off; "
            "a standards-compliant client will fail to authenticate"
        )

    def test_the_flag_is_read_from_whichever_backend_is_bound(self):
        """`srp/__init__.py` prefers the OpenSSL `_ctsrp` and falls back to `_pysrp`, and each has
        its own `_rfc5054_compat`. So the package-level name must be the one we set, or the pin is a
        no-op on whichever module we did not touch."""
        assert lib.rfc5054_enable is lib._mod.rfc5054_enable
        assert S.active_backend() == lib._mod.__name__

    def test_a_padding_mismatch_refuses_and_looks_like_a_wrong_password(self):
        """The justification for pinning, demonstrated rather than asserted.

        Runs a real exchange with the client on one padding mode and the server on the other, and
        shows it is refused — with no error distinguishing it from a bad credential.
        """
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        active = lib._mod
        try:
            usr = _client("rkoike", "pw")
            _, big_a = usr.start_authentication()  # client computes A in RFC mode
            lib.rfc5054_enable(False)  # server now disagrees about padding
            token, s_hex, b_hex = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
            lib.rfc5054_enable(True)
            proof = usr.process_challenge(bytes.fromhex(s_hex), bytes.fromhex(b_hex))
            lib.rfc5054_enable(False)
            with pytest.raises(S.SrpError, match="authentication failed"):
                S.finish(token, proof.hex())
        finally:
            lib.rfc5054_enable(S.RFC5054_COMPAT)
            assert active._rfc5054_compat is True

    def test_the_mode_does_not_change_stored_verifiers(self):
        """Why enabling this needs no migration: padding affects the session values `u` and `k`, not
        `gen_x`. If this ever fails, turning the flag on has invalidated every enrolled credential
        and the change is no longer safe to make in place."""
        active = lib._mod
        try:
            salt, in_rfc_mode = lib.create_salted_verification_key(
                "rkoike", "pw", hash_alg=S.HASH_ALG, ng_type=S.NG_TYPE, salt_len=S.SALT_LEN
            )
            lib.rfc5054_enable(False)
            in_legacy_mode = S.verifier_for_salt("rkoike", "pw", salt)
        finally:
            lib.rfc5054_enable(S.RFC5054_COMPAT)
            assert active._rfc5054_compat is True
        assert in_rfc_mode == in_legacy_mode


class TestTheGroupIsTheStandardOne:
    """The modulus is RFC 5054's 4096-bit group, not one we generated.

    SRP needs `N` to be a safe prime so the group has no small subgroups to confine an exchange
    into. A bespoke modulus buys nothing — it is public and shared by design — and risks getting
    that property wrong silently, so the standard group is both safer and interoperable.

    4096-bit rather than 2048-bit because 2048 is 112-bit security, which NIST SP 800-57 disallows
    after 2030. Note the generator is **5** here, where the 2048-bit group used 2.
    """

    #: RFC 5054 Appendix A, 4096-bit group. (Same modulus as RFC 3526's 4096-bit MODP group — which
    #: is group **16** in the IKE registry, not group 5. "Group 5" in that registry is the 1536-bit
    #: group, weaker than what we replaced; the 5 here is the generator.)
    RFC_5054_N = int(
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
        "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
        "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
        "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
        "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
        "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
        "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
        "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
        "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9"
        "DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
        "15728E5A8AAAC42DAD33170D04507A33A85521ABDF1CBA64"
        "ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
        "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6B"
        "F12FFA06D98A0864D87602733EC86A64521F2B18177B200C"
        "BBE117577A615D6C770988C0BAD946E208E24FA074E5AB31"
        "43DB5BFCE0FD108E4B82D120A92108011A723C12A787E6D7"
        "88719A10BDBA5B2699C327186AF4E23C1A946834B6150BDA"
        "2583E9CA2AD44CE8DBBBC2DB04DE8EF92E8EFC141FBECAA6"
        "287C59474E6BC05D99B2964FA090C3A2233BA186515BE7ED"
        "1F612970CEE2D7AFB81BDD762170481CD0069127D5B05AA9"
        "93B4EA988D8FDDC186FFB7DC90A6C08F4DF435C934063199"
        "FFFFFFFFFFFFFFFF",
        16,
    )

    def test_the_modulus_is_the_rfc_group(self):
        n, _g = _pysrp.get_ng(S.NG_TYPE, None, None)
        assert n == self.RFC_5054_N
        assert n.bit_length() == 4096

    def test_the_generator_is_five(self):
        _n, g = _pysrp.get_ng(S.NG_TYPE, None, None)
        assert g == 5

    def test_the_group_is_not_a_deprecated_size(self):
        """2048-bit is 112-bit security, disallowed by NIST SP 800-57 after 2030. This is the
        assertion that fails if someone steps the group back down for speed."""
        n, _g = _pysrp.get_ng(S.NG_TYPE, None, None)
        assert n.bit_length() >= 4096

    def test_the_modulus_is_a_safe_prime(self):
        """`N = 2q + 1` with `q` prime. The property that rules out small-subgroup confinement, and
        the one a hand-rolled modulus is most likely to miss.

        Costs about 2 s: two Miller-Rabin runs over a 4096-bit number. Worth it — this is the test
        that catches a substituted or hand-generated group, which is the failure that would not
        announce itself.
        """
        n = self.RFC_5054_N
        q, remainder = divmod(n - 1, 2)
        assert remainder == 0
        assert _is_probable_prime(n), "N is not prime"
        assert _is_probable_prime(q), "(N-1)/2 is not prime, so N is not a safe prime"


def _is_probable_prime(n: int, rounds: int = 8) -> bool:
    """Miller-Rabin. Eight rounds against a 4096-bit number is ~2 s and ample here: this verifies a
    fixed published constant and guards against a *substituted* one, rather than screening random
    candidates, and a wrong value would fail the first base with overwhelming probability."""
    if n < 2:
        return False
    for small in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % small == 0:
            return n == small
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = random.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = x * x % n
            if x == n - 1:
                break
        else:
            return False
    return True


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
        _login, hamk_hex, server_key = S.finish(token, proof.hex())
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
        """The response shape must not differ, or the refusal is unnecessary.

        **`len(fake_b) == len(real_b)` is the wrong comparison, and it failed about one run in
        256.** ``B`` is hex-encoded from a minimal big-endian integer encoding, so a value whose
        top byte happens to be zero is two hex characters shorter -- measured at 1022 against
        1024. Both paths compute ``B`` with the same code, so the variation carries no
        information about whether the login exists; only the assertion was reading a coin flip
        as a security property.

        Fixed here rather than in the product, deliberately. Zero-padding ``B`` on the wire
        would make the lengths exactly equal, but the browser client uses **unpadded** ``B`` in
        the proof ``M`` (`srpClient.ts`), so widening the encoding would change every client's
        proof -- and per `identity-auth-traps.mdc` §4 that failure does not raise, it makes
        every login report a wrong password.

        What the test protects against is a decoy path with a *different* shape -- an empty
        salt, a truncated ``B``, a missing field. Those are wrong by hundreds of characters, not
        by one byte, so asserting the modulus width catches them and the coin flip does not
        matter.
        """
        salt_hex, ver_hex = S.enroll("rkoike", "pw")
        usr = _client("x", "y")
        _, big_a = usr.start_authentication()
        _, real_s, real_b = S.begin("rkoike", big_a.hex(), salt_hex, ver_hex)
        _, fake_s, fake_b = S.begin("ghost", big_a.hex())

        # The salt is a fixed-width random value, so this one is exact.
        assert len(fake_s) == len(real_s)

        # Derived from the configured group rather than hardcoded, so moving groups again
        # (2048 -> 4096 happened once already) updates the expectation instead of failing.
        from srp import _pysrp

        n, _g = _pysrp.get_ng(S.NG_TYPE, None, None)
        modulus_bytes = (n.bit_length() + 7) // 8
        for label, value in (("real", real_b), ("decoy", fake_b)):
            assert len(value) % 2 == 0, f"{label} B is not whole bytes of hex"
            assert (len(value) // 2) in {modulus_bytes, modulus_bytes - 1}, (
                f"{label} B is {len(value) // 2} bytes, not the {modulus_bytes}-byte modulus width"
            )

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
        assert len(verifier) > 500  # a 4096-bit group yields a 512-byte verifier


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
