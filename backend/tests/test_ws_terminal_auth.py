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
"""The web terminal must inherit the dashboard auth gate.

``/ws/terminal`` is WSGI middleware wrapping ``app.wsgi_app`` (see ``api/terminal.py``
``install``), so it is reached *below* the Flask router and ``@app.before_request
_require_dashboard_auth`` never runs for it. A local scan on 2026-08-17 found the endpoint
accepting anonymous WebSocket upgrades with ``RACK_WEB_AUTH_MODE=token`` set and a token
configured, while ``GET /api/status`` correctly returned 401 on the same server. It then
reached ``pty.openpty()``, whose next statement spawns ``ssh`` with the app's cluster key.
The terminal is on by default (``RACK_ENABLE_WEB_TERMINAL`` defaults to ``1``).

``test_route_parity.py`` cannot cover this: it snapshots Flask URL rules and ``/ws/terminal``
is not one, so nothing else in the suite would notice the gate being absent or removed.

These tests assert the *property* (an unauthenticated caller must not reach session
creation) rather than any particular implementation, so they hold whichever credential
channel the auth work settles on. If that channel is not the Flask session cookie, update
``_authenticated_environ`` only.
"""

from __future__ import annotations

import os

import pytest

os.environ["RACK_EXEC_MODE"] = "local"

import api.terminal as terminal_module
from app import app


class _FakeWebSocket:
    """Stand-in for ``simple_websocket.Server``: records sends, needs no real socket."""

    def __init__(self, environ, **kwargs):
        self.environ = environ
        self.sent: list[str] = []
        self.closed = False

    def send(self, data):
        self.sent.append(data)

    def receive(self, timeout=None):
        return None

    def close(self, *a, **k):
        self.closed = True


@pytest.fixture
def armed_middleware(monkeypatch):
    """A middleware instance with auth ON, demo OFF, and privileged work instrumented.

    Demo mode must be off: it short-circuits ``_get_or_create_session`` before any PTY work
    and would mask the gap (the first probe of this endpoint was answered by the demo guard,
    not by an auth check).
    """
    import simple_websocket

    monkeypatch.delenv("RACK_WEB_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
    monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "test-token-not-supplied-by-caller")
    monkeypatch.setenv("RACK_STATUS_DEMO", "0")
    monkeypatch.setattr(terminal_module, "_TERMINAL_ENABLED", True, raising=False)
    monkeypatch.setattr(simple_websocket, "Server", _FakeWebSocket)

    # Session creation is the boundary: everything privileged (pty.openpty, then
    # subprocess.Popen of ssh) happens inside it. Recording calls here keeps the test
    # independent of whether a PTY can be allocated in the sandbox or in CI.
    created: list[dict] = []

    def _record(self, qs):
        # Returning None makes _handle_terminal send its error frame and stop, so no PTY
        # is allocated even when the gate is missing.
        created.append(qs)

    monkeypatch.setattr(terminal_module._WebSocketTerminalMiddleware, "_get_or_create_session", _record)

    inner_calls: list[dict] = []

    def _inner_app(environ, start_response):
        inner_calls.append(environ)
        start_response("200 OK", [])
        return [b""]

    mw = terminal_module._WebSocketTerminalMiddleware(_inner_app)
    return mw, created, inner_calls


def _upgrade_environ(query: str = "target=some-node", cookie: str | None = None) -> dict:
    environ = {
        "PATH_INFO": "/ws/terminal",
        "QUERY_STRING": query,
        "HTTP_UPGRADE": "websocket",
        "REQUEST_METHOD": "GET",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "80",
        "wsgi.url_scheme": "http",
        # The middleware requires a socket in the environ before it claims the path.
        "werkzeug.socket": object(),
    }
    if cookie is not None:
        environ["HTTP_COOKIE"] = cookie
    return environ


def _authenticated_environ(query: str = "target=some-node") -> dict:
    """An environ carrying a validly signed dashboard session cookie."""
    serializer = app.session_interface.get_signing_serializer(app)
    assert serializer is not None, "app has no secret key; cannot sign a session cookie"
    cookie_value = serializer.dumps({"rack_web_auth": "1"})
    name = app.config.get("SESSION_COOKIE_NAME") or "session"
    return _upgrade_environ(query, cookie=f"{name}={cookie_value}")


def _start_response(status, headers, exc_info=None):
    return lambda data: None


def test_unauthenticated_upgrade_must_not_create_a_terminal_session(armed_middleware):
    """No credential -> the request must be refused before a PTY/ssh session is created."""
    mw, created, _ = armed_middleware

    mw(_upgrade_environ(), _start_response)

    assert created == [], (
        "/ws/terminal created a terminal session for an unauthenticated caller. "
        "The middleware wraps app.wsgi_app and returns without delegating, so Flask's "
        "before_request auth gate never runs. Enforce the dashboard auth check inside "
        "_WebSocketTerminalMiddleware.__call__ before it claims the path -- reuse "
        "web_auth (e.g. under app.request_context(environ)) rather than adding a second "
        "auth implementation."
    )


def test_authenticated_upgrade_is_still_allowed_through(armed_middleware):
    """A valid session must still reach session creation.

    Guards against 'fixing' the gap by refusing every connection or disabling the feature.
    """
    mw, created, _ = armed_middleware

    mw(_authenticated_environ(), _start_response)

    assert created, (
        "/ws/terminal refused a caller holding a valid dashboard session. The auth check "
        "must accept an authenticated operator, not close the endpoint."
    )


def test_auth_disabled_deployments_are_unaffected(armed_middleware, monkeypatch):
    """RACK_WEB_AUTH_DISABLED=1 (local dev, demo) must keep working without a credential."""
    mw, created, _ = armed_middleware
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")

    mw(_upgrade_environ(), _start_response)

    assert created, (
        "/ws/terminal refused a connection while RACK_WEB_AUTH_DISABLED=1. The gate must "
        "honour the same disable switch as the rest of the dashboard."
    )


def test_non_terminal_paths_are_passed_through(armed_middleware):
    """The middleware must not swallow anything except its own path."""
    mw, created, inner_calls = armed_middleware

    environ = _upgrade_environ()
    environ["PATH_INFO"] = "/api/status"
    mw(environ, _start_response)

    assert created == []
    assert len(inner_calls) == 1, "non-terminal paths must be delegated to the Flask app"


def test_target_with_shell_metacharacters_is_rejected(armed_middleware):
    """``target`` is attacker-controlled and lands in a remote shell command string.

    ``host_exec.ssh_to_node_argv`` appends ``node`` after ``ssh -tt`` on the head-node hop;
    OpenSSH joins the trailing argv with spaces and the remote shell interprets it, so shell
    metacharacters in ``target`` are remote command injection on the head node. Validate
    ``target`` against the known node inventory (or at minimum a hostname charset) before it
    reaches ssh.

    Separate fix from the auth gate; delete this test if target validation is tracked
    elsewhere.
    """
    mw, created, _ = armed_middleware

    mw(_authenticated_environ(query="target=node01;id>/tmp/pwned"), _start_response)

    assert created == [], (
        "/ws/terminal accepted a target containing shell metacharacters. That value is "
        "interpolated into the remote command string on the head-node hop, so it is remote "
        "command injection. Validate target against the node inventory before building argv."
    )
