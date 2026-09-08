# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The authorization seam — the interface Phase 3 replaces from the inside.

Most of this file is about **one distinction**, because getting it wrong is the difference between
a permissive configuration and a silent product-wide bypass:

    an engine that allows everything   != no engine at all

The first is a deployment's recorded choice, visible as `policy_engine: allow-all` in every audit
event. The second is a missing line of startup code, and if it defaulted to permit, nothing would
ever notice. Phase 1 produced this exact failure shape twice — the config materialise hook and the
revocation checker — and both needed a test whose only job was to assert the wire existed.

The rest asserts that the seam cannot be *accidentally* permissive: an engine that raises, an
engine that returns something that is not a `Decision`, an engine that returns a truthy non-verdict.
Each of those is a plausible bug in a real evaluator, and each must land on deny.
"""

from __future__ import annotations

import logging

import authz
import pytest


@pytest.fixture(autouse=True)
def _clean_engine():
    """No engine registered by default, so a test that needs one must say so."""
    authz.reset_for_tests()
    yield
    authz.reset_for_tests()


class TestAnUnregisteredEngineRefuses:
    """The single most important behaviour in this module."""

    def test_authorize_denies_when_nothing_is_registered(self):
        decision = authz.authorize("user:admin", "rack.power:on", "rack:p5-r01")
        assert decision.allowed is False
        assert decision.engine == authz.ENGINE_UNSET

    def test_it_is_loud_about_it(self, caplog):
        """Nothing else will notice a missing engine, so the log has to.

        A quiet denial would be indistinguishable from a policy that happens to refuse, and the
        operator would go looking for a rule that does not exist.
        """
        with caplog.at_level(logging.ERROR, logger="authz"):
            authz.authorize("user:admin", "rack.power:on", "rack:p5-r01")
        assert caplog.records, "a missing engine must be reported at ERROR"
        assert "set_engine" in caplog.records[0].getMessage()

    def test_the_unset_marker_is_distinct_from_a_named_engine(self):
        """`unset` must never collide with an engine name, or an audit reader cannot tell a bug
        from a configuration."""
        assert authz.ENGINE_UNSET not in (
            authz.AllowAllEngine.name, authz.DenyAllEngine.name
        )


class TestTheAllowAllEngine:
    """Constructs the engine directly rather than calling `install_default()`.

    `install_default()` chooses by environment — `AllowAllEngine` when no policy file exists,
    `PolicyEngine` when one does — so a suite that enforces policy gets the enforcing engine and
    these tests would be asserting permissiveness against an engine that is correctly refusing.
    That choice is a contract in its own right and has its own test below; this class is about what
    the permissive engine *does* once installed.
    """

    def test_it_permits_and_names_itself(self):
        authz.set_engine(authz.AllowAllEngine())
        decision = authz.authorize("user:admin", "rack.power:on", "rack:p5-r01")
        assert decision.allowed is True
        assert decision.engine == "allow-all"

    def test_installing_it_is_an_explicit_act(self):
        """Importing the module must not register anything.

        An engine that registers itself on import makes the permissive default impossible to avoid,
        which defeats the unregistered-refuses rule entirely — there would be no state in which the
        missing wire is detectable.
        """
        assert authz.current_engine() is None
        assert authz.engine_name() == authz.ENGINE_UNSET

    def test_it_permits_regardless_of_the_arguments(self):
        """It is a stub, and the test says so rather than implying a policy that is not there."""
        authz.set_engine(authz.AllowAllEngine())
        for principal, action, resource in [
            ("user:admin", "rack.power:off", "rack:p5-r01"),
            ("user:nobody", "terminal:open", "node:whatever"),
            ("", "", ""),
        ]:
            assert authz.authorize(principal, action, resource).allowed


class TestTheDenyAllEngine:
    def test_it_refuses_and_names_itself(self):
        authz.set_engine(authz.DenyAllEngine())
        decision = authz.authorize("user:admin", "rack:uptime", "rack:p5-r01")
        assert decision.allowed is False
        assert decision.engine == "deny-all"


class TestTheSeamFailsClosed:
    def test_an_engine_that_raises_is_a_denial(self):
        """An evaluator that cannot decide has not permitted anything.

        Letting the exception escape would surface as a 500, which a caller reads as "try again"
        rather than "you may not" — and a retry loop against a broken evaluator is the worst of
        both.
        """

        class Exploding:
            name = "exploding"

            def evaluate(self, principal, action, resource):
                raise RuntimeError("policy store unreachable")

        authz.set_engine(Exploding())
        decision = authz.authorize("user:admin", "rack.power:on", "rack:p5-r01")
        assert decision.allowed is False
        assert decision.engine == "exploding"

    def test_an_engine_returning_a_non_decision_is_a_denial(self):
        """`return True` is a plausible bug in a first evaluator, and truthiness is not a verdict."""

        class Sloppy:
            name = "sloppy"

            def evaluate(self, principal, action, resource):
                return True

        authz.set_engine(Sloppy())
        assert authz.authorize("user:admin", "a:b", "r:1").allowed is False

    def test_an_engine_returning_none_is_a_denial(self):
        class Forgetful:
            name = "forgetful"

            def evaluate(self, principal, action, resource):
                return None

        authz.set_engine(Forgetful())
        assert authz.authorize("user:admin", "a:b", "r:1").allowed is False


class TestTheDecisionRecord:
    def test_it_is_immutable(self):
        """A verdict that can be edited after the fact is not evidence."""
        from dataclasses import FrozenInstanceError

        decision = authz.Decision(allowed=True, engine="x")
        with pytest.raises(FrozenInstanceError):
            decision.allowed = False

    def test_it_is_truthy_by_its_verdict(self):
        assert authz.Decision(allowed=True, engine="x")
        assert not authz.Decision(allowed=False, engine="x")

    def test_a_denial_carries_a_reason(self):
        authz.set_engine(authz.DenyAllEngine())
        assert authz.authorize("user:a", "b:c", "d:1").reason


class TestTheAuditRecordCanNameTheEngine:
    """The seam is only useful if the verdict reaches the trail (NFR-13, threat T-52)."""

    def test_emit_accepts_a_policy_engine(self):
        import audit

        event = audit.build_event(
            action="rack.power:on",
            result=audit.Result.SUCCESS,
            decision=audit.Decision.ALLOW,
            policy_engine="allow-all",
        )
        assert event["decision"] == "allow"
        assert event["policy_engine"] == "allow-all"

    def test_it_still_defaults_to_none_for_callers_that_do_not_evaluate(self):
        """Most emit sites are not authorization decisions and must not claim to be."""
        import audit

        event = audit.build_event(action="rack.power:on", result=audit.Result.SUCCESS)
        assert event["decision"] == "not-evaluated"
        assert event["policy_engine"] == "none"

    def test_the_field_set_is_unchanged(self):
        """Adding a parameter must not reshape the record; a trail whose schema drifts cannot be
        compared over time."""
        import audit

        event = audit.build_event(
            action="a:b", result=audit.Result.SUCCESS, policy_engine="allow-all"
        )
        assert set(event) == set(audit.EVENT_FIELDS)
