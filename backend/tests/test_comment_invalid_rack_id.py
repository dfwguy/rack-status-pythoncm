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
"""PUT /api/comments/<rack_id> must reject an unparseable id with 400, not 500.

The guard returned ``jsonify({...}),`` with the ``400`` on the next line, which Python read as a
one-tuple followed by a dead expression statement. Flask rejects a one-tuple response, so the
branch that exists to explain the mistake raised TypeError and answered 500 with no message.
ruff's B018 (useless expression) is what surfaced it.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module  # noqa: E402


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    # Let the view's own error handling answer; do not re-raise into the test client.
    app_module.app.config.update(PROPAGATE_EXCEPTIONS=False)
    with app_module.app.test_client() as c:
        yield c


@pytest.mark.parametrize(
    "rack_id",
    [
        "not-a-rack",          # neither pN-rMM nor an mr: log basename
        "mr:",                 # mr: prefix with nothing after it
        "mr:not-a-log",        # mr: prefix, unusable basename
    ],
)
def test_an_unparseable_rack_id_is_a_400_with_a_reason(client, rack_id):
    res = client.put(f"/api/comments/{rack_id}", json={"text": "hello"})
    assert res.status_code == 400, f"got {res.status_code}: {res.get_data(as_text=True)[:200]}"
    body = res.get_json()
    assert body is not None and body.get("ok") is False
    assert "invalid rack id" in str(body.get("error", "")).lower()
