# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A policy written against a retired action name keeps working.

`slurm.*` became `scheduler.*` when the HTTP surface stopped being Slurm-specific. A
deployment's `policies.json` is a contract we do not control -- operators edit it, it exists
at customer sites, and nothing upgrades it when we rename something -- so the engine accepts
the old spelling.

The half that is easy to get wrong is the **deny** path. Aliasing only allows would leave a
`deny slurm.node:drain` silently not applying, which no test asking "can I still drain?"
would notice, and which turns a deliberate refusal into a permit on upgrade.
"""
from __future__ import annotations

import json

import pytest
from authz.policy import PolicyEngine, _action_spellings

ALICE = "user:alice"
POLICY_ID = "scheduler-operators"


def _engine(tmp_path, statements):
    """An engine with *statements* attached to ALICE.

    Attachment is a lookup callable rather than a field on the document -- the same shape the
    other authz tests use, so this exercises the real loader rather than a stub of it.
    """
    path = tmp_path / "policies.json"
    path.write_text(
        json.dumps(
            {"version": 1, "policies": [{"id": POLICY_ID, "statements": statements}]}
        )
    )
    return PolicyEngine(
        path,
        group_lookup=lambda principal: (),
        attachment_lookup=lambda subject: (POLICY_ID,),
    )


class TestTheSpellingHelper:
    def test_a_renamed_action_offers_both_spellings(self):
        assert _action_spellings("scheduler.node:drain") == (
            "scheduler.node:drain",
            "slurm.node:drain",
        )

    def test_an_unrelated_action_offers_only_itself(self):
        assert _action_spellings("rack.power:on") == ("rack.power:on",)

    def test_the_legacy_spelling_is_not_expanded_further(self):
        """Only the canonical name maps back. Otherwise a policy could be written against a
        name the product no longer emits and appear to work forever."""
        assert _action_spellings("slurm.node:drain") == ("slurm.node:drain",)


class TestAllowUnderTheOldName:
    def test_a_legacy_grant_still_permits_the_renamed_action(self, tmp_path):
        engine = _engine(
            tmp_path, [{"effect": "allow", "actions": ["slurm.node:drain"], "resources": ["*"]}]
        )
        assert engine.evaluate(ALICE, "scheduler.node:drain", "scheduler.node:n01").allowed

    def test_a_canonical_grant_permits_the_canonical_action(self, tmp_path):
        engine = _engine(
            tmp_path,
            [{"effect": "allow", "actions": ["scheduler.node:drain"], "resources": ["*"]}],
        )
        assert engine.evaluate(ALICE, "scheduler.node:drain", "scheduler.node:n01").allowed

    def test_a_legacy_wildcard_grant_still_covers_the_service(self, tmp_path):
        engine = _engine(
            tmp_path, [{"effect": "allow", "actions": ["slurm.queue:*"], "resources": ["*"]}]
        )
        assert engine.evaluate(ALICE, "scheduler.queue:list", "scheduler.queue:default").allowed

    def test_an_unrelated_grant_still_does_not_permit(self, tmp_path):
        """The alias must widen nothing beyond the rename."""
        engine = _engine(
            tmp_path, [{"effect": "allow", "actions": ["rack.power:on"], "resources": ["*"]}]
        )
        assert not engine.evaluate(ALICE, "scheduler.node:drain", "scheduler.node:n01").allowed


class TestDenyUnderTheOldName:
    """The direction that matters: a refusal must survive the rename."""

    def test_a_legacy_deny_still_refuses_the_renamed_action(self, tmp_path):
        engine = _engine(
            tmp_path,
            [
                {"effect": "allow", "actions": ["scheduler.node:*"], "resources": ["*"]},
                {
                    "sid": "no-draining",
                    "effect": "deny",
                    "actions": ["slurm.node:drain"],
                    "resources": ["*"],
                },
            ],
        )
        decision = engine.evaluate(ALICE, "scheduler.node:drain", "scheduler.node:n01")
        assert not decision.allowed
        assert "no-draining" in decision.reason

    def test_a_legacy_deny_on_one_service_does_not_refuse_another(self, tmp_path):
        engine = _engine(
            tmp_path,
            [
                {"effect": "allow", "actions": ["scheduler.queue:*"], "resources": ["*"]},
                {"effect": "deny", "actions": ["slurm.node:drain"], "resources": ["*"]},
            ],
        )
        assert engine.evaluate(ALICE, "scheduler.queue:list", "scheduler.queue:default").allowed


class TestTheShippedPolicyUsesTheCanonicalName:
    def test_no_retired_action_name_remains_in_the_repo_policy(self):
        """Our own file is migrated. The engine alias is for deployments we cannot edit, not
        an excuse to leave the shipped policy on a retired name."""
        from pathlib import Path

        repo_policy = Path(__file__).resolve().parent.parent.parent / "var" / "store" / "policies.json"
        if not repo_policy.is_file():
            pytest.skip("no repo-local policy file in this checkout")
        assert "slurm." not in repo_policy.read_text()
