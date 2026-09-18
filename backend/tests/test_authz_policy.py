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
"""The policy engine — Phase 3, attachment model (TC-10..TC-15, TC-24, TC-26).

**Almost every test here is about a refusal, because the failure modes are asymmetric.** Granting
too little is an annoyance somebody reports within the hour. Granting too much is invisible until it
matters. So the load-bearing cases are default-deny, deny-overrides, and what happens when the
policy file is broken.

Rewritten from the inline-principals version. Policies no longer name principals (FR-7a), so every
fixture attaches instead — and `TestOnlyAttachedPoliciesAreEvaluated` asserts the property that
change was made for, by **counting statement evaluations** rather than timing them. A timing
assertion would pass on a broken implementation on a fast machine.
"""

from __future__ import annotations

import json

import pytest
from authz import policy as P

ALICE, BOB, DAVE = "user:alice", "user:bob", "user:dave"

POWER = {
    "id": "rack-operators",
    "statements": [
        {"sid": "power", "effect": "allow", "actions": ["rack.power:*"],
         "resources": ["rack.power:*"]},
    ],
}
PROTECT = {
    "id": "protect-critical",
    "statements": [
        {"sid": "no-crit", "effect": "deny", "actions": ["rack.power:*"],
         "resources": ["rack.power:critical-*"]},
    ],
}
READONLY = {
    "id": "read-only",
    "statements": [{"effect": "allow", "actions": ["rack.status:read"], "resources": ["*"]}],
}


def write(tmp_path, documents) -> object:
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({"version": 1, "policies": documents}))
    return path


def engine(tmp_path, documents, *, groups=None, attach=None):
    groups, attach = groups or {}, attach or {}
    return P.PolicyEngine(
        write(tmp_path, documents),
        group_lookup=lambda p: groups.get(p, ()),
        attachment_lookup=lambda s: attach.get(s, ()),
    )


#: A grant scoped to ONE rack, which is the shape AC-1 is about. `POWER` above grants
#: `rack.power:*` and so cannot express the criterion at all.
ONE_RACK = {
    "id": "rack-a01-only",
    "statements": [
        {"sid": "just-a01", "effect": "allow", "actions": ["rack.power:*"],
         "resources": ["rack.power:a01"]},
    ],
}


class TestAC1AGrantIsScopedToItsRack:
    """**The criterion the whole phase existed to satisfy**, asserted directly at last.

    Phase 3 opened because the gate passed `request.path` to `authorize()`, so a policy naming
    `resources: ["rack.power:a01"]` matched nothing and every resource-scoped grant silently
    collapsed to default-deny. AC-1 was written with "**This fails today**" against it.

    It has passed for a while — but nothing asserted *this* property. The nearest cases were a
    grant of `rack.power:*` (which cannot express a per-rack scope), a refusal of `cluster:prod`
    (a different resource *type*, which a type-level bug would also refuse), and, incidentally,
    the fixture of an unrelated AC-16a test. So the headline criterion was satisfied by accident
    of other tests' setups: a resolver that dropped the identifier and matched on type alone
    would have kept every one of them green.

    Same action, two racks, one grant. That is the whole criterion and it needs its own test.
    """

    def test_the_granted_rack_is_permitted(self, tmp_path):
        e = engine(tmp_path, [ONE_RACK], attach={ALICE: ["rack-a01-only"]})
        assert e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_the_same_action_on_another_rack_is_refused(self, tmp_path):
        """The half that was broken. Identical action, identical policy, different instance."""
        e = engine(tmp_path, [ONE_RACK], attach={ALICE: ["rack-a01-only"]})
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:b02").allowed

    @pytest.mark.parametrize("verb", ["on", "off", "cycle"])
    def test_the_scope_holds_for_every_verb_the_grant_covers(self, tmp_path, verb):
        """`actions: ["rack.power:*"]` covers all three, so a resource bug would show on any of
        them — and a fix that only handled the verb the first test happened to use would not."""
        e = engine(tmp_path, [ONE_RACK], attach={ALICE: ["rack-a01-only"]})
        assert e.evaluate(ALICE, f"rack.power:{verb}", "rack.power:a01").allowed
        assert not e.evaluate(ALICE, f"rack.power:{verb}", "rack.power:b02").allowed

    def test_a_url_path_is_not_a_resource(self, tmp_path):
        """The original defect in its exact form. The gate used to pass `request.path`; if that
        ever regressed, this is what it would look like from the engine's side.
        """
        e = engine(tmp_path, [ONE_RACK], attach={ALICE: ["rack-a01-only"]})
        for path in ("/api/power-on", "/api/power-off", "rack.power:/api/power-on"):
            assert not e.evaluate(ALICE, "rack.power:cycle", path).allowed


