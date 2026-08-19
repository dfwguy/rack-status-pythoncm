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

import os

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
def auth_client(monkeypatch):
    """A test client with auth ON, in token mode, and a known token."""
    monkeypatch.delenv("RACK_WEB_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("RACK_WEB_AUTH_MODE", "token")
    monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", TOKEN)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


def test_gate_is_actually_on(auth_client):
    """Sanity: without this, every assertion below would pass for the wrong reason."""
    assert app_module._auth_enabled() is True
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


def test_login_endpoint_is_reachable_without_a_session(auth_client):
    """/api/login must bypass the gate -- it is how a session is obtained in the first place."""
    resp = auth_client.post("/api/login", json={"password": "wrong-token"})
    assert resp.status_code == 401, "reachable but refusing a bad token"
    assert (resp.get_json() or {}).get("ok") is False


def test_successful_login_grants_access(auth_client):
    """The whole point: log in, then reach a protected endpoint with the session cookie."""
    resp = auth_client.post("/api/login", json={"password": TOKEN})
    assert resp.status_code == 200
    assert (resp.get_json() or {}).get("ok") is True

    checked = auth_client.get("/api/auth-check")
    assert checked.status_code == 200
    assert (checked.get_json() or {}).get("authenticated") is True

    protected = auth_client.get("/api/status")
    assert protected.status_code == 200, "a logged-in session must reach a protected endpoint"


def test_logout_revokes_the_session(auth_client):
    auth_client.post("/api/login", json={"password": TOKEN})
    assert auth_client.get("/api/auth-check").status_code == 200
    assert auth_client.post("/api/logout").status_code == 200
    assert auth_client.get("/api/auth-check").status_code == 401


def test_spa_assets_stay_public(auth_client):
    """Vite chunks must not be redirected to /login, or the login page loads as HTML-in-JS.

    Asserted through the gate rather than the filesystem: a 401/302 is the failure, while a 404 for
    an asset that is not built in CI is fine.
    """
    for path in ("/assets/does-not-exist.js", "/favicon.svg"):
        code = auth_client.get(path).status_code
        assert code not in (401, 302), f"{path} was blocked by the auth gate ({code})"


def test_auth_disabled_short_circuits(monkeypatch):
    """With the flag set, the gate must not touch sessions at all -- the mode the rest of the suite runs in."""
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    assert app_module._auth_enabled() is False
    with app_module.app.test_client() as client:
        assert client.get("/api/auth-check").status_code == 200


def test_environment_is_restored():
    """Guards the leak that once failed 1-to-80 tests at random (see 10d6186)."""
    assert os.environ.get("RACK_WEB_AUTH_DISABLED") == "1", (
        "conftest's auth-disabled default must survive this module, or unrelated tests start failing"
    )
