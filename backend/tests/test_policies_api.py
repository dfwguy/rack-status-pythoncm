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
"""Policy CRUD, attachment, and the editor's vocabulary (FR-19, FR-20, FR-21).

Three groups carry most of the value.

`TestTheEditorAndServerCannotDisagree` is the one that prevents a whole class of support ticket:
the schema and the action list are *served*, not duplicated in the frontend, so an editor cannot
accept a document the server refuses. That failure presents as a product bug rather than a typo.

`TestDeletingAnAttachedPolicy` guards the operation that silently changes access. An operator
deleting a policy usually cannot see who has it, so the default answer names them and stops.

`TestAWriteNeverLeavesAnUnusablePolicySet` matters because an unparseable policy file makes the
engine deny *every* request — correctly — so writing one would be a self-inflicted outage.
"""

from __future__ import annotations

import json

import pytest

VALID = {
    "id": "rack-operators",
    "description": "power-cycle any rack",
    "statements": [
        {
            "sid": "power",
            "effect": "allow",
            "actions": ["rack.power:*"],
            "resources": ["rack.power:*"],
        }
    ],
}


@pytest.fixture
def policy_api(tmp_path, monkeypatch):
    """An isolated policy file and identity store, with the real app."""
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(tmp_path / "policies.json"))
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def a_user(policy_api, tmp_path):
    from identity.models import Group, User
    from identity.store import IdentityStore

    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        store.create_user(User(login="alice", display_name="Alice"))
        store.create_group(Group(name="ops", description="operators"))
    finally:
        store.close()
    return "user:alice"


class TestCrud:
    def test_an_empty_deployment_lists_no_policies(self, policy_api):
        """A fresh deployment has no policy file, and that is a valid state meaning default-deny —
        the list must agree with the engine rather than inventing a file on first read."""
        body = policy_api.get("/api/policies").get_json()
        assert body["ok"] is True
        assert body["policies"] == []

    def test_create_then_read_back(self, policy_api):
        created = policy_api.post("/api/policies", json=VALID)
        assert created.status_code == 201

        fetched = policy_api.get("/api/policies/rack-operators").get_json()
        assert fetched["policy"]["id"] == "rack-operators"
        assert fetched["policy"]["statements"][0]["sid"] == "power"

    def test_a_duplicate_id_is_refused_not_overwritten(self, policy_api):
        """Overwriting on create means "save" silently replaces somebody else's policy when two
        operators pick the same name, and the one who loses never finds out."""
        policy_api.post("/api/policies", json=VALID)
        again = policy_api.post("/api/policies", json=VALID)

        assert again.status_code == 409
        assert "already exists" in again.get_json()["error"]

    def test_update_replaces_in_place(self, policy_api):
        policy_api.post("/api/policies", json=VALID)
        edited = {**VALID, "description": "now with a note"}

        resp = policy_api.put("/api/policies/rack-operators", json=edited)
        assert resp.status_code == 200
        assert resp.get_json()["policy"]["description"] == "now with a note"

    def test_a_policy_cannot_be_renamed_by_updating_it(self, policy_api):
        """One PUT would otherwise delete a policy and create another, breaking every attachment
        pointing at the old name without saying so."""
        policy_api.post("/api/policies", json=VALID)

        resp = policy_api.put("/api/policies/rack-operators", json={**VALID, "id": "renamed"})
        assert resp.status_code == 400
        assert "cannot be renamed" in resp.get_json()["error"]

    def test_updating_an_absent_policy_is_404(self, policy_api):
        resp = policy_api.put("/api/policies/ghost", json={**VALID, "id": "ghost"})
        assert resp.status_code == 404

    def test_the_list_reports_the_file_it_read(self, policy_api):
        """"Which file am I editing" is the first question when a deployment has two."""
        assert "policies.json" in policy_api.get("/api/policies").get_json()["path"]


