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

    # The credential check is satisfied explicitly, because the subject here is the WSGI contract
    # (Werkzeug finalizes with `write(b'')`, so the middleware must call `start_response`) and not
    # authentication — `test_ws_terminal_auth.py` owns that. It used to pass because the suite ran
    # with the gate off and `wsgi_environ_authenticated` short-circuited; with no way to disable
    # authentication, an unauthenticated upgrade is refused and `_handle_terminal` is never
    # reached, which is the correct behaviour and made this assert 0 == 1.
    import web_auth

    monkeypatch.setattr(web_auth, "_wsgi_authenticator", lambda _environ: True)

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


def test_gunicorn_websocket_closes_the_connection_instead_of_keeping_it_alive(
    terminal_middleware, monkeypatch
):
    """Under gunicorn the middleware must end with StopIteration and never call start_response.

    Returning normally made gunicorn treat the finished upgrade as a keep-alive request and re-poll
    a socket simple_websocket had already closed -- the EBADF that crashed the worker on Nscale NC
    each time an operator's terminal session ended. `test_closed_terminal_sessions_do_not_crash_a_real_gunicorn_worker`
    proves the same thing against the real server.
    """
    import web_auth

    handled: list[dict] = []
    monkeypatch.setattr(terminal_middleware, "_handle_terminal", handled.append)
    monkeypatch.setattr(terminal_module, "_TERMINAL_ENABLED", True)
    monkeypatch.setattr(web_auth, "_wsgi_authenticator", lambda _environ: True)
    statuses: list[str] = []

    environ = {
        "PATH_INFO": "/ws/terminal",
        "QUERY_STRING": "target=headnode&mode=headnode",
        "HTTP_UPGRADE": "websocket",
        "gunicorn.socket": object(),
    }
    with pytest.raises(StopIteration):
        terminal_middleware(environ, lambda status, headers, exc_info=None: statuses.append(status))

    assert len(handled) == 1
    assert statuses == [], "gunicorn would write this status line onto the socket after the WebSocket"


_GUNICORN_APP = '''
import os

os.environ.setdefault("RACK_EXEC_MODE", "local")

import api.terminal as terminal
import web_auth

web_auth._wsgi_authenticator = lambda _environ: True
web_auth.wsgi_origin_allowed = lambda _environ: True
terminal._TERMINAL_ENABLED = True


def _session_that_ends(self, environ):
    """What a PTY EOF does: handshake, last output, close -- with no real shell behind it."""
    from simple_websocket import Server

    ws = Server(environ)
    try:
        ws.send("bye")
    finally:
        ws.close()


terminal._WebSocketTerminalMiddleware._handle_terminal = _session_that_ends


def pid(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [str(os.getpid()).encode()]


app = terminal._WebSocketTerminalMiddleware(pid)
'''


def test_closed_terminal_sessions_do_not_crash_a_real_gunicorn_worker(tmp_path):
    """Open and end terminal sessions against gunicorn gthread; the worker must survive them all.

    The crash was a race in gunicorn's own event loop, so only a real server shows it: before the
    fix, 30 sessions here killed the worker 7 times (`OSError: [Errno 9] Bad file descriptor`,
    `exited with code 255`). The worker pid is the evidence -- a crashed worker is silently
    replaced, so "still answering" alone would pass.
    """
    import socket
    import subprocess
    import sys
    import time
    import urllib.request
    from pathlib import Path

    from simple_websocket import Client, ConnectionClosed

    (tmp_path / "ws_terminal_app.py").write_text(_GUNICORN_APP)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    backend = Path(terminal_module.__file__).resolve().parents[1]
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn",
            "--workers", "1", "--threads", "4", "--worker-class", "gthread", "--keep-alive", "5",
            "--pythonpath", f"{backend},{tmp_path}",
            "--bind", f"127.0.0.1:{port}",
            "ws_terminal_app:app",
        ],
        # Not the repo root: gunicorn reads ./gunicorn.conf.py by default, and the product's config
        # brings TLS and the startup gate, neither of which this race depends on.
        cwd=str(tmp_path),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    def worker_pid() -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/pid", timeout=10) as resp:
            return resp.read().decode()

    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                first = worker_pid()
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    pytest.fail(f"gunicorn never served:\n{proc.stdout.read() if proc.stdout else ''}")
                time.sleep(0.2)

        for _ in range(30):
            ws = Client.connect(f"ws://127.0.0.1:{port}/ws/terminal?mode=headnode")
            try:
                while True:
                    ws.receive(timeout=2)
            except ConnectionClosed:
                pass
            try:
                ws.close()
            except ConnectionClosed:
                pass
            time.sleep(0.05)

        assert worker_pid() == first, "the gunicorn worker died while terminal sessions were closing"
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=15)
    assert "Bad file descriptor" not in out, out[-3000:]
