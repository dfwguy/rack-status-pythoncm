# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""nvis-p2p-coordinator: rack-staged rollout through desired-state documents, read back from metrics.

The decision loop runs against a fake metrics source and clock, so every rule -- rung release, the
stale-generation guard, the three deferrals, cancel -- is observed without a cluster.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "nvis-p2p-coordinator.py"
spec = importlib.util.spec_from_file_location("nvis_p2p_coordinator", PATH)
coord_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = coord_mod
assert spec.loader is not None
spec.loader.exec_module(coord_mod)
Obs = coord_mod.Observation

IMAGE = {"filename": "big.sqsh", "sha256": "a" * 64, "bytes": 10, "tag": "catalog-v1-7",
         "application": "NVIS-P2P-Catalog"}
NODES = [f"gpu{i}-su22" for i in range(1, 19)]


class Fleet:
    """Fake agents: a released node reports downloading, then ready after *delay* ticks."""

    def __init__(self, desired_dir: Path, delay: int = 1, dead=(), failing=()):
        self.desired_dir, self.delay, self.dead, self.failing = desired_dir, delay, set(dead), set(failing)
        self.seen: dict[str, int] = {}

    def observe(self, nodes, filename):
        out = {}
        for n in nodes:
            if n in self.dead:
                out[n] = Obs()
                continue
            path = self.desired_dir / f"{n}.json"
            if not path.exists():
                out[n] = Obs(alive=True, generation=0)
                continue
            doc = json.loads(path.read_text())
            gen = doc["generation"]
            wants = any(i["filename"] == filename for i in doc["images"])
            if not wants:
                out[n] = Obs(alive=True, generation=gen)
                continue
            if n in self.failing:
                out[n] = Obs(alive=True, generation=gen, state="backoff", failures=3)
                continue
            self.seen[n] = self.seen.get(n, 0) + 1
            state = "ready" if self.seen[n] > self.delay else "downloading"
            out[n] = Obs(alive=True, generation=gen, state=state)
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _run(nodes=NODES):
    return coord_mod.Run(run_id="t", rack="Q5", image=IMAGE, cuts=coord_mod.rung_cuts(["1", "50%", "100%"], len(nodes)),
                         nodes={n: coord_mod.NodeRun() for n in nodes}, started=1000.0)


def _coord(tmp_path, fleet, clock, budget=600):
    return coord_mod.Coordinator(tmp_path, "http://10.0.0.1:12130", fleet.observe, budget, 3, 180,
                                 now=clock, log=lambda _m: None)


def _drive(coord, run, clock, ticks=50, step=15):
    for _ in range(ticks):
        if coord.tick(run):
            return True
        clock.t += step
    return False


class TestPlanning:
    @pytest.mark.parametrize("stages, total, cuts", [
        (["1", "50%", "100%"], 18, [1, 9, 18]),
        (["1", "50%", "100%"], 17, [1, 9, 17]),
        (["2", "4", "100%"], 3, [2, 3]),
        (["50%"], 4, [2, 4]),
    ])
    def test_rung_cuts(self, stages, total, cuts):
        assert coord_mod.rung_cuts(stages, total) == cuts

    def test_rack_members_come_from_the_placement_file_in_natural_order(self, tmp_path):
        topo = tmp_path / "topology.tsv"
        topo.write_text("gpu10-su22\t22\tQ5\ngpu9-su22\t22\tQ5\ngpu1-su21\t21\tQ24\n")
        assert coord_mod.rack_nodes(topo, "Q5") == ["gpu9-su22", "gpu10-su22"]

    def test_the_pin_comes_from_the_authored_catalog(self, tmp_path):
        cat = tmp_path / "c.tsv"
        cat.write_text("order\tname\tfilename\tbytes\tsha256\n7\tLlama\tbig.sqsh\t10\t" + "a" * 64 + "\n")
        assert coord_mod.catalog_pin(cat, "big.sqsh") == IMAGE

    def test_merging_keeps_other_images_and_bumps_generation(self):
        existing = {"schema_version": 1, "generation": 6, "node": "n", "origin": "http://o:1",
                    "images": [{"filename": "keep.sqsh", "sha256": "b" * 64, "bytes": 1}]}
        doc = coord_mod.merge_document(existing, "n", "http://o:1", IMAGE)
        assert doc["generation"] == 7 and [i["filename"] for i in doc["images"]] == ["keep.sqsh", "big.sqsh"]
        doc = coord_mod.merge_document(doc, "n", "http://o:1", None, remove="big.sqsh")
        assert doc["generation"] == 8 and [i["filename"] for i in doc["images"]] == ["keep.sqsh"]


