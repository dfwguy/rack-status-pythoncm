# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Read-only canonical rack visibility and exact rack-scoped planning."""
import gc
import sqlite3
import time
from unittest import mock

import pytest
from cluster_snapshot import SnapshotRepository, read_existing_snapshot
from clusterkit_inventory import directory, require_current_inventory, resolve_inventory
from clusterkit_planner import PlanningError, parse_inventory, plan_request
from clusterkit_profile import parse_profile
from clusterkit_service import profile_digest


def node(name, rack="R1", state="idle"):
    return {"id": name, "rack": rack, "status": "up", "scheduler_state": state,
            "scheduler_status": "idle" if state == "idle" else "unknown"}


def write_snapshot(path, nodes=None, *, captured=None, cluster="catalog:test/nc", log_root="/logs", generation=1):
    repo = SnapshotRepository(path, clock=lambda: captured or time.time())
    snapshot, acquired = repo.acquire_refresh_lease(cluster, log_root, generation, "test-writer", 30)
    assert acquired
    repo.commit_success(snapshot.snapshot_key, "test-writer",
                        {"ok": True, "rack_mapping_mode": "translate",
                         "node_sources": {"orchestrator": "bcm", "scheduler": "slurm"},
                         "rows": [{"rack": "R1"}, {"rack": "R2"}],
                         "nodes": nodes if nodes is not None else [node(f"node-{n}") for n in range(1, 9)]})
    # The fixture intentionally produces a checkpoint; the consumer never does.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gc.collect()  # Close fixture-writer connections before read-only side-effect assertions.
    return repo


def profile(tmp_path, *, allowed=None, source=True):
    raw = {"enabled": True, "partition": "validation", "hpcx_init": "/workspace/hpcx-init.sh",
           "results_root": str(tmp_path / "results"), "expected_architecture": "aarch64",
           "expected_link_layer": "Ethernet", "hca": "rail:1", "path_comparison": True,
           "allowed_node_counts": [2, 4, 8], "max_nodes": 8,
           "node_allowlist": allowed if allowed is not None else [f"node-{n}" for n in range(1, 9)]}
    if source:
        raw["rack_inventory"] = {"snapshot_db": str(tmp_path / "inventory.sqlite3"),
                                 "cluster_id": "catalog:test/nc", "log_root": "/logs",
                                 "checkpoint_only": True, "max_age_seconds": 300}
    return raw


class Reader:
    def __init__(self, state="idle"):
        self.state, self.calls = state, []

    def query(self, candidates):
        self.calls.append(candidates)
        return parse_inventory("\n".join(f"{n}|{self.state}|validation" for n in candidates), candidates)


def test_missing_snapshot_has_zero_filesystem_side_effects(tmp_path):
    path = tmp_path / "missing" / "inventory.sqlite3"
    assert read_existing_snapshot(path, "catalog:test/nc", "/logs", checkpoint_only=True) is None
    assert not path.parent.exists()
    resolution = resolve_inventory(parse_profile(profile(tmp_path)))
    assert directory(resolution)["snapshot"]["state"] == "unavailable"
    reader = Reader()
    with pytest.raises(ValueError, match="canonical rack inventory"):
        require_current_inventory(resolution)
    assert not reader.calls
    assert not (tmp_path / "results").exists()


def test_reader_is_scoped_read_only_and_never_initializes_or_collects(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    write_snapshot(path)
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir() if p.is_file()}
    with mock.patch.object(SnapshotRepository, "_initialize", side_effect=AssertionError("No constructor")):
        snapshot = read_existing_snapshot(path, "catalog:test/nc", "/logs", checkpoint_only=True)
    assert snapshot and len(snapshot.payload["nodes"]) == 8
    assert read_existing_snapshot(path, "catalog:other/nc", "/logs", checkpoint_only=True) is None
    assert read_existing_snapshot(path, "catalog:test/nc", "/other", checkpoint_only=True) is None
    after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in tmp_path.iterdir() if p.is_file()}
    assert before == after


def test_visibility_is_independent_of_execution_allowlist(tmp_path):
    write_snapshot(tmp_path / "inventory.sqlite3", [node("node-1"), node("unapproved", "R2")])
    resolution = resolve_inventory(parse_profile(profile(tmp_path, allowed=["node-1"])))
    result = directory(resolution)
    assert [rack["name"] for rack in result["racks"]] == ["R1", "R2"]
    assert result["racks"][0]["test_enabled"]
    assert not result["racks"][1]["test_enabled"]
    assert result["racks"][1]["counts"]["idle"] == 1
    with pytest.raises(PlanningError, match="allowlist"):
        plan_request({"scope": "group", "group": "R2", "node_count": 2}, resolution, Reader(), scripts_dir="/scripts")


