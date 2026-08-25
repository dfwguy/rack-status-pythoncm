# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the help/knowledge content loader, API, and AI prompt enrichment."""
import json
import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import ai_chat
import help_content
from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _sse_content(raw: str) -> str:
    """Reassemble the streamed assistant text from an SSE response body."""
    out = []
    for line in raw.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "content" in obj:
            out.append(obj["content"])
    return "".join(out)


class TestHelpContentLoader:
    def test_loads_entries(self):
        entries = help_content.get_help_content()
        assert isinstance(entries, list)
        assert len(entries) > 0
        # Every entry has the fields the frontend/AI rely on.
        for e in entries:
            assert e.get("id")
            assert e.get("title")
            assert e.get("aiContext")
            assert e.get("aria", {}).get("label")

    def test_get_entry_by_id(self):
        entry = help_content.get_entry("dashboard")
        assert entry is not None
        assert entry["kind"] == "view"
        assert help_content.get_entry("does-not-exist") is None

    def test_ai_context_for_page(self):
        ctx = help_content.get_ai_context("dashboard")
        assert "Cluster overview" in ctx

    def test_ai_context_prefers_rack_detail(self):
        ctx = help_content.get_ai_context("singleRack", "rack-09")
        assert "Rack detail" in ctx
        assert "rack-09" in ctx


class TestHelpContentAPI:
    def test_list_endpoint(self, client):
        resp = client.get("/api/help/content")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["count"] == len(data["entries"])
        assert data["count"] > 0

    def test_entry_endpoint(self, client):
        resp = client.get("/api/help/content/scheduler")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["entry"]["id"] == "scheduler"

    def test_restored_workflow_help_is_available(self, client):
        resp = client.get("/api/help/content/workflows")
        assert resp.status_code == 200
        assert resp.get_json()["entry"]["id"] == "workflows"

    def test_entry_not_found(self, client):
        resp = client.get("/api/help/content/nope")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False


class TestDemoPageAwareHelp:
    def test_demo_answers_from_registry_for_page(self, client):
        resp = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "What information is available in this dashboard?"}],
            "context": {"page": "dashboard"},
        })
        assert resp.status_code == 200
        data = _sse_content(resp.get_data(as_text=True))
        # Should surface the dashboard registry content, not the generic fallback.
        assert "Cluster overview" in data
        assert "What you can do here" in data

    def test_demo_page_help_prefers_rack_detail(self, client):
        resp = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "what can I do here?"}],
            "context": {"page": "singleRack", "rack": "p34-r07"},
        })
        assert resp.status_code == 200
        data = _sse_content(resp.get_data(as_text=True))
        assert "Rack detail" in data
        assert "p34-r07" in data

    def test_demo_topic_still_matches(self, client):
        resp = client.post("/api/ai/chat", json={
            "messages": [{"role": "user", "content": "How do I reset a GPU Xid error?"}],
            "context": {"page": "dashboard"},
        })
        assert resp.status_code == 200
        data = _sse_content(resp.get_data(as_text=True)).lower()
        assert "gpu" in data


class TestSystemPromptEnrichment:
    def test_uses_pushed_additional_context(self):
        prompt = ai_chat.build_system_prompt(
            {"page": "dashboard", "additional_context": "SENTINEL-CONTEXT"}
        )
        assert "SENTINEL-CONTEXT" in prompt

    def test_falls_back_to_registry(self):
        prompt = ai_chat.build_system_prompt({"page": "workflows"})
        # The workflows registry entry text should appear via the fallback path.
        assert "workflow" in prompt.lower()

    def test_custom_rules_appended(self):
        prompt = ai_chat.build_system_prompt(
            {"page": "dashboard"}, custom_rules="ALWAYS-BE-DECIDING"
        )
        assert "ALWAYS-BE-DECIDING" in prompt
