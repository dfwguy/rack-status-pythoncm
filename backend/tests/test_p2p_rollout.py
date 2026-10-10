# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""p2p_rollout: a pull rollout as a workflow-engine run.

The coordinator is driven against fake agents that read the real documents it writes, so release,
the stale-generation guard, rack concurrency, retry, pacing and cancel are all observed through the
same files the node agents would read. The engine integration runs a whole rollout, a cancel and a
resume through workflow_engine itself.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import p2p_rollout as pr
import pytest
import workflow_engine as we
from catalog import models

IMAGE = {"filename": "big.sqsh", "sha256": "a" * 64, "bytes": 10, "tag": "catalog-v1-7",
         "application": "NVIS-P2P-Catalog"}
Obs = pr.Observation


def _plan(tmp_path, racks):
    return {"cluster_id": "nc", "image": IMAGE, "racks": racks, "origin": "http://10.0.0.1:12130",
            "desired_dir": str(tmp_path), "metrics_url": ""}


PACING = {"stages": ["1", "50%", "100%"], "budget_seconds": 600, "max_active_racks": 4}


class Fleet:
    """Fake agents: read the coordinator's documents; downloading for *delay* observations, then ready."""

    def __init__(self, desired_dir, delay=1, dead=(), failing=()):
        self.dir, self.delay, self.dead, self.failing = desired_dir, delay, set(dead), set(failing)
        self.seen: dict[str, int] = {}
        self.calls = 0

    def observe(self, nodes, filename):
        self.calls += 1
        out = {}
        for n in nodes:
            if n in self.dead:
                out[n] = Obs()
                continue
            path = self.dir / f"{n}.json"
            if not path.exists():
                out[n] = Obs(alive=True, generation=0)
                continue
            doc = json.loads(path.read_text())
            wants = any(i["filename"] == filename for i in doc["images"])
            if not wants:
                out[n] = Obs(alive=True, generation=doc["generation"])
            elif n in self.failing:
                out[n] = Obs(alive=True, generation=doc["generation"], state="backoff", failures=3)
            else:
                key = f"{n}@{doc['generation']}"
                self.seen[key] = self.seen.get(key, 0) + 1
                state = "ready" if self.seen[key] > self.delay else "downloading"
                out[n] = Obs(alive=True, generation=doc["generation"], state=state)
        return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _coord(tmp_path, fleet, clock):
    return pr.Coordinator(pr.DocumentStore(tmp_path), fleet.observe, now=clock)


def _drive(coord, plan, pacing, state, clock, ticks=80):
    for _ in range(ticks):
        if coord.tick(plan, pacing, state):
            return True
        clock.t += 15
    return False


RACK = [f"gpu{i}-su22" for i in range(1, 19)]


