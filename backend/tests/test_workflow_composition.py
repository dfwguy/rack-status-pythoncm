# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for composable workflow step blocks (connection/scheduler/evaluation/capability)."""
import importlib
import os
import tempfile

import pytest
import workflow_models


class TestValidateConnection:
    def test_absent_ok(self):
        assert workflow_models.validate_connection(None) == []

    def test_valid_profiles(self):
        assert workflow_models.validate_connection({"profile": "control"}) == []
        assert workflow_models.validate_connection({"profile": "script"}) == []

    def test_bad_profile(self):
        assert workflow_models.validate_connection({"profile": "root"})

    def test_sudo_type(self):
        assert workflow_models.validate_connection({"sudo": "yes"})
        assert workflow_models.validate_connection({"sudo": True}) == []


class TestValidateScheduler:
    def test_absent_ok(self):
        assert workflow_models.validate_scheduler(None) == []

    def test_needs_runnable(self):
        assert workflow_models.validate_scheduler({"kind": "slurm"})  # no script/command/image

    def test_valid(self):
        assert workflow_models.validate_scheduler({"kind": "slurm", "script": "x.sbatch"}) == []
        assert workflow_models.validate_scheduler({"kind": "local", "command": "mpirun ..."}) == []

    def test_bad_kind(self):
        assert workflow_models.validate_scheduler({"kind": "pbs", "script": "x.sbatch"})

    def test_future_kind_accepted_in_schema(self):
        # run:AI accepted at schema level (raises only at run time)
        assert workflow_models.validate_scheduler({"kind": "runai", "image": "img"}) == []


class TestValidateEvaluation:
    def test_absent_ok(self):
        assert workflow_models.validate_evaluation(None) == []

    def test_valid(self):
        ev = {
            "success_when": {"exit_code": 0, "stdout_contains": "OK"},
            "extract": [{"name": "bw", "regex": r"([0-9.]+)", "type": "float"}],
            "assert": ["bw >= 300"],
        }
        assert workflow_models.validate_evaluation(ev) == []

    def test_extract_needs_name_and_regex(self):
        assert workflow_models.validate_evaluation({"extract": [{"name": "bw"}]})
        assert workflow_models.validate_evaluation({"extract": [{"regex": "x"}]})

    def test_bad_extract_type(self):
        assert workflow_models.validate_evaluation({"extract": [{"name": "b", "regex": "x", "type": "bool"}]})

    def test_assert_must_be_string_list(self):
        assert workflow_models.validate_evaluation({"assert": [123]})


class TestValidateStepAndWorkflow:
    def test_backward_compatible_step(self):
        # A classic automated step (no composition blocks) is valid.
        step = {"id": "s1", "type": "automated", "command": "host:echo hi", "on_success": "done"}
        assert workflow_models.validate_step(step) == []

    def test_capability_string(self):
        assert workflow_models.validate_step({"id": "s", "capability": "sysinfo"}) == []
        assert workflow_models.validate_step({"id": "s", "capability": ""})

    def test_workflow_collects_step_problems(self):
        wf = {"steps": [
            {"id": "ok", "command": "host:true"},
            {"id": "bad", "connection": {"profile": "nope"}},
        ]}
        problems = workflow_models.validate_workflow(wf)
        assert any("bad" in p for p in problems)
        assert not any("'ok'" in p for p in problems)


