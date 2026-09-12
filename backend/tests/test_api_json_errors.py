# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""An API caller never receives an HTML error page.

Every test here runs with ``PROPAGATE_EXCEPTIONS=False``, i.e. how the app actually serves. Under
the suite's default (``TESTING``) Flask re-raises, which is deliberate and is asserted below --
so the production behaviour is only reachable by turning propagation off on purpose.

The bug these pin: running outside demo mode with no head node configured,
``GET /api/status`` raised ``RuntimeError: No SSH host is configured...`` and the SPA got Flask's
default HTML 500 page. Since the SPA calls ``res.json()`` on every reply, an actionable message
surfaced as a JSON parse error.
"""
from __future__ import annotations

import app as app_module
import pytest
from werkzeug.exceptions import BadRequest


@pytest.fixture
def serving_client():
    """A client that behaves like the real server: handlers run, exceptions do not propagate."""
    app_module.app.config.update(PROPAGATE_EXCEPTIONS=False)
    try:
        with app_module.app.test_client() as c:
            yield c
    finally:
        app_module.app.config.pop("PROPAGATE_EXCEPTIONS", None)


@pytest.fixture
def boom(monkeypatch):
    """Make a real read endpoint raise the way an unconfigured cluster does."""

    def _raise(*_a, **_k):
        raise RuntimeError(
            "No SSH host is configured. Point RACK_SSH_HOST at the head-node hostname or IP."
        )

    # Patch the module that owns the call site: `paths._resolve_log_dir` is imported by name into
    # api/dashboard, so patching `paths` alone would leave the importer calling the real one.
    from api import dashboard

    monkeypatch.setattr(dashboard, "_resolve_log_dir", _raise)
    return _raise


# --- the reported bug ------------------------------------------------------------------


def test_an_unhandled_exception_on_an_api_path_is_json(serving_client, boom):
    resp = serving_client.get("/api/status")
    assert resp.status_code == 500
    assert resp.is_json, f"got {resp.content_type}: {resp.get_data(as_text=True)[:200]}"
    body = resp.get_json()
    assert body["ok"] is False


def test_the_message_survives_so_the_ui_can_show_the_fix(serving_client, boom):
    """The whole point: the operator sees what to do, not "unexpected token '<'"."""
    body = serving_client.get("/api/status").get_json()
    assert "RACK_SSH_HOST" in body["error"]
    assert "RuntimeError" in body["error"]


def test_no_traceback_is_returned(serving_client, boom):
    """The message is the fix; a traceback in a response body helps no operator and leaks paths."""
    text = serving_client.get("/api/status").get_data(as_text=True)
    assert "Traceback" not in text
    assert "site-packages" not in text


# --- the errors Flask raises itself ----------------------------------------------------


def test_a_wrong_method_on_an_api_route_is_json(serving_client):
    """405 came from werkzeug, reached no route, and rendered as HTML."""
    resp = serving_client.delete("/api/status")
    assert resp.status_code == 405
    assert resp.is_json, resp.get_data(as_text=True)[:200]
    assert resp.get_json()["ok"] is False


def test_an_aborted_request_keeps_its_status_and_reason(serving_client, monkeypatch):
    """An `abort(400, "...")` from inside a route keeps both halves in the JSON body.

    Driven through a real endpoint rather than a route registered here: Flask refuses
    ``@app.route`` once the app has served a request, and a test that mutates the URL map would
    also fight ``test_route_parity``'s snapshot.
    """
    from api import dashboard

    def _abort(*_a, **_k):
        raise BadRequest("nodes must be a list")

    monkeypatch.setattr(dashboard, "_resolve_log_dir", _abort)
    resp = serving_client.get("/api/status")
    assert resp.status_code == 400
    assert resp.is_json, resp.get_data(as_text=True)[:200]
    assert resp.get_json()["error"] == "nodes must be a list"


# --- the deliberate exceptions, which must not change ----------------------------------


def test_a_browser_path_still_gets_html(serving_client):
    """The SPA shell and the "Frontend not built" 503 are for a person, not a program.

    Converting these would turn the one documented HTML response in the product into JSON, and
    `test_app_api.py::test_missing_shell_503s_instead_of_raising` pins it.
    """
    resp = serving_client.get("/")
    assert "application/json" not in (resp.content_type or "")


def test_a_browser_path_exception_is_not_converted(serving_client, monkeypatch):
    """A person in a browser is better served by Flask's page than by a JSON blob."""

    def _raise(*_a, **_k):
        raise RuntimeError("shell exploded")

    monkeypatch.setattr(app_module, "_serve_spa_asset", _raise)
    resp = serving_client.get("/")
    assert resp.status_code == 500
    assert not resp.is_json, "a human in a browser should get Flask's HTML page"


def test_propagation_is_preserved_under_testing(boom):
    """Guard the guard.

    A handler for ``Exception`` normally defeats Flask's re-raise under TESTING, and that re-raise
    is what makes a genuine crash fail a test on its traceback -- the signal that found the
    ``{"steps": 1}`` TypeError in api/workflows.py. If this ever stops raising,
    ``_assert_did_not_crash`` starts passing for the very bug it exists to catch.
    """
    app_module.app.config["PROPAGATE_EXCEPTIONS"] = True
    try:
        with app_module.app.test_client() as c, pytest.raises(RuntimeError, match="RACK_SSH_HOST"):
            c.get("/api/status")
    finally:
        app_module.app.config.pop("PROPAGATE_EXCEPTIONS", None)


# --- secret hygiene --------------------------------------------------------------------


def test_a_secret_in_an_exception_message_is_scrubbed(serving_client, monkeypatch):
    """An exception message is arbitrary text from anywhere, so it is not a safe channel."""
    secret = "super-secret-token-value-1234"
    monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", secret)

    from api import dashboard

    def _leak(*_a, **_k):
        raise RuntimeError(f"upstream rejected token {secret}")

    monkeypatch.setattr(dashboard, "_resolve_log_dir", _leak)
    text = serving_client.get("/api/status").get_data(as_text=True)
    assert secret not in text, "an env-sourced secret reached the response body"


def test_not_found_on_an_api_path_is_json(serving_client):
    """Already handled by ``serve_spa_asset``; pinned so the new handler cannot regress it."""
    resp = serving_client.get("/api/definitely-not-a-real-endpoint")
    assert resp.status_code == 404
    assert resp.is_json
    assert resp.get_json()["ok"] is False