class TestTheCoordinator:
    def test_a_rack_converges_rung_by_rung_from_the_documents(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=2)
        plan, coord = _plan(tmp_path, {"Q5": RACK}), _coord(tmp_path, Fleet(tmp_path), clock)
        coord = _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        coord.tick(plan, PACING, state)
        assert [n for n, nr in state["nodes"].items() if nr["state"] != "pending"] == RACK[:1]
        doc = json.loads((tmp_path / f"{RACK[0]}.json").read_text())
        assert doc["images"][0]["sha256"] == "a" * 64 and doc["origin"] == "http://10.0.0.1:12130"
        assert _drive(coord, plan, PACING, state, clock)
        assert all(nr["state"] == "ready" for nr in state["nodes"].values())
        assert [sum(nr["rung"] == r for nr in state["nodes"].values()) for r in (0, 1, 2)] == [1, 8, 9]

    def test_a_ready_from_before_the_release_does_not_count(self, tmp_path):
        clock = Clock()
        coord = pr.Coordinator(pr.DocumentStore(tmp_path),
                               lambda nodes, f: {n: Obs(alive=True, generation=0, state="ready") for n in nodes},
                               now=clock)
        plan = _plan(tmp_path, {"Q5": RACK})
        state = pr.new_state(plan, clock())
        coord.tick(plan, PACING, state)
        clock.t += 15
        coord.tick(plan, PACING, state)
        assert state["nodes"][RACK[0]]["state"] == "released"

    def test_racks_in_flight_are_capped_and_the_next_starts_when_one_finishes(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path)
        racks = {f"R{i}": [f"n{i}-{j}" for j in range(2)] for i in range(3)}
        plan, coord = _plan(tmp_path, racks), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        coord.tick(plan, {**PACING, "max_active_racks": 2}, state)
        assert [r for r, rs in state["racks"].items() if rs["state"] == "active"] == ["R0", "R1"]
        assert _drive(coord, plan, {**PACING, "max_active_racks": 2}, state, clock)
        assert all(rs["state"] == "done" for rs in state["racks"].values())

    def test_agentless_failing_and_slow_nodes_are_deferred_without_blocking_the_rack(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=1, dead=RACK[:1], failing=RACK[1:2])
        plan, coord = _plan(tmp_path, {"Q5": RACK}), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        assert _drive(coord, plan, PACING, state, clock)
        assert state["nodes"][RACK[0]]["reason"] == "no_agent"
        assert not (tmp_path / f"{RACK[0]}.json").exists()
        assert state["nodes"][RACK[1]]["reason"] == "failed_3x"
        assert sum(nr["state"] == "ready" for nr in state["nodes"].values()) == 16
        slow_clock, slow = Clock(), Fleet(tmp_path / "slow", delay=10_000)
        (tmp_path / "slow").mkdir()
        plan2 = _plan(tmp_path / "slow", {"Q6": RACK[:2]})
        coord2, state2 = _coord(tmp_path / "slow", slow, slow_clock), pr.new_state(plan2, slow_clock())
        assert _drive(coord2, plan2, {**PACING, "budget_seconds": 60}, state2, slow_clock)
        assert {nr["reason"] for nr in state2["nodes"].values()} == {"timeout"}

    def test_a_failed_metrics_read_skips_the_tick(self, tmp_path):
        def boom(nodes, f):
            raise OSError("HTTP Error 422")

        clock = Clock()
        coord, plan = pr.Coordinator(pr.DocumentStore(tmp_path), boom, now=clock), _plan(tmp_path, {"Q5": RACK})
        state = pr.new_state(plan, clock())
        assert coord.tick(plan, PACING, state) is False
        assert all(nr["state"] == "pending" for nr in state["nodes"].values())
        assert "422" in state["last_error"] and not list(tmp_path.glob("*.json"))

    def test_retry_rereleases_deferred_nodes_with_a_new_generation(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, failing=RACK[:1])
        plan, coord = _plan(tmp_path, {"Q5": RACK[:2]}), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        assert _drive(coord, plan, PACING, state, clock)
        assert state["nodes"][RACK[0]]["state"] == "deferred"
        fleet.failing.clear()  # the operator fixed the node
        coord.apply_commands(plan, state, [{"seq": 1, "kind": "retry", "nodes": []}])
        assert state["nodes"][RACK[0]]["state"] == "pending"
        coord.tick(plan, PACING, state)  # released through the normal path, with a new generation
        assert state["nodes"][RACK[0]]["state"] == "released" and state["nodes"][RACK[0]]["attempts"] == 2
        assert json.loads((tmp_path / f"{RACK[0]}.json").read_text())["generation"] == 2
        assert _drive(coord, plan, PACING, state, clock)
        assert state["nodes"][RACK[0]]["state"] == "ready"
        coord.apply_commands(plan, state, [{"seq": 1, "kind": "retry", "nodes": []}])  # applied once only
        assert state["nodes"][RACK[0]]["attempts"] == 2

    def test_retrying_a_node_that_still_has_no_agent_defers_it_at_once(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, dead=RACK[:1])
        plan, coord = _plan(tmp_path, {"Q5": RACK[:2]}), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        assert _drive(coord, plan, PACING, state, clock)
        coord.apply_commands(plan, state, [{"seq": 1, "kind": "retry", "nodes": [RACK[0]]}])
        assert coord.tick(plan, PACING, state) is True  # one tick, not a node budget
        assert state["nodes"][RACK[0]]["reason"] == "no_agent"
        assert not (tmp_path / f"{RACK[0]}.json").exists()

    def test_pacing_changes_apply_on_the_next_tick(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=1)
        plan, coord = _plan(tmp_path, {"Q5": RACK}), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        coord.tick(plan, {**PACING, "stages": ["2", "100%"]}, state)
        assert sum(nr["state"] != "pending" for nr in state["nodes"].values()) == 2

    def test_cancel_withdraws_the_image_only_from_nodes_still_downloading(self, tmp_path):
        clock, fleet = Clock(), Fleet(tmp_path, delay=1)
        plan, coord = _plan(tmp_path, {"Q5": RACK}), _coord(tmp_path, fleet, clock)
        state = pr.new_state(plan, clock())
        for _ in range(4):
            coord.tick(plan, PACING, state)
            clock.t += 15
        ready = [n for n, nr in state["nodes"].items() if nr["state"] == "ready"]
        working = [n for n, nr in state["nodes"].items() if nr["state"] in ("released", "downloading")]
        assert ready and working
        coord.cancel(plan, state)
        assert json.loads((tmp_path / f"{working[0]}.json").read_text())["images"] == []
        assert json.loads((tmp_path / f"{ready[0]}.json").read_text())["images"][0]["filename"] == "big.sqsh"
        assert {state["nodes"][n]["reason"] for n in working} == {"cancelled"}


