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
"""Build gate: every audited action's resource type has a declared resolver (TC-12, AC-6).

The same shape as `test_throttle_coverage.py`, and for the same reason. Without it, adding an
endpoint in a new namespace ships an action whose resource cannot be named, so at runtime the gate
**refuses** it — correct, fail-closed, and discovered by an operator rather than by us. This turns
that into a build failure that names the missing type.

It fails in both directions. An undeclared type is the incident; a resolver for a type no action
uses is dead weight that reads like coverage, and the next person cannot tell which of the forty
rows still matter.
"""

from __future__ import annotations

import json
from pathlib import Path

from authz import resources

SNAPSHOT = Path(__file__).resolve().parent / "action_snapshot.json"


def action_resource_types() -> set[str]:
    """Resource types the audited actions actually use.

    Read from the action snapshot rather than by importing `app`: this must hold in a tracked-only
    CI checkout, where importing the app pulls in the whole blueprint discovery (`local-vs-ci-tree`).
    The snapshot is itself gated by `test_action_coverage.py`, so it cannot drift from the registry
    without that test failing first.
    """
    rows = json.loads(SNAPSHOT.read_text())
    rows = rows if isinstance(rows, list) else rows.get("actions", [])
    return {str(row["action"]).split(":", 1)[0] for row in rows}


class TestTheDeclarationIsExhaustive:
    def test_every_action_type_has_a_resolver(self):
        missing = sorted(action_resource_types() - resources.declared_types())
        assert not missing, (
            "these resource types have audited actions but no resolver, so the gate would refuse "
            "every request to them:\n  "
            + "\n  ".join(missing)
            + "\n\nDeclare each in authz/resources.py: `_by(...)` naming the request field that "
            "carries the id, or `_singleton` if there is one instance per deployment."
        )

    def test_no_resolver_is_declared_for_a_type_nothing_uses(self):
        """Dead rows read like coverage. Kept strict so the forty rows stay meaningful."""
        unused = sorted(resources.declared_types() - action_resource_types())
        assert not unused, (
            "these resolvers match no audited action and are dead:\n  " + "\n  ".join(unused)
        )


class TestTheSingletonChoicesAreDeliberate:
    """A singleton is a *decision that instances need no separate governance*, so the ones that
    obviously do are worth pinning against a careless edit."""

    def test_rack_scoped_types_are_not_singletons(self):
        """The whole product governs racks separately. A `rack.power` collapsed to a singleton
        would make "may power-cycle rack 7" mean "may power-cycle every rack" — the single most
        dangerous possible mistake in this file, and a one-word edit away.
        """
        for resource_type in sorted(action_resource_types()):
            if resource_type.startswith(("rack", "fabric", "diag", "bringup", "validation")):
                assert not resources.is_singleton(resource_type), (
                    f"{resource_type} is a singleton, so a policy naming one rack would grant "
                    "every rack"
                )

    def test_identity_types_are_not_singletons(self):
        """"May delete user bob" must not read as "may delete any user"."""
        assert not resources.is_singleton("identity.user")
        assert not resources.is_singleton("identity.group")