class TestAWriteNeverLeavesAnUnusablePolicySet:
    """Every write parses first. The engine denies everything on an unparseable file."""

    @pytest.mark.parametrize(
        ("body", "why"),
        [
            ({"id": "x"}, "no statements"),
            ({"statements": []}, "no id"),
            ({"id": "x", "statements": [{"effect": "alow", "actions": ["a:b"]}]}, "typo in effect"),
            ({"id": "x", "statements": [{"effect": "allow", "actions": ["*"], "resources": ["*"]}]},
             "bare star in an allow"),
            ({"id": "x", "principals": ["user:a"], "statements": [
                {"effect": "allow", "actions": ["a:b"], "resources": ["*"]}]},
             "inline principals, which the attachment model retired"),
        ],
    )
    def test_an_invalid_document_is_refused_with_a_reason(self, policy_api, body, why):
        resp = policy_api.post("/api/policies", json=body)
        assert resp.status_code == 400, f"accepted a document with {why}"
        assert "invalid policy" in resp.get_json()["error"]

    def test_a_refused_write_leaves_the_file_untouched(self, policy_api, tmp_path):
        policy_api.post("/api/policies", json=VALID)
        before = (tmp_path / "policies.json").read_text()

        policy_api.post("/api/policies", json={"id": "broken", "statements": "nope"})

        assert (tmp_path / "policies.json").read_text() == before

    def test_the_written_file_is_what_the_engine_reads(self, policy_api, tmp_path):
        """The store and the engine must resolve the same path, or an operator edits one file and
        the product enforces another."""
        policy_api.post("/api/policies", json=VALID)

        from authz import policy as engine

        loaded = engine.load(tmp_path / "policies.json")
        assert "rack-operators" in loaded.by_id

    def test_the_file_is_owner_only(self, policy_api, tmp_path):
        """Policies are not secret but they are security configuration; a file anyone may rewrite
        is a privilege-escalation path. `identity-auth-traps.mdc` trap 3: the mode has to be on the
        temp file, because `os.replace` preserves the source inode's mode."""
        policy_api.post("/api/policies", json=VALID)
        assert (tmp_path / "policies.json").stat().st_mode & 0o777 == 0o600


