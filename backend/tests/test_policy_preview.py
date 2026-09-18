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
"""`GET /api/policies/preview` — what may this principal actually do (FR-16, AC-16).

**Why it exists.** A read-only role became expressible in one clause, and an operator still had no
way to ask what a grant *means*. Without an answer, the safe-feeling move is to widen to `["*"]`,
which is the state the whole phase exists to leave behind.

**The property that makes it trustworthy is that it does not compute anything.** The action mode
calls `authz.authorize` — the same call the gate makes — so a preview cannot drift from enforcement.
A preview that disagrees with the gate is worse than no preview: it is a confident wrong answer that
an operator will act on. `TestThePreviewCannotDisagreeWithTheGate` is the assertion of that.

**And it must not become reconnaissance.** Reporting another principal's grants would let an
unprivileged caller map who can reach which rack, which is how you choose whom to phish. Widening
past the caller is authorized separately, by the engine rather than by a comparison in the handler.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import authz
from authz.policy import PolicyEngine
from identity import testing as identity_testing
from identity.models import Group
from identity.store import IdentityStore

PREVIEW = "/api/policies/preview"

READ_ONLY = {
    "id": "read-only",
    "statements": [{
        "sid": "reads", "effect": "allow",
        "actions": ["*:read", "*:list"], "resources": ["*"],
    }],
}


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """A policy file and identity store of this test's own, with the engine enforcing them."""
    policy_file = tmp_path / "policies.json"
    policy_file.write_text(json.dumps({"policies": [READ_ONLY]}), encoding="utf-8")
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))

    captured = authz.current_engine()
    authz.set_engine(PolicyEngine(policy_file))
    try:
        yield policy_file
    finally:
        # Captured and restored: the engine is module state, so leaving one installed would decide
        # verdicts for every later test.
        authz.set_engine(captured)


@pytest.fixture
def viewer(deployment):
    """An operator holding the read-only role through a group, so `via` has something to report."""
    principal = identity_testing.enrol("previewer", groups=("viewers",), authorize=False)
    store = IdentityStore()
    try:
        if store.get_group("viewers") is None:
            store.create_group(Group(name="viewers", description="read-only operators"))
        store.attach_policy("group:viewers", "read-only")
    finally:
        store.close()
    return principal


@pytest.fixture
def client(viewer):
    from app import app
    from flask.testing import FlaskClient

    class _AsViewer(identity_testing.AuthenticatedClient, FlaskClient):
        nvis_principal_login = "previewer"

    app.config["TESTING"] = True
    previous = app.test_client_class
    app.test_client_class = _AsViewer
    try:
        with app.test_client() as handle:
            yield handle
    finally:
        app.test_client_class = previous


