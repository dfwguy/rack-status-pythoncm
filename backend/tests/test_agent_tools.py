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
"""Tests for the agent tool registry (agent/tools.py + registrations.py)."""
import re
from pathlib import Path

from agent import registrations
from agent import tools as agent_tools


def setup_module(_m):
    registrations.register_all()


def test_every_referenced_app_name_resolves():
    """Every ``_app.<name>`` in registrations.py must resolve, wherever the route now lives.

    Handlers resolve lazily, so registration succeeding proves nothing: a view that moved into a
    ``backend/api/`` blueprint fails only when the tool is actually called. Four already moved
    (``api_slurm_squeue``, ``api_slurm_sacct_recent``, ``api_workflow_run``,
    ``api_workflow_run_advance``), and the next extraction would break more silently. Reading the
    names out of the source keeps this list from going stale.
    """
    source = Path(registrations.__file__).read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"_app\.([A-Za-z_][A-Za-z0-9_]*)", source)))
    assert names, "found no _app.<name> references — did the wiring change shape?"

    unresolved = []
    for name in names:
        try:
            getattr(registrations._app, name)
        except AttributeError as exc:
            unresolved.append(f"{name}: {exc}")
    assert not unresolved, "unresolved app references: " + "; ".join(unresolved)


def test_expected_tools_registered():
    names = set(agent_tools.REGISTRY)
    for expected in (
        "get_cluster_overview",
        "get_rack_status",
        "list_squeue",
        "list_recent_jobs",
        "get_action_status",
        "scancel_job",
        "drain_rack",
        "run_validation",
        "run_nemo_xrack",
        "run_workflow",
    ):
        assert expected in names, f"missing tool {expected}"


def test_openai_schemas_wellformed():
    for schema in agent_tools.openai_tool_schemas():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] and isinstance(fn["description"], str)
        assert fn["parameters"]["type"] == "object"


def test_read_vs_action_flags():
    assert agent_tools.get_tool("get_cluster_overview").side_effect is False
    assert agent_tools.get_tool("scancel_job").side_effect is True
    assert agent_tools.get_tool("scancel_job").category == "action"


def test_side_effect_refused_without_permission():
    res = agent_tools.execute("scancel_job", {"job_id": "1"}, allow_side_effects=False)
    assert res["ok"] is False
    assert "side effect" in res["error"].lower()


def test_unknown_tool():
    res = agent_tools.execute("nope_not_a_tool", {})
    assert res["ok"] is False
    assert "unknown tool" in res["error"].lower()


def test_disabled_env_gates_tool(monkeypatch):
    monkeypatch.setenv("RACK_SLURM_SCANCEL_DISABLED", "1")
    tool = agent_tools.get_tool("scancel_job")
    assert tool.is_available() is False
    assert tool not in agent_tools.available_tools()
    res = agent_tools.execute("scancel_job", {"job_id": "1"})
    assert res["ok"] is False
    assert "disabled" in res["error"].lower()


def test_read_tool_executes_via_request_context():
    # get_cluster_overview runs the real view under a nested test request context.
    res = agent_tools.execute("get_cluster_overview", {})
    assert res.get("ok") is True
    assert "rack_count" in res

    wf = agent_tools.execute("list_workflows", {})
    assert wf.get("ok") is True
    assert isinstance(wf.get("workflows"), list)
