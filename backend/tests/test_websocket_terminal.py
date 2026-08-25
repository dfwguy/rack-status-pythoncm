# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Regression tests for the WebSocket terminal WSGI middleware."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import api.terminal as terminal_module  # the middleware moved out of app.py


@pytest.fixture
def terminal_middleware():
    return terminal_module._WebSocketTerminalMiddleware(lambda environ, start_response: (
        start_response("404 Not Found", [("Content-Type", "text/plain")]),
        [b"fallback"],
    )[1])


def test_werkzeug_websocket_calls_start_response(terminal_middleware, monkeypatch):
    """Werkzeug always finalizes with write(b''); middleware must call start_response."""
    called = {"handle": 0, "status": None}

    def fake_handle(environ):
        called["handle"] += 1

    monkeypatch.setattr(terminal_middleware, "_handle_terminal", fake_handle)
    monkeypatch.setattr(terminal_module, "_TERMINAL_ENABLED", True)

    def start_response(status, headers, exc_info=None):
        called["status"] = status
        return lambda _data: None

    environ = {
        "PATH_INFO": "/ws/terminal",
        "QUERY_STRING": "target=headnode&mode=headnode",
        "HTTP_UPGRADE": "websocket",
        "werkzeug.socket": object(),
    }
    body = list(terminal_middleware(environ, start_response))

    assert called["handle"] == 1
    assert called["status"] == "200 OK"
    assert body == []


def test_non_websocket_request_falls_through(terminal_middleware, monkeypatch):
    monkeypatch.setattr(terminal_module, "_TERMINAL_ENABLED", True)
    monkeypatch.setattr(
        terminal_middleware,
        "_handle_terminal",
        lambda environ: (_ for _ in ()).throw(AssertionError("should not handle")),
    )

    def start_response(status, headers, exc_info=None):
        return lambda _data: None

    environ = {
        "PATH_INFO": "/ws/terminal",
        "QUERY_STRING": "target=headnode&mode=headnode",
        "werkzeug.socket": object(),
    }
    body = list(terminal_middleware(environ, start_response))
    assert body == [b"fallback"]


def test_unauthorized_websocket_falls_through_without_opening_a_pty(monkeypatch):
    """Flask owns the 401 response when the signed session is missing/invalid."""
    called = {"fallback": 0, "handle": 0}

    def fallback(environ, start_response):
        called["fallback"] += 1
        start_response("401 Unauthorized", [("Content-Type", "text/plain")])
        return [b"unauthorized"]

    middleware = terminal_module._WebSocketTerminalMiddleware(
        fallback,
        authorized=lambda _environ: False,
    )
    monkeypatch.setattr(
        middleware,
        "_handle_terminal",
        lambda environ: called.__setitem__("handle", called["handle"] + 1),
    )
    monkeypatch.setattr(terminal_module, "_TERMINAL_ENABLED", True)
    status = {"value": None}

    def start_response(value, headers, exc_info=None):
        status["value"] = value
        return lambda _data: None

    body = list(middleware({
        "PATH_INFO": "/ws/terminal",
        "HTTP_UPGRADE": "websocket",
        "werkzeug.socket": object(),
    }, start_response))

    assert body == [b"unauthorized"]
    assert status["value"] == "401 Unauthorized"
    assert called == {"fallback": 1, "handle": 0}