class TestSaveWorkflowValidation:
    """save_workflow rejects invalid composition blocks and round-trips valid ones."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def test_round_trip_valid_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {
                "id": "compos", "name": "compos",
                "steps": [{
                    "id": "nccl", "type": "automated", "name": "nccl",
                    "connection": {"profile": "script", "sudo": True},
                    "scheduler": {"kind": "slurm", "script": "nccl_allreduce.sbatch", "nodes": "${nodes}"},
                    "evaluation": {"success_when": {"exit_code": 0}, "assert": ["bw >= 300"]},
                    "on_success": "done",
                }],
            }
            we.save_workflow(wf)
            loaded = we.get_workflow("compos")
            assert loaded["steps"][0]["connection"]["profile"] == "script"
            assert loaded["steps"][0]["scheduler"]["kind"] == "slurm"
            assert loaded["steps"][0]["evaluation"]["assert"] == ["bw >= 300"]

    def test_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            with pytest.raises(ValueError, match="invalid workflow"):
                we.save_workflow({
                    "id": "bad", "name": "bad",
                    "steps": [{"id": "s", "scheduler": {"kind": "slurm"}}],  # no script/command/image
                })


class TestStepConnectionResolution:
    """Per-step connection routing (Phase 2)."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def test_default_uses_global_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            conn, profile = we._resolve_step_connection({"id": "s"})
            assert conn is None and profile == "control"

    def test_profile_script_no_custom_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            conn, profile = we._resolve_step_connection({"id": "s", "connection": {"profile": "script"}})
            assert conn is None and profile == "script"

    def test_sudo_true_implies_script_and_custom_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            conn, profile = we._resolve_step_connection({"id": "s", "connection": {"sudo": True}})
            assert profile == "script"
            assert conn is not None  # dedicated transport connection with sudo forced

    def test_sudo_false_builds_no_sudo_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            conn, profile = we._resolve_step_connection(
                {"id": "s", "connection": {"profile": "script", "sudo": False}}
            )
            assert conn is not None and profile == "script"

    def test_engine_runs_step_with_connection_block(self):
        # End-to-end: an automated step with an explicit connection still executes
        # and evaluates exit code (local mode: control/script both run locally).
        import time
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "connwf", "name": "connwf", "steps": [
                {"id": "echo", "type": "automated", "name": "echo",
                 "command": "host:echo routed-ok", "connection": {"profile": "control"},
                 "on_success": "done"},
            ]}
            we.save_workflow(wf)
            r = we.start_workflow_run("connwf", {})
            for _ in range(20):
                st = we.get_run_state(r["run_id"])
                if st["state"] in ("completed", "failed"):
                    break
                time.sleep(0.25)
            assert st["state"] == "completed"
            assert "routed-ok" in (we.get_step_output(r["run_id"], "echo") or "")


