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
"""Three Phase 3 properties that were asserted in name only.

An acceptance audit found each of these passing while providing no assurance, which is worse than a
missing test: a green tick invites the next reader to trust the property.

# **The fallback verb set's contents were never asserted.** `COLLECTION_FALLBACK_VERBS` is what
  permits a mutation to resolve to `<type>:_all`, and the suite only ever *read* it as an exemption.
  Adding `update` or `reset` to it would have passed everything.
# **The rack-grant test passed for the wrong reason.** It asserted a refusal against
  `node:a01-h15-15` — a resource type that is not declared at all — so the refusal came from nothing
  matching that string, not from rack→node expansion being prevented. The real node types are
  `scheduler.node` and `report.node`, which it never exercised.
# **The enforcing→permissive transition was untested** — and, it turns out, already blocked. The
  audit read it as "deleting the policy file silently converts the product to permissive". That is
  true of a bare import and false of a *served* deployment, because `startup_gate` lists an empty
  policy set as a fault and refuses to start. The control existed; the test did not.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit.actions as audit_actions
from authz import resources
from authz.policy import PolicyEngine

ALICE = "user:alice"


def _engine(tmp_path: Path, documents: list[dict], attach: tuple[str, ...]) -> PolicyEngine:
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(json.dumps({"policies": documents}), encoding="utf-8")
    return PolicyEngine(
        policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: attach
    )


class TestTheFallbackVerbSetIsPinned:
    """`COLLECTION_FALLBACK_VERBS` decides which mutations may act on `<type>:_all`, so its
    membership is a security decision and belongs in an assertion rather than in a comment."""

    def test_the_set_is_exactly_these_three(self):
        """Named exhaustively, so *adding* a verb fails here and has to be argued for.

        The previous coverage read the set to decide which actions to exempt, which means it agreed
        with whatever the set contained — including a set someone had widened.
        """
        assert resources.COLLECTION_FALLBACK_VERBS == frozenset({"read", "list", "create"}), (
            "the fallback verb set changed. Each member must be a verb whose target genuinely IS "
            "the collection: a read describes the set, and a create adds one member to it. A verb "
            "that acts on every member (off, reset, delete, drain) must never be here — see "
            "COLLECTION_FALLBACK_ACTIONS for the exact-action allowlist instead."
        )

    def test_no_member_is_a_destructive_verb(self):
        """The bar restated as a property, so a future addition is checked against it even if
        someone updates the exhaustive list above without thinking."""
        forbidden = {"off", "on", "cycle", "reset", "delete", "drain", "power", "update", "save"}
        assert not (resources.COLLECTION_FALLBACK_VERBS & forbidden)

    def test_every_create_action_is_genuinely_collection_level(self):
        """`create` earns its place only while every `:create` action really has no instance to
        name. Driven from the live registry, so a new one joins this check automatically."""
        creates = [a for a in audit_actions.registry().actions() if a.endswith(":create")]
        assert creates, "no create actions in the registry; this asserts nothing"

        for action in creates:
            resolved = resources.resolve_all(action, resources.RequestFacts({}, {}, {}))
            assert resolved == (f"{action.split(':')[0]}:{resources.COLLECTION}",), (
                f"{action} resolved to {resolved} with nothing named. If a create can identify its "
                f"target, it should resolve to the instance; if it cannot, the collection is right."
            )


class TestARackGrantDoesNotCoverItsNodes:
    """FR-6 / AC-7, against the resource types the product actually declares.

    The original test used `node:a01-h15-15`, which is not a declared type — so it asserted that a
    string nothing could ever produce is refused. A resolver that expanded a rack through the naming
    templates would emit `scheduler.node:…` or `report.node:…` and sail past it.
    """

    RACK_ONLY: ClassVar[dict[str, object]] = {
        "id": "rack-a01",
        "statements": [{
            "sid": "rack", "effect": "allow",
            "actions": ["rack.power:*"], "resources": ["rack.power:a01"],
        }],
    }

    @pytest.fixture
    def engine(self, tmp_path):
        return _engine(tmp_path, [self.RACK_ONLY], ("rack-a01",))

    def test_the_rack_itself_is_permitted(self, engine):
        """The control: without it every refusal below could be an engine that refuses everything."""
        assert engine.evaluate(ALICE, "rack.power:cycle", "rack.power:a01").allowed

    @pytest.mark.parametrize("node_type", ["scheduler.node", "report.node"])
    def test_a_declared_node_type_is_not_covered(self, engine, node_type):
        """The real types, so the assertion is about a resource the product can actually produce."""
        assert not engine.evaluate(
            ALICE, "rack.power:cycle", f"{node_type}:a01-h15-15"
        ).allowed

    def test_the_node_types_used_here_are_declared(self):
        """Guards the guard. If `scheduler.node` were renamed, the two cases above would silently revert
        to asserting against strings nothing produces — which is the defect this file exists for."""
        declared = resources.declared_types()
        for node_type in ("scheduler.node", "report.node"):
            assert node_type in declared, f"{node_type} is no longer a declared resource type"

    def test_no_resolver_turns_a_rack_request_into_a_node_resource(self):
        """The stronger form: assert at the *resolver*, which is where an expansion would live.

        The engine test above shows a node resource is not matched by a rack grant. This shows the
        resolver never produces one from a rack-scoped request in the first place, so the two
        together cover both halves of the property.
        """
        # **Both paths, and this is the whole point of the case.** `resolve_all` short-circuits to
        # `resolve()` when one identifier is named, so a single-rack request never reaches the
        # multi-target branch. The first version of this test used only `{"rack": "a01"}` and a
        # mutation that made the resolver expand racks into `scheduler.node:…` passed all 11 tests —
        # the same "passes for the wrong reason" flaw this file was written to fix, reproduced.
        single = resources.RequestFacts({}, {}, {"rack": "a01"})
        several = resources.RequestFacts({}, {}, {"racks": ["a01", "a02"]})

        for facts in (single, several):
            for action in ("rack.power:off", "rack.power:on", "rack.power:cycle"):
                resolved = resources.resolve_all(action, facts)
                assert resolved, f"{action} resolved to nothing"
                for entry in resolved:
                    assert entry.startswith("rack.power:"), (
                        f"{action} produced {entry} — a rack has been expanded into node "
                        f"resources, which makes a rack grant cover its nodes (FR-6, AC-7)"
                    )

    def test_the_two_resolution_paths_are_both_exercised(self):
        """Guards the case above by asserting the paths differ, so a refactor that collapsed them
        cannot quietly halve the coverage."""
        single = resources.resolve_all(
            "rack.power:off", resources.RequestFacts({}, {}, {"rack": "a01"})
        )
        several = resources.resolve_all(
            "rack.power:off", resources.RequestFacts({}, {}, {"racks": ["a01", "a02"]})
        )
        assert len(single) == 1 and len(several) == 2, (
            "the single- and multi-target paths no longer differ, so one of them is untested"
        )


class TestAServedDeploymentCannotSilentlyGoPermissive:
    """The audit's concern, corrected: the control exists and was simply untested.

    Deleting `var/store/policies.json` does make `install_default()` choose `AllowAllEngine` — that
    is real and is why the concern was raised. What the audit missed is that a deployment cannot
    *serve* in that state: `startup_gate` lists an empty policy set as a fault, so the process exits
    78 rather than coming up permissive. Both halves are asserted here, because the first without
    the second is the frightening reading and the second without the first hides why it matters.
    """

    @pytest.fixture(autouse=True)
    def _isolate_catalog(self, tmp_path, monkeypatch):
        """`startup_gate.prepare()` resolves the active cluster, which seeds the catalog — a
        documented side effect of the same call `cli.py check` makes.

        Left unisolated it seeds the *developer's* `var/store/catalog.json` and leaves the repo
        cached in module state, which gave eight order-dependent failures in
        `test_config_registry.py` when this file ran first: those tests assert that a fresh
        deployment needs no connection settings, and a catalog with an active SSH cluster makes
        `RACK_SSH_HOST` required. Same trap documented in `local-vs-ci-tree.mdc` — a pass here
        depended on what was lying around the tree.

        `prepare()` also **writes the TLS paths into `os.environ` directly** when it generates a
        certificate, so those two names have to be handed to `monkeypatch` up front — it records
        the pre-test value and restores it on teardown even though the product overwrites it in
        between. Without that, `RACK_WEB_TLS_CERT` survives the test and makes
        `RACK_WEB_TLS_KEY` conditionally required, which is the actual mechanism behind those
        eight failures.
        """
        import catalog.store as catalog_store

        monkeypatch.setenv("NVIS_CATALOG_FILE", str(tmp_path / "catalog.json"))
        for leaked in ("RACK_WEB_TLS_CERT", "RACK_WEB_TLS_KEY"):
            monkeypatch.setenv(leaked, os.environ.get(leaked, ""))
        catalog_store.set_repo(None)
        yield
        catalog_store.set_repo(None)

    def test_an_empty_policy_set_makes_the_engine_permissive(self, tmp_path, monkeypatch):
        """The mechanism the concern is about, stated plainly rather than left implicit."""
        import authz

        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "absent.json"))
        captured = authz.current_engine()
        try:
            authz.install_default()
            assert authz.engine_name() == "allow-all"
        finally:
            authz.set_engine(captured)

    def test_but_the_startup_gate_refuses_to_serve_in_that_state(self, tmp_path, monkeypatch):
        """The control. This is what makes the permissive engine unreachable on a real deployment.

        `bootstrap=False`, because bootstrap would helpfully *create* a policy — which is correct
        for a first boot and would hide the case under test here: an existing deployment whose
        policy set has gone away.
        """
        import startup_gate

        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "policies.json"))
        monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
        monkeypatch.setenv("RACK_WEB_TLS_DIR", str(tmp_path / "tls"))

        # Provision the deployment, then take the policy set away.
        startup_gate.prepare(exit_on_fault=False)
        policy_file = tmp_path / "policies.json"
        assert policy_file.is_file(), "bootstrap should have written a policy set"
        policy_file.unlink()

        faults = startup_gate.prepare(bootstrap=False, exit_on_fault=False)
        assert any("policy" in fault.what.lower() for fault in faults), (
            f"a deployment with no policy set was allowed to serve; faults were "
            f"{[f.what for f in faults]}"
        )

    def test_the_refusal_says_what_it_would_have_meant(self, tmp_path, monkeypatch):
        """A gate that refuses without explaining is one operators learn to route around, and the
        thing to explain here is the consequence: authorization off, not merely a missing file."""
        import startup_gate

        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "policies.json"))
        monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
        monkeypatch.setenv("RACK_WEB_TLS_DIR", str(tmp_path / "tls"))

        startup_gate.prepare(exit_on_fault=False)
        (tmp_path / "policies.json").unlink()

        faults = startup_gate.prepare(bootstrap=False, exit_on_fault=False)
        policy_fault = next(f for f in faults if "policy" in f.what.lower())
        assert policy_fault.fix, "a fault with no fix is a gate operators disable"
        assert "allow-all" in policy_fault.why or "authorization" in policy_fault.why.lower()
