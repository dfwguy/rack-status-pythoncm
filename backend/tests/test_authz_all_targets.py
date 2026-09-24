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
"""A request naming several targets must be authorized against every one (FR-6a, AC-8).

**This was a live authorization bypass, not a theoretical gap.** The gate resolved one resource per
request via `RequestFacts.first()`, so a caller granted `rack.power:p17-r01` could post
`racks: ["p17-r01", "p17-r02"]`, and the gate — having examined only the first element — returned
`allow`. The second rack had no grant of any kind. Measured before the fix; the verdict was
`ALLOWED`.

The defence previously recorded in `first()`'s docstring was that the multirack dispatchers refuse a
group that is not a clean partition of the reviewed target. That control is real and it is a
different one: it checks the *selection* is coherent, never that the caller holds a grant for each
member. A correct-looking partition of racks the operator may not touch passes it.

The fix is all-of evaluation in `resolve_all`: every named target must be permitted and the first
refusal refuses the request. Any-of would be the same bypass with extra steps.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import authz
from authz import resources
from authz.policy import PolicyEngine

GRANTED = "p17-r01"
UNGRANTED = "p17-r02"
ACTION = "rack.power:off"


@pytest.fixture
def one_rack_engine(tmp_path):
    """An engine whose only grant is `rack.power:off` on ONE rack."""
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(json.dumps({"policies": [{
        "id": "one-rack",
        "statements": [{
            "sid": "s1", "effect": "allow",
            "actions": [ACTION], "resources": [f"rack.power:{GRANTED}"],
        }],
    }]}), encoding="utf-8")
    return PolicyEngine(
        policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: ("one-rack",)
    )


def _decide(engine, facts) -> tuple[bool, str]:
    """All-of, the way the gate does it. Returns (allowed, the resource that decided)."""
    for resource in resources.resolve_all(ACTION, facts):
        verdict = engine.evaluate("user:op", ACTION, resource)
        if not verdict.allowed:
            return False, resource
    return True, ""


class TestEveryNamedTargetIsJudged:
    def test_a_grant_for_one_rack_does_not_cover_two(self, one_rack_engine):
        """The bypass, stated as the test that would have caught it."""
        facts = resources.RequestFacts({}, {}, {"racks": [GRANTED, UNGRANTED]})
        allowed, refused = _decide(one_rack_engine, facts)

        assert not allowed, (
            f"a grant for {GRANTED} authorized an operation naming {UNGRANTED} as well"
        )
        assert refused == f"rack.power:{UNGRANTED}", (
            "the refusal should name the target that lacked a grant"
        )

    def test_order_does_not_decide_it(self, one_rack_engine):
        """Ungranted first must refuse too — otherwise the fix is just a differently-placed first().

        Worth its own case: an implementation that checked the *last* element instead of the first
        would pass the test above and fail this one.
        """
        facts = resources.RequestFacts({}, {}, {"racks": [UNGRANTED, GRANTED]})
        allowed, _ = _decide(one_rack_engine, facts)
        assert not allowed

    def test_the_granted_rack_alone_is_still_permitted(self, one_rack_engine):
        """The control. Without it, every assertion here would pass on a gate that refuses
        everything — which is a different defect wearing the same green tick."""
        facts = resources.RequestFacts({}, {}, {"racks": [GRANTED]})
        allowed, _ = _decide(one_rack_engine, facts)
        assert allowed

    def test_all_granted_targets_are_permitted(self, tmp_path):
        """All-of must not become none-of: a caller granted both racks may name both."""
        policy_file = tmp_path / "policies.json"
        policy_file.write_text(json.dumps({"policies": [{
            "id": "both",
            "statements": [{
                "sid": "s1", "effect": "allow", "actions": [ACTION],
                "resources": [f"rack.power:{GRANTED}", f"rack.power:{UNGRANTED}"],
            }],
        }]}), encoding="utf-8")
        engine = PolicyEngine(
            policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: ("both",)
        )

        facts = resources.RequestFacts({}, {}, {"racks": [GRANTED, UNGRANTED]})
        allowed, _ = _decide(engine, facts)
        assert allowed

    def test_a_duplicate_target_costs_one_evaluation(self, one_rack_engine):
        """De-duplicated, so a refusal names a target once and a repeated list is not a multiplier."""
        facts = resources.RequestFacts({}, {}, {"racks": [GRANTED, GRANTED, GRANTED]})
        assert resources.resolve_all(ACTION, facts) == (f"rack.power:{GRANTED}",)


class TestResolveAll:
    def test_a_single_target_is_unchanged(self):
        facts = resources.RequestFacts({}, {"rack": GRANTED}, {})
        assert resources.resolve_all(ACTION, facts) == (f"rack.power:{GRANTED}",)

    def test_a_collection_is_one_resource(self):
        """A request naming nothing has exactly one target — the set. Multiplying that out would
        mean nothing, and the collection is a separate resource that must be granted separately."""
        facts = resources.RequestFacts({}, {}, {})
        assert resources.resolve_all("rack:list", facts) == (f"rack:{resources.COLLECTION}",)

    def test_case_folding_still_applies_to_every_element(self):
        """The other bypass this file's sibling closed (trap §10) must hold for all targets, not
        just the first — otherwise `["p17-r01", "P17-R02"]` would evade a lower-cased deny."""
        facts = resources.RequestFacts({}, {}, {"racks": ["P17-R01", "P17-R02"]})
        assert resources.resolve_all(ACTION, facts) == (
            "rack.power:p17-r01", "rack.power:p17-r02",
        )

    def test_an_unresolvable_target_still_refuses(self):
        facts = resources.RequestFacts({}, {}, {})
        with pytest.raises(resources.Unresolvable):
            resources.resolve_all(ACTION, facts)

    def test_too_many_targets_is_refused_not_truncated(self):
        """All-of is linear in the target count, so an unbounded list spends CPU from an
        authenticated session. Refused rather than truncated — truncation is how the bypass this
        bound protects against would come back.
        """
        many = [f"p17-r{n:03d}" for n in range(resources.MAX_TARGETS + 1)]
        facts = resources.RequestFacts({}, {}, {"racks": many})
        with pytest.raises(resources.Unresolvable) as caught:
            resources.resolve_all(ACTION, facts)
        assert str(resources.MAX_TARGETS) in str(caught.value)

    def test_the_ceiling_admits_a_real_campaign(self):
        """The bound must be well clear of legitimate use, or it becomes the defect. The largest
        reviewed campaign in this product is a few dozen racks."""
        assert resources.MAX_TARGETS >= 64
        many = [f"p17-r{n:03d}" for n in range(64)]
        facts = resources.RequestFacts({}, {}, {"racks": many})
        assert len(resources.resolve_all(ACTION, facts)) == 64


class TestTheGateUsesIt:
    def test_the_gate_calls_resolve_all_not_resolve(self):
        """A source-level check, because the difference is invisible at runtime for the single-target
        case that every other test exercises — the file could revert to `resolve` and only a
        multi-target request would notice."""
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        assert "_authz_resources.resolve_all(" in source, (
            "the gate must authorize every named target"
        )

    def test_the_engine_is_asked_once_per_target(self, one_rack_engine, monkeypatch):
        """Counts the calls, so 'all-of' is asserted as behaviour rather than inferred from source.

        Two targets both granted must produce two evaluations; a short-circuit that checked one and
        assumed the rest would pass the permission tests above.
        """
        seen: list[str] = []
        real = one_rack_engine.evaluate

        def counting(principal, action, resource):
            seen.append(resource)
            return real(principal, action, resource)

        monkeypatch.setattr(one_rack_engine, "evaluate", counting)
        facts = resources.RequestFacts({}, {}, {"racks": [GRANTED, UNGRANTED]})
        _decide(one_rack_engine, facts)

        assert seen == [f"rack.power:{GRANTED}", f"rack.power:{UNGRANTED}"]


def test_the_engine_name_helper_exists():
    """The gate seeds its verdict with `authz.engine_name()`; a rename there would break the gate
    in a way no authorization test would notice, because the seed is only read when the target list
    is empty."""
    assert isinstance(authz.engine_name(), str)


class TestTheKeyListIsAFallbackChain:
    """Not a set of aliases — and getting that wrong broke every policy detach.

    `_by("policy_id", "id", "subject")` means *try policy_id, else id, else subject*. My first
    `all_of` swept all three, so a detach request resolved to two targets: the policy and the
    principal `user:alice`, whose colon the id grammar refuses. Every detach answered 400.

    Expanding one key is still what closes the fan-out bypass, because a multi-target request names
    its targets within a single key (`racks: [a01, a02]`) rather than across the chain.
    """

    def test_a_later_key_in_the_chain_is_not_an_extra_target(self):
        facts = resources.RequestFacts(
            {"policy_id": "rack-operators", "subject": "user:alice"}, {}, {}
        )
        assert resources.resolve_all("policy:detach", facts) == ("policy:rack-operators",)

    def test_the_chain_still_falls_back(self):
        """With no `policy_id`, `subject` is what the route offers — the fallback must still work."""
        facts = resources.RequestFacts({}, {}, {"id": "rack-operators"})
        assert resources.resolve_all("policy:detach", facts) == ("policy:rack-operators",)

    def test_first_and_all_of_agree_on_the_head(self):
        """`first()` must return the head of what `all_of()` returns, or the gate would authorize a
        different set than the trail reports."""
        for facts in (
            resources.RequestFacts({}, {}, {"racks": [GRANTED, UNGRANTED]}),
            resources.RequestFacts({}, {"rack": GRANTED}, {}),
            resources.RequestFacts({"policy_id": "p", "subject": "user:a"}, {}, {}),
        ):
            keys = resources._RESOLVERS["rack.power"].keys  # type: ignore[attr-defined]
            values = facts.all_of(*keys)
            if values:
                assert facts.first(*keys) == values[0]


class TestACommaJoinedTargetIsSeveralTargets:
    """The same bypass, arriving through `target` instead of `racks`.

    `target` is a Slurm nodelist, and node-scoped actions resolve against it since the
    rack-assignment gates were removed. A comma-joined value therefore names several nodes in
    one string, and judging it whole reopens exactly what `all_of` closed: `"n04,n05"` would
    resolve to the single opaque resource `scheduler.node:n04,n05`, which `allow
    scheduler.node:*` matches and a narrow `deny scheduler.node:n05` does not — and the handler
    drains both. Measured before the split; the verdict was `allow`.

    Bracket ranges were never exposed this way: the id grammar refuses `[` and `]`, so
    `n[01-18]` fails closed. Commas are legal characters, which is what made this the reachable
    half.
    """

    NODE_ACTION = "scheduler.node:drain"
    KEPT = "ink50-dh01-h18-04"
    PROTECTED = "ink50-dh01-h18-05"

    @pytest.fixture
    def broad_allow_narrow_deny(self, tmp_path):
        """The commonest policy shape there is, per `identity-auth-traps.mdc` §10."""
        policy_file = tmp_path / "policies.json"
        policy_file.write_text(json.dumps({"policies": [{
            "id": "ops",
            "statements": [
                {"sid": "a", "effect": "allow", "actions": [self.NODE_ACTION],
                 "resources": ["scheduler.node:*"]},
                {"sid": "d", "effect": "deny", "actions": [self.NODE_ACTION],
                 "resources": [f"scheduler.node:{self.PROTECTED}"]},
            ],
        }]}), encoding="utf-8")
        return PolicyEngine(
            policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: ("ops",)
        )

    def _decide(self, engine, body) -> bool:
        facts = resources.RequestFacts({}, {}, body)
        return all(
            engine.evaluate("user:op", self.NODE_ACTION, resource).allowed
            for resource in resources.resolve_all(self.NODE_ACTION, facts)
        )

    def test_a_comma_target_cannot_smuggle_a_denied_node(self, broad_allow_narrow_deny):
        assert not self._decide(
            broad_allow_narrow_deny, {"target": f"{self.KEPT},{self.PROTECTED}"}
        ), "a comma-joined target reached a node the policy denies"

    def test_it_matches_what_the_equivalent_list_does(self, broad_allow_narrow_deny):
        """The two spellings of one request must get one answer.

        A caller choosing `target` over `nodes` is choosing a wire format, not a privilege
        level, and a gate that treats them differently is one the caller picks.
        """
        as_list = self._decide(
            broad_allow_narrow_deny, {"nodes": [self.KEPT, self.PROTECTED]}
        )
        as_target = self._decide(
            broad_allow_narrow_deny, {"target": f"{self.KEPT},{self.PROTECTED}"}
        )
        assert as_list == as_target is False

    def test_the_permitted_node_alone_still_works(self, broad_allow_narrow_deny):
        """The control: node-scoped drain is the feature this branch exists to enable."""
        assert self._decide(broad_allow_narrow_deny, {"target": self.KEPT})

    def test_each_node_is_judged_separately(self):
        facts = resources.RequestFacts({}, {}, {"target": f"{self.KEPT},{self.PROTECTED}"})
        assert resources.resolve_all(self.NODE_ACTION, facts) == (
            f"scheduler.node:{self.KEPT}",
            f"scheduler.node:{self.PROTECTED}",
        )

    def test_surrounding_whitespace_does_not_hide_a_node(self):
        """`scontrol` output and hand-written lists both carry spaces after the comma."""
        facts = resources.RequestFacts({}, {}, {"target": f" {self.KEPT} , {self.PROTECTED} "})
        assert resources.resolve_all(self.NODE_ACTION, facts) == (
            f"scheduler.node:{self.KEPT}",
            f"scheduler.node:{self.PROTECTED}",
        )

    def test_a_bracket_range_still_fails_closed(self):
        """Unchanged, and worth pinning: the id grammar refuses the wildcard characters."""
        facts = resources.RequestFacts({}, {}, {"target": "ink50-dh01-h18-[01-18]"})
        with pytest.raises(resources.Unresolvable):
            resources.resolve_all(self.NODE_ACTION, facts)

    def test_a_single_target_is_not_split(self):
        facts = resources.RequestFacts({}, {}, {"target": self.KEPT})
        assert resources.resolve_all(self.NODE_ACTION, facts) == (
            f"scheduler.node:{self.KEPT}",
        )
