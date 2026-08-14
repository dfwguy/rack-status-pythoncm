# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for workflow_engine.py module."""
import json
import os
import shlex
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import rack_locks
import workflow_engine


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    """Use temp directories for workflow storage during tests."""
    wf_dir = tmp_path / "workflows"
    runs_dir = wf_dir / "runs"
    wf_dir.mkdir()
    runs_dir.mkdir()
    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", wf_dir)
    monkeypatch.setattr(workflow_engine, "RUNS_DIR", runs_dir)
    yield wf_dir, runs_dir


class TestWorkflowCRUD:
    def test_save_and_list_workflows(self):
        wf = workflow_engine.save_workflow({"name": "Test WF", "steps": [{"id": "s1", "type": "manual", "name": "Step 1"}]})
        assert wf["id"]
        assert wf["version"] == 1

        wfs = workflow_engine.list_workflows()
        assert len(wfs) == 1
        assert wfs[0]["name"] == "Test WF"
        assert wfs[0]["step_count"] == 1

    def test_get_workflow(self):
        workflow_engine.save_workflow({"id": "test-get", "name": "Get Test"})
        loaded = workflow_engine.get_workflow("test-get")
        assert loaded is not None
        assert loaded["name"] == "Get Test"

    def test_get_workflow_not_found(self):
        assert workflow_engine.get_workflow("nonexistent") is None

    def test_delete_workflow(self):
        workflow_engine.save_workflow({"id": "to-delete", "name": "Delete Me"})
        assert workflow_engine.delete_workflow("to-delete") is True
        assert workflow_engine.get_workflow("to-delete") is None

    def test_delete_nonexistent(self):
        assert workflow_engine.delete_workflow("nope") is False

    def test_version_auto_increment(self):
        workflow_engine.save_workflow({"id": "ver-test", "name": "V1"})
        wf = workflow_engine.save_workflow({"id": "ver-test", "name": "V2"})
        assert wf["version"] == 2
        wf = workflow_engine.save_workflow({"id": "ver-test", "name": "V3"})
        assert wf["version"] == 3


class TestWorkflowExecution:
    def _create_delay_workflow(self):
        return workflow_engine.save_workflow({
            "id": "delay-wf",
            "name": "Delay Test",
            "steps": [
                {"id": "d1", "type": "delay", "name": "Wait", "timeout": 1},
                {"id": "d2", "type": "automated", "name": "Done", "command": ""},
            ],
        })

    def test_start_run_missing_workflow(self):
        with pytest.raises(ValueError, match="not found"):
            workflow_engine.start_workflow_run("nonexistent")

    def test_start_run_creates_state(self):
        self._create_delay_workflow()
        result = workflow_engine.start_workflow_run("delay-wf")
        assert "run_id" in result
        run = workflow_engine.get_run_state(result["run_id"])
        assert run is not None
        assert run["workflow_id"] == "delay-wf"

    def test_invalidated_terminal_run_is_hidden_but_retained(self):
        run = {
            "run_id": "invalid-run",
            "workflow_id": "hardware-deep-audit",
            "workflow_name": "Hardware Deep Audit",
            "state": "failed",
            "current_step_id": None,
            "started_at": "2026-08-13T00:00:00Z",
            "updated_at": "2026-08-13T00:01:00Z",
        }
        workflow_engine._save_run(run)
        invalidated = workflow_engine.invalidate_run(
            "invalid-run", reason="validator defect", operator="qa"
        )
        assert invalidated["invalidated_at"]
        assert workflow_engine.get_run_state("invalid-run")["invalidation_reason"] == "validator defect"
        assert all(row["run_id"] != "invalid-run" for row in workflow_engine.list_runs())

    def test_delay_step_execution(self):
        self._create_delay_workflow()
        result = workflow_engine.start_workflow_run("delay-wf")
        time.sleep(2.5)
        run = workflow_engine.get_run_state(result["run_id"])
        assert run["state"] in ("completed", "running")
        history_steps = [h["step_id"] for h in run.get("history", [])]
        assert "d1" in history_steps

    def test_sequential_fallback(self):
        workflow_engine.save_workflow({
            "id": "seq-test",
            "name": "Sequential",
            "steps": [
                {"id": "a", "type": "automated", "name": "A", "command": ""},
                {"id": "b", "type": "automated", "name": "B", "command": ""},
            ],
        })
        result = workflow_engine.start_workflow_run("seq-test")
        time.sleep(1)
        run = workflow_engine.get_run_state(result["run_id"])
        assert run["state"] == "completed"
        history_steps = [h["step_id"] for h in run.get("history", [])]
        assert "a" in history_steps
        assert "b" in history_steps


