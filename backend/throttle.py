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
"""Request throttling: one enforcement point, per-route declaration.

Built as a sibling of `audit.actions`, deliberately. That decorator already solved the same shape of
problem — a cross-cutting concern that has to be *declared* next to the route it governs and
*enforced* somewhere central — so this reuses the pattern rather than inventing a second one.

THE KEY IS THE PART THAT MATTERS, AND THE OBVIOUS CHOICE IS WRONG HERE
----------------------------------------------------------------------
Keying on the client address is the textbook answer and it would cause outages on this product.
**Operators reach these deployments through an SSH tunnel**, so every request arrives from
``127.0.0.1`` — a fact Phase 1 already had to document for the audit trail's ``src_ip`` (NFR-7).
An address-keyed global limit therefore treats the entire team as one client: one person running a
bulk rack operation throttles everybody, and it presents as the product randomly refusing work.

So the key depends on what the request has:

* **authenticated** → the principal. It is the only thing that distinguishes operators behind a
  shared tunnel, and it is taken from the verified token, never from anything the caller sets.
* **unauthenticated** → the resolved client address. There is no principal yet, and this is where
  the real exposure is anyway.

WHY THE TWO CATEGORIES GET OPPOSITE TREATMENT WHEN UNDECLARED
--------------------------------------------------------------
An **authenticated** route with no declaration gets a generous default. A missing limit there is
covered by a sane number, and blocking work on a value that has an obvious default would be
friction with no safety return.

An **unauthenticated** route with no declaration **fails the build**. That default cannot be
guessed: `srp.begin` costs ~52 ms of server CPU per call by design (FR-4b makes a cheap path an
enumeration oracle, so every caller gets the expensive one), while `/.well-known/jwks.json` is a
cached read. One number cannot be right for both, and getting it wrong on an unauthenticated route
is the case that actually hurts.

Fail-safe for the first, fail-loud for the second. The difference is whether a default can be
correct, not how much we care.

SIZED FROM MEASUREMENT, NOT FROM COMFORT
-----------------------------------------
The authenticated default has to sit above real burst traffic or it becomes a source of phantom
failures — which is worse than no limit, because it erodes trust in the product rather than
protecting it. Measured against what the SPA actually does:

* the busiest poll is ``ANALYTICS_POLL_MS = 1000`` — one request a second, sustained, from a single
  open view; others run at 900 ms, 2 s, 5 s, 15 s and 30 s concurrently
* worst realistic sustained load for one operator is therefore ~5 requests/second
* a page load fires a burst from among 57 distinct `apiFetch` call sites

``900`` per 60 s is **15/second sustained with a 900-request burst allowance** — three times the
measured sustained need, and a burst ceiling no page load approaches. It still caps a runaway
script, which is the actual threat from an authenticated caller.

NOT YET IMPLEMENTED, AND NAMED SO IT IS NOT FORGOTTEN
------------------------------------------------------
Exponential backoff for callers that keep hammering after a refusal. The token bucket refuses at a
flat rate, so a client in a tight retry loop is refused cheaply but indefinitely. Escalating the
penalty is the intended follow-up; the ``Retry-After`` header is already emitted, which is what a
well-behaved client needs in the meantime.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from identity.rate_limit import RateLimiter

_logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., object])

#: Authenticated default: 15/s sustained, 900 burst. See the sizing note above — this number comes
#: from the SPA's measured poll cadence, not from taste.
DEFAULT_AUTHENTICATED_LIMIT = 900
DEFAULT_WINDOW = 60.0

#: There is no unauthenticated default on purpose. An undeclared unauthenticated route fails the
#: build (`test_throttle_coverage.py`), because no single number is right for both a 52 ms SRP
#: exponentiation and a cached JWKS read.
UNAUTHENTICATED_MUST_DECLARE = True


@dataclass(frozen=True)
class Limit:
    """One route's allowance."""

    limit: int
    window: float = DEFAULT_WINDOW
    #: Why this number, for the next person who wonders whether it can be lowered.
    rationale: str = ""

    @property
    def per_second(self) -> float:
        return self.limit / self.window


class Registry:
    """Endpoint → limit, filled by the decorator at import time."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._limits: dict[str, Limit] = {}
        self._limiters: dict[str, RateLimiter] = {}

    def declare(self, endpoint: str, limit: Limit) -> None:
        with self._lock:
            existing = self._limits.get(endpoint)
            if existing is not None and existing != limit:
                raise ValueError(
                    f"{endpoint!r} already declares a throttle ({existing}); refusing to replace "
                    f"it with {limit}. Two declarations means one of them is silently ignored."
                )
            self._limits[endpoint] = limit

    def declared(self) -> dict[str, Limit]:
        with self._lock:
            return dict(self._limits)

    def limit_for(self, endpoint: str) -> Limit:
        """The declared limit, or the authenticated default."""
        with self._lock:
            declared = self._limits.get(endpoint)
        if declared is not None:
            return declared
        return Limit(
            DEFAULT_AUTHENTICATED_LIMIT,
            DEFAULT_WINDOW,
            "authenticated default; no explicit declaration",
        )

    def limiter_for(self, endpoint: str) -> RateLimiter:
        """The bucket table for one endpoint, created on first use.

        Per endpoint rather than one global table, so a burst against one route cannot consume
        another's allowance — a fan-out and a login should not share a budget.
        """
        with self._lock:
            existing = self._limiters.get(endpoint)
            if existing is not None:
                return existing
            spec = self.limit_for(endpoint)
            created = RateLimiter(limit=spec.limit, window=spec.window)
            self._limiters[endpoint] = created
            return created

    def reset_for_tests(self) -> None:
        with self._lock:
            self._limiters.clear()


_registry = Registry()


def registry() -> Registry:
    return _registry


def throttle(limit: int, *, window: float = DEFAULT_WINDOW, rationale: str = "") -> Callable[[F], F]:
    """Declare this route's allowance.

    Mirrors `audit.actions.action`: the decorator records intent against the endpoint name and does
    **no enforcement itself**. Enforcement is a single `before_request` hook, which is what lets it
    shed before the view function runs and before the auth gate does its work — a decorator can only
    act after routing has already paid for itself.

    A `rationale` is worth writing for anything unusual. The next person's question is never "what is
    the number" but "can I raise it", and the answer lives in why it was chosen.
    """

    def decorate(func: F) -> F:
        # The endpoint Flask will register is the function's name, prefixed by its blueprint. The
        # blueprint is not known here, so the decorator records the bare name and the installer
        # resolves it — the same indirection `audit.actions` uses.
        func._throttle_limit = Limit(limit, window, rationale)
        return func

    return decorate


def declared_limit_of(func: object) -> Limit | None:
    return getattr(func, "_throttle_limit", None)


def install(app: object) -> int:
    """Move every declaration from its view function into the registry.

    Called once at startup, after all blueprints are registered. The decorator cannot do this
    itself: it runs at import time, when the view is still a bare function with no blueprint and
    therefore no endpoint name. Same indirection `audit.actions` uses, for the same reason.

    Returns the number of declarations found, so startup can log it — a count of zero would mean
    the decorators are being imported but never collected, which is the silent-failure shape here.
    """
    found = 0
    for endpoint, view in getattr(app, "view_functions", {}).items():
        spec = declared_limit_of(view)
        if spec is None:
            continue
        _registry.declare(endpoint, spec)
        found += 1
    _logger.info("throttle: %d route(s) declare an explicit limit", found)
    return found
