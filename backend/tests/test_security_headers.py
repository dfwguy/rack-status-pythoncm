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
"""Response security headers, and the two ways a CSP can break this dashboard.

The policy is asserted as a *parsed* set of directives rather than a string match, because the
failure mode that matters is a missing or over-broad directive, not formatting.

Two failures worth naming, since both produce the same blank Grafana panel for different
reasons: a CSP whose ``frame-src`` omits the Grafana origin blocks the iframe, and an ``https``
page framing an ``http`` Grafana is blocked as mixed content no matter what CSP says. Only the
first is this module's problem, and these tests pin it.
"""
from __future__ import annotations

import app as app_module
import pytest
from security_headers import (
    HSTS_MAX_AGE,
    content_security_policy,
    grafana_frame_origin,
    security_headers,
)


def _directives(policy: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for part in policy.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


class TestTheScriptPolicyIsTheWholePoint:
    def test_script_src_is_self_only(self):
        """No ``unsafe-inline`` for script. Verified safe against the built bundle, which has
        zero inline script tags and references no external host."""
        d = _directives(content_security_policy(page_host="localhost:5050"))
        assert d["script-src"] == ["'self'"]

    def test_style_src_admits_inline_because_the_ui_requires_it(self):
        """MUI/emotion and xterm inject style elements at runtime; there is no nonce plumbing.

        Asserted explicitly so that if someone later adds nonces they can tighten this
        deliberately rather than discover the relaxation by reading a header in a browser.
        """
        d = _directives(content_security_policy(page_host="localhost:5050"))
        assert d["style-src"] == ["'self'", "'unsafe-inline'"]

    def test_nothing_may_frame_the_dashboard(self):
        d = _directives(content_security_policy(page_host="localhost:5050"))
        assert d["frame-ancestors"] == ["'none'"]
        assert d["object-src"] == ["'none'"]

    def test_charts_and_diagrams_can_still_render(self):
        d = _directives(content_security_policy(page_host="localhost:5050"))
        assert "data:" in d["img-src"] and "blob:" in d["img-src"]
        assert "blob:" in d["worker-src"]


class TestTheTerminalKeepsWorking:
    """``connect-src`` carries the WebSocket origin explicitly.

    ``'self'`` is honoured inconsistently for ``ws:``/``wss:`` across browser versions, and
    getting it wrong breaks the terminal rather than anything that looks like a policy problem.
    """

    def test_ws_origin_is_allowed_on_plain_http(self):
        d = _directives(content_security_policy(page_scheme="http", page_host="localhost:5156"))
        assert "ws://localhost:5156" in d["connect-src"]

    def test_wss_origin_is_allowed_under_tls(self):
        d = _directives(content_security_policy(page_scheme="https", page_host="nvis.example"))
        assert "wss://nvis.example" in d["connect-src"]


class TestGrafanaFraming:
    def test_an_explicit_uri_wins(self):
        assert grafana_frame_origin(explicit_uri="https://grafana.example/") == "https://grafana.example"

    def test_otherwise_the_panel_is_assumed_beside_the_page(self):
        """Mirrors ``frontend/src/lib/grafanaLinks.ts``; the two must agree or the iframe is blocked."""
        got = grafana_frame_origin(page_scheme="https", page_host="nvis.example:8443", port="3001")
        assert got == "https://nvis.example:3001", "the page's PORT must not leak into the Grafana origin"

    def test_frame_src_is_none_rather_than_empty_when_grafana_is_unknown(self):
        """An empty directive is treated as *allow* by some browsers -- the opposite of intended."""
        d = _directives(content_security_policy(page_host="localhost:5050", grafana_origin=""))
        assert d["frame-src"] == ["'none'"]

    def test_the_grafana_origin_reaches_frame_src(self):
        d = _directives(
            content_security_policy(page_host="localhost:5050", grafana_origin="http://localhost:3000")
        )
        assert d["frame-src"] == ["http://localhost:3000"]


class TestHstsIsOnlyEverSentOverTls:
    def test_absent_on_plain_http(self):
        """Sending HSTS over http is how a dashboard makes itself unreachable for six months."""
        assert "Strict-Transport-Security" not in security_headers(is_secure=False)

    def test_present_over_tls_without_subdomain_or_preload_claims(self):
        h = security_headers(is_secure=True)
        assert h["Strict-Transport-Security"] == f"max-age={HSTS_MAX_AGE}"
        assert "includeSubDomains" not in h["Strict-Transport-Security"]
        assert "preload" not in h["Strict-Transport-Security"]


class TestCspMode:
    def test_enforce_is_the_default(self):
        assert "Content-Security-Policy" in security_headers(is_secure=False)

    def test_report_mode_blocks_nothing(self):
        h = security_headers(is_secure=False, csp_mode="report")
        assert "Content-Security-Policy-Report-Only" in h
        assert "Content-Security-Policy" not in h

    def test_off_is_the_one_line_rollback(self):
        h = security_headers(is_secure=False, csp_mode="off")
        assert not any(k.startswith("Content-Security-Policy") for k in h)
        # The cheap headers stay: they have never broken a browser.
        assert h["X-Content-Type-Options"] == "nosniff"


class TestAppliedToRealResponses:
    @pytest.fixture
    def client(self):
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as c:
            yield c

    def test_headers_reach_an_api_response(self, client):
        resp = client.get("/api/auth-check")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert "script-src 'self'" in (resp.headers.get("Content-Security-Policy") or "")

    def test_no_hsts_on_the_plain_http_test_client(self, client):
        assert "Strict-Transport-Security" not in client.get("/api/auth-check").headers

    def test_no_cors_headers_are_ever_emitted(self, client):
        """Recorded as a test so nobody adds CORS 'for HTTPS'. The SPA and API are one origin."""
        resp = client.get("/api/auth-check")
        # Iterating Headers yields (name, value) pairs, hence the unpack.
        assert not [k for k, _v in resp.headers if k.lower().startswith("access-control-")]

    def test_the_no_store_pair_still_applies(self, client):
        """Guard against a new after_request displacing the existing one."""
        resp = client.get("/api/auth-check")
        assert "no-store" in (resp.headers.get("Cache-Control") or "")


class TestTC37HookOrdering:
    """TC-37 (FR-6): two `after_request` hooks now share the chain, and order is the risk.

    Flask runs `after_request` handlers in REVERSE registration order, so the audit hook -- which
    `app.py` installs before the header decorators are declared -- runs last and therefore sees the
    fully decorated response. Nothing enforces that except the position of one call, and inverting
    it would be silent: headers would still reach the browser, and only the audit hook's view of
    the response would change. Hence a test rather than a comment.

    The event assertion is confined to `result`, because the FR-5 field set has no header or status
    field and TC-6 pins that key set exactly.
    """

    @pytest.fixture
    def observed(self, monkeypatch):
        """Snapshot the headers AT hook time, via the hook's own seam.

        A copy, not the response. Every `after_request` handler receives and mutates the SAME
        Response object, so keeping a reference and inspecting it after the chain finished showed
        the final headers regardless of order -- the first version of this fixture did exactly that
        and the test passed even with the registration order deliberately inverted. Copying the
        header map at the moment the hook runs is what makes the assertion mean anything.
        """
        from audit import request_hook

        seen: list[dict[str, str]] = []
        original = request_hook._record

        def _spy(response):
            seen.append(dict(response.headers))
            return original(response)

        monkeypatch.setattr(request_hook, "_record", _spy, raising=True)
        return seen

    @pytest.fixture
    def client(self):
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as c:
            yield c

    @pytest.fixture
    def gated_client(self, monkeypatch):
        """Auth on, so the gate produces a real 401. `_auth_enabled` reads env per call."""
        monkeypatch.delenv("RACK_WEB_AUTH_DISABLED", raising=False)
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "tc37-token")
        app_module.app.config["TESTING"] = True
        with app_module.app.test_client() as c:
            yield c

    HARDENING = ("X-Content-Type-Options", "Referrer-Policy", "Content-Security-Policy")

    def test_headers_present_with_audit_hook_registered(self, client, observed):
        """The named TC-37 artifact: headers on the response, and on what the audit hook saw."""
        resp = client.get("/api/auth-check")
        assert resp.status_code < 500

        for header in self.HARDENING:
            assert resp.headers.get(header), f"{header} missing from the served response"

        assert observed, "the audit hook did not run, so its view of the response is untested"
        hook_view = observed[-1]
        for header in self.HARDENING:
            assert hook_view.get(header), (
                f"the audit hook saw a response without {header}, so it is running BEFORE the "
                "security-headers hook -- registration order has been inverted"
            )

    def test_a_gate_refused_401_is_also_decorated(self, gated_client, observed):
        """A refusal is exactly the response an attacker sees, so it needs the headers too."""
        resp = gated_client.get("/api/auth-check")
        assert resp.status_code == 401, "auth should be enabled for this case"
        for header in self.HARDENING:
            assert resp.headers.get(header), f"{header} missing from the 401"
        if observed:
            assert observed[-1].get("Content-Security-Policy")

    def test_the_no_store_and_header_hooks_both_still_apply(self, client):
        """Three hooks share the chain now; none may displace another."""
        resp = client.get("/api/auth-check")
        assert "no-store" in (resp.headers.get("Cache-Control") or "")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
