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
"""The documentation's two inline scripts are authorised by a per-response nonce.

Sphinx emits exactly two inline `<script>` tags per page: the theme's light/dark restore, which
reads `localStorage` before first paint, and `DOCUMENTATION_OPTIONS.pagename`. Under
`script-src 'self'` a browser refuses both, which is why the docs logged "invalid theme mode:
undefined" and reset to auto instead of honouring a stored choice.

The alternative was `'unsafe-inline'` for `/docs`, and the two are not close relatives: a nonce
permits exactly the tags in the response it was minted for, while `'unsafe-inline'` permits
anything that later reaches those pages by any route. These tests pin the properties that make the
nonce worth having rather than a decoration — that it *matches* the body, that it is *fresh*, that
it reaches only inline scripts, and that the page is not cached.

`static/docs` is a build artifact absent from a CI checkout (`local-vs-ci-tree.mdc` §1), so each
test skips when the documentation has not been built rather than failing on a tree that never had
it.
"""

from __future__ import annotations

import re

import pytest
from identity import testing

NONCE_IN_CSP = re.compile(r"script-src 'self' 'nonce-([^']+)'")
NONCED_TAG = re.compile(rb'<script[^>]*nonce="([^"]+)"')
EXTERNAL_TAG = re.compile(rb"<script[^>]*\bsrc=[^>]*>")
INLINE_TAG = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>")


@pytest.fixture
def client():
    """Anonymous, because that is how these pages are read.

    The documentation is public, and an authenticated fixture would prove the nonce works for the
    one reader who did not need it checked.
    """
    import app as app_module

    app_module.app.config["TESTING"] = True
    with testing.unauthenticated_client(app_module.app) as anonymous:
        yield anonymous


def _page(client, path="/docs/"):
    resp = client.get(path)
    if resp.status_code == 503:
        pytest.skip("the documentation is not built in this tree")
    assert resp.status_code == 200, f"{path} answered {resp.status_code}"
    return resp


def _csp_nonce(resp) -> str:
    match = NONCE_IN_CSP.search(resp.headers.get("Content-Security-Policy", ""))
    assert match, (
        "no nonce in the documentation's CSP, so its two inline scripts are refused and the "
        f"theme resets to auto: {resp.headers.get('Content-Security-Policy')}"
    )
    return match.group(1)


class TestTheNonceMatchesThePageItWasMintedFor:
    def test_the_header_and_the_body_carry_the_same_single_nonce(self, client):
        """The one property that makes it work. Minting in the handler and again in the header
        hook would produce two values and refuse every script — which looks exactly like having
        no nonce at all, so a test that only checked the header's presence would pass."""
        resp = _page(client)
        in_body = {n.decode() for n in NONCED_TAG.findall(resp.get_data())}
        assert in_body == {_csp_nonce(resp)}

    def test_each_response_gets_a_fresh_one(self, client):
        """A fixed nonce is `'unsafe-inline'` with extra steps: learn it once and any injected
        script carries it forever."""
        first = _csp_nonce(_page(client))
        second = _csp_nonce(_page(client))
        assert first != second

    def test_the_page_is_not_cached(self, client):
        """The nonce is in this body and named in this response's header. A cached copy replayed
        against a later policy is a page whose scripts are all refused — intermittently, which is
        the hardest version of this to diagnose."""
        assert _page(client).headers.get("Cache-Control") == "no-store"


class TestItReachesOnlyWhatNeedsIt:
    def test_external_scripts_are_left_alone(self, client):
        """They are same-origin `_static/…` files, already covered by `'self'`. Noncing them would
        hand the attribute to whatever a future theme decides to pull in."""
        body = _page(client).get_data()
        external = EXTERNAL_TAG.findall(body)
        assert external, "expected the page to load its scripts from files"
        assert [tag for tag in external if b"nonce=" in tag] == []

    def test_every_inline_script_gets_it(self, client):
        """A page where one of the two is missed is the worst outcome: most of it works, and the
        one that failed is the one that runs before first paint."""
        body = _page(client).get_data()
        assert len(INLINE_TAG.findall(body)) == len(NONCED_TAG.findall(body))

    def test_a_subresource_carries_no_nonce(self, client):
        """Only pages are rewritten; CSS, fonts and scripts are still `send_from_directory`, which
        is what keeps ranges, conditional requests and caching working for them."""
        resp = client.get("/docs/_static/local-fonts.css")
        if resp.status_code == 404:
            pytest.skip("the documentation is not built in this tree")
        assert "nonce-" not in resp.headers.get("Content-Security-Policy", "")

    def test_the_rest_of_the_app_keeps_the_strict_policy(self, client):
        """The relaxation is scoped to the documentation. Every other response must still be
        `script-src 'self'` with nothing added — the built SPA has no inline scripts at all."""
        csp = client.get("/api/status").headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "nonce-" not in csp
        assert "unsafe-inline" not in csp.split("style-src")[0]


class TestReadingThePageMyselfDidNotOpenTheTree:
    """The HTML branch reads the file directly instead of going through `send_from_directory`, so
    the containment check it used to inherit has to be made explicitly."""

    @pytest.mark.parametrize(
        "path",
        [
            "/docs/../../version.html",
            "/docs/%2e%2e/%2e%2e/etc/hosts.html",
            "/docs/../backend/app.html",
            "/docs/....//....//version.html",
        ],
    )
    def test_a_path_leaving_the_docs_tree_is_refused(self, client, path):
        resp = client.get(path)
        assert resp.status_code in (400, 404), f"{path} answered {resp.status_code}"
        assert b"SPDX" not in resp.get_data()
