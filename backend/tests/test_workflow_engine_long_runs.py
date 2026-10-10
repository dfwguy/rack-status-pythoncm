# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""What the workflow engine needs to carry a run that lasts hours: built-in definitions,
idempotent starts, steering a live run, honouring a cancel that lands mid-step, and resuming a
run a restart interrupted -- without ever driving one run twice.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import workflow_engine as we


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(we, "WORKFLOWS_DIR", tmp_path / "workflows")
    monkeypatch.setattr(we, "RUNS_DIR", tmp_path / "workflows" / "runs")
    monkeypatch.setattr(we, "_active_runs", {})
    monkeypatch.setattr(we, "_BUILTIN_WORKFLOWS", {})
    monkeypatch.setattr(we, "_NATIVE_STEPS", {})
    monkeypatch.setattr(we, "_RESUMABLE_NATIVE_STEPS", set())
    yield
    # No executor may outlive its test: a straggler would look up the *next* test's native step
    # (the registry is swapped per test) and run it with the wrong params.
    deadline = time.time() + 5
    while time.time() < deadline and any(
        t.name != "MainThread" and t.is_alive() and t.daemon for t in threading.enumerate()
        if t.name.startswith("Thread-")
    ) and any(r.get("state") not in we.TERMINAL_STATES for r in we._active_runs.values()):
        time.sleep(0.02)


def _wait(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def _long_step(release: threading.Event, seen: list):
    """A native step that runs until released or cancelled, recording what it saw."""

    def fn(run, step):
        seen.append(dict(run.get("params", {})))
        while not release.is_set():
            if run.get("state") == "cancelled":
                return False, "stopped on cancel"
            time.sleep(0.02)
        return True, "done"

    return fn


def _register(fn, resumable=True):
    we.register_native_step("long", fn, resumable=resumable)
    we.register_builtin_workflow({"id": "long-wf", "name": "Long", "steps": [
        {"id": "work", "type": "automated", "native": "long"}]})


class TestBuiltinWorkflows:
    def test_a_builtin_definition_runs_without_a_yaml_file(self):
        release, seen = threading.Event(), []
        release.set()
        _register(_long_step(release, seen))
        run_id = we.start_workflow_run("long-wf", {"x": 1})["run_id"]
        _wait(lambda: we.get_run_state(run_id)["state"] == "completed")
        assert seen == [{"x": 1}]

    def test_a_yaml_file_of_the_same_id_overrides_it(self):
        _register(_long_step(threading.Event(), []))
        we.WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
        (we.WORKFLOWS_DIR / "long-wf.yaml").write_text("id: long-wf\nname: Site override\nsteps:\n- id: a\n  type: manual\n")
        assert we.get_workflow("long-wf")["name"] == "Site override"

    def test_a_definition_without_steps_is_refused(self):
        with pytest.raises(ValueError):
            we.register_builtin_workflow({"id": "empty", "steps": []})


class TestSteeringALiveRun:
    def test_a_second_start_with_the_same_key_joins_the_live_run(self):
        release, seen = threading.Event(), []
        _register(_long_step(release, seen))
        first = we.start_workflow_run("long-wf", {}, idempotency_key="k")
        second = we.start_workflow_run("long-wf", {}, idempotency_key="k")
        assert second["run_id"] == first["run_id"] and second["joined"] is True
        other = we.start_workflow_run("long-wf", {}, idempotency_key="other")
        assert other["run_id"] != first["run_id"]
        release.set()
        _wait(lambda: we.get_run_state(first["run_id"])["state"] == "completed")
        # Once the first is finished the key is free again.
        third = we.start_workflow_run("long-wf", {}, idempotency_key="k")
        assert third["run_id"] != first["run_id"]
        for run_id in (third["run_id"], other["run_id"]):  # leave no executor behind for the next test
            _wait(lambda run_id=run_id: we.get_run_state(run_id)["state"] == "completed")

    def test_params_update_reaches_the_running_step_and_is_refused_once_terminal(self):
        release, seen = threading.Event(), []
        _register(_long_step(release, seen))
        run_id = we.start_workflow_run("long-wf", {"pace": 1})["run_id"]
        _wait(lambda: seen)
        we.update_run_params(run_id, {"pace": 2})
        assert we.get_run_state(run_id)["params"]["pace"] == 2
        assert json.loads((we.RUNS_DIR / run_id / "state.json").read_text())["params"]["pace"] == 2
        release.set()
        _wait(lambda: we.get_run_state(run_id)["state"] == "completed")
        with pytest.raises(ValueError, match="final"):
            we.update_run_params(run_id, {"pace": 3})

    def test_a_cancel_that_lands_mid_step_is_not_overwritten_by_the_step_result(self):
        release, seen = threading.Event(), []
        _register(_long_step(release, seen))
        run_id = we.start_workflow_run("long-wf", {})["run_id"]
        _wait(lambda: seen)
        we.cancel_run(run_id)
        _wait(lambda: we.get_run_state(run_id)["history"])
        state = we.get_run_state(run_id)
        assert state["state"] == "cancelled" and state["history"][-1]["cancelled"] is True

    def test_runs_can_be_listed_per_workflow(self):
        release, seen = threading.Event(), []
        release.set()
        _register(_long_step(release, seen))
        we.register_builtin_workflow({"id": "other-wf", "steps": [{"id": "w", "type": "automated", "native": "long"}]})
        a = we.start_workflow_run("long-wf", {})["run_id"]
        we.start_workflow_run("other-wf", {})
        _wait(lambda: len(we.list_runs()) == 2)
        assert [r["run_id"] for r in we.list_runs(workflow_id="long-wf")] == [a]


class TestResume:
    def _interrupted(self, native="long"):
        """A run on disk that a restart left 'running' at a native step, with no executor."""
        run_id = "run-interrupted"
        (we.RUNS_DIR / run_id).mkdir(parents=True)
        (we.RUNS_DIR / run_id / "state.json").write_text(json.dumps({
            "run_id": run_id, "workflow_id": "long-wf", "state": "running", "current_step_id": "work",
            "params": {"resumed": True}, "history": [], "started_at": "2026-10-10T00:00:00Z"}))
        return run_id

    def test_a_resumable_step_is_restarted_and_finishes(self):
        release, seen = threading.Event(), []
        release.set()
        _register(_long_step(release, seen))
        run_id = self._interrupted()
        assert we.resume_interrupted_runs() == [run_id]
        _wait(lambda: we.get_run_state(run_id)["state"] == "completed")
        assert seen == [{"resumed": True}]

    def test_a_step_not_declared_resumable_is_left_alone(self):
        _register(_long_step(threading.Event(), []), resumable=False)
        run_id = self._interrupted()
        assert we.resume_interrupted_runs() == []
        assert json.loads((we.RUNS_DIR / run_id / "state.json").read_text())["state"] == "running"

    def test_a_run_is_never_driven_by_two_executors(self):
        release, seen = threading.Event(), []
        _register(_long_step(release, seen))
        run_id = self._interrupted()
        assert we.resume_interrupted_runs() == [run_id]
        _wait(lambda: seen)
        # A second sweep (another process, a repeated call) finds the run claimed in-process ...
        assert we.resume_interrupted_runs() == []
        # ... and an executor started anyway is refused by the run's lock.
        we._execute_run(run_id)
        assert len(seen) == 1
        release.set()
        _wait(lambda: we.get_run_state(run_id)["state"] == "completed")
