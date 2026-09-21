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
"""`/api/scripts/deploy` and its preview.

The status codes carry meaning here and are the thing worth pinning: a preview must answer
on a deployment whose SSH does not work yet, and a failure has to say whether the cluster or
the image was at fault -- those lead to different fixes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module

_OK = {
    "ok": True, "dry_run": False, "dest": "/base/scripts", "files": 109,
    "bytes": 282879, "layers": ["multi-platform"], "chunks": 5,
}


@pytest.fixture
def client():
    return app_module.app.test_client()


class TestThePreview:
    def test_it_answers_without_touching_the_cluster(self, client, monkeypatch):
        # The wizard shows this before the operator has proven SSH works, so a preview that
        # needed the head would be blank exactly when it is most useful.
        def fail(*_a, **_k):
            raise AssertionError("a preview must not open a connection")

        monkeypatch.setattr("host_exec.connection", fail)
        body = client.get("/api/scripts/deploy-preview").get_json()
        assert body["ok"] is True and body["dry_run"] is True
        assert body["files"] > 0

    def test_it_reports_the_destination_so_the_operator_can_check_it(self, client):
        body = client.get("/api/scripts/deploy-preview").get_json()
        assert body["dest"] and body["layout"]["tree_root"]


class TestTheDeploy:
    def test_a_success_reports_what_was_sent(self, client, monkeypatch):
        monkeypatch.setattr("api.script_deploy.script_deploy.deploy", lambda **_k: dict(_OK))
        response = client.post("/api/scripts/deploy", json={})
        assert response.status_code == 200
        assert response.get_json()["files"] == 109

    def test_an_unreachable_head_is_a_502_not_a_500(self, client, monkeypatch):
        # 502 says we reached for the head and the head is what did not cooperate, which is
        # the operator's cue to fix the connection rather than the install.
        monkeypatch.setattr(
            "api.script_deploy.script_deploy.deploy",
            lambda **_k: {**_OK, "ok": False, "error": "ssh: connect timed out"},
        )
        response = client.post("/api/scripts/deploy", json={})
        assert response.status_code == 502
        assert "timed out" in response.get_json()["error"]

    def test_missing_scripts_in_the_image_is_a_500_not_a_502(self, client, monkeypatch):
        # The opposite diagnosis: nothing is wrong with the cluster, the app shipped without
        # its own scripts. Same symptom to a user, completely different fix.
        def boom(**_k):
            raise FileNotFoundError("no scripts to deploy: /app/bin/scripts is not a directory")

        monkeypatch.setattr("api.script_deploy.script_deploy.deploy", boom)
        response = client.post("/api/scripts/deploy", json={})
        assert response.status_code == 500
        assert "no scripts to deploy" in response.get_json()["error"]

    def test_a_dry_run_is_honoured_through_the_endpoint(self, client, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "api.script_deploy.script_deploy.deploy",
            lambda **k: seen.update(k) or {**_OK, "dry_run": True},
        )
        client.post("/api/scripts/deploy", json={"dry_run": True})
        assert seen == {"dry_run": True}


class TestItIsWiredLikeEveryOtherEndpoint:
    def test_both_routes_are_discovered_without_editing_app_py(self):
        rules = {r.rule for r in app_module.app.url_map.iter_rules()}
        assert {"/api/scripts/deploy", "/api/scripts/deploy-preview"} <= rules

    def test_they_inherit_the_auth_gate(self):
        from identity import testing

        with testing.unauthenticated_client(app_module.app) as anonymous:
            assert anonymous.get("/api/scripts/deploy-preview").status_code == 401
            assert anonymous.post("/api/scripts/deploy", json={}).status_code == 401

    def test_the_write_is_a_post_and_the_preview_is_a_get(self):
        methods = {
            r.rule: r.methods for r in app_module.app.url_map.iter_rules()
            if r.rule.startswith("/api/scripts/")
        }
        assert "POST" in methods["/api/scripts/deploy"]
        assert "POST" not in methods["/api/scripts/deploy-preview"]
