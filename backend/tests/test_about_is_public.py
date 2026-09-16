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
"""`GET /api/about` is public, and carries only what a public route may.

The entry in `web_auth.AUTH_BYPASS_ENDPOINTS` is justified by a set of claims — no store read, no
credential, and a payload of nothing but product and version. These tests are what stop that entry
outliving its claims: the bypass list's own bar is "safe because", and a "because" nobody checks
decays into "convenient because" one field at a time.

The field test is the important one. Adding `cluster` or `project` here would be a natural-looking
change — every module next door has that data to hand — and it would publish a customer's cluster
name to anyone who can reach the port.
"""

from __future__ import annotations

import pytest
from identity import testing

#: Everything the response may contain. A new key is a deliberate act that fails here first.
ALLOWED_KEYS = {"ok", "product", "version"}

#: Substrings that must never appear in the body, whatever it is called. Named for what an
#: unauthenticated caller must not learn rather than for the exact field an author might add.
FORBIDDEN = (
    "cluster",
    "customer",
    "project",
    "platform",
    "hostname",
    "node",
    "ssh",
    "path",
    "log_dir",
    "token",
    "secret",
)


@pytest.fixture
def anonymous():
    """No credential whatsoever: not a cookie, not a header."""
    import app as app_module

    app_module.app.config["TESTING"] = True
    with testing.unauthenticated_client(app_module.app) as client:
        yield client


def test_it_answers_without_a_credential(anonymous):
    """The whole point. A gated version endpoint is what made a public page call an authenticated
    one, which is the coupling this route removes."""
    response = anonymous.get("/api/about")
    assert response.status_code == 200, (
        f"/api/about answered {response.status_code} anonymously; the documentation cannot name "
        "the build it describes without it"
    )
    assert response.get_json()["ok"] is True


def test_it_reports_the_same_version_the_rest_of_the_app_does(anonymous):
    """Not a second derivation. Two paths to "what build is this" is how two screens come to
    disagree, which is the problem `app_version` was extracted to end."""
    import app_version

    assert anonymous.get("/api/about").get_json()["version"] == app_version.app_version()


def test_it_carries_nothing_but_product_and_version(anonymous):
    body = anonymous.get("/api/about").get_json()
    extra = set(body) - ALLOWED_KEYS
    assert not extra, (
        f"/api/about grew {sorted(extra)}. It is unauthenticated: every field is published to "
        "anyone who can reach the port. Add it to a gated endpoint instead, or justify it in the "
        "module docstring and widen ALLOWED_KEYS deliberately."
    )


@pytest.mark.parametrize("word", FORBIDDEN)
def test_it_leaks_nothing_about_the_deployment(anonymous, word):
    """Deliberately checks the serialised body rather than the keys: a nested object, or a value
    that happens to be a path, is the same disclosure as a badly named field."""
    raw = anonymous.get("/api/about").get_data(as_text=True).lower()
    assert word not in raw, f"/api/about mentions {word!r} to an anonymous caller: {raw}"


def test_it_sets_no_cookie(anonymous):
    """A public route that mints or refreshes anything is not a public route."""
    assert "Set-Cookie" not in anonymous.get("/api/about").headers


def test_it_ignores_whatever_a_caller_sends(anonymous):
    """No body, no query parameters, so there is no input to validate — and that claim is part of
    why the bypass entry is defensible. Pinned so it stays true."""
    plain = anonymous.get("/api/about").get_json()
    assert anonymous.get("/api/about?cluster=../../etc&x=1").get_json() == plain
