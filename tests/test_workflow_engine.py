"""Tests for workflow_engine.py module."""
import os
import time

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

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
        wf = workflow_engine.save_workflow({"id": "test-get", "name": "Get Test"})
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
        _, runs_dir = tmp_dirs
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
        assert snippet.count("\n") == 4


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
