# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The response-code contract in `docs/API_RESPONSE_CODES.md`, enforced.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A policy page nobody checks becomes wrong quietly, which is how the OpenAPI document came to claim
authentication failure was impossible on 219 of 221 operations. Everything here is a property of the
whole surface rather than of one endpoint, so a new route inherits it without anyone remembering.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openapi
import web_auth
from identity import testing as identity_testing

DOC = Path(__file__).resolve().parents[2] / "docs" / "API_RESPONSE_CODES.md"

#: Invented codes that some frameworks emit for session expiry. Not in the IANA registry, handled
#: unpredictably by proxies, and a 401 is the correct answer.
NON_STANDARD = ("419", "440", "498", "499", "598", "599")


@pytest.fixture(scope="module")
def spec(app_module):
    return openapi.build_spec(app_module.app, version="test", server_url="http://localhost")


@pytest.fixture(scope="module")
def app_module():
    import app as app_mod

    return app_mod


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "put", "patch", "delete"):
                yield method.upper(), path, op


class TestTheSpecDeclaresWhatTheGateCanReturn:
    """The defect this file exists for: middleware-produced statuses are invisible to source
    scraping, so they have to be added deliberately."""

    def test_every_gated_operation_declares_401(self, spec, app_module):
        bypass = getattr(app_module, "_AUTH_BYPASS_ENDPOINTS", frozenset())
        missing = [
            f"{m} {p}"
            for m, p, op in _operations(spec)
            if "401" not in op["responses"]
            and op.get("operationId", "").rsplit("_", 1)[0] not in bypass
        ]
        assert not missing, (
            "the gate can return 401 for these, so a client generated from the spec would believe "
            f"authentication failure is impossible: {missing[:10]}"
        )

    def test_there_is_exactly_one_bypass_list(self, app_module):
        """A second copy would drift from the gate, and the drift would show up as a spec that is
        wrong for precisely the endpoints that matter most.

        `web_auth` owns it; `app` re-exports the historical name and the generator reads the same
        object. Asserted by identity, not by string match — an earlier version of this test checked
        for a name in the source and broke the moment the list moved, without detecting anything.
        """
        assert app_module._AUTH_BYPASS_ENDPOINTS is web_auth.AUTH_BYPASS_ENDPOINTS
        source = Path(openapi.__file__).read_text()
        assert "web_auth.AUTH_BYPASS_ENDPOINTS" in source
        # An import *statement*, not the phrase — the first version of this matched the docstring
        # that explains we do not do it, which is a neat demonstration of why substring checks on
        # source need anchoring.
        real_import = re.compile(r"^\s*(?:import\s+app\b|from\s+app\s+import\b)", re.MULTILINE)
        assert not real_import.search(source), (
            "the generator must not import app — that is the deferred-lookup pattern "
            "core-designs.mdc bans and the meta-test rejects"
        )

    def test_a_gated_operation_declares_the_auth_unavailable_503(self, spec, app_module):
        """INV-18: fail-closed revocation is a 503, and it applies to every gated route."""
        bypass = getattr(app_module, "_AUTH_BYPASS_ENDPOINTS", frozenset())
        missing = [
            f"{m} {p}"
            for m, p, op in _operations(spec)
            if "503" not in op["responses"]
            and op.get("operationId", "").rsplit("_", 1)[0] not in bypass
        ]
        assert not missing, f"missing the auth-unavailable 503: {missing[:10]}"

    def test_the_401_description_does_not_disclose_the_cause(self, spec):
        """FR-4b at the documentation layer: the spec must not teach a client to distinguish
        'expired' from 'revoked' from 'no such user'."""
        for _m, path, op in _operations(spec):
            text = (op["responses"].get("401", {}).get("description") or "").lower()
            if not text:
                continue
            for leak in ("no such user", "unknown user", "wrong password", "user does not exist"):
                assert leak not in text, f"{path}: 401 description discloses {leak!r}"


