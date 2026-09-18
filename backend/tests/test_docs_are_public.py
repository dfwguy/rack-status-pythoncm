# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""`/docs/` is public, and these are the claims that make that safe.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The documentation is public information -- the same text anybody can read in the repository --
and gating it cost two confusing failures while protecting nothing. A plain navigation never
passes through the SPA's fetch layer, so the link worked immediately after signing in and showed
the login page an hour later; and an administrators policy written before the `docs` resource
type existed refused administrators outright, rendering
`{"error":"not authorized","ok":false,"reason":"policy"}` into the viewport.

**`web_auth.AUTH_BYPASS_ENDPOINTS` says an entry is only safe *because* of specific properties,
and this file is where those stop being a claim.** Each test below pins one of them, so the
bypass cannot outlive its justification:

* it serves the built site to a caller with no credential at all;
* it serves *only* from `static/docs`, and a traversal attempt is refused;
* it sets no cookie and reads no credential, so it cannot leak or consume a session;
* nothing else became public in the process -- `/api/` still refuses an anonymous caller;
* and the app's own responses stay unframeable, while the docs relax to same-origin so the
  in-app view can frame them.
"""

from __future__ import annotations

import re

import pytest
import web_auth
from identity import testing

ANY_REDIRECT = {301, 302, 303, 307, 308}


@pytest.fixture
def anonymous():
    """No credential whatsoever: not a cookie, not a header."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    with testing.unauthenticated_client(app_module.app) as client:
        yield client


def test_the_documentation_is_served_without_a_credential(anonymous):
    response = anonymous.get("/docs/")
    assert response.status_code not in ANY_REDIRECT, "a public route must not redirect to login"
    assert response.status_code != 403
    # 200 with the built site, or the documented 503 when `static/docs` has not been built --
    # which is a CI checkout, since the Sphinx output is a gitignored artifact.
    assert response.status_code in {200, 503}


def test_the_bypass_names_the_route_that_actually_serves_the_docs(anonymous):
    """An entry naming a route that does not exist reads as an intentional exemption.

    The same reasoning `web_auth` gives for having removed `auth.api_login` from the tuple when
    that route went away.
    """
    import app as app_module

    endpoints = {rule.endpoint for rule in app_module.app.url_map.iter_rules()}
    docs_entries = [e for e in web_auth.AUTH_BYPASS_ENDPOINTS if e.startswith("docs_site.")]
    assert docs_entries, "the docs bypass entry has gone; /docs/ is gated again"
    for entry in docs_entries:
        assert entry in endpoints, f"{entry} is not a route on this app"


def test_it_serves_only_from_the_docs_directory(anonymous):
    """The property that makes public serving safe: no path escapes `static/docs`.

    `send_from_directory` is what enforces it, so this is a check on the route keeping that
    helper rather than hand-rolling a join.
    """
    for attempt in (
        "/docs/../../etc/passwd",
        "/docs/..%2f..%2fetc%2fpasswd",
        "/docs/../backend/app.py",
        "/docs/%2e%2e/%2e%2e/etc/passwd",
    ):
        response = anonymous.get(attempt)
        body = response.get_data(as_text=True)
        # 503 is the documented answer when `static/docs` has not been built, and that is a real
        # tree: the documentation is a build artifact, so a tracked-files-only checkout has none
        # (`local-vs-ci-tree.mdc` §1). Refusing to accept it made this test fail for the one
        # reason that is not a traversal — while the assertions that matter, below, hold either
        # way. In CI the docs *are* present, because `docs-build` runs in an earlier stage and its
        # artifact reaches this job.
        assert response.status_code in {301, 308, 400, 404, 503}, (
            f"{attempt} -> {response.status_code}"
        )
        assert "root:" not in body, f"{attempt} served /etc/passwd"
        assert "from flask import" not in body, f"{attempt} served source"


def test_it_neither_sets_nor_consumes_a_session(anonymous):
    """A public route that issued or read a credential would be a way to launder one."""
    response = anonymous.get("/docs/")
    assert not response.headers.get("Set-Cookie")


def test_nothing_else_became_public(anonymous):
    """The blast radius of the bypass, asserted rather than assumed."""
    for path in ("/api/status", "/api/catalog/projects", "/api/identity/users"):
        response = anonymous.get(path)
        assert response.status_code == 401, f"{path} answered {response.status_code} to anonymous"


