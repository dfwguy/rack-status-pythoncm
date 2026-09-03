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
"""The in-app AI assistant: settings, a connectivity test, the streaming chat and the agent loop.

All model access, prompt assembly and provider configuration live in ``ai_chat``; the tool-calling
loop lives in ``agent.loop``. These routes only validate input and stream the result. The chat
response is streamed with ``stream_with_context`` because the generator reads request state, which
Flask tears down at the end of the request otherwise.

When tools are enabled the chat takes the agentic path: read-only tools run inline, and the first
mutating tool pauses the turn with an ``approval_required`` event that the client answers through
``/api/ai/chat/resume``. Nothing mutating ever runs without that round trip.
"""
from __future__ import annotations

import json
import logging

import ai_chat
from audit.actions import action
from core import truthy_env as _truthy_env
from flask import Blueprint, Response, jsonify, request, stream_with_context

from ._common import operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("ai", __name__, url_prefix="")


@bp.route("/api/ai/chat", methods=["POST"])
@action("ai:chat")
def api_ai_chat():
    """POST SSE streaming chat. Body: {messages: [{role, content}], context: {page, rack?}}"""
    if not ai_chat.is_ai_enabled() and not _truthy_env("RACK_STATUS_DEMO"):
        return jsonify({"ok": False, "error": "AI is not configured. Set an API key in Settings."}), 503

    body = request.get_json(silent=True) or {}
    messages = body.get("messages")
    if not messages or not isinstance(messages, list) or len(messages) == 0:
        return jsonify({"ok": False, "error": "messages array is required"}), 400

    context = body.get("context") or {}
    settings = ai_chat.get_ai_settings()
    system_prompt = ai_chat.build_system_prompt(context, settings.get("system_rules", ""))

    full_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        if isinstance(m, dict) and m.get("role") and m.get("content"):
            full_messages.append({"role": m["role"], "content": m["content"]})

    operator = operator_from_request()

    def generate():
        try:
            from agent import loop as agent_loop

            use_demo = _truthy_env("RACK_STATUS_DEMO") and not ai_chat.is_ai_enabled()
            if agent_loop.tools_enabled():
                # Agentic path: read tools run inline, mutating ones pause for approval.
                gen = (
                    agent_loop.run_demo_turn(full_messages, context=context, operator=operator)
                    if use_demo
                    else agent_loop.run_turn(full_messages, operator=operator)
                )
                for ev in gen:
                    yield _agent_event_to_sse(ev)
                return
            # Plain-chat path (tools disabled).
            stream = (
                ai_chat.demo_stream_response(full_messages, context)
                if use_demo
                else ai_chat.chat_completion_stream(full_messages)
            )
            for chunk in stream:
                yield f"data: {json.dumps({'content': chunk})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            _logger.exception("AI chat stream error")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


def _agent_event_to_sse(ev: dict) -> str:
    """Serialize an agent loop event to an SSE ``data:`` line.

    ``token`` -> ``{content}`` (back-compat with the plain-chat client), ``done`` -> [DONE],
    ``error`` -> ``{error}``; tool_call / tool_result / approval_required pass through with ``type``.
    """
    t = ev.get("type")
    if t == "token":
        return f"data: {json.dumps({'content': ev.get('content', '')})}\n\n"
    if t == "done":
        return "data: [DONE]\n\n"
    if t == "error":
        return f"data: {json.dumps({'error': ev.get('error', '')})}\n\n"
    return f"data: {json.dumps(ev)}\n\n"


@bp.route("/api/ai/chat/resume", methods=["POST"])
@action("ai:chat_resume")
def api_ai_chat_resume():
    """Resume a paused agent turn after the user approves/denies a mutating action.

    Body: {run_id, call_id, decision: approve|deny, edited_args?}. Streams the same SSE
    event types as /api/ai/chat.
    """
    if not ai_chat.is_ai_enabled() and not _truthy_env("RACK_STATUS_DEMO"):
        return jsonify({"ok": False, "error": "AI is not configured."}), 503
    body = request.get_json(silent=True) or {}
    run_id = (body.get("run_id") or "").strip()
    call_id = (body.get("call_id") or "").strip()
    decision = (body.get("decision") or "").strip().lower()
    edited = body.get("edited_args") if isinstance(body.get("edited_args"), dict) else None
    if not run_id or decision not in ("approve", "deny"):
        return jsonify({"ok": False, "error": "run_id and decision (approve|deny) are required"}), 400
    operator = operator_from_request()

    def generate():
        try:
            from agent import loop as agent_loop

            for ev in agent_loop.resume_turn(run_id, call_id, decision, edited, operator=operator):
                yield _agent_event_to_sse(ev)
        except Exception as exc:
            _logger.exception("AI chat resume error")
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@bp.route("/api/ai/tools", methods=["GET"])
@action(get="ai:list")
def api_ai_tools():
    """List the agent tools and their availability (reflects RACK_*_DISABLED gates)."""
    from agent import loop as agent_loop
    from agent import registrations as agent_reg
    from agent import tools as agent_tools

    agent_reg.register_all()
    return jsonify({
        "ok": True,
        "tools_enabled": agent_loop.tools_enabled(),
        "tools": [t.to_public_dict() for t in agent_tools.REGISTRY.values()],
    })


@bp.route("/api/ai/settings", methods=["GET", "PUT"])
@action("ai:configure")
@action(get="ai:read")
def api_ai_settings():
    """GET returns masked settings; PUT updates endpoint/model/key."""
    if request.method == "GET":
        return jsonify({"ok": True, **ai_chat.get_ai_settings()})

    body = request.get_json(silent=True) or {}
    updated = ai_chat.update_ai_settings(body)
    return jsonify({"ok": True, **updated})


@bp.route("/api/ai/test", methods=["POST"])
@action("ai:test")
def api_ai_test():
    """Test the AI endpoint connectivity."""
    result = ai_chat.test_connection()
    status = 200 if result.get("ok") else 502
    return jsonify(result), status
