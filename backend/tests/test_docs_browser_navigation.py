# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Reaching `/docs/` the way a browser does, and what happens an hour later.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Reported as "clicking Documentation shows the login screen". The shape of the report is the
useful part: it *worked*, and then later it did not, with no change in between.

`/docs/` is a server route behind the authorization gate. The SPA never 401s visibly because
`apiFetch` refreshes and retries transparently — but a plain navigation, which is what an
anchor is, does not go through `apiFetch` at all. The access cookie lives an hour; the refresh
cookie is deliberately scoped to `/api/auth` and is therefore not even sent to `/docs/`. So
after an hour the gate sees an expired credential, redirects an HTML request to `/login`, and
the operator reads that as being logged out.

These tests pin the mechanism rather than the symptom: a fresh cookie reaches the docs, an
expired one is refused, and the refresh cookie cannot help because it is not in scope. That is
why `DocumentationView` renews *before* it frames the site and keeps renewing while it is open.
"""

from __future__ import annotations

import time

import pytest

import app as app_module
from api.auth_srp import ACCESS_COOKIE, REFRESH_COOKIE_PATH
from identity import testing, tokens


@pytest.fixture
def browser():
    """A client carrying only a cookie, like a browser following a link.

    The suite's default client presents an `Authorization` header, which no `<a href>` ever
    does — so a header-authenticated test could not have found this bug. `log_in` performs the
    real SRP exchange, which is what sets the cookie the gate reads.
    """
    principal = testing.enrol("operator")
    with testing.unauthenticated_client(app_module.app) as client:
        testing.log_in(client, principal.login, principal.password)
        yield client


def _is_login_redirect(response) -> bool:
    return response.status_code in {301, 302, 303, 307, 308} and "/login" in (
        response.headers.get("Location") or ""
    )


def test_a_fresh_cookie_reaches_the_documentation(browser):
    """The working case, pinned so the gate cannot start refusing it."""
    response = browser.get("/docs/")
    assert not _is_login_redirect(response), (
        "a signed-in browser was sent to the login screen for /docs/"
    )
    # 200 with the built site, or the documented 503 when `static/docs` has not been built --
    # which is a CI checkout, since the Sphinx output is a gitignored artifact.
    assert response.status_code in {200, 503}


def test_an_expired_cookie_is_what_sends_the_reader_to_login(browser):
    """The reported bug, reproduced deliberately: nothing is wrong except the clock.

    Minting with a past `iat` is the same thing an hour of reading does. A test that only
    checked the fresh case would pass forever while the feature broke daily.
    """
    # A one-second lifetime, then wait it out: the same state an hour of reading produces, said
    # without reaching into the minting internals. (Zero is refused -- `mint_access` requires a
    # positive ttl, which is itself the right call.)
    principal = testing.enrol("operator-expiring")
    stale = tokens.mint(f"user:{principal.login}", tokens.TOKEN_ACCESS, ttl_seconds=1)
    browser.set_cookie(ACCESS_COOKIE, stale, path="/")
    time.sleep(1.5)

    response = browser.get("/docs/")
    assert _is_login_redirect(response) or response.status_code == 401, (
        "an expired credential must be refused -- if this passes, the gate stopped checking"
    )


def test_the_refresh_cookie_cannot_rescue_a_docs_navigation():
    """Why the fix belongs in the client, not in the gate.

    The refresh cookie is scoped to `/api/auth`, so a browser does not send it to `/docs/` --
    the server has nothing to renew from even if it wanted to. Widening that path would hand a
    long-lived credential to every route on the origin, so the renewal stays where the refresh
    cookie *is* in scope: a call the SPA makes before it frames the site.
    """
    assert REFRESH_COOKIE_PATH.startswith("/api/auth"), (
        "if the refresh cookie became origin-wide, this reasoning -- and its security tradeoff "
        "-- needs revisiting"
    )
    assert not "/docs/".startswith(REFRESH_COOKIE_PATH)


def test_the_documentation_route_is_served_to_the_spa(browser):
    """`/documentation` is a client route now, so a hard refresh there must reach the app.

    It behaves exactly like `/platform/api`, an existing client route -- asserted against that
    rather than against a literal status, so the two cannot diverge unnoticed.
    """
    docs_route = browser.get("/documentation")
    existing_route = browser.get("/platform/api")
    assert docs_route.status_code == existing_route.status_code
    assert _is_login_redirect(docs_route) == _is_login_redirect(existing_route)
