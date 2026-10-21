# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A launch against a rack runs on the hosts the dashboard placed in it.

On Nscale NC (2026-10-10) a multirack NCCL dry run on the 15 eligible racks failed before submitting:
``nvis_nodes: rack 'Q5' matched 0 Slurm nodes with any of: ^Q5-[0-9]+$ ...``. The launcher expanded
rack ids by hostname pattern, and the shared server resolver matched by hostname prefix; NC's rack Q5
holds sys1-gso1-p-phy-gpu...-su.. hosts, so both found nothing. Outside ``direct`` mode the members
now come from the node inventory, and the multirack NCCL launch hands the launcher that nodelist.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module
import native_status as ns
import pytest
import slurm_query
from api import validation_runs

Q5 = [f"sys1-gso1-p-phy-gpu{n}-su5" for n in (10, 2, 1)]  # out of order on purpose
U20 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in (19, 20)]


def _inventory() -> list[dict]:
    rows = [{"id": h, "rack": "Q5", "status": "up", "scheduler_state": "idle", "ip": ""} for h in Q5]
    rows += [{"id": h, "rack": "U20", "status": "up", "scheduler_state": "idle", "ip": ""} for h in U20]
    rows.append({"id": "appserver-nc", "rack": None, "status": "up", "scheduler_state": "idle", "ip": ""})
    return rows


@pytest.fixture
def placed(monkeypatch):
    """A built dashboard state for the active cluster, holding NC-shaped placement."""
    st = SimpleNamespace(node_inventory_snapshot=lambda: {"nodes": _inventory()})
    monkeypatch.setattr(ns, "_states", {ns.native_state_key(Path("/home/cmods/nvis/logs")): st})
    monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "translate")
    monkeypatch.setattr(ns, "_snapshot_placement_nodes", lambda: None)  # no canonical snapshot either
    monkeypatch.setattr(slurm_query, "_all_slurm_nodenames",
                        lambda: pytest.fail("translate must not match racks by hostname prefix"))


class TestPlacedRackMembers:
    def test_members_in_natural_order_whatever_log_root_built_them(self, placed):
        assert ns.placed_rack_members("q5") == [f"sys1-gso1-p-phy-gpu{n}-su5" for n in (1, 2, 10)]

    def test_an_empty_rack_is_empty_and_no_state_is_none(self, placed, monkeypatch):
        assert ns.placed_rack_members("Z9") == []
        monkeypatch.setattr(ns, "_states", {})
        assert ns.placed_rack_members("Q5") is None

    def test_another_clusters_state_is_not_read(self, placed, monkeypatch):
        monkeypatch.setattr(ns.config_env, "_eget", lambda key, default="": (
            "other-cluster" if key == "NVIS_CLUSTER_ID" else default
        ))
        assert ns.placed_rack_members("Q5") is None


class TestPlacementWithoutThisWorkersInventory:
    """Right after a restart, or in a worker that never rendered the dashboard, there is no worker state:
    placement comes from the canonical cluster snapshot that every worker shares (Nscale NC, 2026-10-20:
    every rack name in a multi-rack preset launch resolved to no node)."""

    def test_rack_members_and_host_racks_come_from_the_snapshot(self, placed, monkeypatch):
        monkeypatch.setattr(ns, "_states", {})
        monkeypatch.setattr(ns, "_snapshot_placement_nodes", _inventory)
        assert ns.placed_rack_members("Q5") == [f"sys1-gso1-p-phy-gpu{n}-su5" for n in (1, 2, 10)]
        assert ns.placed_racks_of([U20[0], Q5[0], "elsewhere"]) == {U20[0]: "U20", Q5[0]: "Q5"}
        assert slurm_query._rack_node_hostnames_from_inventory("U20") == U20

    def test_this_workers_own_inventory_still_wins(self, placed, monkeypatch):
        monkeypatch.setattr(ns, "_snapshot_placement_nodes",
                            lambda: pytest.fail("the snapshot is only the fallback"))
        assert ns.placed_rack_members("U20") == U20


class TestSharedResolver:
    def test_translate_takes_members_from_the_inventory(self, placed):
        assert slurm_query._rack_node_hostnames_from_inventory("U20") == U20

    def test_direct_still_matches_by_hostname_prefix(self, monkeypatch):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "direct")
        monkeypatch.setattr(slurm_query, "_all_slurm_nodenames",
                            lambda: ["ink50-dh01-d01-11", "ink50-dh01-d01-12", "ink50-dh01-d010-11"])
        assert slurm_query._rack_node_hostnames_from_inventory("ink50-dh01-d01") == [
            "ink50-dh01-d01-11", "ink50-dh01-d01-12",
        ]


def _prepare(monkeypatch, tmp_path, body):
    monkeypatch.setattr(validation_runs, "_multirack_campaign_snapshot", lambda: {"ok": True, "active": False})
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(validation_runs, "_script_exists", lambda _path: True)
    monkeypatch.setattr(validation_runs, "_normalize_rack_id", lambda rack: rack)
    with app_module.app.app_context():
        return validation_runs._prepare_run_llmb_nccl(body)


class TestMultirackNcclLaunch:
    def test_translate_hands_the_launcher_the_placed_hosts(self, placed, monkeypatch, tmp_path):
        prepared, err = _prepare(monkeypatch, tmp_path, {"racks": ["Q5", "U20"], "dry_run": True})
        assert err is None
        members = [f"sys1-gso1-p-phy-gpu{n}-su5" for n in (1, 2, 10)] + U20
        assert prepared["nodes"] == members
        assert prepared["cmd"][-4:] == ["--nodelist", ",".join(members), "Q5", "U20"]
        assert prepared["display_cmd"].endswith(f"--nodelist {','.join(members)} Q5 U20")
        assert prepared["campaign"] == ""

    def test_a_rack_with_no_placed_node_is_refused(self, placed, monkeypatch, tmp_path):
        prepared, err = _prepare(monkeypatch, tmp_path, {"racks": ["Q5", "Z9"]})
        assert prepared is None
        response, status = err
        assert status == 409
        assert "Z9" in response.get_json()["error"]

    def test_direct_passes_rack_ids_as_before(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "direct")
        prepared, err = _prepare(monkeypatch, tmp_path, {"racks": ["ink50-dh01-d01"]})
        assert err is None
        assert prepared["nodes"] == []
        assert prepared["cmd"][-1] == "ink50-dh01-d01"
        assert "--nodelist" not in prepared["cmd"]
