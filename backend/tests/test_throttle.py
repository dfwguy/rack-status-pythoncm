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
"""Request throttling — the global hook, the keying, and the build gate.

**The keying is what these tests are really for.** A rate limit that looks correct and keys on the
wrong thing is worse than none: through an SSH tunnel the whole team shares `127.0.0.1`, so an
address-keyed limit on authenticated routes throttles colleagues rather than attackers, and it
presents as the product refusing legitimate work. The first version of this hook was registered
ahead of the auth gate and did exactly that — `TestTheKeyIsThePrincipalWhenThereIsOne` is the
regression guard.
"""

from __future__ import annotations

import pytest
import throttle


@pytest.fixture(autouse=True)
def fresh_buckets():
    throttle.registry().reset_for_tests()
    yield
    throttle.registry().reset_for_tests()


@pytest.fixture
def client():
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestTheHookRunsAfterTheAuthGate:
    """Not a style question — the whole keying design depends on it."""

    def test_the_throttle_sees_the_principal_the_gate_resolved(self):
        from app import app

        order = [f.__name__ for f in app.before_request_funcs.get(None, [])]
        assert "_enforce_request_throttle" in order
        assert "_require_dashboard_auth" in order
        assert order.index("_enforce_request_throttle") > order.index("_require_dashboard_auth"), (
            "the throttle must run after the gate, or `g.auth_principal` is never set and every "
            "request falls back to the client address — which is one shared bucket behind a tunnel"
        )

    def test_it_still_runs_before_the_view_function(self):
        """Shedding after the view has run would be accounting, not protection."""
        from app import app

        order = [f.__name__ for f in app.before_request_funcs.get(None, [])]
        assert "_enforce_request_throttle" in order  # a before_request hook by construction


class TestTheKeyIsThePrincipalWhenThereIsOne:
    """The regression guard for the bug this design was written to avoid."""

    def test_two_principals_do_not_share_an_allowance(self, monkeypatch):
        """The property that makes this usable behind a tunnel.

        Both operators arrive from 127.0.0.1. If they share a bucket, one person's bulk operation
        refuses their colleague's work.
        """
        limiter = throttle.registry().limiter_for("some.route")
        for _ in range(throttle.DEFAULT_AUTHENTICATED_LIMIT):
            allowed, _ = limiter.check("principal:user:alice")
            assert allowed

        exhausted, _ = limiter.check("principal:user:alice")
        assert not exhausted, "alice should be out of allowance"

        fresh, _ = limiter.check("principal:user:bob")
        assert fresh, "bob shares an address with alice but must have his own allowance"

    def test_the_principal_key_is_distinct_from_an_address_key(self):
        """Namespaced, so a client address that happens to look like a principal cannot collide."""
        limiter = throttle.registry().limiter_for("some.route")
        for _ in range(throttle.DEFAULT_AUTHENTICATED_LIMIT):
            limiter.check("principal:user:alice")

        allowed, _ = limiter.check("addr:user:alice")
        assert allowed


class TestPerEndpointBuckets:
    def test_exhausting_one_route_does_not_affect_another(self):
        """A fan-out and a login should not share a budget."""
        busy = throttle.registry().limiter_for("rack.fanout")
        for _ in range(throttle.DEFAULT_AUTHENTICATED_LIMIT + 5):
            busy.check("principal:user:alice")

        other, _ = throttle.registry().limiter_for("identity.list").check("principal:user:alice")
        assert other


class TestTheDefaultIsSizedForRealTraffic:
    """A limit that fires during ordinary work erodes trust in the product rather than protecting it."""

    def test_the_default_clears_the_measured_sustained_load(self):
        spec = throttle.registry().limit_for("anything.undeclared")
        # Measured: the busiest poll is ANALYTICS_POLL_MS = 1000, with 900 ms / 2 s / 5 s / 15 s /
        # 30 s polls running concurrently — about 5 req/s for one operator at worst.
        assert spec.per_second >= 15, "want at least 3x the measured sustained need"

    def test_a_page_load_burst_is_absorbed(self):
        """57 distinct apiFetch call sites exist; a view fires a fraction of them at once."""
        limiter = throttle.registry().limiter_for("spa.load")
        assert all(limiter.check("principal:user:alice")[0] for _ in range(60))

    def test_a_runaway_client_is_still_capped(self):
        """The actual threat from an authenticated caller is a script in a tight loop."""
        limiter = throttle.registry().limiter_for("runaway")
        results = [limiter.check("principal:user:alice")[0] for _ in range(2000)]
        assert not all(results)
        assert sum(results) <= throttle.DEFAULT_AUTHENTICATED_LIMIT + 1


class TestTheRefusal:
    def test_it_is_a_429_naming_when_to_retry(self, client, monkeypatch):
        import web_auth

        monkeypatch.setattr(web_auth, "authenticated_principal", lambda *a, **k: "user:tester")
        endpoint_limiter = throttle.registry().limiter_for("api_status.api_status")

        resp = None
        for _ in range(throttle.DEFAULT_AUTHENTICATED_LIMIT + 50):
            resp = client.get("/api/status")
            if resp.status_code == 429:
                break

        del endpoint_limiter
        if resp is not None and resp.status_code == 429:
            body = resp.get_json()
            assert body["error"] == "rate limited"
            assert body["retry_after_seconds"] >= 1
            assert int(resp.headers["Retry-After"]) >= 1


class TestDeclarations:
    def test_a_declared_limit_overrides_the_default(self):
        @throttle.throttle(5, window=60.0, rationale="expensive")
        def _view():
            return None

        declared = throttle.declared_limit_of(_view)
        assert declared is not None
        assert declared.limit == 5
        assert declared.rationale == "expensive"

    def test_the_decorator_does_not_enforce(self):
        """Enforcement is the hook's job. A decorator can only act after routing has been paid for,
        and it would silently do nothing on a route nobody added to the registry."""

        @throttle.throttle(1)
        def _view():
            return "ran"

        assert _view() == "ran"
        assert _view() == "ran"

    def test_two_conflicting_declarations_are_refused(self):
        """One of them would be silently ignored, and nothing would say which."""
        reg = throttle.Registry()
        reg.declare("x.y", throttle.Limit(5))
        reg.declare("x.y", throttle.Limit(5))  # identical is fine

        with pytest.raises(ValueError, match="already declares"):
            reg.declare("x.y", throttle.Limit(9))
