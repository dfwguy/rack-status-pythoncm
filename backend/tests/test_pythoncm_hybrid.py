# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Nscale acceptance sentinels for PythonCM-primary, explicit Slurm fallback mode."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import app as app_module
import host_exec
import native_status
import pytest
import scheduler_runtime
import slurm_query
from schedulers import JobHandle, JobResult, JobSpec
from schedulers.factory import build_scheduler as build_scheduler_factory

UUID = "123e4567-e89b-42d3-a456-426614174000"


def test_nscale_primary_scheduler_typo_fails_closed(monkeypatch):
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    monkeypatch.setenv("RACK_SCHEDULER", "pytoncm")
    with pytest.raises(RuntimeError, match="requires RACK_SCHEDULER=pythoncm"):
        scheduler_runtime.scheduler_kind()


def test_nscale_primary_is_pythoncm_but_explicit_slurm_fallback_can_be_built():
    env = {
        "RACK_CLUSTER_ID": "nscale-tx",
        "RACK_CLUSTER_CUSTOMER_NAME": "Nscale",
        "RACK_SCHEDULER": "pythoncm",
    }
    scheduler = build_scheduler_factory("slurm", connection=object(), env=env)
    assert scheduler.kind == "slurm"


def test_snapshot_identity_is_namespaced_by_pythoncm_provider(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(app_module, "_CATALOG_AVAILABLE", False)
    assert app_module._cluster_snapshot_identity().endswith("/provider:pythoncm")


def test_failed_pythoncm_refresh_marks_cached_state_unknown(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(
        scheduler_runtime,
        "_INVENTORY_CACHE",
        (1.0, [{"node": "n1", "rack": "r1", "state": "idle", "reason": ""}]),
    )
    monkeypatch.setattr(scheduler_runtime.bcm_query, "nodes", lambda: {"ok": False})
    monkeypatch.setattr(scheduler_runtime.bcm_query, "node_states", lambda: {"ok": False})
    monkeypatch.setattr(
        scheduler_runtime,
        "_INVENTORY_SOURCE",
        (scheduler_runtime.bcm_query.nodes, scheduler_runtime.bcm_query.node_states),
    )
    assert scheduler_runtime.nodes(force=True) == [{
        "node": "n1",
        "rack": "r1",
        "state": "unknown",
        "reason": "PythonCM inventory refresh unavailable",
        "stale": "true",
    }]


def test_cold_and_warm_racks_sources_never_poll_slurm_or_cmsh(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")
    calls: list[bool] = []
    node_rows = [
        {"node": "ink50-dh01-d01-11", "rack": "ink50-dh01-d01", "state": "idle", "device_status": "UP"},
        {"node": "ink50-dh01-d01-12", "rack": "ink50-dh01-d01", "state": "drain", "device_status": "DOWN"},
    ]
    monkeypatch.setattr(
        scheduler_runtime,
        "rack_names",
        lambda *, force=False: calls.append(force) or ["ink50-dh01-d01"],
    )
    monkeypatch.setattr(scheduler_runtime, "nodes", lambda **_kw: list(node_rows))
    monkeypatch.setattr(native_status, "_cmsh_rack_list", lambda: pytest.fail("cmsh reached"))
    monkeypatch.setattr(native_status, "_cmsh_ds_text", lambda: pytest.fail("cmsh reached"))
    monkeypatch.setattr(host_exec, "run_slurm_on_host", lambda *_a, **_kw: pytest.fail("Slurm reached"))

    state = native_status.NativeRackTableState("audit", Path(tmp_path))
    state.refresh_racks_blocking()
    state.refresh_racks_blocking()
    state._worker_ds()
    counts = native_status.collect_sinfo_counts()

    assert calls == [True]
    assert state.racks == ["ink50-dh01-d01"]
    assert counts == {("ink50-dh01-d01", "idle"): 1, ("ink50-dh01-d01", "drain"): 1}


def test_status_bootstrap_does_not_start_cmsh_warmer(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(app_module, "_rack_status_background_enabled", lambda: True)
    monkeypatch.setattr(app_module, "_rack_status_bg_thread", None)
    monkeypatch.setattr(app_module, "_rack_status_cache_key", lambda _path: "audit")
    monkeypatch.setattr(app_module, "_default_log_dir", lambda: Path("/logs"))

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.name = kwargs.get("name")

        def start(self):
            return None

        def is_alive(self):
            return False

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(app_module, "_start_cmsh_rack_list_warm_thread", lambda: pytest.fail("cmsh warmer reached"))
    app_module._ensure_rack_status_background_thread()


def test_active_overlay_includes_fallback_registry_without_polling_slurm(monkeypatch):
    activities = [
        {"scheduler": "slurm", "nodes": ["n1"], "tasks": [{"scheduler_ref": "42", "scheduler_state": "RUNNING"}]},
        {"scheduler": "pythoncm", "nodes": ["n2"], "tasks": [{"scheduler_ref": UUID, "scheduler_state": "RUNNING"}]},
    ]
    monkeypatch.setattr(scheduler_runtime, "_activity_rows", lambda _limit=500: activities)
    monkeypatch.setattr(host_exec, "run_slurm_on_host", lambda *_a, **_kw: pytest.fail("Slurm polled"))
    refs, _times = scheduler_runtime.active_job_times()
    assert refs == {"42", UUID}


def test_registry_keeps_provider_provenance(monkeypatch):
    activities = [
        {"scheduler": "slurm", "nodes": ["n1"], "tasks": [{"scheduler_ref": "42", "scheduler_state": "RUNNING"}]},
        {"scheduler": "pythoncm", "nodes": ["n2"], "tasks": [{"scheduler_ref": UUID, "scheduler_state": "RUNNING"}]},
        {"scheduler": "pythoncm", "nodes": ["bad"], "tasks": [{"scheduler_ref": "99", "scheduler_state": "RUNNING"}]},
    ]
    monkeypatch.setattr(scheduler_runtime, "_activity_rows", lambda _limit=500: activities)
    rows = scheduler_runtime.jobs(active_only=True)
    assert [(row["job_id"], row["scheduler"]) for row in rows] == [("42", "slurm"), (UUID, "pythoncm")]


def test_numeric_status_and_cancel_use_targeted_slurm_calls(monkeypatch):
    commands: list[object] = []

    class Proc:
        returncode = 0
        stdout = "JobId=42 JobState=RUNNING StdOut=/logs/42.out\n"
        stderr = ""

    monkeypatch.setattr(host_exec, "run_slurm_on_host", lambda command, **_kw: commands.append(command) or Proc())
    assert scheduler_runtime.live_status("42") == ("RUNNING", "/logs/42.out")
    scheduler_runtime.cancel("42")
    assert len(commands) == 2
    assert "show job" in str(commands[0])
    assert "scancel" in str(commands[1])


def test_uuid_cancel_uses_pythoncm_not_slurm(monkeypatch):
    cancelled: list[str] = []

    class Scheduler:
        def cancel(self, handle):
            cancelled.append(handle.job_id)

    monkeypatch.setattr("schedulers.build_scheduler", lambda *_a, **_kw: Scheduler())
    monkeypatch.setattr(host_exec, "connection", lambda _profile: object())
    monkeypatch.setattr(host_exec, "run_slurm_on_host", lambda *_a, **_kw: pytest.fail("Slurm reached"))
    scheduler_runtime.cancel(UUID)
    assert cancelled == [UUID]


def test_rf0_power_drain_resume_use_pythoncm(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setenv("RACK_POWER_RESET_SLEEP_SEC", "0")
    monkeypatch.setattr(app_module, "_physical_rf0_loaded", False)
    monkeypatch.setattr(app_module, "_physical_rf0_map", {})
    monkeypatch.setattr(app_module.bcm_query, "rf0", lambda **_kw: {"ok": True, "data": [{"hostname": "N1", "ip": "10.0.0.1"}]})
    power_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module.bcm_query, "power", lambda target, action, **_kw: power_calls.append((target, action)) or {"ok": True, "data": []})
    monkeypatch.setattr(host_exec, "run_on_host", lambda *_a, **_kw: pytest.fail("legacy command reached"))
    changes: list[tuple[str, str]] = []
    monkeypatch.setattr(app_module, "_slurm_nodename_target_from_body", lambda _body: ("n1", None))
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda value: value)
    monkeypatch.setattr(app_module.bcm_query, "drain", lambda target, reason: changes.append(("drain", target)) or {"ok": True})
    monkeypatch.setattr(app_module.bcm_query, "node_states", lambda _target: {"ok": True, "data": [{"hostname": "n1", "scheduler_state": "drain"}]})
    monkeypatch.setattr(app_module.bcm_query, "undrain", lambda target: changes.append(("resume", target)) or {"ok": True})
    assert app_module.get_physical_rf0_map() == {"n1": "10.0.0.1"}
    app_module.run_cmsh_power_reset(["ink50-dh01-d01-[11-12]"])
    assert power_calls == [("ink50-dh01-d01-[11-12]", "off"), ("ink50-dh01-d01-[11-12]", "on")]
    client = app_module.app.test_client()
    assert client.post("/api/scheduler-drain", json={"rack": "ink50-dh01-d01"}).status_code == 200
    assert client.post("/api/scheduler-resume", json={"rack": "ink50-dh01-d01"}).status_code == 200
    assert changes == [("drain", "n1"), ("resume", "n1")]


def test_gpu_services_bugreport_hw_and_nvlink_use_programrunner(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    calls = []

    class ReadyPythonCM:
        @staticmethod
        def probe(*, timeout):
            return {"ok": True, "ready": True}

    def fanout(target, command, **kwargs):
        calls.append((target, command, kwargs))
        return {"returncode": 0, "stdout": "n1: [PASS]", "stderr": "", "pdsh_w": "n1", "scheduler_ref": UUID}

    monkeypatch.setattr(app_module, "_run_pythoncm_fanout", fanout)
    monkeypatch.setattr(app_module, "build_scheduler", lambda *_a, **_kw: ReadyPythonCM())
    monkeypatch.setattr(app_module.host_exec, "connection", lambda *_a, **_kw: object())
    monkeypatch.setattr(app_module, "_checker_script_path", lambda: tmp_path / "checker.sh")
    monkeypatch.setattr(app_module, "_nvlink_preflight_script_path", lambda: tmp_path / "nvlink.sh")
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module.pythoncm_validation, "resolve_nodes", lambda **_kw: ["n1"])
    monkeypatch.setattr(app_module.nvlink_preflight, "parse_nvlink_preflight_output", lambda *_a: [])
    monkeypatch.setattr(app_module.nvlink_preflight, "summarize", lambda _rows: {"ready": True})
    app_module.run_gpu_reset("n1")
    app_module.run_services_restart("n1")
    app_module.run_bug_report_pdsh("n1")
    app_module.run_hw_check("n1", ["-g", "1"])
    app_module.run_nvlink_preflight_pdsh("n1", quick=True)
    assert [row[2]["name"] for row in calls] == [
        "gpu-reset", "services-restart", "nvidia-bug-report", "hardware-check", "nvlink-preflight"
    ]


def test_nvlink_preflight_falls_back_to_exact_node_slurm_before_submit(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_SLURM_SUBMIT_UID", "1003")
    monkeypatch.setenv("RACK_SLURM_SUBMIT_GID", "1003")
    monkeypatch.setenv("RACK_SLURM_SUBMIT_HOME", "/home/cmods")
    monkeypatch.setenv("RACK_STATUS_LOG_DIR", "/cluster/OVA/logs/nscaletx-16k-validation")
    submitted: list[JobSpec] = []

    class UnreadyPythonCM:
        @staticmethod
        def probe(*, timeout):
            raise RuntimeError("No valid cookie provided, did your session end?")

    class Slurm:
        @staticmethod
        def submit(spec):
            submitted.append(spec)
            return JobHandle("47801", "slurm")

        @staticmethod
        def wait(handle, *, timeout, poll_interval):
            return JobResult(
                0,
                "COMPLETED",
                stdout=(
                    "n1: ### nvlink_preflight n1 (quick=1) ###\n"
                    "n1: [PASS] gpus_enumerate 4/4\n"
                    "n1: RESULT n1: PASS (NVLS-ready)\n"
                    "n2: ### nvlink_preflight n2 (quick=1) ###\n"
                    "n2: [PASS] gpus_enumerate 4/4\n"
                    "n2: RESULT n2: PASS (NVLS-ready)\n"
                ),
                job_id=handle.job_id,
            )

        @staticmethod
        def cancel(_handle):
            pytest.fail("completed fallback must not be cancelled")

    class Platform:
        @staticmethod
        def srun_cmd(args):
            return f"/cm/local/apps/slurm/current/bin/srun {args}"

    def scheduler(kind, **_kwargs):
        return UnreadyPythonCM() if kind == "pythoncm" else Slurm()

    activity_calls = []
    host_calls = []
    monkeypatch.setattr(app_module, "build_scheduler", scheduler)
    monkeypatch.setattr(app_module, "_get_platform", lambda: Platform())
    monkeypatch.setattr(app_module.host_exec, "connection", lambda *_a, **_kw: object())
    monkeypatch.setattr(
        app_module.host_exec,
        "run_on_host",
        lambda *args, **kwargs: host_calls.append((args, kwargs))
        or subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    monkeypatch.setattr(app_module, "_nvlink_preflight_script_path", lambda: tmp_path / "nvlink.sh")
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(
        app_module.pythoncm_validation,
        "resolve_nodes",
        lambda **_kw: ["n1", "n2"],
    )
    monkeypatch.setattr(
        app_module.validation_activities,
        "record_activity",
        lambda *args, **kwargs: activity_calls.append((args, kwargs)) or "activity-1",
    )
    monkeypatch.setattr(app_module.validation_activities, "add_or_update_task", lambda *_a, **_kw: None)
    monkeypatch.setattr(app_module.validation_activities, "update_activity", lambda *_a, **_kw: None)

    result = app_module.run_nvlink_preflight_pdsh("n1,n2", quick=True)

    assert result["summary"]["ready"] is True
    assert result["scheduler"] == "slurm"
    assert result["execution_provider"] == "slurm"
    assert result["fallback"] is True
    assert "No valid cookie" in result["fallback_reason"]
    assert result["scheduler_ref"] == "47801"
    spec = submitted[0]
    assert spec.nodelist == "n1,n2"
    assert spec.nodes == 2 and spec.ntasks_per_node == 1
    assert "--uid=1003" in spec.extra_args and "--gid=1003" in spec.extra_args
    assert "--gpus-per-node=4" in spec.extra_args
    assert "--exclusive" in spec.extra_args
    assert "hostname -s" in (spec.command or "")
    assert "${node}: " in (spec.command or "")
    assert host_calls == [
        ((["chmod", "g+rw", "/cluster/OVA/logs/nscaletx-16k-validation/nvlink-preflight-slurm-47801.out"],), {"timeout": 30, "role": "logs"})
    ]
    assert activity_calls[0][1]["fallback"] is True
    assert activity_calls[0][1]["execution_provider"] == "slurm"


def test_nvlink_preflight_never_replays_a_failed_pythoncm_submit(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")

    class ReadyPythonCM:
        @staticmethod
        def probe(*, timeout):
            return {"ok": True, "ready": True}

    monkeypatch.setattr(app_module, "build_scheduler", lambda *_a, **_kw: ReadyPythonCM())
    monkeypatch.setattr(app_module.host_exec, "connection", lambda *_a, **_kw: object())
    monkeypatch.setattr(app_module, "_nvlink_preflight_script_path", lambda: tmp_path / "nvlink.sh")
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module.pythoncm_validation, "resolve_nodes", lambda **_kw: ["n1"])
    monkeypatch.setattr(
        app_module,
        "_run_pythoncm_fanout",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("ambiguous submit response")),
    )
    monkeypatch.setattr(
        app_module,
        "_run_nvlink_preflight_slurm_fallback",
        lambda *_a, **_kw: pytest.fail("a failed submit must never be replayed through Slurm"),
    )

    with pytest.raises(RuntimeError, match="ambiguous submit response"):
        app_module.run_nvlink_preflight_pdsh("n1", quick=True)


def test_neutral_and_legacy_scheduler_urls_are_compatible(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    rows = [{"job_id": UUID, "name": "audit", "state": "RUNNING", "elapsed": "00:00:03", "nodes": ["n1"], "nodelist": "n1", "start": "", "end": "", "exit_code": "", "scheduler": "pythoncm"}]
    monkeypatch.setattr(scheduler_runtime, "jobs", lambda **_kw: list(rows))
    monkeypatch.setattr(scheduler_runtime, "nodes", lambda **_kw: [{"node": "n1", "state": "idle", "reason": ""}])
    monkeypatch.setattr(slurm_query, "_get_platform", lambda: pytest.fail("legacy provider reached"))
    client = app_module.app.test_client()
    assert client.get("/api/scheduler-jobs").status_code == 200
    assert client.get("/api/slurm-squeue").status_code == 200
    assert client.get("/api/scheduler-history?state=ALL").status_code == 200
    assert client.get("/api/slurm-sacct-recent?state=ALL").status_code == 200


def test_workflows_and_validation_presets_are_not_blanket_disabled(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    client = app_module.app.test_client()
    assert client.get("/api/workflows").status_code == 200
    assert client.get("/api/workflows").get_json()["workflows"]
    presets = client.get("/api/validation-presets")
    assert presets.status_code == 200
    assert presets.get_json()["presets"]


def test_mnubergemm_stage_only_prefers_pythoncm(monkeypatch):
    recorded = []

    class Thread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target") or args[0]

        def start(self):
            return None

    monkeypatch.setattr(app_module, "_record_validation_activity", lambda *args, **kwargs: recorded.append((args, kwargs)) or "activity")
    monkeypatch.setattr(app_module.threading, "Thread", Thread)
    plan = {
        "preset_id": "mnubergemm-26.03-v1.17",
        "scope": "rack",
        "racks": ["ink50-dh01-d01"],
        "nodes": ["n1"],
        "node_count": 1,
        "log_dir": "/logs",
        "command_display": "stage image",
        "timeout": 60,
        "stage_only": True,
    }
    with app_module.app.test_request_context("/"):
        response = app_module._validation_preset_fallback("mnubergemm-26.03-v1.17", plan)
    assert response.get_json()["execution_provider"] == "pythoncm"
    assert response.get_json()["fallback"] is False
    assert recorded[0][1]["scheduler"] == "pythoncm"


@pytest.mark.parametrize(
    "kind",
    (
        "nccl-fullrack", "llmb-nccl", "nccl-2.31.2", "clusterkit", "nvloom",
        "mnubergemm", "validation", "nemo-xrack", "llama", "llama-exemplar",
        "gpt-oss-cpu", "exemplar-system-info", "sysinfo",
    ),
)
def test_validation_gap_has_documented_slurm_fallback_reason(kind):
    import pythoncm_validation

    reason = pythoncm_validation.unsupported_reason(kind)
    assert reason is not None and len(reason) > 30


def test_pythoncm_worker_persists_uuid_and_terminal_state(monkeypatch):
    task_updates = []

    class Scheduler:
        def submit(self, _spec):
            return JobHandle(UUID, "pythoncm")

        def wait(self, handle, **_kwargs):
            return JobResult(0, "COMPLETED", "passed", "", handle.job_id)

        def cancel(self, _handle):
            pytest.fail("completed command cancelled")

    monkeypatch.setattr(app_module, "build_scheduler", lambda *_a, **_kw: Scheduler())
    monkeypatch.setattr(app_module.host_exec, "connection", lambda _profile: object())
    monkeypatch.setattr(app_module.validation_activities, "add_or_update_task", lambda activity_id, **fields: task_updates.append((activity_id, fields)))
    monkeypatch.setattr(app_module.validation_activities, "update_activity", lambda *_a, **_kw: None)
    registry = {"activity": {"phase": "starting"}}
    spec = JobSpec(name="check", nodes=1, nodelist="n1", command="hostname", output="/logs/result-%j.txt", launch_mode="leader", timeout=60)
    app_module._pythoncm_validation_worker("activity", spec, registry=registry, registry_lock=app_module.threading.Lock(), purge=lambda _job_id: None)
    assert registry["activity"]["scheduler_ref"] == UUID
    assert task_updates[-1][1]["scheduler_state"] == "COMPLETED"


def test_log_probe_executes_no_cluster_command(tmp_path):
    script = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "rack_status.sh"
    marker = tmp_path / "cluster-command-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("cmsh", "sinfo", "squeue", "scontrol", "sacct", "sbatch", "srun", "scancel", "pdsh"):
        shim = fake_bin / name
        shim.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> {marker}\nexit 99\n", encoding="utf-8")
        shim.chmod(0o755)
    log = tmp_path / "root-NCCL-1Rack-ink50-dh01-d01-1N-123.txt"
    log.write_text("NCCL output\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    proc = subprocess.run(["bash", str(script), "--no-squeue", "--no-cmsh", "--no-sinfo", "--probe-json", str(log)], capture_output=True, text=True, timeout=30, env=env, check=False)
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
