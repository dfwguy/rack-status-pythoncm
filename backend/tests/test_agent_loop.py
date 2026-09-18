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
"""Tests for the agent tool-calling loop (agent/loop.py).

The LLM is mocked (``ai_chat.chat_with_tools``) and the registry is replaced with harmless
fake tools so we can drive read-inline / action-pause-resume / max-steps behavior without any
real cluster side effects.
"""
import ai_chat
import pytest
from agent import loop, registrations
from agent import tools as agent_tools
from agent.tools import AgentTool


@pytest.fixture()
def fake_registry(monkeypatch):
    """Replace REGISTRY with a read tool + an action tool; skip real registration."""
    saved = dict(agent_tools.REGISTRY)
    agent_tools.REGISTRY.clear()
    agent_tools.register(AgentTool(
        name="ping", description="echo", category="read", side_effect=False,
        handler=lambda a: {"ok": True, "pong": a.get("x", "hi")},
    ))
    agent_tools.register(AgentTool(
        name="do_thing", description="mutate", category="action", side_effect=True,
        handler=lambda a: {"ok": True, "job_id": "J1"},
    ))
    # register_all() is a no-op so it won't repopulate real tools over our fakes.
    monkeypatch.setattr(registrations, "_registered", True)
    yield
    agent_tools.REGISTRY.clear()
    agent_tools.REGISTRY.update(saved)


def _script(monkeypatch, responses):
    """Make ai_chat.chat_with_tools return each scripted response in turn."""
    calls = {"i": 0}

    def fake(messages, schemas, catalog):
        r = responses[min(calls["i"], len(responses) - 1)]
        calls["i"] += 1
        return r
    monkeypatch.setattr(ai_chat, "chat_with_tools", fake)
    return calls


def _tool_resp(name, args, tid="c1"):
    return {
        "content": "",
        "tool_calls": [{"id": tid, "name": name, "args": args}],
        "assistant_message": {"role": "assistant", "content": "", "tool_calls": []},
        "mode": "native",
    }


def _answer(text):
    return {"content": text, "tool_calls": [], "assistant_message": {"role": "assistant", "content": text}, "mode": "native"}


def test_read_tool_runs_inline_then_answers(fake_registry, monkeypatch):
    _script(monkeypatch, [_tool_resp("ping", {"x": "yo"}), _answer("done reading")])
    events = list(loop.run_turn([{"role": "user", "content": "hi"}], operator="tester"))
    types = [e["type"] for e in events]
    assert "tool_call" in types and "tool_result" in types
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["tool"] == "ping" and tr["ok"] is True
    token = next(e for e in events if e["type"] == "token")
    assert "done reading" in token["content"]
    assert events[-1]["type"] == "done"


def test_action_pauses_for_approval(fake_registry, monkeypatch):
    _script(monkeypatch, [_tool_resp("do_thing", {"rack": "r1"}, tid="c9")])
    events = list(loop.run_turn([{"role": "user", "content": "do it"}], operator="tester"))
    appr = [e for e in events if e["type"] == "approval_required"]
    assert len(appr) == 1
    ev = appr[0]
    assert ev["tool"] == "do_thing" and ev["run_id"] and ev["call_id"] == "c9"
    # No 'done' after pause — the turn is suspended awaiting approval.
    assert all(e["type"] != "done" for e in events)


def test_approve_executes_then_continues(fake_registry, monkeypatch):
    _script(monkeypatch, [_tool_resp("do_thing", {"rack": "r1"}, tid="c9")])
    events = list(loop.run_turn([{"role": "user", "content": "do it"}], operator="tester"))
    run_id = next(e for e in events if e["type"] == "approval_required")["run_id"]

    # after approval the model gives a final answer
    _script(monkeypatch, [_answer("action complete")])
    resumed = list(loop.resume_turn(run_id, "c9", "approve", operator="tester"))
    tr = [e for e in resumed if e["type"] == "tool_result"]
    assert tr and tr[0]["tool"] == "do_thing" and tr[0]["ok"] is True
    token = next(e for e in resumed if e["type"] == "token")
    assert "action complete" in token["content"]
    assert resumed[-1]["type"] == "done"


def test_deny_does_not_execute(fake_registry, monkeypatch):
    executed = {"n": 0}
    agent_tools.REGISTRY["do_thing"].handler = lambda a: (executed.__setitem__("n", executed["n"] + 1) or {"ok": True})
    _script(monkeypatch, [_tool_resp("do_thing", {"rack": "r1"}, tid="c9")])
    events = list(loop.run_turn([{"role": "user", "content": "do it"}], operator="tester"))
    run_id = next(e for e in events if e["type"] == "approval_required")["run_id"]

    _script(monkeypatch, [_answer("ok, cancelled")])
    resumed = list(loop.resume_turn(run_id, "c9", "deny", operator="tester"))
    assert executed["n"] == 0  # handler never ran
    tr = next(e for e in resumed if e["type"] == "tool_result")
    assert tr["ok"] is False


def test_max_steps_cap(fake_registry, monkeypatch):
    monkeypatch.setenv("RACK_AI_TOOLS_MAX_STEPS", "2")
    # model always asks for the read tool -> never answers -> must stop at the cap
    _script(monkeypatch, [_tool_resp("ping", {})])
    events = list(loop.run_turn([{"role": "user", "content": "loop"}], operator="t"))
    assert events[-1]["type"] == "done"
    last_token = [e for e in events if e["type"] == "token"][-1]
    assert "limit" in last_token["content"].lower()


def test_expired_resume():
    events = list(loop.resume_turn("nope", "c1", "approve"))
    assert events[0]["type"] == "error"
    assert "expired" in events[0]["error"].lower()


def test_json_directive_parsing():
    d = ai_chat._parse_json_tool_directive('sure: {"tool": "ping", "args": {"x": 1}} done')
    assert d and d["name"] == "ping" and d["args"] == {"x": 1}
    assert ai_chat._parse_json_tool_directive("no tool here") is None
