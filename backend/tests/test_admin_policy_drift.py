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
"""The administrators policy goes stale as the product gains resource types.

**A real defect, found from the UI on 2026-09-10:** the administrator got `not authorized` on the
newly shipped audit-trail view. The policy enumerates `<type>:*` for every type in
`declared_types()`, and it is written **once**, at first boot, from whatever types existed then.
`ensure_admin_policy` returns early forever afterwards — correctly, because an existing policy set
is a decision it must not overrule. So every resource type added after a deployment was installed
is missing from its policy, and the symptom is a 403 on a feature that just shipped with nothing in
the logs connecting the two. `audit.trail` was simply the first new type since seeding.

**Why the existing test could not catch it, which is the transferable part.**
`test_identity_bootstrap.py::test_it_covers_every_declared_resource_type` compares
`admin_policy_document()` against `declared_types()` — two values computed live, from the same
source, in the same process, and therefore incapable of disagreeing. It is honest about its scope
("on a fresh install") and that is exactly the case that works. **Drift lives in the persisted
artifact, so a test that never reads the stored document cannot see it.** Every case below reads
the stored policy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity import bootstrap


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A deployment whose stored admin policy predates some of today's resource types.

    Built by writing the *generated* document and then removing entries, which is precisely the
    shape a real deployment has: its policy was generated correctly, and the product moved on.
    """
    import authz.store as policy_store
    from authz.resources import declared_types

    policy_file = tmp_path / "policies.json"
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))

    document = bootstrap.admin_policy_document()
    types = sorted(declared_types())
    assert len(types) > 3, "need several declared types for this to mean anything"
    self_dated = types[:-2]  # pretend the last two types are newer than this deployment
    document["statements"][0]["actions"] = sorted(f"{t}:*" for t in self_dated)
    policy_file.write_text(json.dumps({"policies": [document]}), encoding="utf-8")

    policy_store.invalidate() if hasattr(policy_store, "invalidate") else None
    return policy_file, sorted(types[-2:])


class TestTheGapIsDetected:
    def test_it_names_the_types_the_stored_policy_predates(self, seeded):
        _policy_file, newer = seeded
        assert bootstrap.admin_policy_gaps() == newer

    def test_a_complete_policy_reports_nothing(self, tmp_path, monkeypatch):
        policy_file = tmp_path / "policies.json"
        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
        policy_file.write_text(
            json.dumps({"policies": [bootstrap.admin_policy_document()]}), encoding="utf-8"
        )
        assert bootstrap.admin_policy_gaps() == []

    def test_no_policy_at_all_is_not_a_gap(self, tmp_path, monkeypatch):
        """A fresh deployment has no policy yet; `ensure_admin_policy` writes a complete one. A
        gap report there would be noise on every first boot."""
        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "absent.json"))
        assert bootstrap.admin_policy_gaps() == []

    def test_a_bare_wildcard_grant_covers_everything(self, tmp_path, monkeypatch):
        """A policy granting `*` needs no enumeration, so reporting gaps against it would be
        wrong. The schema refuses a bare `*` in an *allow*, so this is defensive rather than
        reachable today — but a future policy shape should not produce phantom gaps."""
        policy_file = tmp_path / "policies.json"
        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
        document = bootstrap.admin_policy_document()
        document["statements"][0]["actions"] = ["*"]
        policy_file.write_text(json.dumps({"policies": [document]}), encoding="utf-8")
        assert bootstrap.admin_policy_gaps() == []


class TestTheGapIsReportedNotSilentlyRepaired:
    def test_ensure_admin_policy_still_refuses_to_touch_an_existing_set(self, seeded):
        """The behaviour that *caused* the defect is correct and stays. An existing policy set is
        an operator decision; a boot that topped it up could not tell a type newer than the policy
        from one deliberately removed, and re-widening a narrowed administrators policy would be
        privilege escalation performed by a housekeeping routine.
        """
        from identity.store import IdentityStore

        policy_file, newer = seeded
        before = policy_file.read_text(encoding="utf-8")

        store = IdentityStore()
        try:
            assert bootstrap.ensure_admin_policy(store) is False
        finally:
            store.close()

        assert policy_file.read_text(encoding="utf-8") == before
        assert bootstrap.admin_policy_gaps() == newer, "the gap must still be reported"


class TestReconcileOnlyEverAdds:
    def _run(self, *args) -> int:
        import cli

        return cli.main(["reconcile-admin-policy", *args])

    def test_a_dry_run_changes_nothing(self, seeded, capsys):
        policy_file, newer = seeded
        before = policy_file.read_text(encoding="utf-8")
        assert self._run("--dry-run") == 0
        assert policy_file.read_text(encoding="utf-8") == before
        out = capsys.readouterr().out
        for resource_type in newer:
            assert f"{resource_type}:*" in out, "the operator must see what would change"

    def test_it_closes_the_gap(self, seeded, capsys):
        _policy_file, _newer = seeded
        assert self._run() == 0
        assert bootstrap.admin_policy_gaps() == []

    def test_it_adds_and_never_removes(self, seeded, capsys):
        """The safety property. An operator who narrowed the policy on purpose loses nothing they
        still hold — they only gain the newer types, and the output says which."""
        policy_file, newer = seeded
        before = set(json.loads(policy_file.read_text(encoding="utf-8"))
                     ["policies"][0]["statements"][0]["actions"])
        self._run()
        after = set(json.loads(policy_file.read_text(encoding="utf-8"))
                    ["policies"][0]["statements"][0]["actions"])
        assert before <= after, "reconciliation removed a pattern"
        assert after - before == {f"{t}:*" for t in newer}

    def test_it_is_idempotent(self, seeded, capsys):
        self._run()
        assert self._run() == 0
        assert "nothing to do" in capsys.readouterr().out

    def test_it_leaves_other_statements_alone(self, tmp_path, monkeypatch, capsys):
        """Only the generated grant is extended. A deny an operator added must survive, or
        reconciliation becomes a way to lose a restriction."""
        import authz.store as policy_store
        from authz.resources import declared_types

        policy_file = tmp_path / "policies.json"
        monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
        document = bootstrap.admin_policy_document()
        types = sorted(declared_types())
        document["statements"][0]["actions"] = sorted(f"{t}:*" for t in types[:-1])
        document["statements"].append({
            "sid": "operator-added-deny", "effect": "deny",
            "actions": ["rack.power:*"], "resources": ["rack.power:critical-*"],
        })
        policy_file.write_text(json.dumps({"policies": [document]}), encoding="utf-8")

        assert self._run() == 0

        stored = policy_store.get(bootstrap.ADMIN_POLICY_ID)
        sids = [s.sid for s in stored.statements]
        assert "operator-added-deny" in sids, "an operator's deny was dropped"
