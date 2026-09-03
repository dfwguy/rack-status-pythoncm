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
"""Policy attachment in the identity store (FR-18, FR-20; TC-16, TC-17).

Written because a mutation went uncaught: removing the "principal must exist" check from
`attach_policy` broke nothing, since the store methods had no tests of their own. That check is the
one thing standing between a typo and a **pending grant** — attach to `user:alcie`, notice nothing,
and the day somebody legitimately creates `alcie` they silently inherit the policy. Membership has
guarded this since Phase 2 (`add_member` refuses an unknown group or user); attachment has to as
well, and now something says so.
"""

from __future__ import annotations

import pytest
from identity.models import Group, User
from identity.store import IdentityError, IdentityStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(path))
    s = IdentityStore()
    s.create_user(User(login="alice", display_name="Alice"))
    s.create_user(User(login="bob", display_name="Bob"))
    s.create_group(Group(name="ops", description="operators"))
    s.add_member("ops", "alice")
    s.identity_path = path  # the composition test needs somewhere to put a policy file
    yield s
    s.close()


class TestTheSubjectEndIsVerified:
    """The uncaught-mutation case. Each of these would otherwise create a pending grant."""

    def test_attaching_to_an_unknown_user_is_refused(self, store):
        with pytest.raises(IdentityError, match="no such user"):
            store.attach_policy("user:alcie", "rack-operators")

    def test_attaching_to_an_unknown_group_is_refused(self, store):
        with pytest.raises(IdentityError, match="no such group"):
            store.attach_policy("group:opz", "rack-operators")

    def test_a_refused_attachment_leaves_nothing_behind(self, store):
        """Half-written state would be worse than the refusal: a row nobody can see through the
        normal lookups but that starts granting the moment the name exists."""
        with pytest.raises(IdentityError):
            store.attach_policy("user:alcie", "rack-operators")
        assert store.policies_for("user:alcie") == []
        assert store.subjects_with_policy("rack-operators") == []

    @pytest.mark.parametrize(
        "subject", ["alice", "user:", "", "role:admin", ":alice", "USER:alice"]
    )
    def test_a_malformed_subject_is_refused(self, store, subject):
        """Only `user:` and `group:` exist. Anything else — including a bare login, which is the
        natural typo — must not be stored as an opaque string that matches nothing and quietly
        grants nothing while the UI shows it as attached."""
        with pytest.raises(IdentityError, match="must be"):
            store.attach_policy(subject, "rack-operators")


class TestTheAttachmentItself:
    def test_attach_then_read_back(self, store):
        store.attach_policy("user:alice", "rack-operators")
        assert store.policies_for("user:alice") == ["rack-operators"]

    def test_attaching_twice_is_idempotent(self, store):
        """Same contract as `add_member`. A second click on "attach" must not create a second row
        that a single detach then fails to remove."""
        store.attach_policy("user:alice", "rack-operators")
        store.attach_policy("user:alice", "rack-operators")
        assert store.policies_for("user:alice") == ["rack-operators"]

        store.detach_policy("user:alice", "rack-operators")
        assert store.policies_for("user:alice") == []

    def test_the_subject_is_normalised_on_write(self, store):
        """So `policies_for` — which the engine calls with a canonical principal on every request —
        finds a row attached through a differently-cased name."""
        store.attach_policy("user:ALICE", "rack-operators")
        assert store.policies_for("user:alice") == ["rack-operators"]

    def test_detaching_something_not_attached_is_quiet(self, store):
        store.detach_policy("user:alice", "never-attached")
        assert store.policies_for("user:alice") == []

    def test_attachments_do_not_leak_between_subjects(self, store):
        store.attach_policy("user:alice", "rack-operators")
        store.attach_policy("group:ops", "read-only")

        assert store.policies_for("user:alice") == ["rack-operators"]
        assert store.policies_for("user:bob") == []
        assert store.policies_for("group:ops") == ["read-only"]

    def test_the_policy_end_is_deliberately_not_verified(self, store):
        """The asymmetry, pinned so it is not "fixed" into a bug.

        Policies live in a file the operator edits outside this store, so requiring the document to
        exist would forbid the natural order of work — attach the role, then write the rule. Safe,
        because the engine skips an unknown id and the effect is default-deny; the opposite
        asymmetry (unverified *principal*) is the dangerous one and is refused above.
        """
        store.attach_policy("user:alice", "policy-that-does-not-exist-yet")
        assert store.policies_for("user:alice") == ["policy-that-does-not-exist-yet"]


class TestWhoHasThisPolicy:
    """FR-20. Deleting a policy that is still attached silently changes access, so the UI has to be
    able to show who is affected first."""

    def test_it_reports_every_subject(self, store):
        store.attach_policy("user:alice", "shared")
        store.attach_policy("user:bob", "shared")
        store.attach_policy("group:ops", "shared")

        assert store.subjects_with_policy("shared") == ["group:ops", "user:alice", "user:bob"]

    def test_it_is_empty_for_an_unattached_policy(self, store):
        assert store.subjects_with_policy("nobody-uses-this") == []

    def test_it_reflects_a_detach(self, store):
        store.attach_policy("user:alice", "shared")
        store.detach_policy("user:alice", "shared")
        assert store.subjects_with_policy("shared") == []


class TestItComposesWithTheEngine:
    def test_a_group_attachment_reaches_a_member_through_the_real_adapters(self, store):
        """End to end through `policy._groups_for` / `_attachments_for` — the product adapters, not
        injected fakes. The unit tests inject, so nothing else exercises the wiring, and a wrong
        principal format there would mean every group grant silently misses.
        """
        import json

        from authz import policy as P

        store.attach_policy("group:ops", "rack-operators")

        policy_file = json.dumps({"version": 1, "policies": [{
            "id": "rack-operators",
            "statements": [{"effect": "allow", "actions": ["rack.power:*"],
                            "resources": ["rack.power:*"]}],
        }]})
        path = store.identity_path.parent / "policies.json"
        path.write_text(policy_file)

        engine = P.PolicyEngine(path)  # no injection: real lookups against the store above

        assert engine.evaluate("user:alice", "rack.power:cycle", "rack.power:a01").allowed
        assert not engine.evaluate("user:bob", "rack.power:cycle", "rack.power:a01").allowed
