# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The rack-staged wave plan: how a distribute turns a roster into waves that fill racks gradually.

Author: Rafael M. Koike

Every peer is labelled with its SU and rack, and the scheduler ranks a parent in the same rack
first. A roster sliced in name order ignores that: on NC the last rack got its first copy in wave
53 of 58, so most nodes started a pull with no holder in their rack. The rack-staged plan puts one
node in every rack first (the only stage that loads the app server's seeds), then grows every rack
up a ladder -- by default to half, then full -- so a rack's first holder never feeds the whole rack.

The properties pinned here are the ones that make that true: one node per rack in the first stage,
no rack asked to grow before it holds a copy, every node exactly once, waves spread over SUs, and a
plan that falls back to plain slicing -- loudly -- when the topology is missing.
"""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts" / "operations-worker.py"
_spec = importlib.util.spec_from_file_location("p2p_operations_worker_plan", WORKER)
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

LADDER = ["1", "50%", "100%"]


def _fleet(racks_per_su: dict[str, list[tuple[str, int]]]) -> tuple[list[str], dict[str, tuple[str, str]]]:
    """A roster and topology from ``{su: [(rack, size), ...]}``; hostnames carry no rack, as on NC."""
    nodes, topology, n = [], {}, 0
    for su, racks in racks_per_su.items():
        for rack, size in racks:
            for _ in range(size):
                n += 1
                name = f"gpu{n}-su{su}"
                nodes.append(name)
                topology[name] = (su, rack)
    return sorted(nodes), topology


def _rack_of(topology, node):
    return topology[node][1]


class TestStageCuts:
    @pytest.mark.parametrize(("size", "ladder", "cuts"), [
        (17, LADDER, [1, 9, 17]),           # half of 17 rounds up, so the first holder feeds 8
        (18, LADDER, [1, 9, 18]),
        (1, LADDER, [1, 1, 1]),             # a one-node rack is done after the first stage
        (4, ["1", "2", "4", "50%", "100%"], [1, 2, 4, 4, 4]),   # a count past the rack clamps
        (16, ["1", "2", "4", "8", "100%"], [1, 2, 4, 8, 16]),   # the doubling ladder
        (10, ["25%", "100%"], [3, 10]),
    ])
    def test_cumulative_targets(self, size, ladder, cuts):
        assert worker._stage_cuts(size, ladder) == cuts

    def test_the_last_rung_always_fills_the_rack(self):
        # Even a malformed ladder (the gateway refuses it, but the worker must not strand nodes).
        assert worker._stage_cuts(9, ["1", "50%"])[-1] == 9


class TestRackStagedWaves:
    def _plan(self, nodes, topology, ladder=LADDER, seed=25, fill=72):
        return worker.rack_staged_waves(nodes, topology, ladder, seed, fill)

    def test_every_node_is_planned_exactly_once(self):
        nodes, topo = _fleet({"19": [("U19", 17), ("U20", 18)], "20": [("Q19", 16), ("Q20", 5)]})
        waves, _ = self._plan(nodes, topo)
        flat = [n for _, wave in waves for n in wave]
        assert sorted(flat) == sorted(nodes) and len(flat) == len(set(flat))

    def test_the_first_stage_is_one_node_in_every_rack(self):
        nodes, topo = _fleet({"19": [(f"U{r}", 17) for r in range(19, 26)],
                              "20": [(f"Q{r}", 18) for r in range(19, 26)]})
        waves, summary = self._plan(nodes, topo, seed=5)
        first = [n for stage, wave in waves if stage == "1" for n in wave]
        assert len(first) == 14 == summary["racks"]
        assert len({_rack_of(topo, n) for n in first}) == 14
        # Paced like the seeds, because these are the pulls that reach them.
        assert [len(w) for s, w in waves if s == "1"] == [5, 5, 4]

    def test_no_rack_grows_before_it_holds_a_copy(self):
        """The point of the plan: a later-stage node always has a rack-mate from an earlier wave."""
        nodes, topo = _fleet({"19": [("U19", 17), ("U20", 9)], "21": [("Q19", 18), ("Q20", 2)],
                              "33": [("T19", 16)]})
        waves, _ = self._plan(nodes, topo, seed=2, fill=10)
        held: dict[str, int] = {}
        for stage, wave in waves:
            if stage != "1":
                assert all(held.get(_rack_of(topo, n), 0) >= 1 for n in wave), (stage, wave)
            for n in wave:
                held[_rack_of(topo, n)] = held.get(_rack_of(topo, n), 0) + 1

    def test_after_the_half_stage_every_rack_holds_half(self):
        nodes, topo = _fleet({"19": [("U19", 17), ("U20", 18), ("U21", 7)]})
        waves, _ = self._plan(nodes, topo)
        upto_half = [n for stage, wave in waves if stage in ("1", "50%") for n in wave]
        for rack, size in (("U19", 17), ("U20", 18), ("U21", 7)):
            assert sum(_rack_of(topo, n) == rack for n in upto_half) == math.ceil(size / 2)

    def test_fill_stages_are_paced_by_the_fill_wave_and_keep_a_rack_slice_together(self):
        nodes, topo = _fleet({"19": [(f"U{r}", 17) for r in range(19, 26)]})
        waves, summary = self._plan(nodes, topo, seed=25, fill=20)
        half = [w for s, w in waves if s == "50%"]
        assert all(len(w) <= 20 for w in half)
        # 8 new nodes per rack this stage; two racks fit a 20-node wave, a third would not.
        assert [len(w) for w in half] == [16, 16, 16, 8]
        for wave in half:
            for rack in {_rack_of(topo, n) for n in wave}:
                assert sum(_rack_of(topo, n) == rack for n in wave) == 8   # never split across waves
        assert [row["stage"] for row in summary["stages"]] == LADDER

    def test_a_slice_larger_than_a_wave_is_split_rather_than_overflowing(self):
        nodes, topo = _fleet({"19": [("U19", 18)]})
        waves, _ = self._plan(nodes, topo, fill=4)
        assert all(len(w) <= 4 for s, w in waves if s != "1")

    def test_racks_alternate_across_sus(self):
        """A wave spreads over the fabric instead of loading one SU's uplinks."""
        nodes, topo = _fleet({"19": [(f"U{r}", 3) for r in (19, 20, 21)],
                              "20": [(f"Q{r}", 3) for r in (19, 20, 21)],
                              "9": [(f"W{r}", 3) for r in (19, 20)]})
        waves, summary = self._plan(nodes, topo, seed=100)
        first = waves[0][1]
        assert [topo[n][0] for n in first] == ["9", "19", "20", "9", "19", "20", "19", "20"]
        assert summary["sus"] == 3

    def test_racks_order_naturally(self):
        nodes, topo = _fleet({"19": [("U7", 2), ("U19", 2), ("U2", 2)]})
        waves, _ = self._plan(nodes, topo, seed=100)
        assert [_rack_of(topo, n) for n in waves[0][1]] == ["U2", "U7", "U19"]

    def test_nodes_missing_from_the_topology_go_last_at_seed_pace(self):
        nodes, topo = _fleet({"19": [("U19", 4)]})
        stray = ["zz-unplaced-1", "zz-unplaced-2", "zz-unplaced-3"]
        waves, summary = self._plan(nodes + stray, topo, seed=2)
        assert [w for s, w in waves if s == "unplaced"] == [stray[:2], stray[2:]]
        assert waves[-1][0] == "unplaced"
        assert summary["unplaced"] == 3


class TestPlanWaves:
    def _env(self, tmp_path, topo_text=None):
        root = tmp_path / "root" / "current"
        (root / "inventory").mkdir(parents=True)
        if topo_text is not None:
            (root / "inventory" / "topology.tsv").write_text(topo_text)
        return {"NVIS_P2P_ROOT": str(root)}

    def test_sequential_slices_the_roster_in_order(self, tmp_path):
        log = tmp_path / "op.log"
        waves = worker._plan_waves([f"n{i}" for i in range(5)], {"wave_size": 2, "strategy": "sequential"},
                                   self._env(tmp_path), log)
        assert [w for _, w in waves] == [["n0", "n1"], ["n2", "n3"], ["n4"]]

    def test_a_request_without_a_strategy_is_sequential(self, tmp_path):
        """Requests queued before this field existed keep running exactly as they were planned."""
        waves = worker._plan_waves(["a", "b", "c"], {"wave_size": 2}, self._env(tmp_path), tmp_path / "op.log")
        assert [s for s, _ in waves] == ["sequential", "sequential"]

    def test_rack_staged_reads_the_installed_topology_and_logs_the_plan(self, tmp_path):
        topo = "".join(f"n{i}\t19\t{'U19' if i < 4 else 'U20'}\n" for i in range(8))
        log = tmp_path / "op.log"
        waves = worker._plan_waves([f"n{i}" for i in range(8)],
                                   {"wave_size": 25, "strategy": "rack-staged", "stages": LADDER, "fill_wave_size": 0},
                                   self._env(tmp_path, topo), log)
        assert [s for s, _ in waves] == ["1", "50%", "100%"]
        assert sorted(waves[0][1]) == ["n0", "n4"]
        text = log.read_text()
        assert "rack-staged plan from" in text and "2 racks in 1 SUs" in text and "1 → 50% → 100%" in text
        assert "stage 50%: 2 nodes over 2 racks in 1 waves" in text

    def test_the_topology_file_can_be_pointed_elsewhere(self, tmp_path):
        other = tmp_path / "elsewhere.tsv"
        other.write_text("a\t1\tR1\nb\t1\tR1\n")
        env = {**self._env(tmp_path), "NVIS_TOPOLOGY_FILE": str(other)}
        waves = worker._plan_waves(["a", "b"], {"wave_size": 5, "strategy": "rack-staged", "stages": LADDER},
                                   env, tmp_path / "op.log")
        # A two-node rack: its first node seeds it, half of it is already held, the last node fills it.
        assert waves == [("1", ["a"]), ("100%", ["b"])]

    def test_a_missing_topology_falls_back_to_sequential_and_says_so(self, tmp_path):
        log = tmp_path / "op.log"
        waves = worker._plan_waves(["a", "b", "c"], {"wave_size": 2, "strategy": "rack-staged", "stages": LADDER},
                                   self._env(tmp_path), log)
        assert [w for _, w in waves] == [["a", "b"], ["c"]]
        text = log.read_text()
        assert "topology inventory unreadable" in text and "falling back to sequential" in text


class TestStagedCampaign:
    def test_waves_run_in_plan_order_with_their_stage_in_the_log(self, tmp_path):
        """End to end through the rollout script: the stand-in records each wave it was handed."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        record = tmp_path / "waves.log"
        (scripts / "preheat-catalog-image.sh").write_text("#!/bin/bash\nexit 0\n")
        (scripts / "rollout-catalog-image-fleet.sh").write_text(
            f'#!/bin/bash\necho "$3 $(paste -sd, "$2")" >> {record}\n')
        root = tmp_path / "root" / "current"
        (root / "inventory").mkdir(parents=True)
        (root / "inventory" / "topology.tsv").write_text(
            "a1\t1\tA\na2\t1\tA\na3\t1\tA\nb1\t2\tB\nb2\t2\tB\n")
        roster = tmp_path / "roster.nodes"
        roster.write_text("a1\na2\na3\nb1\nb2\n")
        log = tmp_path / "op.log"
        rollout = {"wave_size": 25, "fill_wave_size": 25, "strategy": "rack-staged", "stages": LADDER,
                   "timeout_seconds": 60}
        rc = worker._staged_campaign("nemo.sqsh", roster, rollout, str(scripts),
                                     {"NVIS_P2P_ROOT": str(root), "PATH": "/usr/bin:/bin"}, log, "op-1")
        assert rc == 0
        assert record.read_text().splitlines() == ["op-1-w0 a1,b1", "op-1-w1 a2", "op-1-w2 a3,b2"]
        text = log.read_text()
        assert "[worker] wave 0 — 2 nodes (stage 1)" in text
        assert "[worker] wave 2 — 2 nodes (stage 100%)" in text
