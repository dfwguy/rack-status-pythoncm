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
"""The LLM tool-calling loop for the assistant.

A "turn" is driven server-side: the model is asked (with tool schemas) what to do; read
tools run inline and their results are fed back; the FIRST action (side-effecting) tool
call pauses the turn, emits an ``approval_required`` event, and persists the remaining work
in :data:`_PENDING` keyed by a ``run_id``. The client shows an approval card and calls the
resume endpoint, which executes (or denies) the action and continues the loop.

The loop yields plain event dicts; the Flask route serializes them to SSE. Tool decisions
use a NON-streaming LLM call (reliable tool-call parsing across providers); the final answer
text is emitted as a ``token`` event.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Generator
from typing import Any

from . import audit, registrations
from . import tools as agent_tools

_logger = logging.getLogger(__name__)

# run_id -> {messages, queue, operator, mode, created_at}
_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()
_PENDING_TTL_SEC = 1800

_TRUTHY = ("1", "true", "yes", "on")

Event = dict[str, Any]


def tools_enabled() -> bool:
    if (os.environ.get("RACK_AI_TOOLS_MODE") or "").strip().lower() == "off":
        return False
    v = (os.environ.get("RACK_AI_TOOLS_ENABLED") or "").strip().lower()
    if v:
        return v in _TRUTHY
    return True  # default on whenever AI is enabled


def _max_steps() -> int:
    try:
        return max(1, int(os.environ.get("RACK_AI_TOOLS_MAX_STEPS", "6") or 6))
    except ValueError:
        return 6


def _prune() -> None:
    now = time.time()
    with _PENDING_LOCK:
        stale = [k for k, v in _PENDING.items() if now - v.get("created_at", 0) > _PENDING_TTL_SEC]
        for k in stale:
            _PENDING.pop(k, None)


def _summ(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _action_summary(name: str, args: dict) -> str:
    a = {k: v for k, v in (args or {}).items() if not k.startswith("_")}
    parts = ", ".join(f"{k}={v}" for k, v in a.items())
    return f"{name}({parts})"


def _result_summary(name: str, result: dict) -> str:
    if not isinstance(result, dict):
        return _summ(str(result))
    if not result.get("ok", True):
        return f"error: {_summ(result.get('error') or result.get('raw') or 'failed')}"
    for key in ("job_id", "run_id"):
        if result.get(key):
            return f"started ({key}={result[key]}, phase={result.get('phase', 'starting')})"
    if name == "get_cluster_overview":
        return _summ(
            f"{result.get('rack_count', '?')} racks, {result.get('tray_hosts', '?')} nodes, "
            f"~{result.get('estimated_gpu_count', '?')} GPUs"
        )
    if "racks" in result and isinstance(result["racks"], list):
        return f"{len(result['racks'])} racks"
    if "jobs" in result and isinstance(result["jobs"], list):
        return f"{len(result['jobs'])} jobs"
    return "ok"


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return (m.get("content") or "").lower()
    return ""


# --------------------------------------------------------------------------- live loop


def run_turn(base_messages: list[dict], *, operator: str = "") -> Generator[Event, None, None]:
    """Drive a fresh user turn. Yields event dicts."""
    registrations.register_all()
    import ai_chat

    schemas = agent_tools.openai_tool_schemas()
    catalog = agent_tools.tools_text_catalog()
    messages = list(base_messages)
    yield from _drive(messages, operator, ai_chat, schemas, catalog)


def _drive(messages, operator, ai_chat, schemas, catalog) -> Generator[Event, None, None]:
    for _step in range(_max_steps()):
        try:
            out = ai_chat.chat_with_tools(messages, schemas, catalog)
        except Exception as exc:  # any provider failure ends the turn cleanly
            _logger.warning("agent turn aborted: %s", exc, exc_info=True)
            yield {"type": "error", "error": str(exc)}
            yield {"type": "done"}
            return
        tool_calls = out.get("tool_calls") or []
        if not tool_calls:
            yield {"type": "token", "content": out.get("content") or ""}
            yield {"type": "done"}
            return
        messages.append(out["assistant_message"])
        status = yield from _process_calls(messages, tool_calls, operator, ai_chat, out.get("mode", "native"))
        if status == "paused":
            return
    yield {"type": "token", "content": "(Stopped: reached the tool-step limit. Ask me to continue if needed.)"}
    yield {"type": "done"}


def _process_calls(messages, tool_calls, operator, ai_chat, mode) -> Generator[Event, None, str]:
    """Run read tools inline; pause on the first action tool. Returns 'paused' | 'continue'."""
    for idx, tc in enumerate(tool_calls):
        tool = agent_tools.get_tool(tc["name"])
        if tool is None:
            res = {"ok": False, "error": f"unknown tool {tc['name']!r}"}
            messages.append(ai_chat.tool_result_message(mode, tc["id"], tc["name"], res))
            yield {"type": "tool_result", "tool": tc["name"], "ok": False, "summary": "unknown tool"}
            continue
        if tool.side_effect:
            run_id = f"agent_{uuid.uuid4().hex[:12]}"
            _prune()
            with _PENDING_LOCK:
                _PENDING[run_id] = {
                    "messages": messages,
                    "queue": list(tool_calls[idx:]),
                    "operator": operator,
                    "mode": mode,
                    "created_at": time.time(),
                }
            audit.record("approval_required", run_id=run_id, tool=tc["name"], args=tc.get("args"), operator=operator)
            yield {
                "type": "approval_required",
                "run_id": run_id,
                "call_id": tc["id"],
                "tool": tc["name"],
                "category": tool.category,
                "args": tc.get("args") or {},
                "summary": _action_summary(tc["name"], tc.get("args") or {}),
            }
            return "paused"
        # read/eval tool -> run now
        yield {"type": "tool_call", "tool": tc["name"], "category": tool.category, "args": tc.get("args") or {}}
        args = dict(tc.get("args") or {})
        args["_operator"] = operator
        res = agent_tools.execute(tc["name"], args, allow_side_effects=False)
        audit.record("tool_call", tool=tc["name"], args=tc.get("args"), ok=res.get("ok"), operator=operator)
        yield {"type": "tool_result", "tool": tc["name"], "ok": bool(res.get("ok", True)), "summary": _result_summary(tc["name"], res)}
        messages.append(ai_chat.tool_result_message(mode, tc["id"], tc["name"], res))
    return "continue"


def resume_turn(
    run_id: str,
    call_id: str,
    decision: str,
    edited_args: dict | None = None,
    *,
    operator: str = "",
) -> Generator[Event, None, None]:
    """Continue a paused turn after the user approves or denies the pending action."""
    with _PENDING_LOCK:
        pend = _PENDING.pop(run_id, None)
    if not pend:
        yield {"type": "error", "error": "approval expired or unknown run_id"}
        yield {"type": "done"}
        return
    registrations.register_all()
    import ai_chat

    messages = pend["messages"]
    queue = pend["queue"] or []
    mode = pend.get("mode", "native")
    op = operator or pend.get("operator", "")
    schemas = agent_tools.openai_tool_schemas()
    catalog = agent_tools.tools_text_catalog()
    if not queue:
        yield from _drive(messages, op, ai_chat, schemas, catalog)
        return

    tc = queue[0]
    # Defensive: the client echoes the call_id it was shown. If it doesn't match the pending
    # call (e.g. a stale/duplicate approval), refuse rather than act on the wrong action.
    if call_id and tc.get("id") and call_id != tc["id"]:
        audit.record("approval_mismatch", run_id=run_id, expected=tc.get("id"), got=call_id, operator=op)
        yield {"type": "error", "error": "approval did not match the pending action; please retry"}
        yield {"type": "done"}
        return
    tool = agent_tools.get_tool(tc["name"])
    if decision != "approve":
        audit.record("denied", run_id=run_id, tool=tc["name"], operator=op)
        res = {"ok": False, "error": "the user DENIED this action; do not retry it — explain alternatives instead"}
        messages.append(ai_chat.tool_result_message(mode, tc["id"], tc["name"], res))
        yield {"type": "tool_result", "tool": tc["name"], "ok": False, "summary": "Denied by user"}
    else:
        args = dict(edited_args if isinstance(edited_args, dict) else (tc.get("args") or {}))
        args["_operator"] = op
        shown = {k: v for k, v in args.items() if not k.startswith("_")}
        yield {"type": "tool_call", "tool": tc["name"], "category": (tool.category if tool else "action"), "args": shown}
        res = agent_tools.execute(tc["name"], args, allow_side_effects=True)
        audit.record("approved_executed", run_id=run_id, tool=tc["name"], args=shown, ok=res.get("ok"), operator=op)
        yield {"type": "tool_result", "tool": tc["name"], "ok": bool(res.get("ok", True)), "summary": _result_summary(tc["name"], res)}
        messages.append(ai_chat.tool_result_message(mode, tc["id"], tc["name"], res))

    remaining = queue[1:]
    if remaining:
        status = yield from _process_calls(messages, remaining, op, ai_chat, mode)
        if status == "paused":
            return
    yield from _drive(messages, op, ai_chat, schemas, catalog)


# --------------------------------------------------------------------------- demo


def run_demo_turn(
    base_messages: list[dict], *, context: dict | None = None, operator: str = ""
) -> Generator[Event, None, None]:
    """Scripted agent demo when no LLM is configured (RACK_STATUS_DEMO=1).

    Showcases the tool loop for cluster-size/status questions by actually calling a read
    tool; otherwise streams the existing keyword/page-aware demo answer (preserving the
    page/rack context so page-aware help still works).
    """
    registrations.register_all()
    import ai_chat

    text = _last_user_text(base_messages)
    is_help_intent = any(re.search(p, text) for p in ai_chat._HELP_INTENT_PATTERNS)
    if not is_help_intent and re.search(r"cluster size|how big|how many (racks|nodes|gpu)|deployed gpus", text):
        name = "get_cluster_overview"
        yield {"type": "tool_call", "tool": name, "category": "read", "args": {}}
        res = agent_tools.execute(name, {"_operator": operator}, allow_side_effects=False)
        yield {"type": "tool_result", "tool": name, "ok": bool(res.get("ok", True)), "summary": _result_summary(name, res)}
        answer = (
            f"Based on the live inventory: {res.get('rack_count', '?')} compute racks, "
            f"{res.get('tray_hosts', '?')} nodes, ~{res.get('estimated_gpu_count', '?')} GPUs "
            f"(cluster {res.get('cluster_name', '?')})."
        )
        for w in answer.split(" "):
            yield {"type": "token", "content": w + " "}
            time.sleep(0.01)
        yield {"type": "done"}
        return
    # fallback: reuse the keyword/page-aware demo answer with the REAL page/rack context.
    resp = ai_chat._match_demo_response(base_messages, context or {})
    for w in resp.split(" "):
        yield {"type": "token", "content": w + " "}
        time.sleep(0.01)
    yield {"type": "done"}
