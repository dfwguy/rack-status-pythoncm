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
"""The revocation check that :mod:`identity.tokens` refuses to run without (FR-6, FR-6a).

The invariant this module exists to hold: **grants may be stale, denials may not.** A stateless JWT
cannot be withdrawn before it expires, so the only thing that makes JWT acceptable here is a check
consulted on every request that can say "this subject is no longer allowed" faster than the token
would have expired on its own. FR-6 puts a number on it — **10 seconds**.

Four ways to revoke, because they answer different questions:

- **by subject** — a user is disabled or gone; deny everything they hold
- **by token id** — one session, or a leaked refresh token, without disturbing the user's others
- **by issue time** ("not before") — *log out everywhere*, and the only mechanism available for it,
  since outstanding token ids cannot be enumerated but their ``iat`` can be compared
- **implicitly**, for a login that is disabled or retired in the store — defence in depth, so
  deleting a user and forgetting to write a revocation row does not leave their tokens live

**The cache is the whole design problem, and Phase 1 already solved it once.**
NFR-3 gives this check 5 ms at p95 and it runs on every authorised request, while the record lives in
TinyDB. Phase 1 hit precisely this: ``catalog_context.resolve()`` read TinyDB per audited request,
cost **4.74 ms**, and breached the audit budget until it was cached on the store's generation
counter. So that pattern is here from the start rather than added after a measurement — see
`core-designs.mdc` §5 and the Phase 1 brief's NFR-2a.

**Generation alone is not enough, which is the part Decision 4 did not cover.** The counter is a
process-local integer, so a write from *another* process — an operator running a CLI against the same
file — never bumps it and the cache would serve a stale allow indefinitely, blowing the 10-second
bound with no symptom. The key therefore includes the file's identity from ``stat()`` (mtime and
size, ~µs), and a hard :data:`MAX_CACHE_AGE_SECONDS` backstops both, for the filesystem whose mtime
granularity is too coarse to notice a fast second write.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from identity import store as identity_store
from identity import tokens
from identity.principals import KIND_USER, parse_principal

#: Hard ceiling on cache age. Chosen against FR-6's 10-second bound with room to spare: even if both
#: the generation counter and the file stat miss a change, a revocation is honoured within this long.
MAX_CACHE_AGE_SECONDS = 5.0

_lock = threading.RLock()
_cached: dict[str, Any] | None = None
_cached_key: tuple[Any, ...] | None = None
_cached_at: float = 0.0


def _file_identity() -> tuple[Any, ...]:
    """Cheap evidence that another process wrote the file.

    ``st_mtime_ns`` and ``st_size`` rather than a hash: this runs on every authorised request, and a
    ``stat`` is microseconds where re-reading and hashing the file would defeat the point of caching.
    A missing file is a valid state — nothing revoked yet — and must not raise here.
    """
    try:
        st = os.stat(identity_store.identity_file())
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def snapshot() -> dict[str, Any]:
    """The revocation record, cached on (generation, file identity, age)."""
    global _cached, _cached_key, _cached_at
    key = (identity_store.current_generation(), *_file_identity())
    now = time.monotonic()
    with _lock:
        fresh = (
            _cached is not None
            and _cached_key == key
            and (now - _cached_at) < MAX_CACHE_AGE_SECONDS
        )
        if fresh:
            return _cached  # type: ignore[return-value]
        loaded = identity_store.store().revocation_snapshot()
        _cached, _cached_key, _cached_at = loaded, key, now
        return loaded


def invalidate() -> None:
    """Drop the cache. Called by the write helpers below so a revocation made *in this process* is
    effective on the very next request rather than after the generation/stat comparison."""
    global _cached, _cached_key
    with _lock:
        _cached, _cached_key = None, None


def check(claims: dict[str, Any]) -> None:
    """The FR-6 check. Raises :class:`identity.tokens.TokenRevoked` to refuse.

    Registered into :mod:`identity.tokens` by :func:`install`, and called only *after* signature and
    claim validation — so every value read here has already been proven to come from a token we
    minted, which is what makes it safe to trust ``sub``, ``jti`` and ``iat``.
    """
    record = snapshot()
    subject = str(claims.get("sub", ""))

    reason = record["subjects"].get(subject)
    if reason is not None:
        raise tokens.TokenRevoked(f"{subject} is revoked{f': {reason}' if reason else ''}")

    jti = str(claims.get("jti", ""))
    if jti in record["tokens"]:
        raise tokens.TokenRevoked("this token has been revoked")

    cutoff = record["not_before"].get(subject)
    if cutoff is not None and float(claims.get("iat", 0)) < cutoff:
        raise tokens.TokenRevoked("credentials changed; this session is no longer valid")

    kind, identifier = parse_principal(subject)
    if kind == KIND_USER:
        if identifier in record["retired"]:
            raise tokens.TokenRevoked("this account no longer exists")
        if identifier in record["disabled"]:
            raise tokens.TokenRevoked("this account is disabled")


def install() -> None:
    """Wire this module into :mod:`identity.tokens`.

    Must be called during startup. Until it is, ``tokens.verify`` refuses everything (FR-6a), so a
    forgotten call is a total, immediate, obvious failure rather than a silently missing control.
    """
    tokens.set_revocation_checker(check)


# --- Write paths. Each invalidates, so a revocation is effective on the next request. -------------


def revoke_subject(principal: str, reason: str = "") -> None:
    parse_principal(principal)
    identity_store.store().revoke_subject(principal, reason)
    invalidate()


def restore_subject(principal: str) -> None:
    parse_principal(principal)
    identity_store.store().restore_subject(principal)
    invalidate()


def revoke_token(claims: dict[str, Any], reason: str = "") -> None:
    """Revoke one token, given its verified claims.

    Takes claims rather than a bare ``jti`` so ``expires_at`` comes from the token itself; the store
    needs it to prune the row once the token would have expired anyway.
    """
    jti = str(claims.get("jti", ""))
    if not jti:
        raise ValueError("claims carry no jti, so this token cannot be revoked individually")
    identity_store.store().revoke_token(jti, float(claims.get("exp", 0)), reason)
    invalidate()


def revoke_all_for(principal: str, moment: float | None = None) -> None:
    """Log a subject out everywhere, including sessions whose token ids we never saw.

    The cutoff is *now*, exactly, which is only correct because :func:`identity.tokens.mint` records
    ``iat`` with sub-second precision. An earlier version used whole seconds and had to push the
    cutoff a second into the future to avoid leaving a just-minted token on the allowed side — which
    then refused the user's own *next* login for up to a second, so a password change looked like a
    broken account. Worth stating because "add a second to be safe" is the tempting fix and it is the
    wrong one.
    """
    parse_principal(principal)
    identity_store.store().set_not_before(
        principal, moment if moment is not None else time.time()
    )
    invalidate()


def reset_for_tests() -> None:
    invalidate()