class TestPlanning:
    def _settings(self, **kw):
        return models.P2pSettings.from_dict({"cluster_id": "nc", "appserver_ip": "10.10.20.22",
                                              "desired_dir": "/app/p2p-desired", **kw})

    def _inventory(self):
        return [{"id": "gpu20-su19", "rack": "U20", "tray": 2}, {"id": "gpu19-su19", "rack": "U20", "tray": 1},
                {"id": "gpu1-su19", "rack": "U19", "tray": 1}, {"id": "appserver", "rack": None}]

    def test_members_come_from_the_placed_inventory_in_tray_order(self):
        plan = pr.plan_rollout(cluster_id="nc", settings=self._settings(),
                               catalog_images=[{"filename": "big.sqsh", "sha256": "a" * 64, "bytes": 10, "order": 7}],
                               inventory=self._inventory(), filename="big.sqsh", racks=["U20"])
        assert plan["racks"] == {"U20": ["gpu19-su19", "gpu20-su19"]}
        assert plan["origin"] == "http://10.10.20.22:12130" and plan["image"]["tag"] == "catalog-v1-7"

    @pytest.mark.parametrize("kw, match", [
        ({"settings": {"desired_dir": ""}}, "desired_dir"),
        ({"filename": "nope.sqsh"}, "not in the cluster's catalog"),
        ({"catalog": [{"filename": "big.sqsh", "sha256": "", "bytes": 10}]}, "no sha256 pin"),
        ({"inventory": None}, "inventory is not available"),
        ({"racks": ["Z9"]}, "no placed nodes"),
        ({"racks": []}, "at least one rack"),
    ])
    def test_what_is_missing_is_said(self, kw, match):
        with pytest.raises(pr.RolloutError, match=match):
            pr.plan_rollout(cluster_id="nc", settings=self._settings(**kw.get("settings", {})),
                            catalog_images=kw.get("catalog", [{"filename": "big.sqsh", "sha256": "a" * 64, "bytes": 10}]),
                            inventory=kw.get("inventory", self._inventory()) if "inventory" in kw else self._inventory(),
                            filename=kw.get("filename", "big.sqsh"), racks=kw.get("racks", ["U20"]))

    def test_pacing_is_validated(self):
        base = {"stages": ["1", "100%"], "budget_seconds": 3600, "max_active_racks": 4}
        assert pr.normalize_pacing({"max_active_racks": "2"}, base)["max_active_racks"] == 2
        for bad in ({"stages": ["50%", "10%"]}, {"budget_seconds": 5}, {"max_active_racks": 0}, {"budget_seconds": "x"}):
            with pytest.raises(pr.RolloutError):
                pr.normalize_pacing(bad, base)


