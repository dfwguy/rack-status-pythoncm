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
"""SRP-6a enrolment and the server half of the exchange (FR-4, FR-4a, FR-4b, FR-4e).

The password never reaches this process. A client proves it knows one; the server holds only a salt
and a verifier, from which the password cannot be recovered. That is worth having even with TLS in
front, because it means no proxy, memory dump or debug log can contain a credential that does not
exist here in the first place.

Three things in this module are easy to get wrong, so each is stated rather than left to the reader:

**The library's defaults are unsafe** (FR-4e). ``srp`` 1.0.22 defaults to ``hash_alg=0`` — SHA-1 —
and ``salt_len=4``, a 32-bit salt. A reasonable-looking ``create_salted_verification_key(user, pw)``
therefore enrols a SHA-1 credential and nothing fails. Every parameter here is explicit, and
``test_identity_srp.py`` asserts the defaults are *not* in use rather than only asserting the result.

**An unknown login must cost and look the same as a wrong password** (FR-4b). Otherwise the login
form is a user-enumeration oracle. The synthetic path below derives a *deterministic* salt and
pseudo-password from the server secret, so probing the same unknown login twice returns the same
salt — a fresh random salt each time would itself reveal non-existence, including across a restart.

**The exchange is stateful, and the state is the attack surface.** SRP-6a needs two round trips with
a server-held ephemeral value in between. That table is bounded (an unauthenticated caller must not
be able to exhaust memory by starting exchanges it never finishes), single-use under a lock (a
check-then-delete is a replay window), and expiring.

**The exchange is also a CPU amplifier, and the 4096-bit group made it a worse one.** :func:`begin`
must be reachable unauthenticated — it *is* the login endpoint — and one call costs about 52 ms of
server CPU at ``NG_4096``, against 13 ms at ``NG_2048``. With a single worker that is roughly 19
anonymous requests per second to saturate a core, down from about 75. The bounded challenge table
below caps *memory* and does nothing about this. **The route that calls :func:`begin` must be rate
limited** (FR-4g); the limit belongs at the endpoint, not in here, because only the endpoint knows
the caller. The uniform-cost property FR-4b requires makes this unavoidable rather than accidental:
a cheap path for unknown logins would be an enumeration oracle, so every caller gets the expensive
one.

Single process, several threads: the deployment runs ``gunicorn --workers 1 --threads N
--worker-class gthread``. **A second worker would split this table and break login intermittently**
— see :func:`assert_single_worker_safe`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as _secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

import srp as _srp
from srp import _pysrp

from identity.models import canonical_login

# --- Parameters. Explicit at every call site, per FR-4e. -----------------------------------------
#: SHA-256, not the library's SHA-1 default.
HASH_ALG = _srp.SHA256
#: RFC 5054 Appendix A **4096-bit** group, whose generator is ``g = 5`` (not 2, as at 2048).
#:
#: Chosen over the 2048-bit group deliberately: 2048-bit finite-field crypto is 112-bit security,
#: which NIST SP 800-57 disallows after 2030. Changing the group **invalidates every enrolled
#: verifier** — ``v = g**x mod N`` depends on both — so this is nearly free today, with no users
#: enrolled anywhere, and would be a credential migration later. Moving now is the cheap moment.
#:
#: It costs about 3.8x: measured server CPU per login rose from **31.7 ms to 121.6 ms** (52 ms in
#: ``begin``, 70 ms in ``finish``). Irrelevant for a once-per-session login at a 20-operator scale
#: — but see the amplification warning in the module docstring, because ``begin`` is unauthenticated
#: by necessity and that number is now the cost an anonymous caller can impose per request.
NG_TYPE = _srp.NG_4096
#: 32 bytes, not the library's 4.
SALT_LEN = 32

#: How long a started exchange stays completable. Seconds, because the client's second call follows
#: immediately; a longer window only widens the replay and memory surface.
CHALLENGE_TTL_SECONDS = 60
#: Bounded so unfinished exchanges cannot accumulate. 512 concurrent logins is far beyond the
#: 20-operator design target (NFR-7), and the oldest is evicted rather than the newest refused --
#: refusing the newest would let an attacker lock out real logins by filling the table.
MAX_CHALLENGES = 512


class SrpError(Exception):
    """An exchange that cannot proceed. Deliberately says little; the caller maps it to a uniform
    failure so a client cannot distinguish causes (FR-4b)."""


# --- Wire compatibility. Pinned, because the default is non-standard and the flag is per-backend. --

#: RFC 5054 §2.6 requires values be left-padded to the width of ``N`` before hashing (``u =
#: H(PAD(A), PAD(B))``, ``k = H(N, PAD(g))``). **The library defaults this off**, computing a variant
#: that only interoperates with itself.
#:
#: On, because the browser client is in-repo TypeScript and mainstream JS SRP libraries implement the
#: RFC. A mismatch does not error — it produces a proof that does not verify, so **every login fails
#: looking exactly like a wrong password**, which is the most expensive failure mode this module
#: could have. Measured: RFC↔RFC and non-RFC↔non-RFC both authenticate; the two cross pairs both
#: refuse.
#:
#: Safe to turn on now and safe to have turned on later: the padding affects only the session values
#: ``u`` and ``k``, not ``gen_x``, so **stored salts and verifiers are unchanged by this flag**
#: (asserted by ``test_the_mode_does_not_change_stored_verifiers``). Enrolments do not need migrating.
RFC5054_COMPAT = True


def active_backend() -> str:
    """Which implementation ``srp`` bound: ``srp._pysrp`` or ``srp._ctsrp``.

    Worth exposing because the choice is made by whether ``dlopen`` finds OpenSSL, not by us, and it
    changes where the compatibility flag lives.
    """
    return getattr(_srp, "_mod", _pysrp).__name__


def _pin_wire_compatibility() -> None:
    """Set the RFC-5054 mode on **the active backend**, then verify it took.

    The subtlety that makes this a function rather than a line: ``srp/__init__.py`` tries the
    OpenSSL-backed ``_ctsrp`` first and falls back to ``_pysrp``, and **each module has its own
    ``_rfc5054_compat`` global**. Toggling ``_pysrp`` directly is therefore a silent no-op whenever
    the C backend is active. ``srp.rfc5054_enable`` is re-exported from whichever won, so it is the
    only correct thing to call.

    Which one wins is decided by ``dlopen``: ``_ctsrp`` asks for ``libssl.so`` or
    ``libssl.so.1.1.0``, and both this workstation and ``python:3.12-slim`` ship only
    ``libssl.so.3``, so today it is always ``_pysrp``. That is luck, not design — installing
    ``libssl-dev`` creates the ``libssl.so`` symlink and flips it. Hence the assertion: if the
    binding ever changes, this raises at import instead of leaving authentication silently on the
    wrong wire format.
    """
    _srp.rfc5054_enable(RFC5054_COMPAT)
    active = getattr(_srp, "_mod", _pysrp)
    if getattr(active, "_rfc5054_compat", None) is not RFC5054_COMPAT:
        raise SrpError(
            f"could not pin RFC 5054 padding on {active.__name__}: the flag did not take. "
            "Client and server must agree on padding or every login fails looking like a wrong "
            "password. Do not proceed without resolving this."
        )


_pin_wire_compatibility()


# --- Enrolment ------------------------------------------------------------------------------------


def _hash_class() -> Any:
    return _pysrp._hash_map[HASH_ALG]


def verifier_for_salt(login: str, password: str, salt: bytes) -> bytes:
    """The verifier for a **chosen** salt.

    ``create_salted_verification_key`` generates its own random salt and offers no way to supply
    one, which the synthetic path needs (a decoy salt must be reproducible). This is not
    reimplemented cryptography: it is the library's own expression, ``g ** gen_x(...) mod N``, called
    with our salt instead of theirs, using their ``gen_x`` and their group.

    It does reach into ``_pysrp`` for ``gen_x`` and the hash map, which is private API, and does so
    even when :func:`active_backend` reports ``_ctsrp``. That is sound for one specific reason —
    ``gen_x`` takes no width argument, so the verifier does not depend on the RFC-5054 padding mode
    and the two backends agree on it — but it is not obvious, so
    ``test_derived_verifier_matches_the_library`` compares this against **the active backend's**
    ``create_salted_verification_key`` for the same salt. Either a library change or a backend switch
    then fails a test instead of silently breaking authentication.
    """
    n, g = _pysrp.get_ng(NG_TYPE, None, None)
    x = _pysrp.gen_x(_hash_class(), salt, login.encode(), password.encode())
    return _pysrp.long_to_bytes(pow(g, x, n))


def enroll(login: str, password: str) -> tuple[str, str]:
    """``(salt_hex, verifier_hex)`` for a new or changed password (FR-3).

    Hex rather than base64: the store is JSON a human may need to read while debugging, and 512
    characters per user at a 20-operator scale is not worth optimising.
    """
    key = canonical_login(login)
    if not password:
        raise SrpError("password must not be empty")
    salt = _secrets.token_bytes(SALT_LEN)
    verifier = verifier_for_salt(key, password, salt)
    return salt.hex(), verifier.hex()


# --- The decoy credential for an unknown login (FR-4b) --------------------------------------------


def _server_secret() -> bytes:
    """Keyed material for the synthetic credential.

    ``RACK_WEB_SECRET_KEY`` is bootstrapped into the environment by ``app.py`` from
    ``var/secrets/.rack_web_secret_key``, so it is stable across restarts — which is the property
    that matters here: a decoy salt must not change between two probes of the same unknown login.

    Read from the environment rather than by importing ``app``: an ``identity`` module importing
    ``app`` would recreate the dependency cycle ``backend/api/`` exists to remove.
    """
    raw = (os.environ.get("RACK_WEB_SECRET_KEY") or "").strip()
    if not raw:
        # No secret yet (a bare import in a test, say). A per-process value keeps the exchange
        # uniform within this process, which is all an unauthenticated prober can observe in one
        # session; it is not persisted because there is nowhere agreed to persist it from here.
        raw = _PROCESS_FALLBACK_SECRET
    return hashlib.sha256(raw.encode()).digest()


_PROCESS_FALLBACK_SECRET = _secrets.token_hex(32)


def synthetic_credential(login: str) -> tuple[bytes, bytes]:
    """A deterministic decoy ``(salt, verifier)`` for a login that does not exist.

    Deterministic is the whole point. A random salt per probe would make an unknown login
    distinguishable from a known one by asking twice, which is exactly the enumeration FR-4b
    forbids — and it would differ across a restart too, where a real user's salt does not.

    The pseudo-password is derived and never revealed, so the exchange completes the same shape of
    work as a real one and then fails at the proof, which is what a wrong password does.
    """
    key = canonical_login(login)
    secret = _server_secret()
    salt = hmac.new(secret, b"srp-salt:" + key.encode(), hashlib.sha256).digest()[:SALT_LEN]
    pseudo = hmac.new(secret, b"srp-pw:" + key.encode(), hashlib.sha256).hexdigest()
    return salt, verifier_for_salt(key, pseudo, salt)


# --- The exchange ---------------------------------------------------------------------------------


@dataclass(slots=True)
class _Challenge:
    login: str
    verifier: Any  # srp.Verifier — holds the ephemeral private value between the two round trips
    created: float
    real: bool  # False for the decoy path; never told to the client


class _ChallengeTable:
    """Bounded, single-use, expiring, thread-safe.

    Each property is here because of a specific failure: unbounded lets an unauthenticated caller
    exhaust memory with exchanges it never finishes; multi-use is a replay window; unexpiring keeps
    ephemeral key material alive indefinitely; and unlocked ``pop`` under ``gthread`` means two
    threads can both consume one challenge.
    """

    def __init__(self) -> None:
        self._items: dict[str, _Challenge] = {}
        self._lock = threading.Lock()

    def put(self, challenge: _Challenge) -> str:
        token = _secrets.token_urlsafe(24)
        with self._lock:
            self._evict_expired_locked()
            if len(self._items) >= MAX_CHALLENGES:
                # Drop the oldest rather than refuse the newest: refusing would let a flood of
                # unfinished exchanges deny login to real operators.
                oldest = min(self._items, key=lambda k: self._items[k].created)
                self._items.pop(oldest, None)
            self._items[token] = challenge
        return token

    def take(self, token: str) -> _Challenge | None:
        """Removes and returns in one atomic step. Never a get-then-delete."""
        with self._lock:
            self._evict_expired_locked()
            return self._items.pop(token or "", None)

    def _evict_expired_locked(self) -> None:
        cutoff = time.monotonic() - CHALLENGE_TTL_SECONDS
        for token in [t for t, c in self._items.items() if c.created < cutoff]:
            self._items.pop(token, None)

    def __len__(self) -> int:
        with self._lock:
            self._evict_expired_locked()
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_challenges = _ChallengeTable()


def begin(
    login: str, a_hex: str, salt_hex: str = "", verifier_hex: str = ""
) -> tuple[str, str, str]:
    """Round trip 1: ``(challenge_token, salt_hex, B_hex)``.

    Takes the client's public value ``A``, because SRP-6a has the client send it alongside the
    username and the server cannot compute ``B`` without it. (Worth stating: a first draft of this
    module tried to answer with a salt and ``B`` before ``A`` arrived, which the maths does not
    allow — so the protocol is one request in, one response out, not three legs.)

    Pass the stored ``salt_hex``/``verifier_hex`` for a login that exists. Omit them and the decoy
    path runs, which is what makes an unknown login indistinguishable — **the caller must not branch
    on existence in a way the response reveals**, which is why the return shape is identical either
    way and ``real`` is kept server-side.
    """
    key = canonical_login(login)
    real = bool(salt_hex and verifier_hex)
    salt, verifier = (
        (bytes.fromhex(salt_hex), bytes.fromhex(verifier_hex))
        if real
        else synthetic_credential(key)
    )
    try:
        a_bytes = bytes.fromhex(a_hex)
    except ValueError as exc:
        raise SrpError("malformed client public value") from exc

    svr = _srp.Verifier(
        key, salt, verifier, a_bytes, hash_alg=HASH_ALG, ng_type=NG_TYPE
    )
    s, b = svr.get_challenge()
    if s is None or b is None:
        # The library refuses a degenerate A (A mod N == 0). Uniform failure, no detail.
        raise SrpError("authentication failed")
    token = _challenges.put(_Challenge(login=key, verifier=svr, created=time.monotonic(), real=real))
    return token, s.hex(), b.hex()


def finish(token: str, m_hex: str) -> tuple[str, str, bytes]:
    """Round trip 2: verify the client's proof, return ``(login, HAMK_hex, session_key)``.

    Raises :class:`SrpError` for every failure — unknown token, expired token, bad proof, decoy
    path — so the caller has nothing to leak. The challenge is consumed whether or not the proof
    verifies, which makes a wrong guess cost a new exchange rather than allowing retries against one
    ``B``.

    **The login is returned rather than left to the caller, and that is a security property, not a
    convenience.** It comes from the challenge this server minted in round trip 1. A caller that had
    to supply the login again on the second leg could prove knowledge of one account's password and
    name a different account — the proof and the claim would be bound to nothing. Returning it here
    means there is no second place for them to disagree.
    """
    challenge = _challenges.take(token)
    if challenge is None:
        raise SrpError("authentication failed")
    try:
        m = bytes.fromhex(m_hex)
    except ValueError as exc:
        raise SrpError("authentication failed") from exc

    hamk = challenge.verifier.verify_session(m)
    if hamk is None or not challenge.verifier.authenticated() or not challenge.real:
        # `not challenge.real` is last on purpose: the decoy exchange has already done the same work,
        # so the refusal costs what a real wrong password costs.
        raise SrpError("authentication failed")
    return challenge.login, hamk.hex(), challenge.verifier.get_session_key()


# --- Operational guards ---------------------------------------------------------------------------


def pending_count() -> int:
    """Open exchanges. For a health endpoint, and for the test that proves the bound holds."""
    return len(_challenges)


def reset_for_tests() -> None:
    _challenges.clear()


def assert_single_worker_safe(workers: int) -> None:
    """Raise if the deployment would split the challenge table across processes.

    The table is in-process, so a second worker means round trip 2 can land on a worker that never
    saw round trip 1 — an intermittent login failure that looks like a client bug. The Dockerfile
    pins ``--workers 1``; this exists so raising it fails loudly at startup instead of producing a
    login that works about half the time.
    """
    if workers > 1:
        raise SrpError(
            f"SRP login holds per-exchange state in process memory and cannot span "
            f"{workers} workers. Either run --workers 1 or move the challenge table to a shared "
            "store (see the Phase 2 brief, design open question A)."
        )
