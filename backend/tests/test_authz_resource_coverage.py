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

import pytest
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


class TestEveryDeclaredReadResolves:
    """Every read route resolves, and to the *right kind* of resource (AC-6b).

    Added after an ad-hoc check found **32 declared read routes that could not resolve at all** —
    they would have answered 403 to a caller holding a perfectly good grant. Two different causes,
    and the second is the one worth a build gate:

    - `catalog.platform:read` looked for `id`, while the route's parameter is `doc_id`; likewise
      `workflow:read` against `/api/workflow-runs/<run_id>`. A plain typo, invisible until someone
      tried to use a per-document grant.
    - Many reads take their target from a query parameter, and some have no target at all
      (`/api/metrics/health`). Those now fall back to the collection.

    **The fallback is what makes a gate necessary.** It fixes the 403, but it also means a
    forgotten key no longer fails loudly: the read silently resolves to `<type>:_all` instead of
    `<type>:<id>`, so per-instance grants stop working and nothing complains. So this test does not
    merely assert "it resolves" — it pins *which* routes resolve to an instance, and a route that
    stops discriminating shows up as a failure here.
    """

    def read_entries(self):
        import audit.actions as A

        rows = json.loads(SNAPSHOT.read_text())
        rows = rows if isinstance(rows, list) else rows.get("actions", [])
        return [r for r in rows if str(r["method"]).upper() in A.READ_METHODS]

    def test_every_read_action_resolves(self):
        """No read may raise `Unresolvable`, with or without an id."""
        failures = []
        for row in self.read_entries():
            try:
                resources.resolve(row["action"], resources.RequestFacts({}, {}, {}))
            except resources.Unresolvable as exc:
                failures.append(f"{row['action']} ({row['endpoint']}): {exc}")

        assert not failures, (
            "these declared reads cannot resolve, so the gate answers 403 even to a caller with a "
            "valid grant:\n  " + "\n  ".join(sorted(failures))
        )

    def test_a_read_with_an_id_resolves_to_that_instance(self):
        """The granularity half. Each of these regressed to a collection read once already.

        Hard-coded rather than derived from the URL map: the point is to state what the intended
        granularity *is*, so that losing it is a diff somebody has to approve. Deriving it from the
        same resolver under test would make the assertion vacuous.
        """
        cases = [
            ("catalog.platform:read", {"doc_id": "gb200-nvl72"}, "catalog.platform:gb200-nvl72"),
            ("catalog.tool:read", {"doc_id": "sysinfo"}, "catalog.tool:sysinfo"),
            ("catalog.project:read", {"doc_id": "mayo"}, "catalog.project:mayo"),
            ("workflow:read", {"run_id": "abc123"}, "workflow:abc123"),
            ("identity.user:read", {"login": "rkoike"}, "identity.user:rkoike"),
            ("report:read", {"report_id": "r-7"}, "report:r-7"),
            ("history:read", {"run_id": "h-1"}, "history:h-1"),
            ("audit.deep:read", {"run_id": "d-1"}, "audit.deep:d-1"),
            ("help:read", {"content_id": "racks"}, "help:racks"),
            ("metrics:read", {"chart_id": "c-1"}, "metrics:c-1"),
            ("validation.activity:read", {"activity_id": "a-1"}, "validation.activity:a-1"),
        ]
        for action, args, expected in cases:
            got = resources.resolve(action, resources.RequestFacts(args, {}, {}))
            assert got == expected, (
                f"{action} with {args} resolved to {got!r}, not {expected!r} — a per-instance "
                "grant no longer applies to this route"
            )

    def test_a_list_verb_is_always_the_collection(self):
        """Even when an id happens to be present: listing is not reading one thing."""
        for row in self.read_entries():
            action = row["action"]
            if not action.endswith(":list"):
                continue
            got = resources.resolve(action, resources.RequestFacts({"id": "x", "rack": "a01"}, {}, {}))
            assert got.endswith(f":{resources.COLLECTION}"), f"{action} -> {got}"

    def test_a_mutation_never_falls_back_to_the_collection(self):
        """The asymmetry that keeps the fallback safe, and the reason it keys off the verb.

        If a mutation could fall back, `POST /api/power-on` with no rack named would authorize
        against `rack.power:_all` — so an operator granted the collection could power-cycle the
        fleet by *omitting* the target. A read resolving to the collection is a description; a
        mutation resolving to it is an unbounded action.

        **`create` is the single exception, and it is exempted here explicitly rather than by
        loosening the assertion.** A create that names its target still resolves to the instance
        (`identity.group:create` with `name: ops` gives `identity.group:ops`); only one that names
        nothing falls back, and for that request the collection is the honest answer, because the id
        is derived server-side. Acting on "the collection" adds one member — it does not act on
        every member, which is what makes it unlike a power-off.
        """
        import audit.actions as A

        rows = json.loads(SNAPSHOT.read_text())
        rows = rows if isinstance(rows, list) else rows.get("actions", [])
        checked = 0
        exempt = 0
        for row in rows:
            if str(row["method"]).upper() not in A.MUTATING_METHODS:
                continue
            action = row["action"]
            if resources.is_singleton(action.split(":", 1)[0]):
                continue  # a singleton has a fixed id and nothing to fall back from
            if (
                action.split(":", 1)[1] in resources.COLLECTION_FALLBACK_VERBS
                or action in resources.COLLECTION_FALLBACK_ACTIONS
            ):
                # Asserted the other way round, so the exemption cannot silently widen: a `create`
                # naming nothing must resolve to the collection and to nothing else.
                assert resources.resolve(
                    action, resources.RequestFacts({}, {}, {})
                ).endswith(f":{resources.COLLECTION}"), action
                exempt += 1
                continue
            checked += 1
            with pytest.raises(resources.Unresolvable):
                resources.resolve(action, resources.RequestFacts({}, {}, {}))

        # A floor, not just non-zero: this guard is the one that would go quiet if the exemption
        # logic ever swallowed the whole set, and a quiet guard reads exactly like a passing one.
        assert checked > 20, f"only {checked} scoped mutations checked; the guard is too narrow"
        assert exempt, "no exemption was exercised; the allowlist is untested"

    def test_every_allowlisted_action_exists(self):
        """An entry naming an action that no longer exists is a dead exemption, and it reads as an
        intentional one to whoever audits the list next."""

        rows = json.loads(SNAPSHOT.read_text())
        rows = rows if isinstance(rows, list) else rows.get("actions", [])
        declared = {r["action"] for r in rows}
        assert resources.COLLECTION_FALLBACK_ACTIONS <= declared, (
            f"not declared anywhere: "
            f"{sorted(resources.COLLECTION_FALLBACK_ACTIONS - declared)}"
        )

    def test_no_destructive_action_is_allowlisted(self):
        """The bar in one assertion. Acting on "the collection" must mean the set as one object,
        never each member in turn — so a power, reset or delete verb may never appear."""
        forbidden = {"off", "on", "cycle", "reset", "delete", "drain", "power"}
        for action in resources.COLLECTION_FALLBACK_ACTIONS:
            verb = action.split(":", 1)[1]
            assert verb not in forbidden, f"{action} acts on every member, not on the set"
