# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the /api/catalog Flask blueprint."""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from app import app
from catalog import seed
from catalog.store import TinyDBRepo, set_repo


@pytest.fixture
def client(tmp_path):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


class TestCatalogApi:
    def test_overview(self, client):
        data = client.get("/api/catalog/overview").get_json()
        assert data["ok"] is True
        assert len(data["platforms"]) == len(seed.SEED_PLATFORMS)
        assert len(data["tools"]) == len(seed.SEED_TOOLS)
        assert data["active"]["project"] == "default"

    def test_vocab(self, client):
        data = client.get("/api/catalog/vocab").get_json()
        assert "GB200" in data["gpu_models"]
        assert "bcm" in data["cluster_managers"]
        # The scheduler dropdowns (cluster editor + setup wizard) read this list; without it
        # the UI cannot offer pythoncm and the field stays hand-edited in the store.
        assert "slurm" in data["schedulers"]
        assert "pythoncm" in data["schedulers"]

    def test_list_and_get_platform(self, client):
        data = client.get("/api/catalog/platforms").get_json()
        assert any(p["id"] == "gb200-nvl72" for p in data["items"])
        one = client.get("/api/catalog/platforms/gb200-nvl72").get_json()
        assert one["item"]["gpu_model"] == "GB200"

    def test_wizard_platform_list_includes_dgx_b300(self, client):
        response = client.get("/api/catalog/platforms")
        assert response.status_code == 200
        platform = next(p for p in response.get_json()["items"] if p["id"] == "dgx-b300")
        assert platform["name"] == "DGX B300"
        assert platform["geometry"]["gpus_per_node"] == 8

    def test_create_update_delete_platform(self, client):
        # create (id slug derived from name)
        r = client.post("/api/catalog/platforms", json={"name": "Test Plat", "gpu_model": "B200"})
        assert r.status_code == 200
        pid = r.get_json()["item"]["id"]
        assert pid == "test-plat"
        # update
        r2 = client.put(f"/api/catalog/platforms/{pid}", json={"name": "Test Plat", "gpu_model": "B300"})
        assert r2.get_json()["item"]["gpu_model"] == "B300"
        # delete
        assert client.delete(f"/api/catalog/platforms/{pid}").get_json()["deleted"] == pid
        assert client.get(f"/api/catalog/platforms/{pid}").status_code == 404

    def test_create_invalid_tool_returns_400(self, client):
        r = client.post("/api/catalog/tools", json={"name": "No Script Tool"})
        assert r.status_code == 400
        assert any("script" in e for e in r.get_json()["errors"])

    def test_tools_filtered_by_cluster(self, client):
        # gb200 cluster gets mnnvl/imex tools
        data = client.get("/api/catalog/tools?cluster=default").get_json()
        ids = {t["id"] for t in data["items"]}
        assert "mnubergemm-stress" in ids
        assert "sysinfo" in ids

    def test_set_active_validation(self, client):
        assert client.put("/api/catalog/active", json={"project": "nope"}).status_code == 400
        ok = client.put("/api/catalog/active", json={"project": "default", "cluster": "default"})
        assert ok.get_json()["active"]["cluster"] == "default"

    def test_active_includes_ai_context(self, client):
        data = client.get("/api/catalog/active").get_json()
        assert data["platform"]["id"] == "gb200-nvl72"
        assert "Customer: Lab" in data["ai_context"]
