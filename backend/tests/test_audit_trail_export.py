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
"""`GET /api/audit-trail/export` — downloading the trail.

Two properties carry this, and both are about a file outliving the session that produced it:

* **A partial export must say so, in the file.** The scan is capped, so an export can be
  incomplete — and a file that looks complete is evidence of the wrong thing. A CSV opened in six
  months has no UI banner attached to it, so the truncation has to be *in the bytes*.
* **Export is a distinct grant from reading a page.** Reading is looking; taking a file is
  extraction. A deployment should be able to grant one without the other, and the trail must record
  which happened.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit
from audit import reader


def _event(i: int, **over) -> str:
    event = {
        "event_id": f"id-{i:05d}", "ts": f"2026-09-10T00:00:{i % 60:02d}.000+00:00",
        "principal": "user:alice" if i % 2 else "user:bob", "auth_method": "token",
        "service": "rack.power", "action": "rack.power:off", "variant": "sync",
        "resource": f"rack.power:r{i}", "decision": "allow", "policy_engine": "rbac-v1",
        "result": "success", "src_ip": "127.0.0.1", "src_ip_trust": "peer", "user_agent": "UA",
        "request_id": "r" * 32, "cluster": "c", "latency_ms": 1.0,
    }
    event.update(over)
    return json.dumps(event)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
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
    monkeypatch.setenv("NVIS_AUDIT_READS", "0")
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
        return app.test_client()
    finally:
        app.test_client_class = previous


def _seed(tmp_path, how_many: int, **kw) -> Path:
    """`how_many`, not `count`: `count` is also an event field, and a case seeding a flood summary
    needs to pass it through `**kw`."""
    trail = tmp_path / "trail" / "audit.jsonl"
    trail.parent.mkdir(parents=True, exist_ok=True)
    trail.write_text("\n".join(_event(i, **kw) for i in range(how_many)) + "\n", encoding="utf-8")
    return trail


class TestItDownloadsWhatTheFilterMatched:
    def test_json_carries_the_events_and_its_own_metadata(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 10)
        client = _client(app, "granted", grant=True)

        resp = client.get("/api/audit-trail/export?format=json")
        assert resp.status_code == 200
        body = json.loads(resp.get_data(as_text=True))
        assert len(body["events"]) == 10
        assert body["meta"]["truncated"] is False
        assert body["meta"]["events"] == 10

    def test_the_filter_narrows_the_export_and_is_recorded_in_it(self, deployment):
        """The filter travels with the file. Six months later, "why does this only contain alice"
        is answerable from the export itself rather than from whoever clicked the button."""
        app, tmp_path = deployment
        _seed(tmp_path, 10)
        client = _client(app, "granted", grant=True)

        body = json.loads(
            client.get("/api/audit-trail/export?format=json&principal=user:alice")
            .get_data(as_text=True)
        )
        assert body["events"], "nothing matched; the case asserts nothing"
        assert all(e["principal"] == "user:alice" for e in body["events"])
        assert body["meta"]["filter"] == {"principal": "user:alice"}

    def test_csv_has_a_header_row_and_one_row_per_event(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 5)
        client = _client(app, "granted", grant=True)

        text = client.get("/api/audit-trail/export?format=csv").get_data(as_text=True)
        rows = list(csv.DictReader(io.StringIO(
            "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
        )))
        assert len(rows) == 5
        assert rows[0]["event_id"] == "id-00004", "newest first, as on screen"

    def test_csv_columns_are_the_pinned_schema_plus_the_optional_extras(self, deployment):
        """A spreadsheet with a shifting column set cannot be diffed between exports."""
        from api import audit_trail

        assert audit_trail.CSV_COLUMNS[: len(audit.EVENT_FIELDS)] == audit.EVENT_FIELDS
        assert set(audit_trail.CSV_COLUMNS) - set(audit.EVENT_FIELDS) == {
            "count", "collapsed", "target"
        }

    def test_it_is_served_as_a_download_with_a_timestamped_name(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client = _client(app, "granted", grant=True)

        resp = client.get("/api/audit-trail/export?format=csv")
        disposition = resp.headers["Content-Disposition"]
        assert disposition.startswith("attachment;")
        assert ".csv" in disposition
        assert resp.headers["Cache-Control"] == "no-store"

    def test_an_unknown_format_is_refused(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client = _client(app, "granted", grant=True)
        assert client.get("/api/audit-trail/export?format=pdf").status_code == 400


class TestAPartialExportSaysSoInTheFile:
    """The property that matters most. A UI banner does not travel with a downloaded file."""

    def test_json_metadata_reports_truncation(self, deployment, monkeypatch):
        app, tmp_path = deployment
        _seed(tmp_path, 200)
        monkeypatch.setattr(reader, "MAX_SCAN_EVENTS", 40)
        client = _client(app, "granted", grant=True)

        body = json.loads(
            client.get("/api/audit-trail/export?format=json").get_data(as_text=True)
        )
        assert body["meta"]["truncated"] is True
        assert body["meta"]["scanned_events"] == 40

    def test_csv_carries_an_explicit_warning_line(self, deployment, monkeypatch):
        """Not merely a `truncated,True` field among the metadata: a reader skimming a spreadsheet
        needs the word WARNING, because the consequence is an incomplete audit record."""
        app, tmp_path = deployment
        _seed(tmp_path, 200)
        monkeypatch.setattr(reader, "MAX_SCAN_EVENTS", 40)
        client = _client(app, "granted", grant=True)

        text = client.get("/api/audit-trail/export?format=csv").get_data(as_text=True)
        assert "# WARNING" in text
        assert "PARTIAL" in text

    def test_a_complete_csv_carries_no_warning(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 5)
        client = _client(app, "granted", grant=True)
        text = client.get("/api/audit-trail/export?format=csv").get_data(as_text=True)
        assert "WARNING" not in text, "a complete export must not cry wolf"

    def test_the_comment_lines_are_skippable_by_a_csv_reader(self, deployment):
        """`#` lines are the convention `cluster_report.to_csv` already uses precisely because
        spreadsheets show them as text and `pandas.read_csv(comment='#')` skips them."""
        app, tmp_path = deployment
        _seed(tmp_path, 4)
        client = _client(app, "granted", grant=True)

        text = client.get("/api/audit-trail/export?format=csv").get_data(as_text=True)
        head, *_rest = text.splitlines()
        assert head.startswith("#")
        body = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
        assert list(csv.DictReader(io.StringIO(body)))


class TestExportIsItsOwnGrant:
    def test_a_flood_summary_keeps_its_count(self, deployment):
        """One line standing for many events must not export as one occurrence — the count is the
        finding in exactly the review an export is taken for."""
        app, tmp_path = deployment
        _seed(tmp_path, 1, count=47, collapsed=True, result="refused", decision="deny")
        client = _client(app, "granted", grant=True)

        body = json.loads(
            client.get("/api/audit-trail/export?format=json").get_data(as_text=True)
        )
        assert body["events"][0]["count"] == 47
        text = client.get("/api/audit-trail/export?format=csv").get_data(as_text=True)
        rows = list(csv.DictReader(io.StringIO(
            "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
        )))
        # Located by id, not by position. `rows[0]` is the *newest* event, which by now is the
        # `audit.trail:export` record written by the JSON request three lines above -- reading the
        # trail appends to the trail, so position is not a stable way to find a seeded row.
        summary = next(r for r in rows if r["event_id"] == "id-00000")
        assert summary["count"] == "47"

    def test_an_ungranted_operator_is_refused(self, deployment):
        app, tmp_path = deployment
        _seed(tmp_path, 5)
        client = _client(app, "ungranted", grant=False)
        assert client.get("/api/audit-trail/export?format=json").status_code == 403

    def test_the_export_is_recorded_even_with_read_auditing_off(self, deployment):
        """`ALWAYS_AUDITED` covers it. An extraction of everyone's activity that left no trace
        would be the one read most worth hiding."""
        app, tmp_path = deployment
        _seed(tmp_path, 3)
        client = _client(app, "granted", grant=True)

        client.get("/api/audit-trail/export?format=json")
        audit.flush()

        trail = (tmp_path / "trail" / "audit.jsonl").read_text(encoding="utf-8")
        assert "audit.trail:export" in trail

    def test_it_is_a_different_action_from_reading_a_page(self):
        """Separately grantable, so a deployment can allow looking without allowing extraction."""
        from audit import actions as audit_actions

        registry = audit_actions.registry().actions()
        assert "audit.trail:export" in registry
        assert "audit.trail:list" in registry
        # Distinct entries, not just both present: the export bound and the page bound
        # are separate actions, and a registry collapsing them would pass the two
        # membership checks above.
        assert len({"audit.trail:export", "audit.trail:list"} & set(registry)) == 2


class TestTheExportBoundIsNotThePageBound:
    def test_an_export_returns_more_than_a_page(self, deployment):
        """The whole reason `export` exists beside `read`: a page is clamped to `MAX_PAGE` (500) so
        one careless caller cannot pull the trail, while an export *is* that request."""
        app, tmp_path = deployment
        _seed(tmp_path, 600)
        client = _client(app, "granted", grant=True)

        page = client.get("/api/audit-trail?limit=10000").get_json()
        assert len(page["events"]) == reader.MAX_PAGE

        body = json.loads(
            client.get("/api/audit-trail/export?format=json").get_data(as_text=True)
        )
        # `>=`, not `==`: the paging request above is itself audited (ALWAYS_AUDITED), so it
        # appends an event to the very trail being exported. Asserting equality made this fail at
        # 601 -- a fixture assumption, not a defect, but a good reminder that reading the trail
        # changes it.
        assert len(body["events"]) >= 600

    def test_the_paging_endpoint_cannot_be_used_to_export(self, deployment):
        """A `limit=` a caller may raise would have made the clamp advisory — which is why the
        bound lives in two entry points rather than one parameter."""
        app, tmp_path = deployment
        _seed(tmp_path, 600)
        client = _client(app, "granted", grant=True)
        body = client.get(f"/api/audit-trail?limit={reader.MAX_SCAN_EVENTS}").get_json()
        assert len(body["events"]) == reader.MAX_PAGE
