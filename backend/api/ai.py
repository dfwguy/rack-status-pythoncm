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
"""The in-app AI assistant: settings, a connectivity test, and the streaming chat.

All model access, prompt assembly and provider configuration live in ``ai_chat``; these routes only
validate input and stream the result. The chat response is streamed with ``stream_with_context``
because the generator reads request state, which Flask tears down at the end of the request
otherwise.
"""
from __future__ import annotations

import json
import logging

import ai_chat
from core import truthy_env as _truthy_env
from flask import Blueprint, Response, jsonify, request, stream_with_context

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("ai", __name__, url_prefix="")


@bp.route("/api/ai/chat", methods=["POST"])
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

    def generate():
        try:
            use_demo = _truthy_env("RACK_STATUS_DEMO") and not ai_chat.is_ai_enabled()
            if use_demo:
                stream = ai_chat.demo_stream_response(full_messages, context)
            else:
                stream = ai_chat.chat_completion_stream(full_messages)
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


@bp.route("/api/ai/settings", methods=["GET", "PUT"])
def api_ai_settings():
    """GET returns masked settings; PUT updates endpoint/model/key."""
    if request.method == "GET":
        return jsonify({"ok": True, **ai_chat.get_ai_settings()})

    body = request.get_json(silent=True) or {}
    updated = ai_chat.update_ai_settings(body)
    return jsonify({"ok": True, **updated})


@bp.route("/api/ai/test", methods=["POST"])
def api_ai_test():
    """Test the AI endpoint connectivity."""
    result = ai_chat.test_connection()
    status = 200 if result.get("ok") else 502
    return jsonify(result), status
