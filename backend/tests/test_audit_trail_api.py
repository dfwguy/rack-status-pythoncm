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
"""`GET /api/audit-trail` — TC-5, TC-11…TC-15 and TC-22 of the audit-reader test plan.

The HTTP layer's own properties, as distinct from the reader's: that the route is *governed* rather
than merely authenticated, that reading the trail is recorded even with read auditing off, and that
a refusal does not tell a prober whether the trail holds anything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit
from audit import actions as audit_actions
from audit import reader


def _event(i: int, **over) -> str:
    event = {
        "event_id": f"id-{i:05d}", "ts": f"2026-09-10T00:00:{i % 60:02d}.000+00:00",
        "principal": "user:alice", "auth_method": "token", "service": "rack.power",
        "action": "rack.power:off", "variant": "sync", "resource": f"rack.power:r{i}",
        "decision": "allow", "policy_engine": "rbac-v1", "result": "success",
        "src_ip": "127.0.0.1", "src_ip_trust": "peer", "user_agent": "UA",
        "request_id": "r" * 32, "cluster": "c", "latency_ms": 1.0,
    }
    event.update(over)
    return json.dumps(event)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    """A real policy set and identity store, with the trail pointed at tmp_path."""
    import authz
    from identity import bootstrap
    from identity.store import IdentityStore

    policy_file = tmp_path / "policies.json"
    policy_file.write_text(
        json.dumps({"policies": [bootstrap.admin_policy_document()]}), encoding="utf-8"
    )
    monkeypatch.setenv("RACK_WEB_POLICY_FILE", str(policy_file))
    monkeypatch.setenv("NVIS_IDENTITY_FILE", str(tmp_path / "identity.json"))
    monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "trail" / "audit.jsonl"))
    monkeypatch.setenv("NVIS_AUDIT_ENABLED", "1")
    monkeypatch.setenv("NVIS_AUDIT_READS", "0")  # the state ALWAYS_AUDITED exists for
    monkeypatch.setenv("NVIS_CATALOG_FILE", str(tmp_path / "catalog.json"))
    for leaked in ("RACK_WEB_TLS_CERT", "RACK_WEB_TLS_KEY"):
        monkeypatch.setenv(leaked, "")

    import catalog.store as catalog_store

    catalog_store.set_repo(None)

    import app as app_module

    store = IdentityStore(str(tmp_path / "identity.json"))
    try:
        bootstrap.ensure_admin(store)
    finally:
        store.close()

    captured = authz.current_engine()
    authz.install_default()
    audit.configure(force=True)
    try:
        yield app_module.app, tmp_path
    finally:
        authz.set_engine(captured)
        catalog_store.set_repo(None)


def _client(app, login: str, *, grant: bool):
    """An authenticated client, with or without the audit grant."""
    from flask.testing import FlaskClient
    from identity import testing
    from identity.store import IdentityStore

    testing.enrol(login, authorize=False)
    if grant:
        store = IdentityStore()
        try:
            store.attach_policy(f"user:{login}", "administrators")
        finally:
            store.close()

    class _As(testing.AuthenticatedClient, FlaskClient):
        nvis_principal_login = login

    app.config["TESTING"] = True
    previous = app.test_client_class
    app.test_client_class = _As
    try:
        return app.test_client(), previous
    finally:
        app.test_client_class = previous


def _seed(tmp_path, count: int) -> Path:
    trail = tmp_path / "trail" / "audit.jsonl"
    trail.parent.mkdir(parents=True, exist_ok=True)
    trail.write_text("\n".join(_event(i) for i in range(count)) + "\n", encoding="utf-8")
    return trail


class TestTC11ItIsGovernedNotMerelyAuthenticated:
    def test_an_operator_without_the_grant_is_refused(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 10)
        client, _ = _client(app, "ungranted", grant=False)
        assert client.get("/api/audit-trail").status_code == 403

    def test_the_refusal_does_not_reveal_whether_the_trail_holds_anything(self, deployment):
        """An audit reader is a reconnaissance target: "is there a trail at all" is itself worth
        knowing to somebody covering their tracks. The two refusals must be identical."""
        app, tmp_path = deployment
        client, _ = _client(app, "ungranted", grant=False)

        _seed(tmp_path, 25)
        with_events = client.get("/api/audit-trail")
        for path in (tmp_path / "trail").glob("audit.jsonl*"):
            path.unlink()
        without = client.get("/api/audit-trail")

        assert with_events.status_code == without.status_code == 403
        assert with_events.get_data() == without.get_data()

    def test_a_granted_operator_reads_it(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 10)
        client, _ = _client(app, "granted", grant=True)
        body = client.get("/api/audit-trail?limit=5").get_json()
        assert body["ok"] is True
        assert len(body["events"]) == 5


class TestTC12AndTC13TheReadIsRecorded:
    def test_the_read_is_recorded_even_with_read_auditing_off(self, deployment):
        """The whole point of `ALWAYS_AUDITED`. `NVIS_AUDIT_READS=0` is the default, and a
        privileged read of everyone else's activity must not be the one thing that leaves no
        trace."""
        app, tmp_path = deployment
        _seed(tmp_path, 5)
        client, _ = _client(app, "granted", grant=True)

        client.get("/api/audit-trail")
        audit.flush()

        trail = (tmp_path / "trail" / "audit.jsonl").read_text(encoding="utf-8")
        assert "audit.trail:list" in trail

    @pytest.mark.parametrize("reads", [1, 2, 5])
    def test_n_reads_append_exactly_n_events(self, deployment, reads):
        """FR-7 restated measurably. "Not self-amplifying" is not a testable property; linear
        growth is."""
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client, _ = _client(app, "granted", grant=True)

        for _ in range(reads):
            client.get("/api/audit-trail")
        audit.flush()

        lines = (tmp_path / "trail" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        own = [line for line in lines if "audit.trail:list" in line]
        assert len(own) == reads

    def test_a_reads_own_event_is_absent_from_its_own_response(self, deployment):
        """Prevented by where the event is written, not by a filter: the chokepoint emits in
        `after_request`, after this body was built. Asserted because it is a property somebody
        could break by moving the emit earlier."""
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client, _ = _client(app, "granted", grant=True)

        body = client.get("/api/audit-trail?limit=50").get_json()
        assert not [e for e in body["events"] if e.get("action") == "audit.trail:list"]


class TestTC14AndTC15TheNamesArePinned:
    def test_the_actions_are_in_the_generated_vocabulary(self):
        """Read from the generated document rather than typed here — `core-designs.mdc` §14: a
        test that needs to know a name the code defines reads the published document."""
        published = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "action-vocabulary.json")
            .read_text(encoding="utf-8")
        )
        names = {a for a in json.dumps(published).split('"') if a.startswith("audit.")}
        assert "audit.trail:list" in names
        assert "audit.trail:read" in names

    def test_the_trail_and_the_hardware_audit_stay_distinct(self):
        """`audit.deep:*` is NVSwitch hardware auditing and shares nothing with the security
        trail. Two unrelated meanings under one prefix is the `/icons.svg` versus `/icons/`
        mistake; a grant on one must never imply the other."""
        from authz import resources

        declared = resources.declared_types()
        assert "audit.trail" in declared
        assert "audit.deep" in declared

    def test_the_always_audited_set_is_exactly_the_trail_reads(self):
        """Exhaustive on purpose. A set that grows quietly is how an unrelated read starts being
        recorded regardless of configuration; one that shrinks is how this read stops being.

        It did its job on 2026-09-10: adding the export action failed this case, which is the
        review step the exhaustive form exists to force. `audit.trail:export` belongs because
        taking a file is extraction rather than looking — every operator's activity, off the
        machine, outliving the session.
        """
        assert audit_actions.ALWAYS_AUDITED == frozenset(
            {"audit.trail:list", "audit.trail:read", "audit.trail:export"}
        )

    def test_every_always_audited_name_is_a_real_action(self):
        """A typo here would be silent: the name would simply never match, and the read would stop
        being recorded with nothing to indicate it."""
        registry = audit_actions.registry()
        for name in audit_actions.ALWAYS_AUDITED:
            assert name in registry.actions(), f"{name} is not a declared action"


class TestTC22AndTheHonestyFields:
    def test_an_empty_trail_is_an_empty_result_not_an_error(self, deployment):
        """FR-11 at the HTTP layer.

        **Asserted as "empty", not "absent", and the distinction is a real finding.** This case
        first asserted `files == 0` and got 1: `audit.configure()` opens the rotating handler,
        which *creates* the file eagerly, so on any deployment where auditing has been configured
        the trail exists from that moment — possibly with nothing in it. The genuinely file-absent
        path is reachable only before configuration, and is covered as a unit case in
        `test_audit_reader.py::TestTC22AnAbsentTrail`.

        What matters here is the same either way: a deployment that has emitted nothing must answer
        200 with an empty list, because a fresh, healthy install is in exactly this state and a 404
        or 500 would make it look broken.
        """
        app, _tmp_path = deployment
        client, _ = _client(app, "granted", grant=True)
        resp = client.get("/api/audit-trail")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["events"] == []
        assert body["truncated"] is False
        assert body["oldest_held"] == ""

    def test_the_response_says_whether_the_answer_is_complete(self, deployment):
        """Without these a caller cannot tell "nobody did that" from "we stopped looking"."""
        app, tmp_path = deployment
        _seed(tmp_path, 10)
        client, _ = _client(app, "granted", grant=True)
        body = client.get("/api/audit-trail").get_json()
        for field in ("truncated", "cursor_expired", "scanned_events", "skipped_lines",
                      "oldest_held", "files"):
            assert field in body, f"{field} missing: a partial answer would look complete"

    def test_a_bad_limit_is_a_400_not_a_traceback(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client, _ = _client(app, "granted", grant=True)
        assert client.get("/api/audit-trail?limit=banana").status_code == 400

    def test_an_oversized_limit_is_clamped_rather_than_refused(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 600)
        client, _ = _client(app, "granted", grant=True)
        body = client.get("/api/audit-trail?limit=10000").get_json()
        assert len(body["events"]) == reader.MAX_PAGE

    def test_the_fields_endpoint_serves_the_filter_list(self, deployment):
        """Served rather than duplicated in the frontend: a hard-coded list is wrong the first
        time somebody adds a field, and the failure is a filter that silently does nothing."""
        app, _tmp_path = deployment
        client, _ = _client(app, "granted", grant=True)
        body = client.get("/api/audit-trail/fields").get_json()
        assert set(body["filters"]) == set(reader.Filter.__dataclass_fields__)
