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
"""`GET /api/scheduler-probe` — is the cluster's scheduler able to take work?

The endpoint's whole value is distinguishing three ways a launch can fail to start, so what
matters is that each one produces a *different, named* answer rather than a generic error.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as app_module


@pytest.fixture
def client():
    return app_module.app.test_client()


def _probe(client):
    response = client.get("/api/scheduler-probe")
    assert response.status_code == 200, response.get_data(as_text=True)
    return response.get_json()


class TestItReportsWhatItFound:
    def test_a_ready_backend_says_so_and_names_itself(self, client, monkeypatch):
        monkeypatch.setattr(
            "api.scheduler_health.build_scheduler",
            lambda *a, **k: type("S", (), {
                "kind": "pythoncm", "probe": staticmethod(lambda: {"ready": True}),
            })(),
        )
        body = _probe(client)
        assert body["ok"] is True and body["ready"] is True
        assert body["scheduler"] == "pythoncm"

    def test_an_unreachable_backend_is_a_200_carrying_the_reason(self, client, monkeypatch):
        # Not a 5xx: an unreachable scheduler is the fact this endpoint exists to report,
        # not a failure to answer the question.
        def boom():
            raise RuntimeError("pythoncm probe failed: cert/key pair rejected")

        monkeypatch.setattr(
            "api.scheduler_health.build_scheduler",
            lambda *a, **k: type("S", (), {"kind": "pythoncm", "probe": staticmethod(boom)})(),
        )
        body = _probe(client)
        assert body["ok"] is False and body["ready"] is False
        # The operator needs the cause, because all three prerequisites fail identically
        # from the outside.
        assert "cert/key pair" in body["error"]

    def test_a_backend_with_no_probe_says_unknown_rather_than_ready(self, client, monkeypatch):
        # Slurm has no readiness call cheaper than submitting. Claiming "ready" on no
        # evidence would be worse than admitting we did not look.
        monkeypatch.setattr(
            "api.scheduler_health.build_scheduler",
            lambda *a, **k: type("S", (), {"kind": "slurm"})(),
        )
        body = _probe(client)
        assert body["ok"] is True
        assert body["ready"] is None
        assert "no readiness probe" in body["detail"]

    def test_a_backend_that_cannot_even_be_built_still_answers(self, client, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("no connection configured")

        monkeypatch.setattr("api.scheduler_health.build_scheduler", boom)
        body = _probe(client)
        assert body["ok"] is False and body["ready"] is False


class TestItIsWiredLikeEveryOtherEndpoint:
    def test_it_is_discovered_without_editing_app_py(self):
        assert "/api/scheduler-probe" in {r.rule for r in app_module.app.url_map.iter_rules()}

    def test_it_inherits_the_auth_gate(self):
        from identity import testing

        with testing.unauthenticated_client(app_module.app) as anonymous:
            assert anonymous.get("/api/scheduler-probe").status_code == 401

    def test_it_never_submits_anything(self, client, monkeypatch):
        calls = []

        class Probed:
            kind = "pythoncm"

            @staticmethod
            def probe():
                calls.append("probe")
                return {"ready": True}

            @staticmethod
            def submit(_spec):
                raise AssertionError("a readiness check must not queue work")

        monkeypatch.setattr("api.scheduler_health.build_scheduler", lambda *a, **k: Probed())
        _probe(client)
        assert calls == ["probe"]
