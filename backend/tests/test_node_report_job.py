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

    def _collect(_fleet, progress=None):
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

        def _slow(_fleet, progress=None):
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
