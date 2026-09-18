# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for ai_chat.py module."""
import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import ai_chat


@pytest.fixture(autouse=True)
def tmp_settings(tmp_path, monkeypatch):
    """Use a temp file for AI settings during tests."""
    settings_file = tmp_path / "ai_settings.json"
    monkeypatch.setattr(ai_chat, "AI_SETTINGS_FILE", settings_file)
    yield settings_file


class TestSettings:
    def test_load_settings_empty(self):
        result = ai_chat._load_settings()
        assert result == {}

    def test_save_and_load_settings(self, tmp_settings):
        ai_chat._save_settings({"endpoint": "http://test", "api_key": "key123"})
        loaded = ai_chat._load_settings()
        assert loaded["endpoint"] == "http://test"
        assert loaded["api_key"] == "key123"

    def test_get_ai_settings_defaults(self):
        settings = ai_chat.get_ai_settings()
        assert settings["endpoint"] == ai_chat._DEFAULT_ENDPOINT
        assert settings["model"] == ai_chat._DEFAULT_MODEL
        assert settings["api_key_configured"] is False
        assert settings["api_key_masked"] == ""

    def test_update_ai_settings(self, tmp_settings):
        ai_chat.update_ai_settings({"endpoint": "http://custom", "model": "llama-3"})
        settings = ai_chat.get_ai_settings()
        assert settings["endpoint"] == "http://custom"
        assert settings["model"] == "llama-3"


class TestAIEnabled:
    def test_is_ai_enabled_no_key(self):
        assert ai_chat.is_ai_enabled() is False

    def test_is_ai_enabled_with_key(self, tmp_settings, monkeypatch):
        ai_chat._save_settings({"api_key": "test-key-12345678"})
        assert ai_chat.is_ai_enabled() is True

    def test_is_ai_enabled_with_env_key(self, monkeypatch):
        monkeypatch.setenv("RACK_AI_API_KEY", "env-key-12345678")
        assert ai_chat.is_ai_enabled() is True


class TestMaskKey:
    def test_mask_key_short(self):
        assert ai_chat._mask_key("short") == ""

    def test_mask_key_long(self):
        masked = ai_chat._mask_key("sk-1234567890abcdef")
        assert masked.startswith("sk-1")
        assert masked.endswith("cdef")
        assert "•" in masked


class TestBuildSystemPrompt:
    def test_base_prompt_included(self):
        prompt = ai_chat.build_system_prompt({})
        assert "NVIS Greenlight AI Assistant" in prompt

    def test_dashboard_context(self):
        # Page context is now sourced from the help-content registry.
        prompt = ai_chat.build_system_prompt({"page": "dashboard"})
        assert "dashboard" in prompt.lower()
        assert "Cluster overview" in prompt

    def test_rack_context(self):
        prompt = ai_chat.build_system_prompt({"page": "singleRack", "rack": "p34-r07"})
        assert "p34-r07" in prompt

    def test_slurm_context(self):
        prompt = ai_chat.build_system_prompt({"page": "slurm"})
        assert "Slurm" in prompt

    def test_custom_rules_appended(self):
        prompt = ai_chat.build_system_prompt({}, custom_rules="Always answer in French")
        assert "Always answer in French" in prompt

    def test_live_inventory_injected(self):
        block = "Live cluster inventory: - Compute racks: 8 - Compute nodes: 144 - GPUs: 576"
        prompt = ai_chat.build_system_prompt(
            {"page": "singleRack", "rack": "bos01-b08", "live_inventory": block}
        )
        assert block in prompt

    def test_live_inventory_absent_when_not_provided(self):
        previous = ai_chat._live_inventory_provider
        ai_chat.set_live_inventory_provider(None)
        try:
            prompt = ai_chat.build_system_prompt({"page": "dashboard"})
            assert "Live cluster inventory" not in prompt
        finally:
            ai_chat.set_live_inventory_provider(previous)

    def test_live_inventory_from_registered_provider(self):
        """app.py registers the builder; the prompt must pick it up without the route passing it.

        This is the wire that makes cluster-size questions answerable at all: with it dropped,
        nothing raises -- the assistant just silently loses the deployed counts again.
        """
        previous = ai_chat._live_inventory_provider
        ai_chat.set_live_inventory_provider(lambda: "Live cluster inventory: 5 racks, 90 nodes")
        try:
            prompt = ai_chat.build_system_prompt({"page": "dashboard"})
            assert "5 racks, 90 nodes" in prompt
        finally:
            ai_chat.set_live_inventory_provider(previous)

    def test_explicit_live_inventory_wins_over_provider(self):
        previous = ai_chat._live_inventory_provider
        ai_chat.set_live_inventory_provider(lambda: "from-provider")
        try:
            prompt = ai_chat.build_system_prompt({"live_inventory": "from-caller"})
            assert "from-caller" in prompt
            assert "from-provider" not in prompt
        finally:
            ai_chat.set_live_inventory_provider(previous)


class TestDemoStreamResponse:
    def test_gpu_topic(self):
        messages = [{"role": "user", "content": "How do I reset a GPU?"}]
        output = "".join(ai_chat.demo_stream_response(messages))
        assert "GPU" in output or "gpu" in output.lower()

    def test_slurm_topic(self):
        messages = [{"role": "user", "content": "Show drain nodes in slurm"}]
        output = "".join(ai_chat.demo_stream_response(messages))
        assert "Slurm" in output or "slurm" in output.lower() or "drain" in output.lower()

    def test_mermaid_topic(self):
        messages = [{"role": "user", "content": "Generate a Mermaid flowchart for GPU troubleshooting. Output ONLY the mermaid code starting with graph TD."}]
        output = "".join(ai_chat.demo_stream_response(messages))
        assert "graph TD" in output

    def test_nvlink_topic(self):
        messages = [{"role": "user", "content": "Check NVLink status"}]
        output = "".join(ai_chat.demo_stream_response(messages))
        assert "NVLink" in output or "nvlink" in output.lower()

    def test_fallback_topic(self):
        messages = [{"role": "user", "content": "Hello!"}]
        output = "".join(ai_chat.demo_stream_response(messages))
        assert "help" in output.lower() or "can" in output.lower()