class TestAttachment:
    def test_attach_then_list_both_directions(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)

        resp = policy_api.post(
            "/api/policies/rack-operators/attachments", json={"subject": a_user}
        )
        assert resp.status_code == 201

        by_policy = policy_api.get("/api/policies/rack-operators/attachments").get_json()
        assert by_policy["subjects"] == [a_user]

        by_subject = policy_api.get(f"/api/policies/subjects/{a_user}").get_json()
        assert by_subject["policies"] == ["rack-operators"]

    def test_attaching_a_policy_that_does_not_exist_is_404(self, policy_api, a_user):
        """The opposite asymmetry to the store's, and deliberate: the store cannot verify a policy
        exists because an operator may be mid-edit, but an API caller naming a missing policy has
        simply made a mistake, and telling them beats silently creating an attachment that grants
        nothing."""
        resp = policy_api.post("/api/policies/ghost/attachments", json={"subject": a_user})
        assert resp.status_code == 404

    def test_attaching_to_a_principal_that_does_not_exist_is_refused(self, policy_api):
        """The store's guard, surfaced. Without it a typo creates a *pending grant* that activates
        the day somebody legitimately creates that name."""
        policy_api.post("/api/policies", json=VALID)

        resp = policy_api.post(
            "/api/policies/rack-operators/attachments", json={"subject": "user:alcie"}
        )
        assert resp.status_code == 400
        assert "no such user" in resp.get_json()["error"]

    def test_a_missing_subject_is_refused(self, policy_api):
        policy_api.post("/api/policies", json=VALID)
        resp = policy_api.post("/api/policies/rack-operators/attachments", json={})
        assert resp.status_code == 400

    def test_detach_is_idempotent(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        policy_api.post("/api/policies/rack-operators/attachments", json={"subject": a_user})

        first = policy_api.delete(f"/api/policies/rack-operators/attachments/{a_user}")
        second = policy_api.delete(f"/api/policies/rack-operators/attachments/{a_user}")

        assert first.status_code == second.status_code == 200
        assert policy_api.get("/api/policies/rack-operators/attachments").get_json()["subjects"] == []

    def test_a_group_can_be_attached(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        resp = policy_api.post(
            "/api/policies/rack-operators/attachments", json={"subject": "group:ops"}
        )
        assert resp.status_code == 201


class TestDeletingAnAttachedPolicy:
    """The operation that silently changes access."""

    def test_it_refuses_and_names_who_is_affected(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        policy_api.post("/api/policies/rack-operators/attachments", json={"subject": a_user})

        resp = policy_api.delete("/api/policies/rack-operators")

        assert resp.status_code == 409
        assert a_user in resp.get_json()["error"], "the refusal does not say who is affected"

    def test_the_policy_survives_a_refused_delete(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        policy_api.post("/api/policies/rack-operators/attachments", json={"subject": a_user})
        policy_api.delete("/api/policies/rack-operators")

        assert policy_api.get("/api/policies/rack-operators").status_code == 200

    def test_force_deletes_and_reports_the_detachments(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        policy_api.post("/api/policies/rack-operators/attachments", json={"subject": a_user})

        resp = policy_api.delete("/api/policies/rack-operators?force=true")

        assert resp.status_code == 200
        assert resp.get_json()["detached_from"] == [a_user]
        assert policy_api.get("/api/policies/rack-operators").status_code == 404

    def test_force_leaves_no_dangling_attachment(self, policy_api, a_user):
        policy_api.post("/api/policies", json=VALID)
        policy_api.post("/api/policies/rack-operators/attachments", json={"subject": a_user})
        policy_api.delete("/api/policies/rack-operators?force=true")

        assert policy_api.get(f"/api/policies/subjects/{a_user}").get_json()["policies"] == []

    def test_an_unattached_policy_deletes_without_force(self, policy_api):
        policy_api.post("/api/policies", json=VALID)
        assert policy_api.delete("/api/policies/rack-operators").status_code == 200


class TestTheEditorAndServerCannotDisagree:
    """FR-21. The schema and vocabulary are served, not duplicated in the frontend."""

    def test_the_schema_is_the_one_the_server_enforces(self, policy_api):
        served = policy_api.get("/api/policies/schema").get_json()["schema"]

        from authz import schema as authz_schema

        assert served == authz_schema.document_json_schema()

    def test_the_schema_carries_the_bare_star_rule(self, policy_api):
        """pydantic expresses it as a validator and the export bridges it as a conditional.
        Without the bridge the editor accepts a document the server refuses, which reads as a
        product bug rather than a typo."""
        served = json.dumps(policy_api.get("/api/policies/schema").get_json()["schema"])
        assert "allow" in served
        assert "not" in served or "if" in served, (
            "the exported schema carries no conditional, so the bare-* rule is server-only"
        )

    def test_the_vocabulary_comes_from_the_live_registry(self, policy_api):
        body = policy_api.get("/api/policies/vocabulary").get_json()

        from audit import actions as audit_actions

        assert body["actions"] == sorted(audit_actions.registry().actions())
        assert len(body["actions"]) > 100, "the vocabulary looks truncated"

    def test_the_vocabulary_includes_read_actions(self, policy_api):
        """Reads joined the vocabulary in Phase 3; an editor offering only mutations would make a
        read-only role look impossible to express."""
        actions = policy_api.get("/api/policies/vocabulary").get_json()["actions"]
        assert any(a.endswith(":read") for a in actions)
        assert any(a.endswith(":list") for a in actions)

    def test_the_vocabulary_names_the_resource_types_and_sentinels(self, policy_api):
        """An editor cannot validate a resource without knowing the type list, or explain
        `rack:_all` without being told the collection sentinel."""
        body = policy_api.get("/api/policies/vocabulary").get_json()

        from authz import resources as authz_resources

        assert body["resource_types"] == sorted(authz_resources.declared_types())
        assert body["collection_id"] == authz_resources.COLLECTION
        assert body["singleton_id"] == authz_resources.SINGLETON

    def test_a_document_the_schema_accepts_is_accepted_by_the_api(self, policy_api):
        """The property that makes the served schema worth anything, asserted end to end."""
        import jsonschema

        served = policy_api.get("/api/policies/schema").get_json()["schema"]
        jsonschema.validate(VALID, served)

        assert policy_api.post("/api/policies", json=VALID).status_code == 201