class TestStepOutput:
    def test_save_and_get_output(self, tmp_dirs):
        path = workflow_engine._save_step_output("run-123", "step-1", "Hello output")
        assert path == "run-123/step-1.log"
        content = workflow_engine.get_step_output("run-123", "step-1")
        assert content == "Hello output"

    def test_get_output_missing(self):
        assert workflow_engine.get_step_output("no-run", "no-step") is None

    def test_output_snippet_short(self):
        text = "line1\nline2\nline3"
        snippet = workflow_engine._get_output_snippet(text)
        assert snippet == text.strip()

    def test_output_snippet_truncates(self):
        lines = "\n".join(f"line {i}" for i in range(50))
        snippet = workflow_engine._get_output_snippet(lines, max_lines=5)
        assert "lines above" in snippet
        tail_lines = [l for l in snippet.split("\n") if l.startswith("line ")]
        assert len(tail_lines) == 5


class TestAutomatedHostStepLocks:
    """Verify the host: branch of _execute_automated_step acquires + releases an explicit claim."""

    @pytest.fixture(autouse=True)
    def isolated_claims(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rack_locks, "CLAIMS_FILE", tmp_path / "rack_locks.json")

    def _fake_run_on_host_ok(self, *args, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = "ok\n"
        m.stderr = ""
        return m

    def test_host_step_with_explicit_rack_claims_and_releases(self):
        run = {"run_id": "abcdef0123456789", "params": {}, "history": []}
        step = {"id": "s1", "type": "automated", "command": "host:uptime", "rack": "p5-r01", "timeout": 5}
        with patch("workflow_engine.host_exec.run_on_host", side_effect=self._fake_run_on_host_ok):
            success, _ = workflow_engine._execute_automated_step(run, step)
        assert success is True
        # claim acquired and released
        assert rack_locks.list_claims() == {}

    def test_host_step_extracts_rack_from_command(self):
        run = {"run_id": "abcdef0123456789", "params": {}, "history": []}
        step = {"id": "s1", "type": "automated", "command": "host:pdsh -w p7-r02-ct[01-18] uptime", "timeout": 5}
        captured = {}

        def _spy(*args, **kwargs):
            # at the moment run_on_host runs, the rack should be claimed by workflow:<run_id[:8]>
            claims = rack_locks.list_claims()
            captured["claims"] = dict(claims)
            return self._fake_run_on_host_ok()

        with patch("workflow_engine.host_exec.run_on_host", side_effect=_spy):
            workflow_engine._execute_automated_step(run, step)

        assert "p7-r02" in captured["claims"]
        assert captured["claims"]["p7-r02"]["operator"].startswith("workflow:")
        # released after run_on_host returns
        assert rack_locks.list_claims() == {}

    def test_host_step_releases_claim_on_exception(self):
        run = {"run_id": "abcdef0123456789", "params": {}, "history": []}
        step = {"id": "s1", "type": "automated", "command": "host:false", "rack": "p5-r01", "timeout": 5}
        with patch("workflow_engine.host_exec.run_on_host", side_effect=OSError("boom")):
            success, _ = workflow_engine._execute_automated_step(run, step)
        assert success is False
        assert rack_locks.list_claims() == {}

    def test_host_step_continues_when_lock_held_by_other(self):
        # Pre-claim by a different operator; workflow should NOT block (advisory only).
        rack_locks.upsert_claim("p5-r01", "alice", ttl_sec=300)
        run = {"run_id": "abcdef0123456789", "params": {}, "history": []}
        step = {"id": "s1", "type": "automated", "command": "host:uptime", "rack": "p5-r01", "timeout": 5}
        with patch("workflow_engine.host_exec.run_on_host", side_effect=self._fake_run_on_host_ok):
            success, _ = workflow_engine._execute_automated_step(run, step)
        assert success is True
        # alice's claim is preserved
        claims = rack_locks.list_claims()
        assert claims["p5-r01"]["operator"] == "alice"


class TestMermaidGeneration:
    def test_generate_mermaid_from_steps(self):
        steps = [
            {"id": "start", "name": "Start", "type": "manual", "on_complete": "check"},
            {"id": "check", "name": "Check", "type": "automated", "on_success": "done"},
            {"id": "done", "name": "Done", "type": "manual"},
        ]
        mermaid = workflow_engine.generate_mermaid_from_steps(steps)
        assert "graph TD" in mermaid
        assert "start" in mermaid
        assert "check" in mermaid
        assert "done" in mermaid

    def test_decision_step_generates_options(self):
        steps = [
            {"id": "decide", "name": "Choose", "type": "decision", "options": [
                {"label": "Yes", "next": "a"},
                {"label": "No", "next": "b"},
            ]},
        ]
        mermaid = workflow_engine.generate_mermaid_from_steps(steps)
        assert '"Yes"' in mermaid
        assert '"No"' in mermaid


def test_slurm_fanout_maps_labeled_results_back_to_exact_nodes(monkeypatch):
    captured = {}
    output = (
        "0: __NVIS_JOB__ink50-dh01-j21-11|147892\n"
        '0: __NVIS_NODE__ink50-dh01-j21-11|NVIS_DEEP_AUDIT_JSON={"status":"pass"}\n'
        "0: __NVIS_RC__ink50-dh01-j21-11|0\n"
        "1: __NVIS_JOB__ink50-dh01-j21-12|147892\n"
        '1: __NVIS_NODE__ink50-dh01-j21-12|NVIS_DEEP_AUDIT_JSON={"status":"fail"}\n'
        "1: __NVIS_RC__ink50-dh01-j21-12|2\n"
    )

    def run_slurm(cmd, **_kwargs):
        if "sinfo" in cmd:
            return subprocess.CompletedProcess("sinfo", 0, "", "")
        captured["cmd"] = cmd
        return subprocess.CompletedProcess("srun", 0, output, "")

    monkeypatch.setattr(
        workflow_engine.host_exec,
        "run_slurm_on_host",
        run_slurm,
    )
    rows = workflow_engine._execute_slurm_fanout(
        ["ink50-dh01-j21-11", "ink50-dh01-j21-12"],
        "bash /scripts/audit.sh",
        timeout=90,
        sudo=True,
        job_name="audit",
        gpus_per_node=4,
    )
    assert [(row.node, row.returncode) for row in rows] == [
        ("ink50-dh01-j21-11", 0),
        ("ink50-dh01-j21-12", 2),
    ]
    assert "status\":\"fail" in rows[1].stdout
    assert rows[0].stdout.startswith("NVIS_SLURM_JOB_ID=147892\n")
    assert workflow_engine._audit_scheduler_ref(rows[1].stdout) == "147892"
    assert "--kill-on-bad-exit=0" in captured["cmd"]
    assert "--immediate=60" in captured["cmd"]
    assert "--gpus-per-node=4" in captured["cmd"]
    assert "--kill-on-bad-exit 0" not in captured["cmd"]
    assert "--immediate 60" not in captured["cmd"]
    generated = shlex.split(captured["cmd"])
    srun_index = next(
        index for index, arg in enumerate(generated)
        if arg == "srun" or arg.endswith("/srun")
    )
    srun_argv = generated[srun_index:]
    assert "0" not in srun_argv[:-1]
    assert srun_argv[-3:-1] == ["bash", "-c"]


def test_slurm_fanout_preflight_isolates_one_drained_node_from_healthy_peers(monkeypatch):
    captured = {}

    def run_slurm(cmd, **_kwargs):
        if "sinfo" in cmd:
            return subprocess.CompletedProcess(
                "sinfo",
                0,
                "ink50-dh01-d01-11|drained|Prolog error\n"
                "ink50-dh01-d01-12|idle|none\n",
                "",
            )
        captured["cmd"] = cmd
        output = (
            "0: __NVIS_JOB__ink50-dh01-d01-12|147893\n"
            '0: __NVIS_NODE__ink50-dh01-d01-12|NVIS_DEEP_AUDIT_JSON={"status":"pass"}\n'
            "0: __NVIS_RC__ink50-dh01-d01-12|0\n"
        )
        return subprocess.CompletedProcess("srun", 0, output, "")

    monkeypatch.setattr(workflow_engine.host_exec, "run_slurm_on_host", run_slurm)
    rows = workflow_engine._execute_slurm_fanout(
        ["ink50-dh01-d01-11", "ink50-dh01-d01-12"],
        "bash /scripts/audit.sh",
        timeout=90,
        sudo=True,
        job_name="audit",
        gpus_per_node=4,
    )

    blocked = json.loads(rows[0].stdout.split("=", 1)[1])
    assert blocked["status"] == "fail"
    assert blocked["category"] == "control_plane"
    assert blocked["codes"] == ["slurm_node_unavailable"]
    assert "drained" in blocked["reason"]
    assert rows[1].returncode == 0
    assert workflow_engine._audit_scheduler_ref(rows[1].stdout) == "147893"
    assert "--nodes 1" in captured["cmd"]
    assert "--nodelist ink50-dh01-d01-12" in captured["cmd"]
    assert "ink50-dh01-d01-11" not in captured["cmd"]
