# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for AI and Workflow API endpoints."""
import os
import time

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import workflow_engine
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def tmp_workflow_dirs(tmp_path, monkeypatch):
    """Isolate workflow storage per test."""
    wf_dir = tmp_path / "workflows"
    runs_dir = wf_dir / "runs"
    wf_dir.mkdir()
    runs_dir.mkdir()
    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", wf_dir)
    monkeypatch.setattr(workflow_engine, "RUNS_DIR", runs_dir)
    yield wf_dir


class TestAIChatAPI:
    def test_ai_chat_stream(self, client):
        resp = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "What is the rack status?"}],
            "context": {"page": "dashboard"},
        })
        assert resp.status_code == 200
        assert resp.content_type == "text/event-stream; charset=utf-8"
        data = resp.get_data(as_text=True)
        assert "data: " in data
        assert "[DONE]" in data

    def test_ai_chat_no_messages(self, client):
        resp = client.post("/api/ai/chat", json={"messages": [], "context": {}})
        assert resp.status_code == 400

    def test_ai_chat_missing_body(self, client):
        resp = client.post("/api/ai/chat", json={})
        assert resp.status_code == 400


class TestAISettingsAPI:
    def test_ai_settings_get(self, client):
        resp = client.get("/api/ai/settings")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "endpoint" in data
        assert "model" in data

    def test_ai_settings_put(self, client, tmp_path, monkeypatch):
        import ai_chat
        monkeypatch.setattr(ai_chat, "AI_SETTINGS_FILE", tmp_path / "ai_settings.json")
        resp = client.put("/api/ai/settings", json={"endpoint": "http://test", "model": "test-model"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["endpoint"] == "http://test"
        assert data["model"] == "test-model"


class TestAITestAPI:
    def test_ai_test_no_key(self, client, tmp_path, monkeypatch):
        import ai_chat
        monkeypatch.setattr(ai_chat, "AI_SETTINGS_FILE", tmp_path / "ai_settings.json")
        monkeypatch.delenv("RACK_AI_API_KEY", raising=False)
        resp = client.post("/api/ai/test")
        assert resp.status_code == 502
        data = resp.get_json()
        assert data["ok"] is False


class TestWorkflowsAPI:
    def test_workflows_list_empty(self, client):
        resp = client.get("/api/workflows")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert isinstance(data["workflows"], list)

    def test_workflow_crud(self, client):
        resp = client.post("/api/workflows", json={
            "name": "API Test WF",
            "steps": [{"id": "s1", "type": "automated", "name": "Step"}],
            "mermaid": "graph TD\n    s1[\"Step\"]",
        })
        assert resp.status_code == 200
        wf = resp.get_json()["workflow"]
        wf_id = wf["id"]
        assert wf["name"] == "API Test WF"

        resp = client.get(f"/api/workflows/{wf_id}")
        assert resp.status_code == 200
        assert resp.get_json()["workflow"]["name"] == "API Test WF"

        resp = client.delete(f"/api/workflows/{wf_id}")
        assert resp.status_code == 200

        resp = client.get(f"/api/workflows/{wf_id}")
        assert resp.status_code == 404

    def test_workflow_create_no_name(self, client):
        resp = client.post("/api/workflows", json={"steps": []})
        assert resp.status_code == 400


class TestWorkflowRunAPI:
    def test_workflow_run_lifecycle(self, client):
        client.post("/api/workflows", json={
            "id": "run-test",
            "name": "Run Test",
            "steps": [
                {"id": "a", "type": "automated", "name": "Auto", "command": ""},
            ],
        })

        resp = client.post("/api/workflows/run-test/run", json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        run_id = data["run_id"]

        time.sleep(0.5)

        resp = client.get(f"/api/workflow-runs/{run_id}")
        assert resp.status_code == 200
        run = resp.get_json()["run"]
        assert run["state"] == "completed"

    def test_workflow_run_not_found(self, client):
        resp = client.post("/api/workflows/nonexistent/run", json={})
        assert resp.status_code == 400

    def test_workflow_runs_list(self, client):
        resp = client.get("/api/workflow-runs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert isinstance(data["runs"], list)


class TestToolsAvailableAPI:
    def test_tools_available(self, client):
        resp = client.get("/api/tools/available")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        tools = data["tools"]
        assert len(tools) > 0
        assert any(t["id"] == "RACK_SYSINFO_SCRIPT" for t in tools)
        assert all("category" in t for t in tools)
        assert all("label" in t for t in tools)