class TestDefaultDeny:
    """The property that makes every grant meaningful."""

    def test_no_attachments_means_refused(self, tmp_path):
        e = engine(tmp_path, [POWER])
        assert not e.evaluate(DAVE, "rack.power:cycle", "rack.power:a01").allowed

    def test_an_empty_policy_set_refuses_everything(self, tmp_path):
        e = engine(tmp_path, [], attach={ALICE: ["anything"]})
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_a_missing_policy_file_refuses(self, tmp_path):
        """The state a fresh deployment is in. It must not mean "allow"."""
        e = P.PolicyEngine(tmp_path / "absent.json",
                           group_lookup=lambda p: (), attachment_lookup=lambda s: ("x",))
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_an_unmatched_action_is_refused(self, tmp_path):
        e = engine(tmp_path, [POWER], attach={ALICE: ["rack-operators"]})
        assert not e.evaluate(ALICE, "identity.user:delete", "identity.user:bob").allowed

    def test_an_unmatched_resource_is_refused(self, tmp_path):
        e = engine(tmp_path, [POWER], attach={ALICE: ["rack-operators"]})
        assert not e.evaluate(ALICE, "rack.power:cycle", "cluster:prod").allowed

    def test_the_refusal_does_not_confirm_the_resource_exists(self, tmp_path):
        """NFR-3. A reason naming the resource is an existence oracle for anyone probing."""
        e = engine(tmp_path, [])
        reason = e.evaluate(ALICE, "rack.power:cycle", "rack.power:secret-lab-01").reason
        assert "secret-lab" not in reason
        assert "rack.power:cycle" in reason