class TestNoAntiPatterns:
    def test_no_operation_declares_a_non_standard_code(self, spec):
        for m, path, op in _operations(spec):
            bad = [c for c in op["responses"] if c in NON_STANDARD]
            assert not bad, f"{m} {path} declares invented status {bad}; a dead session is a 401"

    def test_no_error_body_is_returned_with_an_implicit_200(self):
        """An `ok: false` in a 200 cannot be handled by ordinary HTTP error handling, and every retry
        policy built on it is wrong. Audited at zero; this keeps it there."""
        api_dir = Path(__file__).resolve().parents[1] / "api"
        offenders = []
        pattern = re.compile(r'return\s+jsonify\(\s*\{[^}]*"ok"\s*:\s*False[^}]*\}\s*\)\s*$', re.MULTILINE)
        for py in sorted(api_dir.glob("*.py")):
            src = py.read_text()
            for m in pattern.finditer(src):
                offenders.append(f"{py.name}:{src[: m.start()].count(chr(10)) + 1}")
        assert not offenders, f"error bodies with an implicit 200: {offenders}"

    def test_every_operation_declares_200(self, spec):
        for m, path, op in _operations(spec):
            assert "200" in op["responses"], f"{m} {path} declares no success response"


class TestTheGatesActualResponse:
    """Behaviour, not just the document."""

    def test_an_unauthenticated_api_call_is_a_json_401_with_a_challenge(self, monkeypatch, app_module):
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "x" * 24)
        client = identity_testing.unauthenticated_client(app_module.app)
        resp = client.get("/api/status")
        assert resp.status_code == 401
        assert resp.headers.get("Content-Type", "").startswith("application/json")
        assert "WWW-Authenticate" in resp.headers, "RFC 9110 §11.6.1 makes this mandatory on a 401"
        assert resp.headers["WWW-Authenticate"].startswith("Bearer realm=")

    def test_the_challenge_never_explains_which_credential_problem_applied(self):
        """`invalid_token` covers expired, revoked, malformed and wrong-audience alike — that
        collapsing *is* the uniformity requirement, so no `error_description` may appear."""
        for presented in (True, False):
            value = web_auth.www_authenticate(presented)
            assert "error_description" not in value
            assert "realm=" in value
        assert 'error="invalid_token"' in web_auth.www_authenticate(True)
        assert "error=" not in web_auth.www_authenticate(False)

    def test_credential_presented_never_raises(self):
        """It only picks between two equally non-disclosing challenge forms, so it must not be able
        to turn a 401 into a 500."""
        for headers, cookies in ((None, None), ({}, {}), (object(), object())):
            assert web_auth.credential_presented(headers, cookies) in (True, False)

    def test_an_html_route_redirects_rather_than_401ing(self, monkeypatch, app_module):
        """A 401 on an HTML route triggers a browser auth dialog for a scheme we do not use."""
        monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
        monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "x" * 24)
        resp = identity_testing.unauthenticated_client(app_module.app).get("/analytics")
        assert resp.status_code in (301, 302, 303, 307, 308)
        assert "/login" in resp.headers.get("Location", "")


class TestTheDocumentMatchesTheCode:
    """So the page cannot quietly become fiction."""

    def test_the_document_exists_and_names_the_key_decisions(self):
        text = DOC.read_text()
        for required in (
            "WWW-Authenticate",  # the RFC 9110 rule
            "INV-18",  # the 503-not-401 case
            "feature_disabled",  # the 403 discriminator
            "not_authorized",
            "429",  # the login rate limit
            "Retry-After",
        ):
            assert required in text, f"API_RESPONSE_CODES.md no longer documents {required!r}"

    def test_the_documented_non_standard_codes_match_this_test(self):
        text = DOC.read_text()
        for code in NON_STANDARD[:2]:  # the two the doc names explicitly
            assert code in text

    def test_the_realm_in_the_document_matches_the_code(self):
        assert web_auth.AUTH_REALM in DOC.read_text()