class TestEveryActionIsAnswered:
    """The matrix, which is the point: default deny made visible rather than implied by absence."""

    def test_every_declared_action_appears(self, client):
        import audit.actions as A

        body = client.get(PREVIEW).get_json()
        assert body["ok"] is True
        assert {row["action"] for row in body["actions"]} == set(A.registry().actions())

    def test_an_ungranted_action_is_denied_not_absent(self, client):
        """"Not mentioned" and "refused" are the same thing only if the map is complete, so the map
        has to be complete."""
        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        assert rows["rack.power:on"]["effect"] == "deny"

    def test_the_read_only_role_flips_the_reads(self, client):
        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        assert rows["rack:list"]["effect"] == "allow"
        assert rows["rack:list"]["effect"] == "allow"

    def test_the_summary_counts_agree_with_the_rows(self, client):
        """A summary computed separately from the rows is a second source of truth for one fact."""
        body = client.get(PREVIEW).get_json()
        allowed = sum(1 for r in body["actions"] if r["effect"] == "allow")
        assert body["summary"]["allowed"] == allowed
        assert body["summary"]["total"] == len(body["actions"])
        assert body["summary"]["denied"] == len(body["actions"]) - allowed

    def test_an_allowed_row_says_which_policy_decided(self, client):
        """"Allowed" without a source is unactionable: an operator removing a grant needs to know
        which document and which group carries it."""
        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        row = rows["rack:list"]
        assert row["policy"] == "read-only"
        assert row["via"] == "group:viewers"
        assert row["sid"] == "reads"

    def test_an_allowed_row_carries_its_resource_scope(self, client):
        """`allow rack.power:on on rack.power:p17-*` is not "may power off any rack", so the
        qualifier travels with the verdict instead of being collapsed into a boolean."""
        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        assert rows["rack:list"]["resources"] == ["*"]

    def test_a_deny_statement_overrides_an_allow(self, client, deployment):
        """The engine's own precedence, not a second opinion about it."""
        deployment.write_text(json.dumps({"policies": [
            READ_ONLY,
            {"id": "no-identity", "statements": [{
                "sid": "block", "effect": "deny",
                "actions": ["identity.user:read"], "resources": ["*"],
            }]},
        ]}), encoding="utf-8")
        store = IdentityStore()
        try:
            store.attach_policy("user:previewer", "no-identity")
        finally:
            store.close()

        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        assert rows["rack:list"]["effect"] == "allow"
        assert rows["identity.user:read"]["effect"] == "deny"
        assert rows["identity.user:read"]["policy"] == "no-identity"

    def test_a_dangling_attachment_grants_nothing(self, client):
        """The engine logs and skips an absent policy; the preview must not treat it as a grant, and
        must still say it is attached so a refusal can be explained."""
        store = IdentityStore()
        try:
            store.attach_policy("user:previewer", "was-deleted")
        finally:
            store.close()

        body = client.get(PREVIEW).get_json()
        entry = next(a for a in body["attachments"] if a["policy"] == "was-deleted")
        assert entry["found"] is False
        assert all(r["policy"] != "was-deleted" for r in body["actions"])


class TestTheThreeRenderings:
    """One dataset, three renderings — `cluster_report.py`'s rule, for its reason: a second
    generator drifts, and a report contradicting the product is worse than none."""

    def test_json_is_the_default(self, client):
        assert client.get(PREVIEW).get_json()["ok"] is True

    def test_text_lists_every_action_one_per_line(self, client):
        body = client.get(f"{PREVIEW}?format=text").get_data(as_text=True)
        assert "ALLOW  rack:list" in body
        assert "DENY   rack.power:on" in body

    def test_the_table_shows_only_the_allows(self, client):
        """250 denials bury the six grants that matter, and the denials are the default anyway."""
        body = client.get(f"{PREVIEW}?format=table").get_data(as_text=True)
        assert "rack:list" in body
        assert "rack.power:on" not in body

    def test_the_renderings_agree_on_the_count(self, client):
        """The assertion that keeps them one dataset rather than three implementations."""
        payload = client.get(PREVIEW).get_json()
        text = client.get(f"{PREVIEW}?format=text").get_data(as_text=True)
        assert f"{payload['summary']['allowed']} allowed" in text

    def test_an_unknown_format_is_refused(self, client):
        assert client.get(f"{PREVIEW}?format=yaml").status_code == 400


class TestItAnswersMayIDoThis:
    """The single-question mode, for "would this call work" rather than "what do I have"."""

    def test_a_permitted_action_is_allowed(self, client, an_allowed_read):
        resource = f"{an_allowed_read.split(':')[0]}:p17-r01"
        body = client.get(f"{PREVIEW}?action={an_allowed_read}&resource={resource}").get_json()
        assert body["allowed"] is True

    def test_a_refused_action_is_refused_with_a_reason(self, client, a_mutation):
        resource = f"{a_mutation.split(':')[0]}:p17-r01"
        body = client.get(f"{PREVIEW}?action={a_mutation}&resource={resource}").get_json()
        assert body["allowed"] is False
        assert body["reason"], "a refusal with no reason cannot be acted on"

    def test_an_action_without_a_resource_is_refused(self, client, a_mutation):
        """Refused rather than defaulted. A verdict against a guessed resource is a confident answer
        to a question the operator did not ask, and they would act on it."""
        resp = client.get(f"{PREVIEW}?action={a_mutation}")
        assert resp.status_code == 400
        assert "resource" in resp.get_json()["error"]