class TestTheRollout:
    def test_a_rack_converges_rung_by_rung(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=2)
        coord, run = _coord(tmp_path, fleet, clock), _run()
        coord.tick(run)
        assert [n for n, nr in run.nodes.items() if nr.state != "pending"] == NODES[:1]  # rung 1 seeds the rack
        assert _drive(coord, run, clock)
        assert all(nr.state == "ready" for nr in run.nodes.values())
        assert {nr.rung for nr in run.nodes.values()} == {0, 1, 2}
        assert sum(nr.rung == 1 for nr in run.nodes.values()) == 8 and sum(nr.rung == 2 for nr in run.nodes.values()) == 9

    def test_a_stale_ready_from_an_older_generation_does_not_count(self, tmp_path):
        clock = Clock()

        def observe(nodes, filename):  # every agent still reports the generation before our write
            return {n: Obs(alive=True, generation=0, state="ready") for n in nodes}

        coord = coord_mod.Coordinator(tmp_path, "http://o:1", observe, 600, 3, 180, now=clock, log=lambda _m: None)
        run = _run()
        coord.tick(run)
        clock.t += 15
        coord.tick(run)
        assert run.nodes[NODES[0]].state == "released"

    def test_nodes_without_a_live_agent_are_deferred_and_do_not_block(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, dead=NODES[:2])
        coord, run = _coord(tmp_path, fleet, clock), _run()
        assert _drive(coord, run, clock)
        assert [run.nodes[n].reason for n in NODES[:2]] == ["no_agent", "no_agent"]
        assert not (tmp_path / f"{NODES[0]}.json").exists()  # nothing is written for a node nobody listens on
        assert sum(nr.state == "ready" for nr in run.nodes.values()) == 16

    def test_a_node_that_keeps_failing_is_deferred(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, failing=NODES[:1])
        coord, run = _coord(tmp_path, fleet, clock), _run()
        assert _drive(coord, run, clock)
        assert run.nodes[NODES[0]].reason == "failed_3x"

    def test_a_node_over_budget_is_deferred_as_timeout(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=10_000)
        coord, run = _coord(tmp_path, fleet, clock, budget=60), _run()
        assert _drive(coord, run, clock, ticks=200)
        assert {nr.reason for nr in run.nodes.values()} == {"timeout"}

    def test_cancel_removes_the_image_and_keeps_the_rest(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path)
        coord, run = _coord(tmp_path, fleet, clock), _run(NODES[:2])
        assert _drive(coord, run, clock)
        assert coord.cancel(NODES[:2], "big.sqsh") == 2
        doc = json.loads((tmp_path / f"{NODES[0]}.json").read_text())
        assert doc["images"] == [] and doc["generation"] == 2
        assert coord.cancel(NODES[:2], "big.sqsh") == 0  # idempotent


class TestTheMetricsRead:
    def test_the_selector_is_a_valid_promql_string(self):
        sel = coord_mod.instance_selector(["sys1-gso1-p-phy-gpu1-su22", "a.b-c"])
        assert sel == "sys1-gso1-p-phy-gpu1-su22|a\\\\.b-c"
        assert "\\-" not in sel

    def test_a_name_that_is_not_a_hostname_is_refused(self):
        with pytest.raises(ValueError):
            coord_mod.instance_selector(['x"} or vector(1)'])

    def test_a_failed_read_skips_the_tick_instead_of_deferring_the_rack(self, tmp_path):
        clock = Clock()

        def observe(nodes, filename):
            raise OSError("HTTP Error 422")

        coord = coord_mod.Coordinator(tmp_path, "http://o:1", observe, 600, 3, 180, now=clock, log=lambda _m: None)
        run = _run()
        assert coord.tick(run) is False
        assert all(nr.state == "pending" for nr in run.nodes.values())
        assert not list(tmp_path.glob("*.json"))
