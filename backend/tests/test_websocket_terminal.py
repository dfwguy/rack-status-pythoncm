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
