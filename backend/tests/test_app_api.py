"""Tests for Flask API endpoints."""
import os
import json

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from app import app

import rack_locks


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def isolated_locks_file(tmp_path, monkeypatch):
    """Redirect the claims file so rack-locks tests don't leak into ./var/cache."""
    monkeypatch.setattr(rack_locks, "CLAIMS_FILE", tmp_path / "rack_locks.json")
    yield


class TestAuthEndpoints:
    def test_auth_check_when_disabled(self, client):
        resp = client.get("/api/auth-check")
        assert resp.status_code == 200

    def test_login_when_disabled(self, client):
        resp = client.post("/api/login", json={"password": "anything"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True


class TestStatusEndpoint:
    def test_demo_status(self, client):
        resp = client.get("/api/status?log_dir=var/logs/")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["rows"]) == 18
        assert "summary" in data
        assert "comments" in data

    def test_status_has_native_workers(self, client):
        resp = client.get("/api/status?log_dir=var/logs/")
        data = resp.get_json()
        assert data["native_workers"]["mode"] == "demo"


class TestParserResultsDemo:
    def test_demo_parser_results(self, client):
        resp = client.get("/api/parser-results?rack=p5-r01&job_id=123")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "data" in data
        nccl = data["data"]["nccl_peak_out_of_place_busbw_gbps"]
        assert len(nccl) > 0
        assert "all_reduce_busbw_series" in nccl[0]
        nemo = data["data"]["nemotron_5h_56b_train_window"]
        assert len(nemo) > 0
        assert "step_time_series" in nemo[0]

    def test_parser_results_deterministic(self, client):
        r1 = client.get("/api/parser-results?rack=p5-r01&job_id=456").get_json()
        r2 = client.get("/api/parser-results?rack=p5-r01&job_id=456").get_json()
        assert r1["data"] == r2["data"]

    def test_parser_results_different_per_rack(self, client):
        r1 = client.get("/api/parser-results?rack=p5-r01&job_id=1").get_json()
        r2 = client.get("/api/parser-results?rack=p5-r02&job_id=1").get_json()
        peak1 = r1["data"]["nccl_peak_out_of_place_busbw_gbps"][0]["peak_busbw_gbps"]
        peak2 = r2["data"]["nccl_peak_out_of_place_busbw_gbps"][0]["peak_busbw_gbps"]
        assert peak1 != peak2


class TestCommentsAPI:
    def test_get_comments(self, client):
        resp = client.get("/api/comments")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "comments" in data


class TestSPAServing:
    def test_spa_index(self, client):
        from pathlib import Path
        spa_index = Path(__file__).resolve().parent.parent.parent / "static" / "spa" / "index.html"
        if not spa_index.is_file():
            pytest.skip("SPA not built (static/spa/ missing in CI)")
        resp = client.get("/spa/")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()


class TestRackLocksAPI:
    def test_get_snapshot_shape(self, client, isolated_locks_file):
        resp = client.get("/api/rack-locks", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["me"] == "alice"
        assert "now" in data and "locks" in data
        assert isinstance(data["locks"], dict)

    def test_post_creates_claim(self, client, isolated_locks_file):
        resp = client.post(
            "/api/rack-locks/p5-r01",
            headers={"X-Rack-Operator": "alice"},
            json={"note": "planned"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "alice"
        assert data["claim"]["note"] == "planned"

        snap = client.get("/api/rack-locks", headers={"X-Rack-Operator": "alice"}).get_json()
        assert "p5-r01" in snap["locks"]
        assert snap["locks"]["p5-r01"]["operator"] == "alice"
        assert snap["locks"]["p5-r01"]["kind"] == "claim"

    def test_post_without_operator_header_400(self, client, isolated_locks_file):
        resp = client.post("/api/rack-locks/p5-r01", json={})
        assert resp.status_code == 400

    def test_post_conflict_returns_409(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "bob"})
        assert resp.status_code == 409

    def test_post_force_takeover_succeeds(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.post(
            "/api/rack-locks/p5-r01",
            headers={"X-Rack-Operator": "bob"},
            json={"force": True, "note": "alice left"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "bob"
        assert data["claim"]["stolen_from"] == "alice"
        assert data["claim"]["note"] == "alice left"

    def test_post_invalid_rack_400(self, client, isolated_locks_file):
        resp = client.post(
            "/api/rack-locks/not-a-rack",
            headers={"X-Rack-Operator": "alice"},
        )
        assert resp.status_code == 400

    def test_delete_by_owner_succeeds(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["removed"] is True

    def test_delete_by_other_returns_403(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "bob"})
        assert resp.status_code == 403

    def test_delete_nonexistent_returns_ok_not_removed(self, client, isolated_locks_file):
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        assert resp.get_json()["removed"] is False
