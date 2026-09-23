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

    @pytest.mark.parametrize(
        ("action", "body", "expected"),
        [
            ("rack.power:on", {"rack": "", "specs": ["compute-001"]}, "rack.power:compute-001"),
            ("rack.gpu:reset", {"specs": ["compute-001"]}, "rack.gpu:compute-001"),
            ("rack.bmc:recover", {"node": "compute-001"}, "rack.bmc:compute-001"),
            ("rack.services:restart", {"specs": ["compute-001"]}, "rack.services:compute-001"),
            ("diag:sysinfo", {"target": "compute-001"}, "diag:compute-001"),
            ("fabric:preflight", {"target": "compute-001"}, "fabric:compute-001"),
            ("scheduler.node:drain", {"target": "compute-001"}, "scheduler.node:compute-001"),
            ("validation:launch", {"target": "compute-001"}, "validation:compute-001"),
        ],
    )
    def test_exact_node_is_used_when_rack_is_empty(self, action, body, expected):
        assert R.resolve(action, facts(body=body)) == expected

    def test_the_path_wins_over_the_body(self):
        """The URL is what the route matched and what the audit line records, so when a request
        carries both, the path is what actually selected the target. A body that could override it
        would let a caller aim a request at one rack and have it authorized against another."""
        got = R.resolve("rack.power:on", facts(view_args={"rack": "real"}, body={"rack": "other"}))
        assert got == "rack.power:real"

    def test_a_numeric_id_is_accepted(self):
        """Slurm job ids arrive as ints. Rejecting non-strings would refuse every job route."""
        assert R.resolve("scheduler.job:cancel", facts(view_args={"jid": 127391})) == "scheduler.job:127391"

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

    def test_a_workspace_tool_install_needs_no_rack(self):
        body = {
            "workspace": "/cm/shared/workspace",
            "build_host": "compute-001",
            "cuda_major": "13",
        }
        assert R.resolve("tools:install_cuda_nccl", facts(body=body)) == "tools:default"

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


class TestCallerSuppliedIdsAreValidated:
    """FR-4 / AC-5 — and the bypass they exist to prevent, pinned shut.

    The defect was a **normalisation differential**: the gate judged the string the caller sent
    while the handler folded it first, so the two disagreed about which resource was being touched.
    Measured 2026-09-08 on the commonest policy shape there is — a broad allow with a narrow deny.
    """

    @staticmethod
    def _engine(tmp_path):
        """Allow every rack power action, except on `p17-r01`."""
        path = tmp_path / "policies.json"
        path.write_text(json.dumps({"policies": [{
            "id": "operator",
            "statements": [
                {"effect": "allow", "actions": ["rack.power:*"], "resources": ["*"]},
                {"effect": "deny", "actions": ["rack.power:*"],
                 "resources": ["rack.power:p17-r01"]},
            ],
        }]}))
        return P.PolicyEngine(path, group_lookup=lambda p: (),
                              attachment_lookup=lambda s: ("operator",))

    @pytest.mark.parametrize("sent", ["p17-r01", "P17-R01", "P17-r01", "p17-R01"])
    def test_a_case_variant_cannot_evade_the_deny(self, tmp_path, sent):
        """The bug, in one assertion.

        `_normalize_rack_id` lower-cases, so every spelling here reaches the *same* rack. Before the
        fix the gate judged `rack.power:P17-R01`, which `fnmatchcase` does not match against
        `deny rack.power:p17-r01` — so the deny was skipped, the broad allow applied, and the denied
        rack was powered off by a caller who only had to hold shift.
        """
        engine = self._engine(tmp_path)
        resolved = R.resolve("rack.power:off", facts(body={"rack": sent}))
        assert not engine.evaluate("user:alice", "rack.power:off", resolved).allowed

    def test_the_gate_judges_what_the_handler_will_act_on(self, tmp_path):
        """The property underneath the test above, stated directly.

        Asserted against `naming._normalize_rack_id` rather than a hardcoded string, so if the
        handler's canonicalisation ever changes, this fails instead of the two silently diverging
        again.
        """
        import naming

        for sent in ("P17-R01", "p17-r01", "  P17-r01  "):
            resolved = R.resolve("rack.power:off", facts(body={"rack": sent}))
            assert resolved == f"rack.power:{naming._normalize_rack_id(sent.strip())}"

    @pytest.mark.parametrize(
        "sent",
        [
            "a*",          # fnmatch wildcard
            "a?b",         # fnmatch single-character wildcard
            "a[0-9]",      # fnmatch character class
            "a:b",         # would re-split the <type>:<id> rendering
            "../etc/passwd",
            "a/b",
            "a\\b",
            "a b",         # internal whitespace
            "a\x00b",      # control character: a log-injection primitive in an audit line
            "a\nb",
        ],
    )
    def test_an_id_outside_the_grammar_is_refused(self, sent):
        with pytest.raises(R.Unresolvable):
            R.resolve("rack.power:off", facts(body={"rack": sent}))

    def test_a_refused_id_is_a_bad_request_not_a_silent_grant(self, tmp_path):
        """`Unresolvable` is what the gate answers as 400. The alternative — passing the id through
        — is the failure mode `TestUnresolvableIsRefusedNotGuessed` already guards for missing ids:
        a wildcard grant matches anything, so a tolerated malformed id becomes a grant."""
        with pytest.raises(R.Unresolvable, match="may not contain"):
            R.resolve("rack.power:off", facts(body={"rack": "p17-*"}))

    def test_types_whose_ids_are_mixed_case_keep_their_case(self):
        """Folding is per-type on purpose. `help` content ids are camelCase (`singleRack`,
        `rackTable`) and their handler does not fold, so lower-casing them here would refuse every
        help read while fixing nothing — the gate and handler already agree for those types."""
        assert R.resolve("help:read", facts(view_args={"content_id": "singleRack"})) == "help:singleRack"

    def test_every_folded_type_is_a_declared_type(self):
        """A typo in `_FOLDED_TYPES` would silently stop folding a type that needs it."""
        assert R._FOLDED_TYPES <= R.declared_types()

    def test_the_folded_set_covers_the_rack_family(self):
        """The rack family is where the measured bypass lived, and it is easy to add a new
        rack-scoped type without noticing it needs folding too."""
        rack_scoped = set()
        for rtype, extractor in R._RESOLVERS.items():
            if extractor is R._singleton:
                continue
            # Types keyed on something other than `rack` simply do not resolve from this body, and
            # are not what this test is about.
            if extractor(facts(body={"rack": "p17-r01"})) == "p17-r01":
                rack_scoped.add(rtype)

        assert rack_scoped, "the probe found no rack-scoped types, so it is asserting nothing"
        assert rack_scoped <= R._FOLDED_TYPES