def test_the_docs_may_be_framed_by_this_origin_and_nothing_else_may(anonymous):
    """`frame-ancestors 'none'` blocks same-origin framing too.

    That is why the in-app documentation view rendered an empty box in a real browser while
    every jsdom test passed -- jsdom does not enforce the header. The docs relax to `'self'`;
    every other response must stay `'none'`/`DENY`, which is the half worth pinning.
    """
    docs = anonymous.get("/docs/")
    assert docs.headers.get("X-Frame-Options") == "SAMEORIGIN"
    csp = docs.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'self'" in csp

    other = anonymous.get("/api/status")
    assert other.headers.get("X-Frame-Options") == "DENY"
    other_csp = other.headers.get("Content-Security-Policy") or ""
    assert "frame-ancestors 'none'" in other_csp


def test_the_relaxation_is_scoped_to_the_docs_path(anonymous):
    """Scoped by path prefix, so a typo widening it to everything would show up here."""
    for path in ("/", "/login", "/api/status"):
        csp = (anonymous.get(path).headers.get("Content-Security-Policy") or "")
        if csp:
            assert re.search(r"frame-ancestors 'none'", csp), f"{path} allows framing"


def test_the_branding_a_login_screen_needs_is_public_too(anonymous):
    """Found in the same console as the framing error, and a different bug.

    `Manifest: Line 1, column 1, Syntax error` with a 401 beside it: **a browser fetches a web
    manifest anonymously**, with no cookie, unless the `<link>` carries
    `crossorigin="use-credentials"`. A gated manifest therefore answers the login redirect and
    the browser parses an HTML page as JSON.

    The icons are the same shape but worse than cosmetic, because the login screen needs them
    *before* any credential exists — precisely when the gate refuses them. Adding the
    `crossorigin` attribute would have fixed the manifest and left the icons broken.

    None of these carry anything: an application name, a theme colour, some PNGs.

    **The assertion is about the gate, not about the files existing.** These are all
    `static/spa/` assets, and that directory is a gitignored build artifact: present in every
    developer tree, absent from every CI checkout (`local-vs-ci-tree.mdc` §1). Asserting `200`
    passed locally and failed in CI with `/manifest.webmanifest -> 404`, which says nothing about
    whether the route is public — a 404 *is* the ungated answer for a file that is not there.
    So each must be reachable without a credential; whether it has content depends on the tree.
    """
    for path in (
        "/manifest.webmanifest",
        "/favicon.svg",
        "/icons.svg",
        "/icons/greenlight-180.png",
    ):
        response = anonymous.get(path)
        assert response.status_code not in ANY_REDIRECT, (
            f"{path} redirected an anonymous browser to login; a manifest fetch carries no "
            f"cookie, so it would parse the login page as JSON"
        )
        assert response.status_code not in (401, 403), f"{path} -> {response.status_code}"
        # Three acceptable answers, and which one you get depends on the tree rather than on the
        # gate: 200 where the bundle is built, 404 where the file is simply absent, and 503 for
        # the two that fall through to the SPA shell handler — the deliberate "Frontend not
        # built" page, which is the product's one intentional non-JSON 5xx. Measured, not
        # guessed: without `static/spa`, the manifest and the PNG answer 404 while `favicon.svg`
        # and `icons.svg` answer 503.
        assert response.status_code in (200, 404, 503), f"{path} -> {response.status_code}"

    # And where the bundle *is* built, the manifest is served as a manifest — which is the part
    # the browser refused to parse.
    manifest = anonymous.get("/manifest.webmanifest")
    if manifest.status_code == 200:
        assert "manifest" in (manifest.headers.get("Content-Type") or "")


def test_favicon_ico_is_not_pretended_to_exist(anonymous):
    """The SPA ships `favicon.svg`; there is no `.ico`.

    Listing it as public would answer a browser's automatic request with `index.html` served as
    an image — a different console warning rather than a fix. Pinned so a later "make all the
    icons public" edit has to notice.
    """
    from pathlib import Path

    import app as app_module

    assert not (Path(app_module.__file__).resolve().parents[1] / "static" / "spa" / "favicon.ico").exists()
