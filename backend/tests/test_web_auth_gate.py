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
"""The dashboard auth gate, exercised with authentication actually ENABLED.

Every other test in this suite runs with ``RACK_WEB_AUTH_DISABLED=1``, so ``_require_dashboard_auth``
returns at its first line and the gate is never executed. That leaves the login path -- the one route
set whose failure locks every operator out -- as the least covered code in the app, which is why
these tests exist and why they flip the flag back on.

The specific hazard: the bypass list holds Flask *endpoint* names. Moving ``api_login`` into a
blueprint renames it to ``auth.api_login``, and a stale bypass entry then makes ``/api/login`` demand
the very session the caller is trying to obtain. Nothing raises. The suite stays green. The dashboard
becomes unreachable and only a deploy rollback fixes it.

``_auth_enabled`` reads the environment on every call, so enabling auth here needs no reimport of the
app module.
"""
from __future__ import annotations

import app as app_module
import pytest
from paths import REPO_ROOT

TOKEN = "test-token-not-a-real-secret"


@pytest.fixture()
def spa_index():
    """Guarantee ``static/spa/index.html`` exists, creating a placeholder only if it is missing.

    The backend test job does not build the frontend, so any test that exercises a route serving the
    SPA has to supply the artifact itself rather than assume a developer's local build. A real bundle
    is left untouched; a placeholder is removed again so a later frontend build is not confused by it.
    """
    index = REPO_ROOT / "static" / "spa" / "index.html"
    if index.exists():
        yield index
        return
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("<!doctype html><title>test placeholder</title>", encoding="utf-8")
    try:
        yield index
    finally:
        index.unlink(missing_ok=True)


@pytest.fixture
def auth_client():
    """A client that presents **no** credential, which is this whole file's subject.

    It used to configure token mode — `RACK_WEB_AUTH_MODE` and `RACK_WEB_AUTH_TOKEN`, both removed
    by FR-18 — and then relied on the suite-wide bypass, so "the gate is on" was never actually the
    state under test. Now the suite authenticates by default and refusal is the thing that has to be
    asked for, so this is an explicitly anonymous client.
    """
    from identity import testing as identity_testing

    with identity_testing.unauthenticated_client(app_module.app) as client:
        yield client


def test_gate_is_actually_on(auth_client):
    """Sanity: without this, every assertion below would pass for the wrong reason.

    Asserted by *behaviour* rather than by reading a flag. It used to check
    `app._auth_enabled() is True`, which stopped meaning anything when the switch was deleted —
    there is no longer a state in which the gate is off, so a refused request is the only evidence
    that says so.
    """
    resp = auth_client.get("/api/status")
    assert resp.status_code == 401, "an unauthenticated API call must be refused"
    assert (resp.get_json() or {}).get("ok") is False


def test_every_bypass_endpoint_exists():
    """Each bypass entry must name a real endpoint.

    This is the regression guard for moving auth routes between modules: a blueprint changes the
    endpoint name, and a stale entry here is invisible at runtime.
    """
    known = {rule.endpoint for rule in app_module.app.url_map.iter_rules()}
    missing = [e for e in app_module._AUTH_BYPASS_ENDPOINTS if e not in known]
    assert not missing, (
        f"bypass list names endpoints that do not exist: {missing}. "
        f"If a route moved into a blueprint, use its qualified name (e.g. 'auth.api_login')."
    )


def test_login_page_is_reachable_unauthenticated(auth_client, spa_index):
    """The login screen must be reachable unauthenticated, or the operator sees a redirect loop.

    What is asserted is the GATE, not the frontend: ``/login`` serves the SPA bundle, so the
    ``spa_index`` fixture guarantees the artifact exists. Without it this test passed on any machine
    with a built frontend and returned 500 in CI, where ``python-test`` never runs a Vite build --
    exactly the environment split that let it stay red while the suite looked green locally.
    """
    resp = auth_client.get("/login")
    assert resp.status_code == 200


def test_auth_check_reports_unauthenticated_rather_than_blocking(auth_client):
    """The SPA needs a truthful 401 here to know it should show the login form."""
    resp = auth_client.get("/api/auth-check")
    assert resp.status_code == 401
    assert (resp.get_json() or {}).get("authenticated") is False


def test_a_flask_session_cookie_is_not_a_credential(auth_client):
    """Even a validly signed session cookie must not authenticate (FR-12, FR-18).

    Forged the way the legacy login would have set it, so this fails if the gate ever consults
    `session` again — a plausible regression, because the cookie keeps working for everything else
    Flask uses it for.
    """
    from app import app

    serializer = app.session_interface.get_signing_serializer(app)
    assert serializer is not None
    auth_client.set_cookie(
        app.config.get("SESSION_COOKIE_NAME") or "session",
        serializer.dumps({"rack_web_auth": "1", "rack_web_user": "admin"}),
    )

    assert auth_client.get("/api/status").status_code == 401


def test_spa_assets_stay_public(auth_client):
    """Vite chunks must not be redirected to /login, or the login page loads as HTML-in-JS.

    Asserted through the gate rather than the filesystem: a 401/302 is the failure, while a 404 for
    an asset that is not built in CI is fine.
    """
    for path in ("/assets/does-not-exist.js", "/favicon.svg"):
        code = auth_client.get(path).status_code
        assert code not in (401, 302), f"{path} was blocked by the auth gate ({code})"


def test_the_legacy_login_route_does_not_exist(auth_client):
    """Replaces two tests that POSTed to `/api/login` expecting a 401.

    FR-18 removed the route, so they were asserting the refusal of something absent — which passes
    for the wrong reason and would keep passing if it came back as a 500. The structural claim is
    the one worth making: there is no second way in.
    """
    known = {rule.rule for rule in app_module.app.url_map.iter_rules()}
    assert "/api/login" not in known

    # 401 rather than 404, and that is correct: the gate runs before routing resolves, so an unknown
    # `/api/*` path is refused as unauthenticated rather than reported as absent. Asserting "not
    # 200" is the claim that matters — the old token must not open anything, however the path is
    # answered.
    assert auth_client.post("/api/login", json={"password": TOKEN}).status_code != 200