class TestThePreviewCannotDisagreeWithTheGate:
    """**The property that makes the preview trustworthy: it evaluates nothing of its own.**

    Restored after an edit of mine dropped it, which is worth recording — it is the most important
    assertion in the file. A preview that disagrees with enforcement is worse than no preview: it is
    a confident wrong answer an operator will act on.
    """

    def test_the_single_question_mode_matches_authorize_exactly(
        self, client, an_allowed_read, a_mutation
    ):
        """Both actions come from the vocabulary, so this can never assert against a name that does
        not exist — the failure that prompted publishing the document."""
        for action_name in (an_allowed_read, a_mutation):
            resource = f"{action_name.split(':')[0]}:p17-r01"
            preview = client.get(
                f"{PREVIEW}?action={action_name}&resource={resource}"
            ).get_json()["allowed"]
            direct = authz.authorize("user:previewer", action_name, resource).allowed
            assert preview is direct, f"the preview disagreed with the gate on {action_name}"

    def test_the_matrix_agrees_with_the_engine_on_every_action(self, client, vocabulary):
        """The matrix matches on the *action*, and the engine also matches the resource — so they
        can only be compared where the resource is unrestricted. This policy grants `["*"]`, which
        makes that comparison meaningful across all 173 actions rather than a sample.
        """
        rows = {r["action"]: r for r in client.get(PREVIEW).get_json()["actions"]}
        disagreed = []
        for service in vocabulary["services"]:
            for entry in service["actions"]:
                name = entry["action"]
                resource = f"{name.split(':')[0]}:p17-r01"
                engine = authz.authorize("user:previewer", name, resource).allowed
                if (rows[name]["effect"] == "allow") is not engine:
                    disagreed.append(name)
        assert not disagreed, f"matrix and engine disagreed on: {disagreed[:8]}"


class TestItIsNotReconnaissance:
    def test_previewing_another_principal_is_refused(self, client):
        """Otherwise an unprivileged caller can map who may reach which rack — which is how an
        attacker picks a target, not an abstract concern."""
        identity_testing.enrol("someone-else", authorize=False)
        resp = client.get(f"{PREVIEW}?principal=user:someone-else")
        assert resp.status_code == 403

    def test_the_refusal_says_nothing_about_the_other_principal(self, client):
        """Not even whether they exist. A different message for a real and an invented subject
        would make this an existence oracle."""
        real = client.get(f"{PREVIEW}?principal=user:someone-else")
        invented = client.get(f"{PREVIEW}?principal=user:no-such-person")
        assert real.status_code == invented.status_code == 403
        assert real.get_json() == invented.get_json()

    def test_previewing_yourself_needs_no_extra_grant(self, client):
        """The common case must not require a permission an ordinary operator lacks, or nobody uses
        it and everyone keeps widening their policy instead."""
        assert client.get(f"{PREVIEW}?principal=user:previewer").status_code == 200

    def test_an_administrator_may_preview_another_principal(self, deployment):
        """The control. Without it, the two refusals above would pass against a handler that always
        refuses — which is a different defect wearing the same green tick, and it would make the
        feature useless to the one role that needs it most.
        """
        from app import app
        from flask.testing import FlaskClient

        identity_testing.enrol("previewed-person", authorize=False)
        # An administrator: holds `policy:attach`, which is what widening is gated on.
        deployment.write_text(json.dumps({"policies": [
            READ_ONLY,
            {"id": "admins", "statements": [{
                "sid": "all", "effect": "allow",
                "actions": ["policy:attach", "policy:read"], "resources": ["*"],
            }]},
        ]}), encoding="utf-8")
        identity_testing.enrol("policy-admin", authorize=False)
        store = IdentityStore()
        try:
            store.attach_policy("user:policy-admin", "admins")
        finally:
            store.close()

        class _AsAdmin(identity_testing.AuthenticatedClient, FlaskClient):
            nvis_principal_login = "policy-admin"

        previous = app.test_client_class
        app.test_client_class = _AsAdmin
        try:
            with app.test_client() as admin:
                resp = admin.get(f"{PREVIEW}?principal=user:previewed-person")
        finally:
            app.test_client_class = previous

        assert resp.status_code == 200, resp.get_data(as_text=True)[:200]
        assert resp.get_json()["subject"] == "user:previewed-person"
