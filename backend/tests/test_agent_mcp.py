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
"""Tests for the MCP facade (agent/mcp_server.py) — transport-agnostic helpers."""
from agent import mcp_server, registrations


def setup_module(_m):
    registrations.register_all()


def test_writes_hidden_by_default(monkeypatch):
    monkeypatch.delenv("RACK_MCP_ALLOW_WRITES", raising=False)
    names = {t["name"] for t in mcp_server.list_tools_payload()}
    assert "get_cluster_overview" in names  # reads exposed
    assert "scancel_job" not in names  # actions hidden
    assert "run_nemo_xrack" not in names


def test_writes_shown_when_allowed(monkeypatch):
    monkeypatch.setenv("RACK_MCP_ALLOW_WRITES", "1")
    names = {t["name"] for t in mcp_server.list_tools_payload()}
    assert "scancel_job" in names
    # inputSchema is present and object-typed for MCP clients
    schema = next(t for t in mcp_server.list_tools_payload() if t["name"] == "scancel_job")
    assert schema["inputSchema"]["type"] == "object"


def test_call_read_tool_ok():
    res = mcp_server.call_tool("get_cluster_overview", {})
    assert res.get("ok") is True


def test_call_action_refused_without_writes(monkeypatch):
    monkeypatch.delenv("RACK_MCP_ALLOW_WRITES", raising=False)
    res = mcp_server.call_tool("scancel_job", {"job_id": "1"})
    assert res["ok"] is False
    assert "RACK_MCP_ALLOW_WRITES" in res["error"]


def test_call_unknown_tool():
    res = mcp_server.call_tool("does_not_exist", {})
    assert res["ok"] is False
