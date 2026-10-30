# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack HTTP contracts share existing authentication, profile limits and planner."""
import json
from pathlib import Path

import pytest

from .test_clusterkit_api import api_env as inherited_api_env
from .test_clusterkit_rack_inventory import node, write_snapshot


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    yield from inherited_api_env.__wrapped__(tmp_path, monkeypatch)


def configure(env, monkeypatch):
    path = env.root / "canonical.sqlite3"
    write_snapshot(path, [node(name, "R1") for name in env.nodes] + [node("unapproved-node", "R2")])
    profile = dict(env.profile, topology_groups={},
                   rack_inventory={"snapshot_db": str(path), "cluster_id": "catalog:test/nc", "log_root": "/logs",
                                   "checkpoint_only": True, "max_age_seconds": 300})
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps(profile))
    return path


def test_all_racks_visible_but_live_limits_are_enforced(api_env, monkeypatch):
    env = api_env
    configure(env, monkeypatch)
    response = env.client.get("/api/clusterkit/racks")
    assert response.status_code == 200, response.get_json()
    racks = response.get_json()["racks"]
    assert [rack["name"] for rack in racks] == ["R1", "R2"]
    assert racks[0]["test_enabled"] and not racks[1]["test_enabled"]
    inventory = env.client.get("/api/clusterkit/inventory?group=R2")
    assert inventory.status_code == 200
    assert inventory.get_json()["inventory"]["nodes"][0]["allowed"] is False
    response = env.client.post("/api/clusterkit/preview", json={"scope": "group", "group": "R2", "node_count": 2})
    assert response.status_code == 409
    assert not env.slurm.submissions and not list(env.results.iterdir())


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_rack_previews_are_side_effect_free(api_env, monkeypatch, count):
    env = api_env
    configure(env, monkeypatch)
    before = sorted(env.root.rglob("*"))
    response = env.client.post("/api/clusterkit/preview",
        json={"scope": "preflight" if count == 1 else "group", "group": "R1", "node_count": count})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["plan"]["nodes"] == env.nodes[:count]
    assert response.get_json()["plan"]["expected_pair_count"] == count * (count - 1) // 2
    assert sorted(env.root.rglob("*")) == before
    assert not env.slurm.submissions


def test_topology_change_invalidates_a_signed_confirmation(api_env, monkeypatch):
    env = api_env
    path = configure(env, monkeypatch)
    response = env.client.post("/api/clusterkit/preview", json={"scope": "group", "group": "R1", "node_count": 2})
    assert response.status_code == 200
    token = response.get_json()["confirmation_token"]
    write_snapshot(path, [node(name, "R1") for name in env.nodes[1:]], generation=2)
    response = env.client.post("/api/clusterkit/runs", json={"confirm": True, "confirmation_token": token})
    assert response.status_code == 409
    assert not env.slurm.submissions and not list(env.results.iterdir())


def test_rack_visibility_requires_authentication(api_env, monkeypatch):
    from identity.testing import unauthenticated
    env = api_env
    configure(env, monkeypatch)
    response = env.client.get("/api/clusterkit/racks", headers=unauthenticated())
    assert response.status_code == 401
    assert not env.slurm.calls


def test_stale_confirmation_is_rejected_before_any_submission(api_env, monkeypatch):
    import time
    env = api_env
    path = configure(env, monkeypatch)
    response = env.client.post("/api/clusterkit/preview", json={"scope": "group", "group": "R1", "node_count": 2})
    token = response.get_json()["confirmation_token"]
    write_snapshot(path, [node(name, "R1") for name in env.nodes], captured=time.time() - 1000, generation=2)
    response = env.client.post("/api/clusterkit/runs", json={"confirm": True, "confirmation_token": token})
    assert response.status_code == 409
    assert "stale" in response.get_json()["error"]
    assert not env.slurm.submissions and not list(env.results.iterdir())


def test_disabled_profile_can_show_racks_but_cannot_preview(api_env, monkeypatch):
    env = api_env
    configure(env, monkeypatch)
    raw = json.loads(__import__("os").environ["RACK_CLUSTERKIT_SITE_PROFILE"])
    raw["enabled"] = False
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps(raw))
    response = env.client.get("/api/clusterkit/racks")
    assert response.status_code == 200
    assert all(not rack["test_enabled"] for rack in response.get_json()["racks"])
    response = env.client.post("/api/clusterkit/preview", json={"scope": "group", "group": "R1", "node_count": 2})
    assert response.status_code == 409
    assert not env.slurm.submissions


def test_existing_attempt_survives_rack_inventory_expansion(api_env, monkeypatch):
    from .test_clusterkit_api import submit
    from .test_clusterkit_service import write_passing_summary
    env = api_env
    previous = submit(env)
    activity = env.registry.get_activity(previous["activity_id"])
    plan = json.loads((Path(activity["log_dir"]) / "plan.json").read_text())
    write_passing_summary(plan, Path(activity["log_dir"]))
    env.slurm.state = "COMPLETED"
    configure(env, monkeypatch)
    history = env.client.get("/api/clusterkit/runs")
    assert history.status_code == 200, history.get_json()
    assert previous["activity_id"] in {row["activity_id"] for row in history.get_json()["runs"]}
    response = env.client.get(previous["status_url"])
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["summary"]["functional_status"] == "PASS"
    assert response.get_json()["summary"]["performance_status"] == "UNVERIFIED"
    assert env.registry.get_activity(previous["activity_id"])["log_dir"] == activity["log_dir"]
    assert len(env.slurm.submissions) == 1
