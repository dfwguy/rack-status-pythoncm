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
"""The resource resolver (FR-5, FR-6; TC-11, TC-12, TC-14).

`TestTheResourceIsNotTheUrlPath` is the regression test for the bug this module was written to fix,
and it is the one to keep. The gate passed `request.path` as the resource, so a policy naming
`rack.power:a01` matched nothing — and the failure was **silent**, because with no policy file the
engine is permissive and nothing appeared wrong. That is a defect that surfaces the first time a
resource-scoped policy is the only thing standing between an operator and a rack.

The other axis worth stating up front: **an unresolvable resource must be refused, never filled
in.** `rack.power:*` matches `rack.power:<anything>`, so any placeholder for "could not work out
which rack" is matched by every wildcard grant an operator writes, turning a resolution failure into
a grant. `TestUnresolvableIsRefusedNotGuessed` pins that, including the specific case of a filler
being caught by a wildcard.
"""

from __future__ import annotations

import json

import pytest
from authz import policy as P
from authz import resources as R


def facts(view_args=None, query=None, body=None) -> R.RequestFacts:
    return R.RequestFacts(view_args or {}, query or {}, body or {})


class TestTheTypeComesFromTheAction:
    """Not from the URL, and not passed in separately — the two would drift."""

    @pytest.mark.parametrize(
        ("action", "given", "expected"),
        [
            ("rack.power:on", {"rack": "a01"}, "rack.power:a01"),
            ("rack.gpu:reset", {"rack": "h18"}, "rack.gpu:h18"),
            ("identity.user:delete", {"login": "bob"}, "identity.user:bob"),
            ("identity.group:create", {"name": "ops"}, "identity.group:ops"),
            ("workflow:run", {"workflow_id": "wf-3"}, "workflow:wf-3"),
            ("validation.activity:cancel", {"activity_id": "abc"}, "validation.activity:abc"),
        ],
    )
    def test_the_namespace_becomes_the_resource_type(self, action, given, expected):
        assert R.resolve(action, facts(view_args=given)) == expected

    def test_the_verb_is_not_part_of_the_resource(self):
        """`rack.power:on` acts on `rack.power:a01`, not on `on:a01`. Getting this backwards would
        make every policy match by accident."""
        assert R.resolve("rack.power:on", facts(view_args={"rack": "a01"})) == "rack.power:a01"

    def test_the_user_resource_rendering_is_not_the_principal_rendering(self):
        """`identity.user:bob` (administered) versus `user:bob` (acting). Same record, two roles —
        a policy about who may edit users needs the first, and conflating them would let a grant to
        *be* bob read as a grant to *edit* bob (RESOURCE_MODEL.md)."""
        assert R.resolve("identity.user:update", facts(view_args={"login": "bob"})) \
            == "identity.user:bob"


class TestWhereTheIdIsFound:
    def test_a_url_parameter(self):
        assert R.resolve("rack.power:on", facts(view_args={"rack": "a01"})) == "rack.power:a01"

    def test_a_query_parameter(self):
        assert R.resolve("validation:launch", facts(query={"rack": "h18"})) == "validation:h18"

    def test_a_json_body_field(self):
        assert R.resolve("rack.power:on", facts(body={"rack": "b06"})) == "rack.power:b06"

    def test_the_path_wins_over_the_body(self):
        """The URL is what the route matched and what the audit line records, so when a request
        carries both, the path is what actually selected the target. A body that could override it
        would let a caller aim a request at one rack and have it authorized against another."""
        got = R.resolve("rack.power:on", facts(view_args={"rack": "real"}, body={"rack": "other"}))
        assert got == "rack.power:real"

    def test_a_numeric_id_is_accepted(self):
        """Slurm job ids arrive as ints. Rejecting non-strings would refuse every job route."""
        assert R.resolve("slurm.job:cancel", facts(view_args={"jid": 127391})) == "slurm.job:127391"

    def test_a_boolean_is_not_an_id(self):
        """`bool` is an `int` in Python, so a careless numeric check turns `{"rack": true}` into
        `rack.power:True` — a resource that exists in no cluster and matches `rack.power:*`."""
        with pytest.raises(R.Unresolvable):
            R.resolve("rack.power:on", facts(body={"rack": True}))

    def test_whitespace_is_not_an_id(self):
        with pytest.raises(R.Unresolvable):
            R.resolve("rack.power:on", facts(body={"rack": "   "}))