class TestSchedulerStep:
    """Scheduler-block execution in the engine (Phase 4), using the local backend."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def _run_to_terminal(self, we, wf_id, params):
        import time
        r = we.start_workflow_run(wf_id, params)
        for _ in range(40):
            st = we.get_run_state(r["run_id"])
            if st["state"] in ("completed", "failed", "waiting_manual"):
                break
            time.sleep(0.25)
        return st, r["run_id"]

    def test_local_scheduler_step_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "schedwf", "name": "schedwf", "steps": [{
                "id": "job", "type": "automated", "name": "job",
                "scheduler": {"kind": "local", "command": "echo busbw 557"},
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, rid = self._run_to_terminal(we, "schedwf", {})
            assert st["state"] == "completed"
            out = we.get_step_output(rid, "job") or ""
            assert "busbw 557" in out and "scheduler=local" in out

    def test_local_scheduler_step_failure_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "schedfail", "name": "schedfail", "steps": [{
                "id": "job", "type": "automated", "name": "job",
                "scheduler": {"kind": "local", "command": "sh -c 'exit 4'"},
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, _ = self._run_to_terminal(we, "schedfail", {})
            assert st["state"] == "waiting_manual"
            assert st["current_step_id"] == "handle"

    def test_slurm_scheduler_step_builds_sbatch(self, monkeypatch):
        # Verify the engine builds a JobSpec and hands it to the Slurm backend with
        # the expected nodelist/ntasks/export, without a real cluster.
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            captured = {}

            class FakeSlurm:
                kind = "slurm"

                def __init__(self, *a, **k):
                    pass

                def submit(self, spec):
                    captured["spec"] = spec
                    from schedulers.base import JobHandle
                    return JobHandle("42", "slurm")

                def wait(self, handle, timeout=1800, poll_interval=5):
                    from schedulers.base import JobResult
                    return JobResult(0, "COMPLETED", stdout="Avg bus bandwidth : 557", job_id="42")

            import schedulers
            monkeypatch.setattr(schedulers, "build_scheduler", lambda *a, **k: FakeSlurm())
            # engine imports build_scheduler inside the function, from schedulers
            wf = {"id": "nccl", "name": "nccl", "steps": [{
                "id": "j", "type": "automated", "name": "j",
                "scheduler": {"kind": "slurm", "script": "nccl_allreduce.sbatch",
                              "nodelist": "${target}", "ntasks_per_node": "${gpus_per_node}"},
                "on_success": "done",
            }]}
            we.save_workflow(wf)
            os.environ["GPUS_PER_NODE"] = "4"
            st, _rid = self._run_to_terminal(we, "nccl", {"target": "n1,n2,n3"})
            spec = captured["spec"]
            assert spec.nodelist == "n1,n2,n3" and spec.nodes == 3 and spec.ntasks_per_node == 4
            assert spec.script.endswith("nccl_allreduce.sbatch")
            assert any("--export=" in a and "NVIS_SCRIPTS_DIR=" in a for a in spec.extra_args)
            assert st["state"] == "completed"


class TestEvaluationInEngine:
    """Value-based evaluation drives transitions + persists outputs (Phase 5)."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def _run(self, we, wf_id, params=None):
        import time
        r = we.start_workflow_run(wf_id, params or {})
        for _ in range(40):
            st = we.get_run_state(r["run_id"])
            if st["state"] in ("completed", "failed", "waiting_manual"):
                break
            time.sleep(0.25)
        return st, r["run_id"]

    def test_threshold_pass_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "evpass", "name": "evpass", "steps": [{
                "id": "bw", "type": "automated", "name": "bw",
                "command": "host:echo busbw 557.1",
                "evaluation": {
                    "extract": [{"name": "bw", "regex": r"busbw\s+([0-9.]+)", "type": "float"}],
                    "assert": ["bw >= 300"],
                },
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, rid = self._run(we, "evpass")
            assert st["state"] == "completed"
            assert st.get("outputs", {}).get("bw") == 557.1
            assert "result: PASS" in (we.get_step_output(rid, "bw") or "")

    def test_evaluation_message_and_prompt_surface_on_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "evmsg", "name": "evmsg", "steps": [{
                "id": "bw", "type": "automated", "name": "bw",
                "command": "host:echo busbw 120.0",
                "evaluation": {
                    "extract": [{"name": "bw", "regex": r"busbw\s+([0-9.]+)", "type": "float"}],
                    "assert": ["bw >= 300"],
                    "message_fail": "bandwidth ${bw} below floor 300",
                    "prompt": "check NIC rail mapping and cabling",
                },
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, rid = self._run(we, "evmsg")
            # message + prompt persisted on the run for UI / agents
            meta = st.get("evaluations", {}).get("bw", {})
            assert meta.get("result") == "fail"
            assert meta.get("message") == "bandwidth 120.0 below floor 300"
            assert meta.get("prompt") == "check NIC rail mapping and cabling"
            # and appended to the step output
            out = we.get_step_output(rid, "bw") or ""
            assert "message: bandwidth 120.0 below floor 300" in out
            assert "hint: check NIC rail mapping" in out

    def test_threshold_fail_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "evfail", "name": "evfail", "steps": [{
                "id": "bw", "type": "automated", "name": "bw",
                "command": "host:echo busbw 120.0",
                "evaluation": {
                    "extract": [{"name": "bw", "regex": r"busbw\s+([0-9.]+)", "type": "float"}],
                    "assert": ["bw >= 300"],
                },
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, _ = self._run(we, "evfail")
            assert st["state"] == "waiting_manual" and st["current_step_id"] == "handle"


class TestCapabilityResolution:
    """capability id -> command + param validation (Phase 6)."""

    class _FakeRepo:
        def __init__(self, tools):
            self._tools = tools

        def list(self, table):
            return self._tools

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def test_env_key_tool(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import catalog.store as cs
            monkeypatch.setattr(cs, "get_repo", lambda: self._FakeRepo(
                [{"id": "sysinfo", "env_key": "RACK_SYSINFO_SCRIPT", "script": "sysinfo.sh", "params": []}]))
            cmd, _params, problems = we._resolve_capability("sysinfo", {})
            assert cmd == "RACK_SYSINFO_SCRIPT" and problems == []

    def test_script_fallback_tool(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import catalog.store as cs
            monkeypatch.setattr(cs, "get_repo", lambda: self._FakeRepo(
                [{"id": "probe", "script": "fabric_probe.sh", "params": []}]))
            cmd, _params, problems = we._resolve_capability("probe", {})
            assert cmd.startswith("host:bash ") and cmd.endswith("fabric_probe.sh") and problems == []

    def test_not_found(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import catalog.store as cs
            monkeypatch.setattr(cs, "get_repo", lambda: self._FakeRepo([]))
            cmd, _params, problems = we._resolve_capability("nope", {})
            assert cmd is None and any("not found" in p for p in problems)

    def test_required_param_missing_then_present(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import catalog.store as cs
            monkeypatch.setattr(cs, "get_repo", lambda: self._FakeRepo(
                [{"id": "t", "script": "t.sh", "params": [{"key": "rack", "required": True}]}]))
            cmd, _p, problems = we._resolve_capability("t", {})
            assert cmd is None and any("missing required" in p for p in problems)
            cmd2, _p2, problems2 = we._resolve_capability("t", {"rack": "r1"})
            assert cmd2 and problems2 == []

    def test_param_default_applied(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import catalog.store as cs
            monkeypatch.setattr(cs, "get_repo", lambda: self._FakeRepo(
                [{"id": "t", "script": "t.sh",
                  "params": [{"key": "mode", "required": True, "default": "fast"}]}]))
            _cmd, params, problems = we._resolve_capability("t", {})
            assert problems == [] and params.get("mode") == "fast"


class TestInlineRun:
    """Embedded run: batch-script/command steps (base64 transport) + continue_on_failure."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def _run(self, we, wf_id, params=None):
        import time
        r = we.start_workflow_run(wf_id, params or {})
        for _ in range(60):
            st = we.get_run_state(r["run_id"])
            if st["state"] in ("completed", "failed", "waiting_manual"):
                break
            time.sleep(0.25)
        return st, r["run_id"]

    def test_multiline_run_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "runwf", "name": "runwf", "steps": [{
                "id": "s", "type": "automated", "name": "s",
                "run": "echo line-one\necho line-two\nexit 0",
            }]}
            we.save_workflow(wf)
            st, rid = self._run(we, "runwf")
            out = we.get_step_output(rid, "s") or ""
            assert st["state"] == "completed"
            assert "line-one" in out and "line-two" in out

    def test_run_nonzero_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "runfail", "name": "runfail", "steps": [{
                "id": "s", "type": "automated", "name": "s",
                "run": "echo checking\nexit 1",
                "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, _ = self._run(we, "runfail")
            assert st["state"] == "waiting_manual" and st["current_step_id"] == "handle"

    def test_run_with_param_substitution(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "runsub", "name": "runsub", "steps": [{
                "id": "s", "type": "automated", "name": "s",
                "run": "echo target=${target}",
            }]}
            we.save_workflow(wf)
            st, rid = self._run(we, "runsub", {"target": "n1,n2"})
            assert st["state"] == "completed"
            assert "target=n1,n2" in (we.get_step_output(rid, "s") or "")

    def test_continue_on_failure_runs_all_and_marks_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            wf = {"id": "contwf", "name": "contwf", "continue_on_failure": True, "steps": [
                {"id": "a", "type": "automated", "name": "a", "run": "echo A; exit 1"},
                {"id": "b", "type": "automated", "name": "b", "run": "echo B; exit 0"},
                {"id": "c", "type": "automated", "name": "c", "run": "echo C; exit 0"},
            ]}
            we.save_workflow(wf)
            st, rid = self._run(we, "contwf")
            # all three ran despite the first failing; overall run is failed
            assert st["state"] == "failed"
            assert "B" in (we.get_step_output(rid, "b") or "")
            assert "C" in (we.get_step_output(rid, "c") or "")
            steps_run = {h["step_id"] for h in st.get("history", [])}
            assert {"a", "b", "c"} <= steps_run


class TestFanoutStep:
    """Engine fanout step: run a command across N nodes via the Connection chain."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def _run(self, we, wf_id, params=None):
        import time
        r = we.start_workflow_run(wf_id, params or {})
        for _ in range(40):
            st = we.get_run_state(r["run_id"])
            if st["state"] in ("completed", "failed", "waiting_manual"):
                break
            time.sleep(0.25)
        return st, r["run_id"]

    def _patch_fanout(self, monkeypatch, per_node_stdout):
        import transport
        captured = {}

        def fake_fanout(base, nodes, cmd, **kw):
            captured["nodes"] = list(nodes)
            captured["cmd"] = cmd
            captured["kw"] = kw
            return [transport.NodeResult(n, per_node_stdout, "", 0) for n in nodes]

        monkeypatch.setattr(transport, "fanout", fake_fanout)
        return captured

    def test_fanout_groups_and_completes(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            cap = self._patch_fanout(monkeypatch, "GB200\n")
            wf = {"id": "fo", "name": "fo", "steps": [{
                "id": "gpus", "type": "automated", "name": "gpus",
                "fanout": {"nodes": "${nodelist}", "node": {"kind": "ssh", "user": "cmods"}},
                "run": "nvidia-smi -L",
            }]}
            we.save_workflow(wf)
            st, rid = self._run(we, "fo", {"nodelist": "n1,n2,n3"})
            assert st["state"] == "completed"
            assert cap["nodes"] == ["n1", "n2", "n3"] and cap["cmd"] == "nvidia-smi -L"
            assert cap["kw"].get("user") == "cmods"
            out = we.get_step_output(rid, "gpus") or ""
            assert "n1,n2,n3" in out and "GB200" in out

    def test_fanout_expect_empty_gate_pass(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            self._patch_fanout(monkeypatch, "")  # all nodes silent -> grouped empty
            wf = {"id": "foe", "name": "foe", "steps": [{
                "id": "ber", "type": "automated", "name": "ber",
                "fanout": {"nodes": "${nodelist}"},
                "run": "nvidia-smi nvlink -e | grep BER | grep -v 15e-255 || true",
                "evaluation": {"success_when": {"stdout_empty": True}},
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, _ = self._run(we, "foe", {"nodelist": "n1,n2"})
            assert st["state"] == "completed"

    def test_fanout_expect_empty_gate_fail_branches(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            self._patch_fanout(monkeypatch, "mlx5_7: issue\n")  # non-empty -> problem
            wf = {"id": "fof", "name": "fof", "steps": [{
                "id": "ber", "type": "automated", "name": "ber",
                "fanout": {"nodes": "${nodelist}"},
                "run": "check",
                "evaluation": {"success_when": {"stdout_empty": True}},
                "on_success": "done", "on_failure": "handle",
            }, {"id": "handle", "type": "manual", "name": "handle"}]}
            we.save_workflow(wf)
            st, _ = self._run(we, "fof", {"nodelist": "n1,n2"})
            assert st["state"] == "waiting_manual" and st["current_step_id"] == "handle"

    def test_fanout_rack_fallback_when_nodelist_unresolved(self, monkeypatch):
        # fanout.nodes="${nodelist}" but the run passes only a rack: the unresolved
        # placeholder must fall through to rack->inventory resolution.
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            cap = self._patch_fanout(monkeypatch, "OK\n")
            import workflow_engine as _we
            monkeypatch.setattr(_we, "validation_target_for_rack",
                                lambda rack, override=None: "bos01-b05-11,bos01-b05-12")
            wf = {"id": "forack", "name": "forack", "steps": [{
                "id": "s", "type": "automated", "name": "s",
                "fanout": {"nodes": "${nodelist}"}, "run": "hostname",
            }]}
            we.save_workflow(wf)
            st, _ = self._run(we, "forack", {"rack": "bos01-b05"})
            assert cap["nodes"] == ["bos01-b05-11", "bos01-b05-12"]
            assert st["state"] == "completed"

    def test_fanout_no_nodes_fails(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            self._patch_fanout(monkeypatch, "x")
            wf = {"id": "fon", "name": "fon", "steps": [{
                "id": "s", "type": "automated", "name": "s",
                "fanout": {"nodes": "${nodelist}"}, "run": "echo x",
            }]}
            we.save_workflow(wf)
            st, rid = self._run(we, "fon", {"nodelist": ""})  # resolves to no nodes at runtime
            assert st["state"] == "failed"
            assert "no target nodes" in (we.get_step_output(rid, "s") or "")


class TestResolveStep:
    """A resolve step turns rack(s) into a ${nodelist} output for downstream steps."""

    def _engine(self, tmp):
        os.environ["RACK_WORKFLOWS_DIR"] = tmp
        os.environ["RACK_EXEC_MODE"] = "local"
        import workflow_engine
        importlib.reload(workflow_engine)
        return workflow_engine

    def _run(self, we, wf_id, params=None):
        import time
        r = we.start_workflow_run(wf_id, params or {})
        for _ in range(40):
            st = we.get_run_state(r["run_id"])
            if st["state"] in ("completed", "failed", "waiting_manual"):
                break
            time.sleep(0.25)
        return st, r["run_id"]

    def test_resolve_feeds_downstream_fanout(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import transport
            cap = {}

            def fake_fanout(base, nodes, cmd, **kw):
                cap["nodes"] = list(nodes)
                return [transport.NodeResult(n, "OK\n", "", 0) for n in nodes]

            monkeypatch.setattr(transport, "fanout", fake_fanout)
            import workflow_engine as _we
            monkeypatch.setattr(_we, "validation_target_for_rack",
                                lambda rack, override=None: "n1,n2,n3" if rack else "")
            wf = {"id": "res", "name": "res", "continue_on_failure": True, "steps": [
                {"id": "resolve", "type": "automated", "name": "resolve",
                 "resolve": {"racks": "${rack}", "into": "nodelist"}},
                {"id": "chk", "type": "automated", "name": "chk",
                 "fanout": {"nodes": "${nodelist}"}, "run": "hostname"},
            ]}
            we.save_workflow(wf)
            st, _rid = self._run(we, "res", {"rack": "bos01-b05"})
            assert st["state"] == "completed"
            assert cap["nodes"] == ["n1", "n2", "n3"]
            assert st.get("outputs", {}).get("nodelist") == "n1,n2,n3"

    def test_resolve_multi_rack_concats_dedup(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import workflow_engine as _we
            monkeypatch.setattr(_we, "validation_target_for_rack",
                                lambda rack, override=None: {"a": "n1,n2", "b": "n2,n3"}.get(rack, ""))
            wf = {"id": "resm", "name": "resm", "steps": [
                {"id": "resolve", "type": "automated", "name": "resolve",
                 "resolve": {"racks": "a,b", "into": "nodelist"}},
            ]}
            we.save_workflow(wf)
            st, _ = self._run(we, "resm", {})
            assert st.get("outputs", {}).get("nodelist") == "n1,n2,n3"  # deduped, order-preserving

    def test_resolve_single_rack_preserves_exact_selected_nodes(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import workflow_engine as _we

            calls = []

            def resolve(rack, override=None):
                calls.append((rack, override))
                return override or "n1,n2,n3"

            monkeypatch.setattr(_we, "validation_target_for_rack", resolve)
            wf = {"id": "ress", "name": "ress", "steps": [
                {"id": "resolve", "type": "automated", "name": "resolve",
                 "resolve": {"racks": "${rack}", "into": "nodelist"}},
            ]}
            we.save_workflow(wf)
            st, _ = self._run(
                we,
                "ress",
                {"rack": "a", "target": "n1,n3"},
            )
            assert calls == [("a", "n1,n3")]
            assert st.get("outputs", {}).get("nodelist") == "n1,n3"

    def test_resolve_no_nodes_fails(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            we = self._engine(tmp)
            import workflow_engine as _we
            monkeypatch.setattr(_we, "validation_target_for_rack", lambda rack, override=None: "")
            wf = {"id": "resn", "name": "resn", "steps": [
                {"id": "resolve", "type": "automated", "name": "resolve", "resolve": {"racks": "${rack}"}},
            ]}
            we.save_workflow(wf)
            st, _ = self._run(we, "resn", {"rack": "bogus"})
            assert st["state"] == "failed"


class TestValidateFanout:
    def test_valid(self):
        fo = {"nodes": "${nodelist}", "hops": [{"kind": "ssh", "host": "h", "user": "u"}],
              "node": {"kind": "ssh", "user": "c", "sudo": True}}
        assert workflow_models.validate_fanout(fo) == []

    def test_bad_hop_kind(self):
        assert workflow_models.validate_fanout({"hops": [{"kind": "telnet", "host": "h"}]})

    def test_ssh_hop_needs_host(self):
        assert workflow_models.validate_fanout({"hops": [{"kind": "ssh"}]})

    def test_node_hop_host_optional(self):
        # per-node hop gets its host from `nodes`, so no host is required
        assert workflow_models.validate_fanout({"nodes": "x", "node": {"kind": "ssh", "user": "c"}}) == []

    def test_sudo_type(self):
        assert workflow_models.validate_fanout({"node": {"kind": "ssh", "sudo": "yes"}})


class TestBackwardCompat:
    """Existing + reference workflows validate and round-trip unchanged."""

    def _shipped_workflow(self, name):
        from pathlib import Path

        import yaml
        p = Path(__file__).resolve().parents[2] / "var" / "workflows" / name
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_gpu_health_check_validates(self):
        wf = self._shipped_workflow("gpu-health-check.yaml")
        assert workflow_models.validate_workflow(wf) == []

    def test_reference_nccl_workflow_validates(self):
        wf = self._shipped_workflow("nccl-allreduce.yaml")
        assert workflow_models.validate_workflow(wf) == []
        nccl = next(s for s in wf["steps"] if s["id"] == "nccl")
        assert nccl["scheduler"]["kind"] == "slurm"
        assert nccl["connection"]["profile"] == "control"

    def test_sysinfo_precheck_validates(self):
        wf = self._shipped_workflow("sysinfo-precheck.yaml")
        assert workflow_models.validate_workflow(wf) == []

    def test_classic_workflow_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RACK_WORKFLOWS_DIR"] = tmp
            import workflow_engine
            importlib.reload(workflow_engine)
            wf = self._shipped_workflow("gpu-health-check.yaml")
            workflow_engine.save_workflow(wf)
            loaded = workflow_engine.get_workflow(wf["id"])
            assert loaded and len(loaded["steps"]) == len(wf["steps"])
            # mermaid preserved
            assert (loaded.get("mermaid") or "") == (wf.get("mermaid") or "")
