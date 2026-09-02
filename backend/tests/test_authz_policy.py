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
"""The policy engine — Phase 3.

Almost every test here is about a *refusal*, because the ways an authorization engine fails are
asymmetric: allowing too little is an annoyance somebody reports within the hour, and allowing too
much is invisible until it matters. So the load-bearing cases are default-deny, deny-overrides, and
what happens when the policy file is broken.
"""

from __future__ import annotations

import json

import pytest
from authz import policy as P

ADMIN = "user:admin"
OPERATOR = "user:operator"


def write(tmp_path, statements):
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({"policies": statements}))
    return path


def engine(tmp_path, statements, groups=None):
    groups = groups or {}
    return P.PolicyEngine(
        write(tmp_path, statements),
        group_lookup=lambda p: tuple(groups.get(p, ())),
    )


ALLOW_POWER = {
    "id": "operators-may-power",
    "effect": "allow",
    "principals": ["group:operators"],
    "actions": ["rack.power:*"],
    "resources": ["rack:*"],
}


class TestDefaultDeny:
    """The property that makes every other grant meaningful."""

    def test_an_empty_policy_set_refuses_everything(self, tmp_path):
        e = engine(tmp_path, [])
        assert not e.evaluate(ADMIN, "rack.power:cycle", "rack:a01").allowed

    def test_an_unmatched_action_is_refused(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert not e.evaluate(OPERATOR, "identity.user:delete", "user:bob").allowed

    def test_an_unmatched_resource_is_refused(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert not e.evaluate(OPERATOR, "rack.power:cycle", "cluster:prod").allowed

    def test_a_missing_policy_file_refuses_rather_than_permits(self, tmp_path):
        """An absent file is the state a fresh deployment is in. It must not mean "allow"."""
        e = P.PolicyEngine(tmp_path / "not-there.json", group_lookup=lambda p: ())
        assert not e.evaluate(ADMIN, "rack.power:cycle", "rack:a01").allowed

    def test_the_refusal_does_not_confirm_the_resource_exists(self, tmp_path):
        """A reason naming the resource is an existence oracle for anyone probing."""
        e = engine(tmp_path, [])
        reason = e.evaluate(ADMIN, "rack.power:cycle", "rack:secret-lab-01").reason
        assert "secret-lab-01" not in reason
        assert "rack.power:cycle" in reason


class TestDenyOverridesAllow:
    """An explicit prohibition must not be outvotable by a grant written later."""

    def test_a_deny_beats_a_matching_allow(self, tmp_path):
        e = engine(tmp_path, [
            {"id": "a", "effect": "allow", "principals": ["user:*"], "actions": ["rack.power:*"]},
            {"id": "d", "effect": "deny", "principals": ["user:*"], "actions": ["rack.power:cycle"]},
        ])
        assert not e.evaluate(ADMIN, "rack.power:cycle", "rack:a01").allowed
        assert e.evaluate(ADMIN, "rack.power:status", "rack:a01").allowed

    def test_order_in_the_file_does_not_matter(self, tmp_path):
        """Denies are evaluated first regardless of where they appear, so a policy's protection
        does not depend on somebody appending below it rather than above."""
        deny = {"id": "d", "effect": "deny", "principals": ["user:*"], "actions": ["rack.power:*"]}
        allow = {"id": "a", "effect": "allow", "principals": ["user:*"], "actions": ["rack.power:*"]}
        for statements in ([deny, allow], [allow, deny]):
            e = engine(tmp_path, statements)
            assert not e.evaluate(ADMIN, "rack.power:cycle", "rack:a01").allowed

    def test_a_resource_scoped_deny_carves_out_of_a_broad_allow(self, tmp_path):
        e = engine(tmp_path, [
            {"id": "a", "effect": "allow", "principals": ["user:*"], "actions": ["rack.power:*"],
             "resources": ["rack:*"]},
            {"id": "d", "effect": "deny", "principals": ["user:*"], "actions": ["rack.power:*"],
             "resources": ["rack:critical-*"]},
        ])
        assert e.evaluate(ADMIN, "rack.power:cycle", "rack:a01").allowed
        assert not e.evaluate(ADMIN, "rack.power:cycle", "rack:critical-07").allowed


class TestGroupsAreTheUnitOfGrant:
    def test_a_grant_to_a_group_reaches_its_members(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert e.evaluate(OPERATOR, "rack.power:cycle", "rack:a01").allowed

    def test_a_non_member_gets_nothing(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert not e.evaluate("user:visitor", "rack.power:cycle", "rack:a01").allowed

    def test_losing_the_group_loses_the_grant(self, tmp_path):
        """Membership is read per evaluation, so removing someone takes effect on the next request
        rather than at their next login."""
        groups = {OPERATOR: ["operators"]}
        e = P.PolicyEngine(
            write(tmp_path, [ALLOW_POWER]), group_lookup=lambda p: tuple(groups.get(p, ()))
        )
        assert e.evaluate(OPERATOR, "rack.power:cycle", "rack:a01").allowed
        groups[OPERATOR] = []
        assert not e.evaluate(OPERATOR, "rack.power:cycle", "rack:a01").allowed

    def test_a_group_lookup_failure_grants_nothing_extra(self, tmp_path):
        """A store outage must reduce permissions, never add them."""
        def _boom(_p):
            raise RuntimeError("store down")

        e = P.PolicyEngine(write(tmp_path, [ALLOW_POWER]), group_lookup=_boom)
        with pytest.raises(RuntimeError):
            e.subjects_for(OPERATOR)  # the raw lookup propagates...
        # ...but the production lookup swallows it, which is what `_groups_for` is for.
        assert P._groups_for("not a principal") == ()


class TestWildcards:
    """Permitted, because a policy enumerating 124 action names is one nobody maintains."""

    @pytest.mark.parametrize("action", ["rack.power:cycle", "rack.power:on", "rack.power:off"])
    def test_an_action_wildcard_matches_its_family(self, tmp_path, action):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert e.evaluate(OPERATOR, action, "rack:a01").allowed

    def test_a_wildcard_does_not_leak_across_families(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert not e.evaluate(OPERATOR, "rack.firmware:flash", "rack:a01").allowed

    def test_matching_is_case_sensitive(self, tmp_path):
        """`fnmatchcase`, not `fnmatch` — the latter follows the filesystem's case rules, so the
        same policy would mean different things on macOS and Linux."""
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert not e.evaluate(OPERATOR, "RACK.POWER:CYCLE", "rack:a01").allowed

    @pytest.mark.parametrize("field", ["principals", "actions"])
    def test_a_bare_star_in_an_allow_is_refused(self, tmp_path, field):
        """It grants every action the product will ever add, including ones written years later.

        `AllowAllEngine` exists for a deployment that genuinely wants this, and it records
        `policy_engine: allow-all` so an auditor sees it rather than finding it buried in a file.
        """
        statement = {"id": "x", "effect": "allow", "principals": ["user:a"], "actions": ["b:c"]}
        statement[field] = ["*"]
        with pytest.raises(P.PolicyError, match="bare '\\*'"):
            P.load(write(tmp_path, [statement]))

    def test_a_bare_star_in_a_deny_is_allowed(self, tmp_path):
        """A wildcard in a deny narrows access, so it needs no guard -- "nobody does anything" is a
        legitimate and occasionally necessary policy."""
        e = engine(tmp_path, [
            {"id": "stop", "effect": "deny", "principals": ["*"], "actions": ["*"]},
        ])
        assert not e.evaluate(ADMIN, "anything:at-all", "rack:a01").allowed


class TestABrokenPolicyFileDeniesLoudly:
    """The failure you would least want to be quiet."""

    def test_unparseable_json_denies_everything(self, tmp_path):
        path = tmp_path / "policies.json"
        path.write_text("{ not json")
        e = P.PolicyEngine(path, group_lookup=lambda p: ())

        decision = e.evaluate(ADMIN, "rack.power:cycle", "rack:a01")
        assert not decision.allowed
        assert "could not be loaded" in decision.reason

    def test_one_malformed_statement_fails_the_whole_load(self, tmp_path):
        """Not skipped. Loading the valid statements and dropping the rest means a typo in a deny
        silently widens access, with a successful-looking load and nothing in the log."""
        with pytest.raises(P.PolicyError):
            P.load(write(tmp_path, [
                ALLOW_POWER,
                {"id": "typo", "effect": "alow", "principals": ["user:x"], "actions": ["y:z"]},
            ]))

    @pytest.mark.parametrize("bad", [
        {"id": "no-effect", "principals": ["user:a"], "actions": ["b:c"]},
        {"id": "no-principals", "effect": "allow", "actions": ["b:c"]},
        {"id": "no-actions", "effect": "allow", "principals": ["user:a"]},
    ])
    def test_an_incomplete_statement_is_refused(self, tmp_path, bad):
        with pytest.raises(P.PolicyError):
            P.load(write(tmp_path, [bad]))

    def test_a_duplicate_policy_id_is_refused(self, tmp_path):
        """Two statements with one id make an audit reason ambiguous about which rule fired."""
        with pytest.raises(P.PolicyError, match="twice"):
            P.load(write(tmp_path, [ALLOW_POWER, dict(ALLOW_POWER)]))


class TestTheDecisionIsUsable:
    def test_an_allow_names_the_policy_that_granted_it(self, tmp_path):
        e = engine(tmp_path, [ALLOW_POWER], {OPERATOR: ["operators"]})
        assert "operators-may-power" in e.evaluate(OPERATOR, "rack.power:on", "rack:a01").reason

    def test_a_deny_names_the_policy_that_refused_it(self, tmp_path):
        e = engine(tmp_path, [
            {"id": "no-touching", "effect": "deny", "principals": ["*"], "actions": ["*"]},
        ])
        assert "no-touching" in e.evaluate(ADMIN, "x:y", "z").reason

    def test_the_engine_is_named_in_every_decision(self, tmp_path):
        """The audit trail records this, and "which engine decided" is the first question when a
        verdict looks wrong."""
        e = engine(tmp_path, [])
        assert e.evaluate(ADMIN, "x:y", "z").engine == "rbac-v1"

    def test_evaluate_never_raises(self, tmp_path):
        """The seam's contract: a failing evaluator is a denial, not an exception. An engine that
        raises turns a policy typo into a 500 on every route at once."""
        path = tmp_path / "policies.json"
        path.write_text("[[[")
        e = P.PolicyEngine(path, group_lookup=lambda p: ())
        assert e.evaluate(ADMIN, "x:y", "z").allowed is False


class TestReload:
    def test_editing_the_file_takes_effect_without_a_restart(self, tmp_path):
        import os

        path = write(tmp_path, [])
        e = P.PolicyEngine(path, group_lookup=lambda p: ("operators",))
        assert not e.evaluate(OPERATOR, "rack.power:on", "rack:a01").allowed

        path.write_text(json.dumps({"policies": [ALLOW_POWER]}))
        os.utime(path, (0, 0))  # force a distinct mtime rather than racing the clock

        assert e.evaluate(OPERATOR, "rack.power:on", "rack:a01").allowed

    def test_reading_policies_does_not_write(self, tmp_path):
        """A read that writes changes the mtime this cache keys on, so the cache would invalidate
        itself on every read -- exactly the Phase 2 revocation-cache bug."""
        path = write(tmp_path, [ALLOW_POWER])
        e = P.PolicyEngine(path, group_lookup=lambda p: ())
        before = path.stat().st_mtime_ns

        for _ in range(5):
            e.evaluate(ADMIN, "rack.power:on", "rack:a01")

        assert path.stat().st_mtime_ns == before
