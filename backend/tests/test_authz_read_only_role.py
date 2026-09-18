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
"""A read-only role must be expressible in one clause (FR-10, AC-11).

**It was the one role an operator could not write**, which is the opposite of what the phase is for.
Three constraints combined to forbid it: the action grammar required a literal type, so `*:read` was
rejected; a bare `*` is refused in an allow, correctly; and enumerating the read surface is 50 types
× 2 verbs = 100 patterns against a cap of 64. The only thing left that *did* validate was `["*"]` —
a blanket grant — so the syntax pushed operators toward the widest possible policy while the
narrowest was unavailable.

`*:read` and `*:list` are now valid, and `*:*` is not: the verb must be concrete, so a type wildcard
can never grant a mutation. That distinction is the whole safety argument, and the tests below
assert it from both directions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit.actions as A
from authz.policy import PolicyEngine
from authz.schema import PolicyFile

#: The whole role. Two patterns, one statement — the thing that was impossible.
READ_ONLY = {
    "id": "read-only",
    "statements": [{
        "sid": "reads",
        "effect": "allow",
        "actions": ["*:read", "*:list"],
        "resources": ["*"],
    }],
}


@pytest.fixture
def engine(tmp_path):
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(json.dumps({"policies": [READ_ONLY]}), encoding="utf-8")
    return PolicyEngine(
        policy_file, group_lookup=lambda _p: (), attachment_lookup=lambda _s: ("read-only",)
    )


class TestOneClauseIsEnough:
    def test_the_document_validates(self):
        """The first hurdle: it used to fail the action grammar before any evaluation happened."""
        PolicyFile.model_validate({"policies": [READ_ONLY]})

    def test_it_permits_a_read(self, engine):
        assert engine.evaluate("user:viewer", "rack:read", "rack:p17-r01").allowed

    def test_it_permits_a_collection_listing(self, engine):
        assert engine.evaluate("user:viewer", "rack:list", "rack:_all").allowed

    def test_it_refuses_a_mutation(self, engine):
        """The property that makes a type wildcard safe: the verb is pinned, so no `*:read` grant
        can ever reach a power-off."""
        assert not engine.evaluate("user:viewer", "rack.power:off", "rack.power:p17-r01").allowed

    @pytest.mark.parametrize("action", [
        "rack.power:off", "identity.user:delete", "policy:attach",
        "validation:run", "workflow:save", "catalog.platform:update",
    ])
    def test_it_refuses_every_dangerous_verb(self, engine, action):
        """Parametrized over the actions an operator would most regret granting by accident."""
        resource = f"{action.split(':')[0]}:anything"
        assert not engine.evaluate("user:viewer", action, resource).allowed

    def test_it_covers_the_whole_declared_read_surface(self, engine):
        """The claim that matters for adoption: every read the product declares is permitted by
        those two patterns, so nobody has to enumerate and nobody drifts out of date.

        Driven from the live registry rather than a list, so a newly declared read joins this
        assertion automatically.
        """
        reads = [
            action for action in A.registry().actions()
            if action.split(":", 1)[1] in ("read", "list")
        ]
        assert reads, "no read actions in the registry; this asserts nothing"

        refused = [
            action for action in reads
            if not engine.evaluate(
                "user:viewer", action, f"{action.split(':')[0]}:x"
            ).allowed
        ]
        assert not refused, f"a read-only role did not cover: {sorted(set(refused))[:10]}"


class TestTheWildcardCannotWiden:
    def test_a_bare_star_is_still_refused_in_an_allow(self):
        """Unchanged, and the reason the type wildcard had to be added instead of relaxing this."""
        with pytest.raises(ValueError, match="bare '\\*'"):
            PolicyFile.model_validate({"policies": [{
                "id": "everything",
                "statements": [{
                    "sid": "s", "effect": "allow", "actions": ["*"], "resources": ["*"],
                }],
            }]})

    def test_star_colon_star_is_refused(self):
        """A bare star spelled differently. Refused by the *pattern*, which is why pydantic and the
        exported JSON Schema agree here without the bridge the bare-`*` rule needed."""
        with pytest.raises(ValueError):
            PolicyFile.model_validate({"policies": [{
                "id": "everything",
                "statements": [{
                    "sid": "s", "effect": "allow", "actions": ["*:*"], "resources": ["*"],
                }],
            }]})

    def test_a_deny_may_still_use_a_bare_star(self):
        """"Nobody does anything" narrows access and is occasionally necessary."""
        PolicyFile.model_validate({"policies": [{
            "id": "lockdown",
            "statements": [{
                "sid": "s", "effect": "deny", "actions": ["*"], "resources": ["*"],
            }],
        }]})

    def test_a_deny_on_a_read_family_still_overrides(self, tmp_path):
        """Composability check: the role is only useful if a narrower deny can carve out of it —
        an operator who may read everything except the audit trail, say."""
        policy_file = tmp_path / "policies.json"
        policy_file.write_text(json.dumps({"policies": [
            READ_ONLY,
            {"id": "no-identity", "statements": [{
                "sid": "d", "effect": "deny",
                "actions": ["identity.user:read"], "resources": ["*"],
            }]},
        ]}), encoding="utf-8")
        engine = PolicyEngine(
            policy_file, group_lookup=lambda _p: (),
            attachment_lookup=lambda _s: ("read-only", "no-identity"),
        )

        assert engine.evaluate("user:viewer", "rack:read", "rack:p17-r01").allowed
        assert not engine.evaluate("user:viewer", "identity.user:read", "identity.user:bob").allowed


class TestTheEditorAgrees:
    def test_the_served_schema_accepts_the_role(self):
        """The Monaco editor validates against the exported schema, so a document the server accepts
        and the editor rejects would make the feature unusable in the UI even though it works."""
        import jsonschema
        from authz.schema import document_json_schema

        jsonschema.validate(READ_ONLY, document_json_schema())

    def test_the_served_schema_refuses_star_colon_star(self):
        import jsonschema
        from authz.schema import document_json_schema

        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({
                "id": "everything",
                "statements": [{
                    "sid": "s", "effect": "allow", "actions": ["*:*"], "resources": ["*"],
                }],
            }, document_json_schema())
