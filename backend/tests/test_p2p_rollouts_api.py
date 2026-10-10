# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The /api/p2p/rollouts surface: start, list, preview, detail, cancel, retry, pace."""

from __future__ import annotations

import os
import time

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import p2p_rollout as pr
import workflow_engine as we
from api import p2p_rollouts as route
from app import app
from catalog import models, seed
from catalog.context import get_active
from catalog.store import TABLE_PROJECTS, TinyDBRepo, get_repo, set_repo

SHA = "a" * 64
INVENTORY = [{"id": f"gpu{i}-su22", "rack": "Q5", "tray": i} for i in range(1, 5)] + [{"id": "app", "rack": None}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
    monkeypatch.setattr(we, "WORKFLOWS_DIR", tmp_path / "wf")
    monkeypatch.setattr(we, "RUNS_DIR", tmp_path / "wf" / "runs")
    monkeypatch.setattr(we, "_active_runs", {})
    monkeypatch.setattr(route, "_inventory", lambda: INVENTORY)
    # A step that holds the run open until cancelled, so the controls act on a live run.

    def hold(run, step):
        while run.get("state") != "cancelled":
            time.sleep(0.02)
        return False, "cancelled"
    monkeypatch.setitem(we._NATIVE_STEPS, pr.NATIVE_STEP, hold)
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    for r in list(we._active_runs.values()):
        if r.get("state") not in we.TERMINAL_STATES:
            we.cancel_run(r["run_id"])
    set_repo(None)


def _enable(desired_dir="/app/p2p-desired", catalog=True):
    import p2p_catalog
    import p2p_config

    repo = get_repo()
    active = get_active(repo)
    project = models.Project.from_dict(repo.get(TABLE_PROJECTS, active["project"]))
    for cluster in project.clusters:
        if cluster.id == active["cluster"]:
            cluster.p2p_enabled = True
    repo.upsert(TABLE_PROJECTS, project.to_dict())
    p2p_config.save_settings(models.P2pSettings.from_dict({
        "cluster_id": active["cluster"], "appserver_ip": "10.10.20.22", "desired_dir": desired_dir}))
    if catalog:
        p2p_catalog.save_catalog(models.P2pCatalog.from_dict({"cluster_id": active["cluster"], "images": [
            {"order": 7, "name": "NeMo", "filename": "big.sqsh", "bytes": 10, "sha256": SHA}]}))
    return active["cluster"]


def test_everything_is_404_while_p2p_is_off(client):
    assert client.get("/api/p2p/rollouts").status_code == 404
    assert client.post("/api/p2p/rollouts", json={}).status_code == 404


def test_the_list_says_what_is_missing_before_rollouts_can_run(client, monkeypatch):
    import p2p_catalog
    monkeypatch.setattr(p2p_catalog, "publish_to_scripts", lambda *_a, **_k: {"ok": True})
    _enable(desired_dir="")
    body = client.get("/api/p2p/rollouts").get_json()
    assert body["ok"] and body["rollouts"] == [] and "desired_dir" in body["blockers"][0]
    assert body["racks"] == [{"rack": "Q5", "nodes": 4}]
    r = client.post("/api/p2p/rollouts", json={"image": "big.sqsh", "racks": ["Q5"]})
    assert r.status_code == 400 and "not configured" in r.get_json()["message"]


def test_plan_previews_members_and_agents(client, monkeypatch):
    import p2p_catalog
    monkeypatch.setattr(p2p_catalog, "publish_to_scripts", lambda *_a, **_k: {"ok": True})
    _enable()
    monkeypatch.setattr(pr, "vm_observer", lambda _base: lambda nodes, f: {
        n: pr.Observation(alive=n != "gpu4-su22", generation=0, state="ready" if n == "gpu1-su22" else "")
        for n in nodes})
    body = client.get("/api/p2p/rollouts/plan?image=big.sqsh&racks=Q5").get_json()
    rack = body["racks"][0]
    assert [n["node"] for n in rack["nodes"]] == ["gpu1-su22", "gpu2-su22", "gpu3-su22", "gpu4-su22"]
    assert rack["live_agents"] == 3 and rack["holders"] == 1 and body["image"]["sha256"] == SHA


def test_start_list_detail_pace_retry_cancel(client, monkeypatch):
    import p2p_catalog
    monkeypatch.setattr(p2p_catalog, "publish_to_scripts", lambda *_a, **_k: {"ok": True})
    _enable()
    r = client.post("/api/p2p/rollouts", json={"image": "big.sqsh", "racks": ["Q5"], "pacing": {"stages": ["2", "100%"]}})
    assert r.status_code == 201
    run_id = r.get_json()["run_id"]
    again = client.post("/api/p2p/rollouts", json={"image": "big.sqsh", "racks": ["Q5"]})
    assert again.status_code == 200 and again.get_json()["joined"] is True and again.get_json()["run_id"] == run_id

    rows = client.get("/api/p2p/rollouts").get_json()["rollouts"]
    assert [x["run_id"] for x in rows] == [run_id] and rows[0]["image"] == "big.sqsh"
    detail = client.get(f"/api/p2p/rollouts/{run_id}").get_json()["rollout"]
    assert detail["image_pin"]["sha256"] == SHA and detail["pacing"]["stages"] == ["2", "100%"]

    assert client.put(f"/api/p2p/rollouts/{run_id}/pacing", json={"max_active_racks": 2}).status_code == 200
    assert we.get_run_state(run_id)["params"]["pacing"]["max_active_racks"] == 2
    assert client.put(f"/api/p2p/rollouts/{run_id}/pacing", json={"budget_seconds": 1}).status_code == 400

    assert client.post(f"/api/p2p/rollouts/{run_id}/retry", json={"nodes": ["gpu1-su22"]}).status_code == 200
    assert we.get_run_state(run_id)["params"]["commands"][0]["nodes"] == ["gpu1-su22"]
    assert client.post(f"/api/p2p/rollouts/{run_id}/retry", json={"nodes": ['x"} or 1']}).status_code == 400

    assert client.post(f"/api/p2p/rollouts/{run_id}/cancel").get_json()["state"] == "cancelled"
    assert client.get("/api/p2p/rollouts/not-a-run").status_code == 404


@pytest.mark.parametrize("body, match", [
    ({"image": "nope.sqsh", "racks": ["Q5"]}, "not in the cluster's catalog"),
    ({"image": "big.sqsh", "racks": ["Z9"]}, "no placed nodes"),
    ({"image": "big.sqsh", "racks": ["Q5"], "pacing": {"stages": ["50%", "1"]}}, "stages"),
])
def test_bad_starts_are_400_with_the_reason(client, monkeypatch, body, match):
    import p2p_catalog
    monkeypatch.setattr(p2p_catalog, "publish_to_scripts", lambda *_a, **_k: {"ok": True})
    _enable()
    r = client.post("/api/p2p/rollouts", json=body)
    assert r.status_code == 400 and match in r.get_json()["message"]