class TestAsAWorkflowRun:
    @pytest.fixture(autouse=True)
    def engine(self, tmp_path, monkeypatch):
        monkeypatch.setattr(we, "WORKFLOWS_DIR", tmp_path / "wf")
        monkeypatch.setattr(we, "RUNS_DIR", tmp_path / "wf" / "runs")
        monkeypatch.setattr(we, "_active_runs", {})
        monkeypatch.setattr(pr, "TICK_SECONDS", 0.01)
        yield
        deadline = time.time() + 5
        while time.time() < deadline and any(r.get("state") not in we.TERMINAL_STATES for r in we._active_runs.values()):
            time.sleep(0.02)

    def _fake(self, monkeypatch, docs, fleet_factory):
        fleets = []

        def coordinator_for(run, step, **kw):
            fleet = fleet_factory(docs)
            fleets.append(fleet)
            return pr.run_rollout_step(run, step, coordinator=pr.Coordinator(pr.DocumentStore(docs), fleet.observe),
                                       tick_seconds=0.01)

        monkeypatch.setitem(we._NATIVE_STEPS, pr.NATIVE_STEP, coordinator_for)
        return fleets

    def _wait(self, run_id, states, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = we.get_run_state(run_id)
            if run and run["state"] in states:
                return run
            time.sleep(0.02)
        raise AssertionError(f"run never reached {states}: {we.get_run_state(run_id)['state']}")

    def test_a_rollout_runs_to_completion_and_records_progress(self, tmp_path, monkeypatch):
        docs = tmp_path / "desired"
        docs.mkdir()
        self._fake(monkeypatch, docs, lambda d: Fleet(d, delay=1))
        started = pr.start(_plan(docs, {"Q5": RACK[:4]}), PACING, "rkoike")
        run = self._wait(started["run_id"], {"completed", "failed"})
        assert run["state"] == "completed" and run["progress"]["ready"] == 4
        assert pr.describe(run, detail=True)["rack_detail"][0]["state"] == "done"
        assert pr.start(_plan(docs, {"Q5": RACK[:4]}), PACING, "rkoike")["run_id"] != started["run_id"]

    def test_deferred_nodes_leave_the_rollout_completed_with_them_counted(self, tmp_path, monkeypatch):
        docs = tmp_path / "desired"
        docs.mkdir()
        self._fake(monkeypatch, docs, lambda d: Fleet(d, delay=1, dead=RACK[:1]))
        run_id = pr.start(_plan(docs, {"Q5": RACK[:3]}), PACING, "rkoike")["run_id"]
        run = self._wait(run_id, {"completed", "failed"})
        assert run["state"] == "completed"
        assert run["progress"]["deferred"] == 1 and run["progress"]["ready"] == 2

    def test_a_second_start_of_the_same_rollout_joins_it_and_cancel_stops_it(self, tmp_path, monkeypatch):
        docs = tmp_path / "desired"
        docs.mkdir()
        self._fake(monkeypatch, docs, lambda d: Fleet(d, delay=10_000))
        first = pr.start(_plan(docs, {"Q5": RACK}), PACING, "rkoike")
        assert pr.start(_plan(docs, {"Q5": RACK}), PACING, "someone-else")["joined"] is True
        deadline = time.time() + 5
        while not list(docs.glob("*.json")) and time.time() < deadline:
            time.sleep(0.02)
        we.cancel_run(first["run_id"])
        run = self._wait(first["run_id"], {"cancelled"})
        self._wait(first["run_id"], {"cancelled"})
        deadline = time.time() + 5
        while time.time() < deadline and not run.get("rollout", {}).get("finished"):
            run = we.get_run_state(first["run_id"])
            time.sleep(0.02)
        assert json.loads((docs / f"{RACK[0]}.json").read_text())["images"] == []

    def test_pacing_and_retry_reach_the_running_step(self, tmp_path, monkeypatch):
        docs = tmp_path / "desired"
        docs.mkdir()
        self._fake(monkeypatch, docs, lambda d: Fleet(d, delay=10_000))
        run_id = pr.start(_plan(docs, {"Q5": RACK}), PACING, "rkoike")["run_id"]
        pr.set_pacing(run_id, {"stages": ["3", "100%"]})
        deadline = time.time() + 5
        while time.time() < deadline and len(list(docs.glob("*.json"))) < 3:
            time.sleep(0.02)
        assert len(list(docs.glob("*.json"))) == 3
        pr.request_retry(run_id, [RACK[0]])
        deadline = time.time() + 5
        while time.time() < deadline and json.loads((docs / f"{RACK[0]}.json").read_text())["generation"] < 2:
            time.sleep(0.02)
        assert json.loads((docs / f"{RACK[0]}.json").read_text())["generation"] == 2
        with pytest.raises(pr.RolloutError):
            pr.set_pacing(run_id, {"max_active_racks": 0})
        we.cancel_run(run_id)
        self._wait(run_id, {"cancelled"})

    def test_a_restart_resumes_from_the_saved_state(self, tmp_path, monkeypatch):
        docs = tmp_path / "desired"
        docs.mkdir()
        plan = _plan(docs, {"Q5": RACK[:2]})
        state = pr.new_state(plan, time.time())
        state["nodes"][RACK[0]].update(state="ready", rung=0)
        state["racks"]["Q5"]["rung"] = 1
        run_id = "rollout-interrupted"
        (we.RUNS_DIR / run_id).mkdir(parents=True)
        (we.RUNS_DIR / run_id / "state.json").write_text(json.dumps({
            "run_id": run_id, "workflow_id": pr.WORKFLOW_ID, "state": "running", "current_step_id": "rollout",
            "params": {"cluster_id": "nc", "plan": plan, "pacing": PACING, "commands": []},
            "rollout": state, "history": [], "started_at": "2026-10-10T00:00:00Z"}))
        fleets = self._fake(monkeypatch, docs, lambda d: Fleet(d, delay=1))
        we._RESUMABLE_NATIVE_STEPS.add(pr.NATIVE_STEP)
        assert we.resume_interrupted_runs() == [run_id]
        run = self._wait(run_id, {"completed", "failed"})
        assert run["state"] == "completed"
        # The ready node was not released again; only the second node got a document.
        assert not (docs / f"{RACK[0]}.json").exists() and (docs / f"{RACK[1]}.json").exists()
        assert fleets


def test_the_rollout_is_a_resumable_builtin_workflow():
    assert we.get_workflow(pr.WORKFLOW_ID)["steps"][0]["native"] == pr.NATIVE_STEP
    assert pr.NATIVE_STEP in we._RESUMABLE_NATIVE_STEPS


def test_the_settings_say_what_a_pull_rollout_needs():
    s = models.P2pSettings.from_dict({"cluster_id": "nc"})
    assert s.rollout_blockers() and s.resolved_origin_url() == ""
    s = models.P2pSettings.from_dict({"cluster_id": "nc", "appserver_ip": "10.10.20.22", "desired_dir": "/d"})
    assert s.rollout_blockers() == [] and s.resolved_origin_url() == "http://10.10.20.22:12130"
    bad = models.P2pSettings.from_dict({"cluster_id": "nc", "desired_dir": "rel", "metrics_url": "ftp://x",
                                        "rollout_node_budget_seconds": 5, "rollout_max_active_racks": 0})
    problems = " ".join(bad.validate())
    for word in ("desired_dir", "metrics_url", "rollout_node_budget_seconds", "rollout_max_active_racks"):
        assert word in problems


def test_vm_observer_reads_the_four_series(monkeypatch):
    now = 10_000.0
    answers = {
        "nvis_p2p_agent_last_poll_timestamp_seconds": [{"metric": {"instance": "a"}, "value": [now, str(now - 10)]},
                                                       {"metric": {"instance": "b"}, "value": [now, str(now - 999)]}],
        "nvis_p2p_agent_up": [{"metric": {"instance": "a", "generation": "3"}, "value": [now, "1"]}],
        "nvis_p2p_image_state": [{"metric": {"instance": "a", "state": "ready"}, "value": [now, "1"]}],
        "nvis_p2p_image_failures_total": [{"metric": {"instance": "a"}, "value": [now, "2"]}],
    }
    seen = []

    def fake_instant(expr, base=None):
        seen.append((expr, base))
        return answers[expr.split("{")[0]]

    monkeypatch.setattr(pr.metrics_query, "instant", fake_instant)
    obs = pr.vm_observer("http://vm:8428", now=lambda: now)(["a", "b"], "big.sqsh")
    assert (obs["a"].alive, obs["a"].generation, obs["a"].state, obs["a"].failures) == (True, 3, "ready", 2)
    assert obs["b"].alive is False
    assert all(base == "http://vm:8428" for _e, base in seen)
    assert 'instance=~"a|b"' in seen[0][0]


def test_describe_reports_counts_and_racks():
    plan = {"image": IMAGE, "racks": {"Q5": ["a", "b"]}}
    state = pr.new_state(plan, 0)
    state["nodes"]["a"]["state"] = "ready"
    run = {"run_id": "r", "state": "running", "params": {"plan": plan, "pacing": PACING, "cluster_id": "nc"},
           "rollout": state}
    d = pr.describe(run, detail=True)
    assert d["counts"]["ready"] == 1 and d["counts"]["pending"] == 1 and d["racks"] == ["Q5"]
    assert d["rack_detail"][0]["cuts"] == [1, 2]


def test_documents_are_written_atomically_and_world_readable(tmp_path):
    store = pr.DocumentStore(tmp_path)
    store.write("n1", {"schema_version": 1, "generation": 1})
    assert (tmp_path / "n1.json").stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".*.tmp"))
    assert store.read("missing") is None


def test_selector_rejects_non_hostnames():
    with pytest.raises(ValueError):
        pr.instance_selector(['x"} or vector(1)'])
    assert pr.instance_selector(["a.b-c"]) == "a\\\\.b-c"


_ = (threading, SimpleNamespace)