class TestAttachment:
    """FR-7a, FR-7b."""

    def test_a_direct_attachment_grants(self, tmp_path):
        e = engine(tmp_path, [READONLY], attach={BOB: ["read-only"]})
        assert e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed

    def test_a_group_attachment_reaches_members(self, tmp_path):
        e = engine(tmp_path, [POWER], groups={ALICE: ["ops"]},
                   attach={"group:ops": ["rack-operators"]})
        assert e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_a_non_member_gets_nothing_from_the_group_grant(self, tmp_path):
        e = engine(tmp_path, [POWER], groups={ALICE: ["ops"]},
                   attach={"group:ops": ["rack-operators"]})
        assert not e.evaluate(BOB, "rack.power:cycle", "rack.power:a01").allowed

    def test_detaching_takes_effect_on_the_next_request(self, tmp_path):
        """AC-15a. Resolved per request, not per session — so removing a grant does not wait for a
        re-login, which is the difference between revoking access and asking politely."""
        attach = {"group:ops": ["rack-operators"]}
        e = P.PolicyEngine(write(tmp_path, [POWER]),
                           group_lookup=lambda p: ("ops",),
                           attachment_lookup=lambda s: attach.get(s, ()))
        assert e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

        attach["group:ops"] = []
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    def test_a_policy_attached_twice_is_resolved_once(self, tmp_path):
        """Directly and via a group.

        Asserted on the resolved *documents*, not on `statements_examined`. The count version of
        this test was vacuous and a mutation proved it: the allow pass returns on the first match,
        so a duplicated document is never reached and the count reads 1 either way. Deduplication
        is a property of resolution, so it has to be asserted there.
        """
        e = engine(tmp_path, [READONLY], groups={BOB: ["readers"]},
                   attach={BOB: ["read-only"], "group:readers": ["read-only"]})

        assert [d.id for d in e.attached_documents(BOB)] == ["read-only"]
        assert e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed

    def test_an_attachment_to_an_unknown_policy_is_skipped_not_fatal(self, tmp_path):
        """Deleting a document must not make every request fail: the attachment is stale, not the
        request. Skipping yields default-deny, which is the safe direction."""
        e = engine(tmp_path, [READONLY], attach={BOB: ["read-only", "deleted-policy"]})
        assert e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed

    def test_a_lookup_failure_grants_nothing_extra(self, tmp_path):
        """A store outage must reduce permissions, never add them."""
        def boom(_s):
            raise RuntimeError("store down")

        e = P.PolicyEngine(write(tmp_path, [READONLY]),
                           group_lookup=lambda p: (), attachment_lookup=boom)
        assert not e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed

    def test_a_group_lookup_failure_still_honours_direct_attachments(self, tmp_path):
        def boom(_p):
            raise RuntimeError("store down")

        e = P.PolicyEngine(write(tmp_path, [READONLY]),
                           group_lookup=boom, attachment_lookup=lambda s: {BOB: ["read-only"]}.get(s, ()))
        assert e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed


class TestOnlyAttachedPoliciesAreEvaluated:
    """TC-13, AC-15c — the property the attachment model exists for.

    Counted, not timed. `O(g + A·S)` versus `O(P·S·(1+g)·p̄)` is a claim about *which* statements
    are considered, and a timing assertion would pass on a broken implementation given a fast
    enough machine.
    """

    def test_unattached_policies_are_never_examined(self, tmp_path):
        noise = [
            {"id": f"noise-{i}",
             "statements": [{"effect": "allow", "actions": [f"svc{i}.act:*"], "resources": ["*"]}]}
            for i in range(500)
        ]
        e = engine(tmp_path, [*noise, READONLY], attach={BOB: ["read-only"]})

        e.statements_examined = 0
        assert e.evaluate(BOB, "rack.status:read", "rack.power:a01").allowed
        assert e.statements_examined == 1, (
            "evaluation touched statements outside the attached policy, so cost grows with the "
            "whole corpus rather than with what is attached"
        )

    def test_a_principal_with_nothing_attached_examines_nothing(self, tmp_path):
        e = engine(tmp_path, [POWER, PROTECT, READONLY])
        e.statements_examined = 0
        e.evaluate(DAVE, "rack.power:cycle", "rack.power:a01")
        assert e.statements_examined == 0


class TestDenyOverridesAllow:
    """FR-7. An explicit prohibition must not be outvotable by a grant written later."""

    def test_a_deny_beats_a_matching_allow(self, tmp_path):
        e = engine(tmp_path, [POWER, PROTECT], groups={ALICE: ["ops"]},
                   attach={"group:ops": ["rack-operators", "protect-critical"]})
        assert e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:critical-7").allowed

    @pytest.mark.parametrize("order", [[POWER, PROTECT], [PROTECT, POWER]])
    def test_file_order_does_not_decide_it(self, tmp_path, order):
        """A policy's protection must not depend on somebody appending below it rather than above."""
        e = engine(tmp_path, order, attach={ALICE: ["rack-operators", "protect-critical"]})
        assert not e.evaluate(ALICE, "rack.power:cycle", "rack.power:critical-7").allowed

    def test_a_deny_in_one_policy_beats_an_allow_in_another(self, tmp_path):
        """Across documents, not just within one — the case an operator actually writes."""
        e = engine(tmp_path, [POWER, PROTECT], attach={ALICE: ["rack-operators", "protect-critical"]})
        assert not e.evaluate(ALICE, "rack.power:on", "rack.power:critical-1").allowed

    def test_the_reason_names_the_rule_that_decided(self, tmp_path):
        """"Which rule refused me" is the first question after a denial, and a document with eight
        statements cannot answer it."""
        e = engine(tmp_path, [PROTECT], attach={ALICE: ["protect-critical"]})
        reason = e.evaluate(ALICE, "rack.power:on", "rack.power:critical-1").reason
        assert "protect-critical/no-crit" in reason