@pytest.mark.parametrize(("count", "pairs"), [(1, 0), (2, 1), (4, 6), (8, 28)])
def test_exact_rack_plans_and_group_preflight(tmp_path, count, pairs):
    write_snapshot(tmp_path / "inventory.sqlite3")
    resolution = resolve_inventory(parse_profile(profile(tmp_path)))
    reader = Reader()
    request = {"scope": "preflight" if count == 1 else "group", "group": "R1", "node_count": count}
    plan = plan_request(request, resolution, reader, scripts_dir="/scripts")
    assert plan["nodes"] == [f"node-{n}" for n in range(1, count + 1)]
    assert plan["expected_pair_count"] == pairs
    assert plan["resources"]["nodes"] == count and plan["resources"]["ntasks_per_node"] == 1
    assert "--gres=none" in plan["sbatch_argv"] and "--exclusive" not in plan["sbatch_argv"]
    assert plan["dry_run"] and not plan["submitted"]
    assert not (tmp_path / "results").exists()


def test_stale_topology_remains_visible_but_cannot_plan(tmp_path):
    write_snapshot(tmp_path / "inventory.sqlite3", captured=time.time() - 1000)
    resolution = resolve_inventory(parse_profile(profile(tmp_path)))
    assert directory(resolution)["snapshot"]["state"] == "stale"
    assert len(directory(resolution)["racks"]) == 2
    reader = Reader()
    with pytest.raises(ValueError, match="stale"):
        plan_request({"scope": "group", "group": "R1", "node_count": 2}, resolution, reader, scripts_dir="/scripts")
    assert not reader.calls


def test_flags_and_missing_placement_do_not_become_usable_idle_nodes(tmp_path):
    write_snapshot(tmp_path / "inventory.sqlite3", [
        node("node-1", state="idle*"), node("node-2", state="idle+drain"),
        node("node-3", state="down*"), node("node-4", state="alloc"),
        node("unplaced", rack=None)])
    result = directory(resolve_inventory(parse_profile(profile(tmp_path))))
    assert result["racks"][0]["counts"] == {"idle": 0, "busy": 1, "drain": 1, "down": 1, "reserved": 0, "unknown": 1}
    assert not result["racks"][1]["test_enabled"]
    assert result["unplaced_node_count"] == 1


@pytest.mark.parametrize("bad_nodes", [
    [node("node-1"), node("node-1", "R2")],
    [node("node-1"), node("NODE-1", "R2")],
    [node("node-1;touch")],
    [node("node-1", "R1;touch")],
])
def test_ambiguous_or_injected_inventory_fails_closed(tmp_path, bad_nodes):
    write_snapshot(tmp_path / "inventory.sqlite3", bad_nodes)
    resolution = resolve_inventory(parse_profile(profile(tmp_path)))
    assert directory(resolution)["snapshot"]["state"] == "unavailable"
    with pytest.raises(ValueError):
        require_current_inventory(resolution)


def test_membership_changes_alter_confirmation_policy_digest(tmp_path):
    path = tmp_path / "inventory.sqlite3"
    write_snapshot(path)
    first = resolve_inventory(parse_profile(profile(tmp_path)))
    write_snapshot(path, [node(f"node-{n}") for n in range(2, 9)], generation=2)
    second = resolve_inventory(parse_profile(profile(tmp_path)))
    assert profile_digest(first) != profile_digest(second)


def test_legacy_serialization_and_manual_groups_are_preserved(tmp_path):
    raw = profile(tmp_path, source=False)
    raw["topology_groups"] = {"manual": {"nodes": ["node-1", "node-2"], "kind": "group"}}
    resolution = parse_profile(raw)
    assert resolution.ready and "rack_inventory" not in resolution.profile.as_dict()
    assert directory(resolution)["racks"][0]["name"] == "manual"
    plan = plan_request({"scope": "explicit", "node_count": 2, "nodes": ["node-1", "node-2"]},
                        resolution, Reader(), scripts_dir="/scripts")
    assert plan["nodes"] == ["node-1", "node-2"]


@pytest.mark.parametrize("change", [
    {"snapshot_db": "../../etc/shadow"}, {"cluster_id": ""},
    {"log_root": "/logs/../secrets"}, {"checkpoint_only": "yes"}, {"checkpoint_only": False},
    {"max_age_seconds": 0}, {"unknown": "override"}])
def test_invalid_admin_inventory_source_is_rejected(tmp_path, change):
    raw = profile(tmp_path)
    raw["rack_inventory"].update(change)
    assert not parse_profile(raw).ready
