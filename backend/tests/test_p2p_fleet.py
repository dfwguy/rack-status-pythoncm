# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the /api/p2p gateway blueprint (backend/api/p2p_fleet.py)."""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from app import app
from catalog import models, seed
from catalog.context import get_active
from catalog.store import TABLE_PROJECTS, TinyDBRepo, get_repo, set_repo


@pytest.fixture
def client(tmp_path):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


def _set_active_p2p(enabled: bool) -> None:
    """Flip `p2p_enabled` on the active cluster, the way the Edit Cluster dialog would."""
    repo = get_repo()
    active = get_active(repo)
    project = models.Project.from_dict(repo.get(TABLE_PROJECTS, active["project"]))
    for cluster in project.clusters:
        if cluster.id == active["cluster"]:
            cluster.p2p_enabled = enabled
    repo.upsert(TABLE_PROJECTS, project.to_dict())


class TestP2pStatus:
    def test_status_404_when_p2p_disabled(self, client):
        # Default seed leaves P2P off, so the gateway must not disclose a fleet for it.
        resp = client.get("/api/p2p/status")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_status_greenfield_when_enabled(self, client):
        _set_active_p2p(True)
        resp = client.get("/api/p2p/status")
        assert resp.status_code == 200
        body = resp.get_json()
        # Enabled but unprovisioned: real catalog, empty coverage, object store down.
        assert body["provisioned"] is False
        assert body["object_store_healthy"] is False
        assert body["coverage"]["nodes"] == []
        assert body["operations"] == []
        # The shipped catalog manifest is surfaced, with runtime observations blanked.
        assert isinstance(body["catalog"]["images"], list)
        assert body["catalog"]["images"], "expected the shipped catalog manifest to be non-empty"
        first = body["catalog"]["images"][0]
        assert first["canonical_present"] is False
        assert first["persistent_import"] is None
        assert {"order", "name", "filename", "bytes", "sha256"} <= set(first)

    def test_operations_points_at_provision(self, client):
        _set_active_p2p(True)
        resp = client.post("/api/p2p/operations", json={"action": "sync", "sus": [1]})
        assert resp.status_code == 501
        assert "/api/p2p/provision" in resp.get_json()["message"]


class TestP2pProvision:
    def test_provision_404_when_disabled(self, client):
        resp = client.post("/api/p2p/provision", json={})
        assert resp.status_code == 404

    def test_provision_starts_the_reconcile_run(self, client, monkeypatch):
        _set_active_p2p(True)
        calls = {}

        def _fake_start(workflow_id, params=None):
            calls["workflow_id"] = workflow_id
            calls["params"] = params
            return {"run_id": "run-abc", "state": "pending"}

        import workflow_engine

        monkeypatch.setattr(workflow_engine, "start_workflow_run", _fake_start)
        resp = client.post("/api/p2p/provision", json={})
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["ok"] is True
        assert body["run_id"] == "run-abc"
        assert body["poll"] == "/api/workflow-runs/run-abc"
        # It triggers the shipped reconcile workflow, scoped to the active cluster.
        assert calls["workflow_id"] == "nvis-p2p-provision"
        assert calls["params"]["cluster_id"]

    def test_reconcile_workflow_is_shipped_and_wellformed(self):
        import workflow_engine

        wf = workflow_engine.get_workflow("nvis-p2p-provision")
        assert wf is not None, "nvis-p2p-provision.yaml must ship in var/workflows/"
        step_ids = [s.get("id") for s in wf.get("steps", [])]
        assert "preflight" in step_ids
        # The first mutating work must sit behind the read-only preflight.
        assert step_ids[0] == "start" and "preflight" in step_ids
