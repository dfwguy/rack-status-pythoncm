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
"""The web manifest and app icons must serve, or 404 -- never fall back to HTML.

These two files are what make the dashboard installable as a desktop app. They fail differently
from a dropped ``.js``: a browser handed HTML for a bundle logs a MIME-type error and renders a
blank page, but a browser handed HTML for a *manifest or icon* reports nothing at all. It drops
the icon, fails the installability check silently, and the operator is left with a dashboard that
simply never offers "Install" -- with no error anywhere to explain why.

So ``api/_common.serve_spa_asset`` treats them like build artifacts rather than client routes, and
this pins that. Written as its own file rather than added to ``test_app_api.py``'s ``TestSPAServing``
because that file carries the FR-18 auth-migration tests and is under active change; a new file has
no merge surface.

**These paths are not yet in the auth gate's public list**, so with ``RACK_WEB_AUTH_ENABLED`` they
redirect to ``/login`` and the app is uninstallable exactly when a first-time operator would try.
See ``app._is_public_spa_static_path``, whose docstring already claims to cover icons. Deliberately
not asserted here: the fix belongs to the auth work in flight, and a test that fails until someone
else lands a change is a broken build, not a reminder.
"""

from __future__ import annotations

import pytest
from app import app

#: The bundle is a gitignored build artifact, so this suite runs against real files locally and
#: against the absent-bundle path in CI. Both are correct; what must never happen is HTML.
#:
#: **These cases briefly tolerated a 503 and no longer need to.** With no bundle they used to fall
#: through to `serve_spa_asset`'s "Frontend not built" page — deliberate for a *client route*,
#: where the audience is a human in a browser, and wrong for an install asset, where the audience
#: is a browser's manifest fetcher that reports nothing and simply drops the icon. The install
#: assets are now classified as build artifacts (`_SPA_ARTIFACT_PREFIXES` / `_SPA_ARTIFACT_NAMES`
#: in `api/_common.py`), so a miss is a 404 whether or not a bundle exists — which makes the
#: original contract here true unconditionally, and a 503-tolerant assertion strictly weaker.
INSTALL_ASSETS = ("/manifest.webmanifest", "/icons/greenlight-192.png", "/icons/greenlight-512.png")


@pytest.mark.parametrize("path", INSTALL_ASSETS)
def test_install_asset_is_served_or_404s_but_is_never_html(authed_client, path):
    """The content-type contract, exercised through the real gate.

    Uses ``conftest``'s ``authed_client`` rather than a local client. An earlier version of this
    file built its own and patched ``app._auth_enabled`` to switch the gate off; that name was
    deleted the same afternoon by ``35c46c2 feat(auth)!: refuse to serve without an administrator,
    a policy and TLS``, which makes the gate unconditional. Patching a private name in a module
    somebody else owns is exactly the deferred-lookup hazard ``backend-api.mdc`` warns about, and
    it broke here in under an hour. ``authed_client`` is the supported handle and moves with them.
    """
    resp = authed_client.get(path)
    assert resp.status_code in (200, 404), f"{path} answered {resp.status_code}"
    if resp.status_code == 200:
        expected = "image/png" if path.endswith(".png") else "application/manifest+json"
        assert (resp.content_type or "").startswith(expected), resp.content_type
    else:
        assert "text/html" not in (resp.content_type or ""), "a miss must not be the SPA shell"
        assert resp.get_json()["ok"] is False


def test_a_missing_icon_directory_entry_does_not_serve_the_shell(authed_client):
    """The `icons/` prefix guard, not just the files that happen to exist.

    ``/icons.svg`` (the in-app sprite sheet) and ``/icons/`` (the app icons) are different things
    one character apart, and only the latter is guarded. A name nobody types is always a broken
    deploy, so it must 404.
    """
    resp = authed_client.get("/icons/greenlight-does-not-exist.png")
    assert resp.status_code == 404
    assert "text/html" not in (resp.content_type or "")
    assert resp.get_json()["ok"] is False


@pytest.mark.xfail(
    strict=False,
    reason=(
        "app._is_public_spa_static_path() omits the manifest and the /icons/ prefix, so the gate "
        "302s them to /login. A browser reads both WITHOUT a session, so the dashboard is not "
        "installable. Fix is two entries in that list; it belongs to the auth work in flight, "
        "hence xfail rather than a red build. strict=False so landing the fix does not fail "
        "their pipeline on an unexpected pass -- delete this test's marker when it goes green."
    ),
)
@pytest.mark.parametrize("path", INSTALL_ASSETS)
def test_install_assets_are_reachable_without_a_session(path):
    """A logged-out browser must be able to read the manifest and icons.

    Not a style preference -- it is how installability works. Chrome fetches
    ``/manifest.webmanifest`` and its icons from the login page, before any session exists, and
    reports nothing when they fail: it simply never offers "Install". The function's own docstring
    already says "Vite build assets *and icons* must be served without a session"; ``/icons.svg``
    was just the only icon file when it was written.

    A deliberately unauthenticated client, since the whole point is the no-session path.
    """
    app.config["TESTING"] = True
    with app.test_client() as anonymous:
        resp = anonymous.get(path)
    assert resp.status_code in (200, 404), f"{path} answered {resp.status_code}"
