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

import json
import logging
import os

import pytest

os.environ["RACK_EXEC_MODE"] = "local"

import api.terminal as terminal_module
import audit
import logging_config as lc
import web_auth
from audit import sink


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
    """An environ carrying a valid access token in the session cookie.

    This is the single place the module's docstring promised would need editing when the credential
    channel changed, and Phase 2 changed it: the gate now verifies a signed JWT rather than a Flask
    session, so a signed session cookie authenticates nobody. Everything else in this file asserts
    the *property* — an unauthenticated caller must not reach session creation — and is untouched.
    """
    from identity import revocation, tokens

    # `verify` refuses everything until a checker is registered (FR-6a), and a bare test module has
    # not run app.py's startup wiring.
    if not tokens.revocation_checker_registered():
        revocation.install()
    token = tokens.mint("user:test-operator", tokens.TOKEN_ACCESS)
    return _upgrade_environ(query, cookie=f"{web_auth.ACCESS_COOKIE}={token}")


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


class TestTC25TerminalRefusalsAreAudited:
    """TC-25: every refusal reason produces its own audit event, readable immediately.

    Traces to FR-10b, AC-14d.

    The tests above prove the gate *refuses*. This proves the refusal is *recorded*, which is a
    separate property and the one with evidentiary value: an operator investigating an intrusion
    needs to know that somebody tried, from where, and why they were turned away. A gate that
    refuses silently leaves the product's highest-privilege surface with no denial trail at all.

    Parametrized over all three reasons `_refuse_reason` can return, because they are three
    distinct code paths, and because each is a distinct flood-guard key. Each reason is driven once,
    so all three stay below the collapse threshold and every event must be individually present --
    which is what "readable immediately, with no flush step" in the plan means.
    """

    #: Every value `api.terminal._refuse_reason` can return. If a fourth is ever added, this list
    #: and the parametrisation below are what should fail to make somebody consider auditing it.
    ALL_REASONS = ("unauthenticated", "cross-origin", "invalid target")

    @pytest.fixture(autouse=True)
    def _audit_to_tmp(self, monkeypatch, tmp_path):
        """Point the audit sink at a file this test owns, and reset it afterwards."""
        for name in list(lc.SETTINGS):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        monkeypatch.setenv("NVIS_LOG_FILE", str(tmp_path / "app.log"))
        monkeypatch.setenv("NVIS_AUDIT_FILE", str(tmp_path / "audit" / "audit.jsonl"))
        audit.reset_for_tests()
        lc.reset_for_tests()
        lc.configure(force=True)
        audit.configure(force=True)
        yield
        audit.reset_for_tests()
        lc.reset_for_tests()

    @staticmethod
    def _events(tmp_path) -> list[dict]:
        for handler in logging.getLogger(sink.AUDIT_LOGGER).handlers:
            handler.flush()
        path = tmp_path / "audit" / "audit.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(ln)
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]

    @staticmethod
    def _environ_for(reason: str) -> dict:
        """An environ that `_refuse_reason` will reject for exactly the named reason."""
        if reason == "unauthenticated":
            return _upgrade_environ()
        if reason == "cross-origin":
            # Authenticated, but the Origin header names a host the allowlist does not.
            environ = _authenticated_environ()
            environ["HTTP_ORIGIN"] = "https://attacker.example.net"
            return environ
        if reason == "invalid target":
            return _authenticated_environ(query="target=node01;id>/tmp/pwned")
        raise AssertionError(f"unhandled reason {reason!r}")

    @pytest.mark.parametrize("reason", ALL_REASONS)
    def test_each_refusal_reason_is_recorded_with_its_reason(
        self, armed_middleware, monkeypatch, tmp_path, reason
    ):
        mw, created, _ = armed_middleware
        if reason == "cross-origin":
            # An allowlist that exists and does not contain the attacker's origin. Without this the
            # policy is permissive by default and the request would be allowed, so the case would
            # silently test nothing.
            monkeypatch.setenv("RACK_WEB_ALLOWED_ORIGINS", "https://dashboard.example.com")

        mw(self._environ_for(reason), _start_response)

        assert created == [], f"a {reason} upgrade must not reach session creation"

        events = self._events(tmp_path)
        matching = [
            e for e in events
            if e.get("action") == "terminal:open" and e.get("reason") == reason
        ]
        assert len(matching) == 1, (
            f"expected exactly one audit event for a {reason!r} refusal, found {len(matching)}. "
            "One refusal is below the flood-guard threshold, so it must be written individually "
            f"and be readable without a flush step.\nevents: {json.dumps(events, indent=1)}"
        )

        event = matching[0]
        assert event.get("result") == "refused"
        assert event.get("principal") == "anonymous"
        assert event.get("resource") == "/ws/terminal"
        assert event.get("auth_method") == "none"
        for field in ("event_id", "ts", "src_ip", "src_ip_trust"):
            assert field in event, f"{field} missing; the event cannot answer 'who, from where'"

    def test_the_three_reasons_are_distinct_flood_guard_keys(
        self, armed_middleware, monkeypatch, tmp_path
    ):
        """All three in one run must yield three events, not one collapsed summary.

        The flood guard keys on (principal, action, reason). These refusals share principal and
        action and differ only in reason, so this is what proves `reason` is part of the key. If it
        were not, three different attacks on the same endpoint would collapse into one line and the
        trail would lose the distinction that matters most.
        """
        mw, created, _ = armed_middleware
        monkeypatch.setenv("RACK_WEB_ALLOWED_ORIGINS", "https://dashboard.example.com")

        for reason in self.ALL_REASONS:
            mw(self._environ_for(reason), _start_response)

        assert created == []
        events = [e for e in self._events(tmp_path) if e.get("action") == "terminal:open"]
        observed = sorted(e.get("reason") for e in events)
        assert observed == sorted(self.ALL_REASONS), (
            f"expected one event per reason, got {observed}. Three distinct refusals collapsing "
            "into fewer events means `reason` is not part of the flood-guard key."
        )