class TestRackGrantsDoNotCoverNodes:
    """FR-6, AC-7 — feature-lead decision 2026-09-02."""

    def test_a_rack_grant_does_not_permit_a_node(self, tmp_path):
        """Expanding rack grants through the naming templates would make the meaning of a grant
        depend on per-cluster naming config, so one policy file would confer different access at
        Mayo and Boston, and a naming change would silently widen it."""
        rack_only = {
            "id": "rack-a01",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:a01"]}],
        }
        e = engine(tmp_path, [rack_only], attach={ALICE: ["rack-a01"]})

        assert e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed
        # `scheduler.node`, not `node`: the latter is not a declared resource type, so asserting a
        # refusal against it proved only that a string nothing can produce is unmatched. A resolver
        # that expanded a rack through the naming templates would emit `scheduler.node:…` and sail past
        # the original form of this line. `test_authz_verification_gaps.py` covers both node types
        # and the resolver side as well.
        assert not e.evaluate(ALICE, "rack.power:cycle", "scheduler.node:a01-h15-15").allowed


class TestCaseSensitivity:
    def test_matching_is_case_sensitive(self, tmp_path):
        """A behavioural pin, and honestly a weak one — it cannot fail on our platforms.

        `fnmatch` normalises through `os.path.normcase`, which lowercases only on Windows, so
        `fnmatch` and `fnmatchcase` are indistinguishable on macOS and Linux. A mutation swapping
        them was **not** caught here and could not be. Kept because the asserted behaviour is the
        one we want, and noted so nobody reads a passing run as proof the explicit call is load-
        bearing.
        """
        e = engine(tmp_path, [POWER], attach={ALICE: ["rack-operators"]})
        assert not e.evaluate(ALICE, "RACK.POWER:CYCLE", "rack.power:a01").allowed


class TestABrokenPolicyFileDeniesLoudly:
    """TC-24. The failure you would least want to be quiet."""

    def test_unparseable_json_denies_everything(self, tmp_path):
        path = tmp_path / "policies.json"
        path.write_text("{ not json")
        e = P.PolicyEngine(path, group_lookup=lambda p: (), attachment_lookup=lambda s: ("x",))

        decision = e.evaluate(ALICE, "rack.power:cycle", "rack.power:a01")
        assert not decision.allowed
        assert "could not be loaded" in decision.reason

    def test_one_invalid_document_fails_the_whole_load(self, tmp_path):
        """Not skipped. Loading the valid documents and dropping the rest means a typo in a deny
        silently widens access, with a successful-looking load and nothing in the log."""
        with pytest.raises(P.PolicyError):
            P.load(write(tmp_path, [POWER, {"id": "typo", "statements": [
                {"effect": "alow", "actions": ["a:b"]}]}]))

    def test_a_document_with_inline_principals_fails_the_load(self, tmp_path):
        """AC-15b. The retired model must not half-work."""
        with pytest.raises(P.PolicyError):
            P.load(write(tmp_path, [dict(POWER, principals=["user:alice"])]))

    def test_evaluate_never_raises(self, tmp_path):
        """The seam's contract. An engine that raises turns a policy typo into a 500 on every route
        at once — both an outage and a worse diagnostic than a refusal naming the rule."""
        path = tmp_path / "policies.json"
        path.write_text("[[[")
        e = P.PolicyEngine(path, group_lookup=lambda p: (), attachment_lookup=lambda s: ())
        assert e.evaluate(ALICE, "x:y", "z").allowed is False


