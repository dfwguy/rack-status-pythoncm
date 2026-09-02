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
"""The build gate: an unauthenticated route must declare its own throttle.

**Why this fails the build while an undeclared *authenticated* route does not.** An authenticated
route can take a sane default — 900/60 s, sized from measured poll traffic — because the caller is
known, bounded by the operator count, and the realistic threat is a runaway script. No such default
exists for an unauthenticated route: `srp/begin` costs ~52 ms of server CPU per call *by design*
(FR-4b makes a cheap path an enumeration oracle, so every caller gets the expensive one), while
`/.well-known/jwks.json` is a cached read served from memory. One number cannot be right for both,
and being wrong on a route anyone can reach is the case that actually hurts.

So: fail-safe for authenticated, fail-loud for unauthenticated. The difference is whether a default
can be correct, not how much the two matter.

This is modelled on `test_action_coverage.py`, which does the same job for the audit vocabulary.
"""

from __future__ import annotations

import pytest
import throttle
import web_auth


@pytest.fixture(scope="module")
def app_routes():
    from app import app

    return app


#: Routes that are reachable without a credential and are deliberately left on the shared default.
#:
#: Empty, and it should stay that way. An entry here is a route anyone who can reach the port may
#: call at the *authenticated* rate, which is 900/60 s — generous by design for a known operator and
#: far too generous for an anonymous caller. Adding one needs a reason written next to it.
UNAUTHENTICATED_DEFAULT_ALLOWED: frozenset[str] = frozenset()


def _unauthenticated_endpoints(app) -> set[str]:
    """Endpoints the auth gate does not protect.

    Read from `web_auth.AUTH_BYPASS_ENDPOINTS` rather than re-derived, so this gate and the gate it
    guards can never disagree about which routes are exposed.
    """
    known = {rule.endpoint for rule in app.url_map.iter_rules()}
    return {e for e in web_auth.AUTH_BYPASS_ENDPOINTS if e in known and e != "static"}


def test_every_unauthenticated_route_declares_a_throttle(app_routes):
    """The gate. A new unauthenticated route without an explicit limit fails here.

    The fix is one decorator on the view, with a rationale saying why that number. If the shared
    default is genuinely right, add the endpoint to `UNAUTHENTICATED_DEFAULT_ALLOWED` above with a
    comment — that is a deliberate, reviewable act rather than an omission.
    """
    exposed = _unauthenticated_endpoints(app_routes)
    assert exposed, "the bypass list resolved to nothing — this gate would be vacuous"

    declared = set(throttle.registry().declared())
    missing = sorted(exposed - declared - UNAUTHENTICATED_DEFAULT_ALLOWED)

    assert not missing, (
        "these routes are reachable with no credential and declare no throttle, so they fall back "
        "to the authenticated default (900/60s) — far too generous for an anonymous caller:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd @throttle.throttle(n, rationale=...) to each view, sized for what that route "
        "actually costs. `srp/begin` is ~52 ms of CPU per call; a cached JWKS read is not."
    )


def test_the_bypass_list_and_this_gate_cannot_drift(app_routes):
    """Every bypass entry must name a live route.

    An entry for a route that no longer exists reads as an intentional exemption to whoever audits
    the list next — the same reasoning that made removing `auth.api_login` from it part of FR-18.
    """
    known = {rule.endpoint for rule in app_routes.url_map.iter_rules()}
    stale = sorted(e for e in web_auth.AUTH_BYPASS_ENDPOINTS if e not in known and e != "static")
    assert not stale, f"bypass entries naming no live route: {stale}"


def test_declared_limits_are_stricter_than_the_authenticated_default():
    """A declaration that is *looser* than the default is almost certainly a mistake.

    The reason to declare on an unauthenticated route is that the shared default is too permissive
    for it. A larger number means either the route is unusually cheap — in which case say so in the
    rationale — or somebody raised a limit to make a symptom go away.
    """
    default = throttle.DEFAULT_AUTHENTICATED_LIMIT / throttle.DEFAULT_WINDOW
    looser = {
        endpoint: spec
        for endpoint, spec in throttle.registry().declared().items()
        if spec.per_second > default and not spec.rationale
    }
    assert not looser, (
        "declared limits looser than the authenticated default and with no rationale: "
        f"{ {k: v.limit for k, v in looser.items()} }"
    )
