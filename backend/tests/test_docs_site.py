# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The documentation site is served by the app, and behaves when it is not built.

`static/docs` is a build artifact and gitignored, so a developer's tree and a CI checkout
genuinely differ here — the same divergence that makes the SPA shell answer 503. Both states are
pinned, because only one of them is reproducible locally.
"""
from __future__ import annotations

import paths
import pytest
from api import docs_site


@pytest.fixture
def unbuilt(monkeypatch, tmp_path):
    """Point DOCS_DIR at a directory that does not exist."""
    missing = tmp_path / "never-built"
    monkeypatch.setattr(docs_site, "DOCS_DIR", missing)
    return missing


@pytest.fixture
def built(monkeypatch, tmp_path):
    """A minimal built site: an index, and a subdirectory with its own index."""
    root = tmp_path / "docs"
    (root / "reference" / "identity").mkdir(parents=True)
    (root / "index.html").write_text("<h1>NVIS Greenlight</h1>", encoding="utf-8")
    (root / "reference" / "identity" / "index.html").write_text("<h1>Identity</h1>", encoding="utf-8")
    (root / "_static").mkdir()
    (root / "_static" / "custom.css").write_text(":root{}", encoding="utf-8")
    monkeypatch.setattr(docs_site, "DOCS_DIR", root)
    return root


def test_docs_dir_is_the_built_output_path():
    """A local path, unlike almost everything else in `paths` — see its comment there."""
    assert paths.DOCS_DIR.parts[-2:] == ("static", "docs")


def test_the_root_serves_the_index(authed_client, built):
    resp = authed_client.get("/docs/")
    assert resp.status_code == 200
    assert b"NVIS Greenlight" in resp.data


def test_a_directory_url_serves_its_index(authed_client, built):
    """Sphinx's own links point at directories, so this is what makes navigation work."""
    for path in ("/docs/reference/identity/", "/docs/reference/identity"):
        resp = authed_client.get(path)
        assert resp.status_code == 200, path
        assert b"Identity" in resp.data, path


def test_an_asset_is_served(authed_client, built):
    assert authed_client.get("/docs/_static/custom.css").status_code == 200


def test_an_unbuilt_site_is_a_503_with_instructions(authed_client, unbuilt):
    """Not a 404 and not a crash.

    The audience is a person in a browser, so the body is a command they can run. This is the
    same choice `api/_common.serve_spa_asset` makes for the same reason.
    """
    resp = authed_client.get("/docs/")
    assert resp.status_code == 503
    body = resp.get_data(as_text=True)
    assert "sphinx-build" in body
    assert "docs/README.md" in body


def test_a_missing_page_is_a_404_naming_the_way_back(authed_client, built):
    resp = authed_client.get("/docs/no/such/page.html")
    assert resp.status_code == 404
    assert "/docs/" in resp.get_data(as_text=True)


def test_the_route_declares_a_read_action(vocabulary):
    """Without a declaration the authorization gate refuses the route for everybody.

    A *read* verb specifically, so an existing read-only role (`["*:read", "*:list"]`) already
    covers the docs instead of needing a new grant. Read from the generated vocabulary rather
    than asserting the string against itself.
    """
    actions = {
        entry["action"]: entry
        for service in vocabulary["services"]
        for entry in service["actions"]
    }
    assert "docs:read" in actions
    assert actions["docs:read"]["kind"] == "read"


def test_the_docs_route_is_not_shadowed_by_the_spa_catch_all(built, authed_client):
    """`/<path:path>` serves the SPA shell for client routes; `/docs/...` must win over it.

    Werkzeug ranks the more specific rule first, but this is worth pinning: if it ever stopped
    winning, the symptom would be the documentation silently rendering the application.
    """
    resp = authed_client.get("/docs/reference/identity/index.html")
    assert resp.status_code == 200
    assert b"Identity" in resp.data
    assert b'<div id="root"' not in resp.data


def test_the_bare_index_url_canonicalises(authed_client, built):
    """`/docs/index.html` redirects to `/docs/`.

    Werkzeug's own doing, not ours: the root rule carries `defaults={"filename": "index.html"}`,
    which makes the two URLs equivalent and the prefixed one canonical. Recorded because a 308
    here looks like a misconfiguration until you know why.
    """
    resp = authed_client.get("/docs/index.html")
    assert resp.status_code == 308
    assert resp.headers["Location"].endswith("/docs/")
    assert authed_client.get("/docs/index.html", follow_redirects=True).status_code == 200