class TestUnresolvableIsRefusedNotGuessed:
    """The property that makes the whole module safe."""

    def test_a_scoped_type_with_no_id_raises(self):
        with pytest.raises(R.Unresolvable, match="which rack.power"):
            R.resolve("rack.power:on", facts())

    def test_an_undeclared_type_raises(self):
        """Only reachable if the build gate was bypassed. Refusing is the sole safe answer: we
        cannot name the resource, so we cannot know a policy meant to permit it."""
        with pytest.raises(R.Unresolvable, match="no resource resolver declared"):
            R.resolve("brand.new.thing:do", facts(view_args={"id": "x"}))

    def test_a_filler_id_would_have_been_matched_by_a_wildcard(self, tmp_path):
        """Why raising beats returning a placeholder, demonstrated rather than asserted in prose.

        A grant of `rack.power:*` — the obvious thing an operator writes for "the rack operators may
        power-cycle racks" — matches any filler we could have invented. So a request whose target we
        failed to identify would have been *permitted*.
        """
        path = tmp_path / "policies.json"
        path.write_text(json.dumps({"policies": [{
            "id": "ops",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:*"]}],
        }]}))
        engine = P.PolicyEngine(path, group_lookup=lambda p: (),
                                attachment_lookup=lambda s: ("ops",))

        for filler in ("unknown", "unresolved", "", "*", "None"):
            assert engine.evaluate("user:alice", "rack.power:on", f"rack.power:{filler}").allowed, (
                f"{filler!r} is matched by the wildcard grant, which is exactly why resolve() "
                "raises instead of returning one"
            )


class TestSingletons:
    @pytest.mark.parametrize("action", ["settings:update", "vault:write", "cluster:read"])
    def test_a_singleton_needs_no_id(self, action):
        assert R.resolve(action, facts()).endswith(":default")

    def test_a_singleton_ignores_a_supplied_id(self):
        """One instance per deployment, so a caller cannot invent a second one to be authorized
        against — `settings:mine` would otherwise be a resource nobody wrote a deny for."""
        assert R.resolve("settings:update", facts(body={"id": "mine"})) == "settings:default"


class TestTheResourceIsNotTheUrlPath:
    """TC-14 — the regression test for the silent failure this module fixes.

    Kept as an explicit comparison rather than folded into the cases above, because the defect was
    not "the wrong string" but "a string no policy could ever match, failing quietly".
    """

    def test_a_policy_matches_the_resolved_resource(self, tmp_path):
        path = tmp_path / "policies.json"
        path.write_text(json.dumps({"policies": [{
            "id": "rack-a01-only",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:a01"]}],
        }]}))
        engine = P.PolicyEngine(path, group_lookup=lambda p: (),
                                attachment_lookup=lambda s: ("rack-a01-only",))

        resolved = R.resolve("rack.power:on", facts(view_args={"rack": "a01"}))
        assert engine.evaluate("user:alice", "rack.power:on", resolved).allowed

        # ...and the URL the gate used to pass matches nothing, which is the whole bug: a
        # deployment would default-deny every resource-scoped rule and look fine while doing it.
        assert not engine.evaluate("user:alice", "rack.power:on", "/api/rack-power/on").allowed

    def test_the_resolved_resource_still_discriminates_between_racks(self, tmp_path):
        """A resolver that produced the same string for every rack would restore the bug in the
        opposite direction — one grant covering the fleet."""
        path = tmp_path / "policies.json"
        path.write_text(json.dumps({"policies": [{
            "id": "rack-a01-only",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:a01"]}],
        }]}))
        engine = P.PolicyEngine(path, group_lookup=lambda p: (),
                                attachment_lookup=lambda s: ("rack-a01-only",))

        permitted = R.resolve("rack.power:on", facts(view_args={"rack": "a01"}))
        other = R.resolve("rack.power:on", facts(view_args={"rack": "critical-7"}))

        assert engine.evaluate("user:alice", "rack.power:on", permitted).allowed
        assert not engine.evaluate("user:alice", "rack.power:on", other).allowed
