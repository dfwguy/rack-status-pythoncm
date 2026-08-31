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
"""A per-client token bucket, for shedding load before it is expensive (FR-4g).

**Why this exists at all.** `srp.begin()` performs a 4096-bit modular exponentiation for an
*unauthenticated* caller — about 52 ms of server CPU — so one worker saturates a core at roughly 19
anonymous requests per second. FR-4b makes that unavoidable: a cheap path for unknown logins would be
an enumeration oracle, so every caller gets the expensive one. The bounded challenge table caps
memory and does nothing at all for CPU.

**The constraint that shapes the design.** The limiter must refuse *before* the exponentiation, not
after. A limiter that runs afterwards protects the challenge table and leaves the amplifier fully
exposed, which is the failure this module exists to prevent — and it is an easy one to ship, because
both versions pass a test that only asserts "the 21st request gets a 429".

A token bucket rather than a fixed window: a fixed window lets a caller spend its whole allowance in
the first millisecond of each window and then again in the first millisecond of the next, which is
a burst of 2N at the boundary. The bucket refills continuously, so the ceiling is the ceiling.

In-process and per-worker, deliberately. SRP challenge state is already in-process — `--workers 1`
is load-bearing and `srp.assert_single_worker_safe()` refuses more — so a shared store would add a
dependency without buying correctness the deployment does not already assume.

**What this does not do, stated plainly.** A per-key bucket cannot limit a caller who controls the
key: every distinct source gets its own fresh allowance, whatever the eviction policy. So this
raises the cost for a single-source attacker and stops one noisy client starving the others; it does
not stop a distributed or address-spoofing attacker from reaching `srp.begin`. The one thing that
narrows the spoofing case is upstream — the caller's address is resolved through the trusted-proxy
policy, so `X-Forwarded-For` is only believed from a configured proxy. Beyond that this is a
network-layer problem and should not be mistaken for a solved one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: Requests per window, per client. Chosen against the measured cost: 10 begins is ~520 ms of CPU,
#: so a single client cannot occupy more than about half a core-second per minute. Well above any
#: human login rate — a person who mistypes a password five times in a minute is unaffected.
DEFAULT_LIMIT = 10

#: Seconds. The bucket refills at DEFAULT_LIMIT/DEFAULT_WINDOW tokens per second.
DEFAULT_WINDOW = 60.0

#: Ceiling on tracked clients. Without it the limiter is itself a memory amplifier: an attacker
#: spoofing a new source per request would grow the table without bound, which is a slower version
#: of the attack the limiter is meant to stop.
MAX_TRACKED = 4096


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Fixed-rate token bucket keyed by an opaque client identifier.

    `monotonic`, never `time.time()`: a wall-clock step — NTP correction, a VM resuming from
    suspend — would otherwise either grant a windfall of tokens or freeze the bucket.
    """

    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        window: float = DEFAULT_WINDOW,
        max_tracked: int = MAX_TRACKED,
    ) -> None:
        if limit <= 0 or window <= 0:
            raise ValueError("limit and window must be positive")
        self.limit = limit
        self.window = window
        self.max_tracked = max_tracked
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _refill_rate(self) -> float:
        return self.limit / self.window

    def check(self, key: str) -> tuple[bool, float]:
        """``(allowed, retry_after_seconds)``. Consumes a token when allowed.

        Returning the retry hint rather than just a boolean so the caller can send `Retry-After`;
        a 429 with no indication of when to come back invites an immediate retry loop, which is the
        traffic the limiter is trying to shed.
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self.max_tracked:
                    self._evict_stale_locked(now)
                # `updated=now`, not the field default. The default calls `time.monotonic()` at
                # construction — which is *after* `now` was sampled — so `elapsed` below came out
                # negative and the refill term subtracted a sliver of a token. With limit=1 that
                # put a brand-new bucket at 0.999… and refused the caller's very first request.
                bucket = _Bucket(tokens=float(self.limit), updated=now)
                self._buckets[key] = bucket

            elapsed = now - bucket.updated
            bucket.tokens = min(float(self.limit), bucket.tokens + elapsed * self._refill_rate())
            bucket.updated = now

            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            # Time until one whole token is available again.
            deficit = 1.0 - bucket.tokens
            return False, deficit / self._refill_rate()

    def _evict_stale_locked(self, now: float) -> None:
        """Drop buckets that have refilled completely; they are indistinguishable from absent.

        If nothing is stale, drop the oldest. The table is a cache, and a full one must not become a
        reason to stop limiting — failing open here would hand an attacker the amplifier back by
        simply filling the table first.
        """
        full = [
            k for k, b in self._buckets.items()
            if b.tokens + (now - b.updated) * self._refill_rate() >= self.limit
        ]
        for k in full:
            del self._buckets[k]
        if not self._buckets:
            return
        while len(self._buckets) >= self.max_tracked:
            oldest = min(self._buckets, key=lambda k: self._buckets[k].updated)
            del self._buckets[oldest]

    def reset(self) -> None:
        """Forget every bucket. For tests, and for a deliberate operational clear."""
        with self._lock:
            self._buckets.clear()

    def tracked(self) -> int:
        with self._lock:
            return len(self._buckets)
