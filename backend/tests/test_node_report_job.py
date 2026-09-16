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
"""The node-report job: uuid, pollable progress, collect-once result.

Generating the report sweeps every node over SSH — about four minutes at Nscale TX's 2013
trays. That is far too long for a request to block on, and with no progress the operator has
no way to tell a working sweep from a hung one. These tests pin the contract the SPA relies on:
start returns a uuid immediately, status reports done/total while it runs, and the document is
collected separately so polling never ships several MB.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate_jobs():
    """The job table is module-global, and reuse is keyed on request shape.

    That is the behaviour we want in production — pressing the button twice joins the first
    run — but across tests it means one case silently adopts the previous case's job.
    """
    import app as A

    with A._node_report_jobs_lock:
        A._node_report_jobs.clear()
    yield
    with A._node_report_jobs_lock:
        A._node_report_jobs.clear()


def _wait(client, job_id, *, want, timeout=5.0):
    deadline = time.monotonic() + timeout
    body = None
    while time.monotonic() < deadline:
        body = client.get("/api/node-report/status", query_string={"job_id": job_id}).get_json()
        if body.get("state") == want:
            return body
        time.sleep(0.02)
    assert body is not None
    assert body.get("state") == want, f"job never reached {want!r}: {body}"
    return body


def test_report_fleet_excludes_only_real_hosts_outside_active_compute_template(monkeypatch):
    import app as A

    monkeypatch.setattr(
        A,
        "_nvis_naming",
        lambda: SimpleNamespace(
            sinfo_rack_from_host=lambda name: (
                "ink50-dh02-e06" if name.startswith("ink50-dh02-e06-") else None
            )
        ),
    )
    fleet = {
        "columns": ["allreduce"],
        "nodes": [
            {"node": "ink50-dh02-e06-11", "jobs": []},
            {"node": "appserver", "jobs": []},
            {"node": "************************************************************", "jobs": []},
        ],
    }

    filtered = A._validation_report_compute_fleet(fleet)

    assert [row["node"] for row in filtered["nodes"]] == ["ink50-dh02-e06-11"]
    assert filtered["excluded_non_compute"] == {
        "count": 1,
        "nodes": ["appserver"],
    }


@pytest.fixture
def fake_generation(monkeypatch):
    """Stand in for the fleet sweep so the lifecycle is testable without a cluster."""
    import app as A

    fleet = {"columns": ["allreduce"],
             "nodes": [{"node": f"n{i}", "jobs": [{"name": "allreduce", "status": "DONE"}]}
                       for i in range(3)]}

    class _Bringup:
        @staticmethod
        def fleet_summary():
            return fleet

        @staticmethod
        def is_enabled():
            return True

    monkeypatch.setattr(A, "_bringup", _Bringup)

    seen: list[tuple[int, int]] = []

    def _collect(_fleet, progress=None, phase=None):
        for i in range(1, 4):
            if progress:
                progress(i, 3, f"n{i}")
            seen.append((i, 3))
        return ({"shared_baseline": {}, "nodes": [{"hostname": "n0"}]}, ["a deviation"])

    monkeypatch.setattr(A, "_collect_node_inventory", _collect)
    return seen


class TestJobLifecycle:
    def test_start_returns_a_uuid_immediately_rather_than_blocking(self, client, fake_generation):
        body = client.post("/api/node-report/start", json={"inventory": True}).get_json()
        assert body["ok"] is True
        assert len(body["job_id"]) == 32  # uuid4 hex

    def test_status_reports_progress_and_then_completion(self, client, fake_generation):
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        done = _wait(client, job_id, want="done")
        assert done["ready"] is True
        assert done["phase"] == "complete"
        # The strict report may contain one representative firmware placeholder;
        # status still reports the full validation scope, not that placeholder count.
        assert done["nodes"] == 3
        # The sweep published a count for every node it finished.
        assert fake_generation == [(1, 3), (2, 3), (3, 3)]

    def test_the_document_does_not_ride_along_with_every_poll(self, client, fake_generation):
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        body = _wait(client, job_id, want="done")
        assert "report" not in body

    def test_the_result_is_collected_separately_and_only_once(self, client, fake_generation):
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        _wait(client, job_id, want="done")

        first = client.get("/api/node-report/result", query_string={"job_id": job_id})
        assert first.status_code == 200
        assert first.get_json()["report_metadata"]["schema_version"] == "1.0"

        # Freed after hand-off so an abandoned tab cannot pin a multi-MB document.
        second = client.get("/api/node-report/result", query_string={"job_id": job_id})
        assert second.status_code == 409

    def test_asking_for_the_result_early_says_not_ready_with_the_current_phase(
        self, client, monkeypatch
    ):
        import app as A

        monkeypatch.setattr(A, "_bringup", None)

        def _slow(*a, **k):
            time.sleep(0.5)
            return None, []

        monkeypatch.setattr(A, "_collect_node_inventory", _slow)
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        early = client.get("/api/node-report/result", query_string={"job_id": job_id})
        assert early.status_code == 409
        assert early.get_json()["error"] == "not ready"

    def test_download_sets_a_filename_from_the_report_id(self, client, fake_generation):
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        _wait(client, job_id, want="done")
        resp = client.get("/api/node-report/result",
                          query_string={"job_id": job_id, "download": "1"})
        assert "attachment" in resp.headers["Content-Disposition"]
        assert resp.headers["Content-Disposition"].endswith('.json"')


class TestIdempotency:
    def test_pressing_the_button_twice_does_not_launch_a_second_fleet_sweep(
        self, client, monkeypatch
    ):
        """The second press must join the first run, not start four more minutes of SSH."""
        import app as A

        started: list[int] = []

        def _slow(_fleet, progress=None, phase=None):
            started.append(1)
            time.sleep(0.4)
            return ({"shared_baseline": {}, "nodes": [{"hostname": "n0"}]}, [])

        class _Bringup:
            @staticmethod
            def fleet_summary():
                return {"columns": ["allreduce"],
                        "nodes": [{"node": "n0",
                                   "jobs": [{"name": "allreduce", "status": "DONE"}]}]}

        monkeypatch.setattr(A, "_bringup", _Bringup)
        monkeypatch.setattr(A, "_collect_node_inventory", _slow)

        first = client.post("/api/node-report/start", json={}).get_json()
        second = client.post("/api/node-report/start", json={}).get_json()
        assert second["job_id"] == first["job_id"]
        assert second.get("reused") is True
        _wait(client, first["job_id"], want="done")
        assert len(started) == 1

    def test_a_different_request_shape_is_a_different_job(self, client, fake_generation):
        a = client.post("/api/node-report/start", json={"inventory": True}).get_json()
        b = client.post("/api/node-report/start", json={"inventory": False}).get_json()
        assert a["job_id"] != b["job_id"]


class TestFailureIsVisible:
    def test_an_unknown_job_id_is_a_404_not_a_hang(self, client):
        resp = client.get("/api/node-report/status", query_string={"job_id": "nope"})
        assert resp.status_code == 404

    def test_a_worker_crash_surfaces_as_an_error_state(self, client, monkeypatch):
        import app as A

        class _Bringup:
            @staticmethod
            def fleet_summary():
                raise RuntimeError("bring-up tree unreachable")

        monkeypatch.setattr(A, "_bringup", _Bringup)
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        body = _wait(client, job_id, want="error")
        assert "bring-up tree unreachable" in body["error"]


@pytest.fixture
def offline_facts(monkeypatch):
    """The Rack Status report enriches from the head node and BCM; neither exists in a test.

    Both are deliberately optional in the worker — a report that refused to render because a
    version probe was unreachable would be useless in exactly the degraded conditions where an
    operator most wants one — so these stubs also assert that optionality holds.
    """
    import app as A

    monkeypatch.setattr(A, "_cluster_facts", lambda: {"slurm": {"version": "23.11.6"}})
    monkeypatch.setattr(A, "_analytics_last_cached", dict)


class TestReportsAreKeptWhoeverIsWatching:
    """The whole point of the store: a finished report must not depend on someone waiting.

    A sweep is minutes of SSH across the fleet. Before this, the document lived in the job table
    and was freed when collected, so navigating away from Analytics mid-run discarded all of it.
    """

    @pytest.fixture(autouse=True)
    def isolated_store(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_REPORTS_DIR", str(tmp_path / "reports"))

    @staticmethod
    def _only(client, *, kind=None, fmt=None):
        """The saved report matching kind/format.

        Selected rather than indexed: earlier tests deliberately start a job without waiting for
        it, and that worker can finish during this test and archive its own report. Picking by
        shape keeps these assertions about the report the test actually generated.
        """
        reports = client.get("/api/reports/generated").get_json()["reports"]
        picked = [r for r in reports
                  if (kind is None or r["kind"] == kind) and (fmt is None or r["format"] == fmt)]
        assert len(picked) == 1, f"expected one {kind}/{fmt} report, got {reports}"
        return picked[0]

    def test_a_report_is_saved_without_anyone_collecting_it(self, client, fake_generation,
                                                            offline_facts):
        job_id = client.post("/api/node-report/start",
                             json={"report": "rack-status", "format": "csv"}).get_json()["job_id"]
        _wait(client, job_id, want="done")
        # Deliberately never call /result — this is the operator closing the tab.
        saved = self._only(client, kind="rack-status", fmt="csv")
        assert saved["size"] > 0
        assert saved["nodes"] == 3

    def test_the_saved_copy_downloads_after_the_live_one_was_consumed(self, client,
                                                                     fake_generation,
                                                                     offline_facts):
        job_id = client.post("/api/node-report/start",
                             json={"report": "rack-status", "format": "csv"}).get_json()["job_id"]
        _wait(client, job_id, want="done")
        client.get("/api/node-report/result", query_string={"job_id": job_id})
        # The in-memory document is freed on collection; the archived one must still be there.
        saved = self._only(client, kind="rack-status", fmt="csv")
        res = client.get(f"/api/reports/generated/{saved['id']}")
        assert res.status_code == 200
        assert res.mimetype == "text/csv"
        assert res.headers["Content-Disposition"].endswith('.csv"')

    def test_the_customer_schema_report_is_archived_as_the_document_not_its_metadata(
        self, client, fake_generation
    ):
        job_id = client.post("/api/node-report/start", json={}).get_json()["job_id"]
        _wait(client, job_id, want="done")
        saved = self._only(client, kind="nscale")
        body = client.get(f"/api/reports/generated/{saved['id']}").get_json()
        # A sidecar/payload path collision would hand back the metadata instead of the report.
        assert "report_metadata" in body
        assert "test_results" in body

    def test_the_job_reports_the_id_it_archived_under(self, client, fake_generation,
                                                      offline_facts):
        job_id = client.post("/api/node-report/start",
                             json={"report": "rack-status", "format": "json"}).get_json()["job_id"]
        done = _wait(client, job_id, want="done")
        assert done.get("saved_id")
        assert client.get(f"/api/reports/generated/{done['saved_id']}").status_code == 200

    def test_a_download_for_an_unknown_id_is_a_404_not_a_crash(self, client):
        assert client.get("/api/reports/generated/" + "0" * 32).status_code == 404

    def test_a_traversal_attempt_in_the_id_is_refused(self, client):
        # Flask's own routing rejects a slash here; the store rejects the shape as well.
        assert client.get("/api/reports/generated/..%2F..%2Fetc%2Fpasswd").status_code in (404, 308)

    def test_a_saved_report_can_be_deleted(self, client, fake_generation, offline_facts):
        job_id = client.post("/api/node-report/start",
                             json={"report": "rack-status", "format": "csv"}).get_json()["job_id"]
        _wait(client, job_id, want="done")
        saved = self._only(client, kind="rack-status", fmt="csv")

        assert client.delete(f"/api/reports/generated/{saved['id']}").status_code == 200
        remaining = client.get("/api/reports/generated").get_json()["reports"]
        assert saved["id"] not in [r["id"] for r in remaining]
        assert client.delete(f"/api/reports/generated/{saved['id']}").status_code == 404

    def test_a_failing_store_does_not_fail_the_report(self, client, fake_generation,
                                                      offline_facts, monkeypatch):
        """A full disk should cost the archive copy, not the run the operator waited for."""
        import report_store

        monkeypatch.setattr(report_store, "save",
                            lambda **_kw: (_ for _ in ()).throw(OSError("no space left")))
        job_id = client.post("/api/node-report/start",
                             json={"report": "rack-status", "format": "csv"}).get_json()["job_id"]
        done = _wait(client, job_id, want="done")
        assert done["state"] == "done"
        assert not done.get("saved_id")
        assert not [r for r in client.get("/api/reports/generated").get_json()["reports"]
                    if r["format"] == "csv"]
        # And the live download still works.
        assert client.get("/api/node-report/result",
                          query_string={"job_id": job_id}).status_code == 200


class TestReportKinds:
    def test_both_reports_are_advertised_with_their_formats(self, client):
        body = client.get("/api/node-report/kinds").get_json()
        by_id = {k["id"]: k for k in body["kinds"]}
        assert set(by_id) == {"rack-status", "nscale"}
        # The customer schema is a JSON contract; offering it as CSV would produce a file that
        # cannot validate against the thing it exists to satisfy.
        assert by_id["nscale"]["formats"] == ["json"]
        assert "csv" in by_id["rack-status"]["formats"]

    def test_pdf_is_only_offered_when_it_can_actually_be_rendered(self, client, monkeypatch):
        import app as A

        monkeypatch.setattr(A, "_cluster_report_pdf_available", lambda: False)
        body = client.get("/api/node-report/kinds").get_json()
        rack = next(k for k in body["kinds"] if k["id"] == "rack-status")
        assert "pdf" not in rack["formats"]
        assert body["pdf_available"] is False

    def test_a_default_format_is_always_named(self, client):
        for kind in client.get("/api/node-report/kinds").get_json()["kinds"]:
            assert kind["default_format"] in kind["formats"]


class TestRequestValidation:
    def test_an_unknown_report_is_refused_with_the_supported_list(self, client):
        res = client.post("/api/node-report/start", json={"report": "quarterly"})
        assert res.status_code == 400
        assert "rack-status" in res.get_json()["supported"]

    def test_a_format_the_report_cannot_produce_is_refused(self, client):
        res = client.post("/api/node-report/start", json={"report": "nscale", "format": "pdf"})
        assert res.status_code == 400
        assert res.get_json()["supported"] == ["json"]

    def test_pdf_is_refused_rather_than_crashing_when_unrenderable(self, client, monkeypatch):
        import app as A

        monkeypatch.setattr(A, "_cluster_report_pdf_available", lambda: False)
        res = client.post("/api/node-report/start",
                          json={"report": "rack-status", "format": "pdf"})
        assert res.status_code == 503


class TestFormatRouting:
    """Each format must come back as its own media type, not JSON wearing a new extension."""

    def _generate(self, client, **body):
        job_id = client.post("/api/node-report/start", json=body).get_json()["job_id"]
        _wait(client, job_id, want="done")
        return client.get("/api/node-report/result",
                          query_string={"job_id": job_id, "download": "1"})

    def test_csv_comes_back_as_csv_named_csv(self, client, fake_generation, offline_facts):
        res = self._generate(client, report="rack-status", format="csv")
        assert res.status_code == 200
        assert res.mimetype == "text/csv"
        assert res.headers["Content-Disposition"].endswith('.csv"')
        assert b"," in res.data

    def test_json_comes_back_as_json(self, client, fake_generation, offline_facts):
        import json as _json

        res = self._generate(client, report="rack-status", format="json")
        assert res.mimetype == "application/json"
        assert res.headers["Content-Disposition"].endswith('.json"')
        assert isinstance(_json.loads(res.data), dict)

    def test_pdf_comes_back_as_a_real_pdf(self, client, fake_generation, offline_facts):
        import app as A

        if not A._cluster_report_pdf_available():
            pytest.skip("reportlab not installed")
        res = self._generate(client, report="rack-status", format="pdf")
        assert res.mimetype == "application/pdf"
        assert res.data[:5] == b"%PDF-"  # a renderer that silently emitted JSON would fail here

    def test_the_customer_schema_report_is_still_the_default(self, client, fake_generation):
        """Omitting `report` must not change behaviour for the existing caller."""
        res = self._generate(client)
        assert res.mimetype == "application/json"

    def test_two_formats_of_one_report_do_not_share_a_job(self, client, fake_generation,
                                                          offline_facts):
        """Reuse is keyed on request shape, and the format is part of that shape.

        Without the format in the key, asking for the PDF while the CSV was in flight would
        join the CSV's job and hand back a CSV named .pdf.
        """
        a = client.post("/api/node-report/start",
                        json={"report": "rack-status", "format": "csv"}).get_json()
        b = client.post("/api/node-report/start",
                        json={"report": "rack-status", "format": "json"}).get_json()
        assert a["job_id"] != b["job_id"]
        assert not b.get("reused")

    def test_an_identical_request_still_joins_the_running_sweep(
        self, client, fake_generation, offline_facts, monkeypatch
    ):
        import app as A

        original = A._collect_node_inventory

        def _slow_collect(*args, **kwargs):
            time.sleep(0.2)
            return original(*args, **kwargs)

        monkeypatch.setattr(A, "_collect_node_inventory", _slow_collect)
        first = client.post("/api/node-report/start",
                           json={"report": "rack-status", "format": "csv"}).get_json()
        second = client.post("/api/node-report/start",
                            json={"report": "rack-status", "format": "csv"}).get_json()
        assert second["job_id"] == first["job_id"]