class TestDeterminism:
    """NFR-5, AC-19. A verdict that varies cannot be explained after the fact, and "why was I
    refused" is the question this phase must answer."""

    def test_the_same_inputs_give_the_same_verdict(self, tmp_path):
        e = engine(tmp_path, [POWER, PROTECT], attach={ALICE: ["rack-operators", "protect-critical"]})
        verdicts = {e.evaluate(ALICE, "rack.power:cycle", "rack.power:critical-7").allowed
                    for _ in range(1000)}
        assert verdicts == {False}

    def test_a_reload_does_not_change_the_verdict(self, tmp_path):
        import os

        path = write(tmp_path, [POWER])
        e = P.PolicyEngine(path, group_lookup=lambda p: (),
                           attachment_lookup=lambda s: ("rack-operators",))
        first = e.evaluate(ALICE, "rack.power:on", "rack.power:a01").allowed
        os.utime(path, (0, 0))  # force a distinct mtime rather than racing the clock
        assert e.evaluate(ALICE, "rack.power:on", "rack.power:a01").allowed is first


class TestReloadAndCaching:
    def test_editing_the_file_takes_effect_without_a_restart(self, tmp_path):
        import os

        path = write(tmp_path, [])
        e = P.PolicyEngine(path, group_lookup=lambda p: (),
                           attachment_lookup=lambda s: ("rack-operators",))
        assert not e.evaluate(ALICE, "rack.power:on", "rack.power:a01").allowed

        path.write_text(json.dumps({"policies": [POWER]}))
        os.utime(path, (0, 0))
        assert e.evaluate(ALICE, "rack.power:on", "rack.power:a01").allowed

    def test_reading_policies_performs_no_write(self, tmp_path):
        """NFR-2. A read that writes changes the mtime this cache keys on, so the cache would
        invalidate itself on every read — exactly the Phase 2 revocation-cache bug."""
        path = write(tmp_path, [POWER])
        e = P.PolicyEngine(path, group_lookup=lambda p: (),
                           attachment_lookup=lambda s: ("rack-operators",))
        before = path.stat().st_mtime_ns

        for _ in range(5):
            e.evaluate(ALICE, "rack.power:on", "rack.power:a01")

        assert path.stat().st_mtime_ns == before


class TestTheEngineStaysReusable:
    """FR-7c, AC-15d, TC-26 — the constraint that lets this engine serve another product."""

    def test_no_authz_module_imports_identity_at_module_level(self):
        """An import-graph assertion, the same shape as `test_no_deferred_app_lookups.py`.

        A coupling added by accident is exactly what a future consumer would have to unpick, and it
        would not show up in any behavioural test.
        """
        import ast
        from pathlib import Path

        authz_dir = Path(__file__).resolve().parent.parent / "authz"
        offenders: list[str] = []
        for source in sorted(authz_dir.glob("*.py")):
            tree = ast.parse(source.read_text())
            for node in tree.body:  # module level ONLY, not nested in a function
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    if name.split(".")[0] == "identity":
                        offenders.append(f"{source.name}: {name}")

        assert not offenders, (
            "authz/ must not import identity/ at module level — the adapters are lazy so another "
            "product can supply its own lookups:\n  " + "\n  ".join(offenders)
        )

    def test_both_lookups_are_injectable(self, tmp_path):
        """Constructed with neither default touched: no identity store, no environment."""
        e = P.PolicyEngine(write(tmp_path, [READONLY]),
                           group_lookup=lambda p: ("readers",),
                           attachment_lookup=lambda s: ("read-only",) if s == "group:readers" else ())
        assert e.evaluate("someone-elses-principal-format", "rack.status:read", "x:y").allowed
