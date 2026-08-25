# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Nscale acceptance sentinels: PythonCM mode must never fall through to Slurm."""
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

FORBIDDEN = (
    "cmsh -c 'rack; list'",
    "sinfo -N",
    "squeue -h",
    "scontrol show job 1",
    "sacct -j 1",
    "sbatch job.sh",
    "srun hostname",
    "scancel 1",
    "pdsh -w n1 uptime",
    "module load slurm",
    "source slurm-init.inc.sh",
    "bash run-validation.sh rack-01",
    "bash run_nemo_xrack.sh rack-01",
    "bash gpu_reset.sh rack-01",
    "bash nccl_allreduce.sbatch",
    "bash sysinfo.sh rack-01",
    "bash nvswitches_rack_audit.sh rack-01",
    "bash install-cuda-nccl-workspace.sh /workspace",
)


@pytest.mark.parametrize("command", FORBIDDEN)
def test_transport_rejects_every_legacy_command_before_connection(monkeypatch, command):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(
        host_exec,
        "_conn",
        lambda: (_ for _ in ()).throw(AssertionError("transport was reached")),
    )
    with pytest.raises(RuntimeError, match="forbidden"):
        host_exec.run_on_host(command)


@pytest.mark.parametrize("kind", ("slurm", "local", "runai", "slinky", "maas"))
def test_nscale_factory_cannot_construct_any_non_pythoncm_scheduler(kind):
    env = {
        "RACK_CLUSTER_ID": "nscale-tx",
        "RACK_CLUSTER_CUSTOMER_NAME": "Nscale",
        "RACK_SCHEDULER": "pythoncm",
    }
    with pytest.raises(ValueError, match="requires the pythoncm scheduler"):
        build_scheduler_factory(kind, connection=object(), env=env)


def test_nscale_scheduler_typo_fails_closed(monkeypatch):
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    monkeypatch.setenv("RACK_SCHEDULER", "pytoncm")
    with pytest.raises(RuntimeError, match="requires RACK_SCHEDULER=pythoncm"):
        scheduler_runtime.scheduler_kind()


def test_snapshot_identity_is_namespaced_by_pythoncm_provider(monkeypatch):
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(app_module, "_CATALOG_AVAILABLE", False)
    assert app_module._cluster_snapshot_identity().endswith("/provider:pythoncm")


def test_cold_and_warm_racks_sources_are_pythoncm_only(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")
    racks_calls = []
    node_rows = [
        {"node": "ink50-dh01-d01-11", "rack": "ink50-dh01-d01", "state": "idle", "device_status": "UP"},
        {"node": "ink50-dh01-d01-12", "rack": "ink50-dh01-d01", "state": "drain", "device_status": "DOWN"},
    ]

    def rack_names(*, force=False):
        racks_calls.append(force)
        return ["ink50-dh01-d01"]

    monkeypatch.setattr(scheduler_runtime, "rack_names", rack_names)
    monkeypatch.setattr(scheduler_runtime, "nodes", lambda **_kw: list(node_rows))
    monkeypatch.setattr(
        native_status,
        "_cmsh_rack_list",
        lambda: (_ for _ in ()).throw(AssertionError("cmsh rack list reached")),
    )
    monkeypatch.setattr(
        native_status,
        "_cmsh_ds_text",
        lambda: (_ for _ in ()).throw(AssertionError("cmsh ds reached")),
    )
    monkeypatch.setattr(
        host_exec,
        "run_slurm_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Slurm runner reached")),
    )

    state = native_status.NativeRackTableState("audit", Path(tmp_path))
    state.refresh_racks_blocking()
    state.refresh_racks_blocking()
    state._worker_ds()
    counts = native_status.collect_sinfo_counts()

    assert racks_calls == [True]
    assert state.racks == ["ink50-dh01-d01"]
    assert state.ds_done is True
    assert state.ct_by_rack["ink50-dh01-d01"]
    assert counts == {("ink50-dh01-d01", "idle"): 1, ("ink50-dh01-d01", "drain"): 1}


def test_failed_pythoncm_refresh_never_replays_cached_healthy_state(monkeypatch):
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
        (id(scheduler_runtime.bcm_query.nodes), id(scheduler_runtime.bcm_query.node_states)),
    )

    rows = scheduler_runtime.nodes(force=True)

    assert rows == [{
        "node": "n1",
        "rack": "r1",
        "state": "unknown",
        "reason": "PythonCM inventory refresh unavailable",
        "stale": "true",
    }]


def test_status_rf0_uses_pythoncm_not_platform_command(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(app_module, "_physical_rf0_loaded", False)
    monkeypatch.setattr(app_module, "_physical_rf0_map", {})
    monkeypatch.setattr(
        app_module.bcm_query,
        "rf0",
        lambda **_kw: {"ok": True, "data": [{"hostname": "N1", "ip": "10.0.0.1"}]},
    )
    monkeypatch.setattr(
        app_module,
        "_get_platform",
        lambda: (_ for _ in ()).throw(AssertionError("legacy platform reached")),
    )
    assert app_module.get_physical_rf0_map() == {"n1": "10.0.0.1"}


def test_power_cycle_uses_pythoncm_only(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_POWER_RESET_SLEEP_SEC", "0")
    calls = []
    monkeypatch.setattr(
        app_module.bcm_query,
        "power",
        lambda target, action, **_kw: calls.append((target, action)) or {"ok": True, "data": []},
    )
    monkeypatch.setattr(
        host_exec,
        "run_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("legacy host command reached")),
    )
    rows = app_module.run_cmsh_power_reset(["ink50-dh01-d01-[11-12]"])
    assert calls == [
        ("ink50-dh01-d01-[11-12]", "off"),
        ("ink50-dh01-d01-[11-12]", "on"),
    ]
    assert all(row["returncode"] == 0 for row in rows)


def test_gpu_services_bugreport_hw_and_nvlink_use_programrunner(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    calls = []

    def fanout(target, command, **kwargs):
        calls.append((target, command, kwargs))
        return {
            "returncode": 0,
            "stdout": "n1: [PASS] gpu",
            "stderr": "",
            "pdsh_w": "n1",
            "scheduler_ref": "123e4567-e89b-42d3-a456-426614174000",
        }

    monkeypatch.setattr(app_module, "_run_pythoncm_fanout", fanout)
    monkeypatch.setattr(app_module, "_checker_script_path", lambda: tmp_path / "checker.sh")
    monkeypatch.setattr(app_module, "_nvlink_preflight_script_path", lambda: tmp_path / "nvlink.sh")
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(
        app_module.pythoncm_validation, "resolve_nodes", lambda **_kw: ["n1"]
    )
    monkeypatch.setattr(app_module.nvlink_preflight, "parse_nvlink_preflight_output", lambda *_a: [])
    monkeypatch.setattr(app_module.nvlink_preflight, "summarize", lambda _rows: {"ready": True})

    app_module.run_gpu_reset("n1")
    app_module.run_services_restart("n1")
    app_module.run_bug_report_pdsh("n1")
    app_module.run_bug_report_list_pdsh("n1")
    app_module.run_hw_check("n1", ["-g", "1"])
    app_module.run_nvlink_preflight_pdsh("n1", quick=True)

    assert [row[2]["name"] for row in calls] == [
        "gpu-reset",
        "services-restart",
        "nvidia-bug-report",
        "nvidia-bug-report-list",
        "hardware-check",
        "nvlink-preflight",
    ]
    forbidden = ("pdsh", "cmsh", "srun", "sbatch", "slurmd", "module load slurm")
    assert not any(token in command.lower() for _, command, _ in calls for token in forbidden)


def test_hw_check_rejects_workload_manager_preset(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    with pytest.raises(ValueError, match="forbidden"):
        app_module.run_hw_check("n1", ["-S", "1"])


@pytest.mark.parametrize(
    "path",
    (
        "/api/bmc-recovery-async",
        "/api/mac-change-async",
        "/api/nvswitch-fix-async",
        "/api/nvswitch-sdn-reset-async",
        "/api/nvswitch-system-reset-async",
        "/api/imex-reset-async",
        "/api/nvlmapper-async",
        "/api/precheck-async",
        "/api/nvswitch-check-async",
        "/api/imex-check-async",
        "/api/run-nvswitch-audit-stream",
        "/api/bug-report-async",
    ),
)
def test_unconverted_hardware_routes_fail_before_worker(monkeypatch, path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(
        app_module.threading,
        "Thread",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("worker started")),
    )
    response = app_module.app.test_client().post(path, json={})
    payload = response.get_json()
    assert response.status_code == 409
    assert payload["scheduler"] == "pythoncm"
    assert payload["blocked"] is True


def test_legacy_named_job_endpoints_are_not_exposed(monkeypatch):
    ref = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    client = app_module.app.test_client()
    status_response = client.get(f"/api/slurm-scontrol-show-job?job_id={ref}")
    log_response = client.get(f"/api/slurm-job-log-path?job_id={ref}")
    assert status_response.status_code == 404
    assert log_response.status_code == 404
    assert status_response.get_json()["scheduler"] == "pythoncm"
    assert log_response.get_json()["scheduler"] == "pythoncm"


def test_every_legacy_slurm_route_is_hidden_in_pythoncm(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    client = app_module.app.test_client()
    checked = []
    for rule in app_module.app.url_map.iter_rules():
        if not str(rule.rule).startswith("/api/slurm-"):
            continue
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            response = client.open(str(rule.rule), method=method, json={} if method != "GET" else None)
            assert response.status_code == 404, f"{method} {rule.rule} remained reachable"
            assert response.get_json()["scheduler"] == "pythoncm"
            checked.append((method, str(rule.rule)))
    assert checked


def test_queue_history_and_reason_endpoints_use_pythoncm_registry(monkeypatch):
    ref = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    rows = [{
        "job_id": ref, "name": "audit", "state": "COMPLETED", "elapsed": "00:00:03",
        "nodes": ["n1"], "nodelist": "n1", "start": "", "end": "", "exit_code": "0",
    }]
    monkeypatch.setattr(scheduler_runtime, "jobs", lambda **_kw: list(rows))
    monkeypatch.setattr(
        scheduler_runtime,
        "nodes",
        lambda **_kw: [{"node": "n1", "state": "drain", "reason": "maintenance"}],
    )
    monkeypatch.setattr(
        slurm_query,
        "_get_platform",
        lambda: (_ for _ in ()).throw(AssertionError("legacy platform reached")),
    )
    client = app_module.app.test_client()
    assert client.get("/api/slurm-squeue").status_code == 404
    assert client.get("/api/slurm-sacct-recent?state=ALL").status_code == 404
    assert client.get("/api/slurm-sinfo-r").status_code == 404
    scheduler_active = client.get("/api/scheduler-jobs").get_json()
    scheduler_recent = client.get("/api/scheduler-history?state=ALL").get_json()
    scheduler_reasons = client.get("/api/scheduler-node-reasons").get_json()
    assert scheduler_active["scheduler"] == "pythoncm"
    assert scheduler_recent["scheduler"] == "pythoncm"
    assert scheduler_recent["jobs"][0]["job_id"] == ref
    assert scheduler_reasons["scheduler"] == "pythoncm"
    assert scheduler_reasons["nodes"][0]["node"] == "n1"


def test_registry_ignores_legacy_and_unprovenanced_tasks(monkeypatch):
    ref = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    activities = [
        {"scheduler": "slurm", "nodes": ["legacy-node"], "tasks": [{"scheduler_ref": "42", "scheduler_state": "RUNNING"}]},
        {"nodes": ["unknown-node"], "tasks": [{"scheduler_ref": ref, "scheduler_state": "RUNNING"}]},
        {"scheduler": "pythoncm", "nodes": ["n1"], "tasks": [{"scheduler_ref": ref, "scheduler_state": "RUNNING"}]},
        {"scheduler": "pythoncm", "nodes": ["bad-node"], "tasks": [{"scheduler_ref": "99", "scheduler_state": "RUNNING"}]},
    ]
    monkeypatch.setattr(scheduler_runtime, "_activity_rows", lambda _limit=500: activities)
    rows = scheduler_runtime.jobs(active_only=True)
    assert [row["job_id"] for row in rows] == [ref]
    assert rows[0]["nodes"] == ["n1"]


def test_cancel_drain_and_resume_use_pythoncm_only(monkeypatch):
    ref = "123e4567-e89b-42d3-a456-426614174000"
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    cancelled = []
    changes = []
    monkeypatch.setattr(scheduler_runtime, "cancel", lambda value: cancelled.append(value))
    monkeypatch.setattr(
        app_module,
        "_slurm_nodename_target_from_body",
        lambda _body: ("n1", None),
    )
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda value: value)
    monkeypatch.setattr(
        app_module.bcm_query,
        "drain",
        lambda target, reason: changes.append(("drain", target, reason)) or {"ok": True},
    )
    monkeypatch.setattr(
        app_module.bcm_query,
        "node_states",
        lambda _target: {"ok": True, "data": [{"hostname": "n1", "scheduler_state": "drain"}]},
    )
    monkeypatch.setattr(
        app_module.bcm_query,
        "undrain",
        lambda target: changes.append(("resume", target, "")) or {"ok": True},
    )
    monkeypatch.setattr(
        host_exec,
        "run_slurm_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Slurm runner reached")),
    )
    client = app_module.app.test_client()
    assert client.post("/api/slurm-scancel", json={"job_id": ref}).status_code == 404
    assert client.post("/api/slurm-drain", json={"rack": "ink50-dh01-d01"}).status_code == 404
    assert client.post("/api/slurm-undrain", json={"rack": "ink50-dh01-d01"}).status_code == 404
    assert client.post("/api/scheduler-cancel", json={"job_id": ref}).status_code == 200
    assert client.post("/api/scheduler-drain", json={"rack": "ink50-dh01-d01"}).status_code == 200
    assert client.post("/api/scheduler-resume", json={"rack": "ink50-dh01-d01"}).status_code == 200
    assert cancelled == [ref]
    assert [row[0] for row in changes] == ["drain", "resume"]


def test_fabric_probe_runs_on_node_through_pythoncm(monkeypatch):
    from api import fabric_probe

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(scheduler_runtime, "node_names", lambda: ["n1"])
    specs = []

    class FakeScheduler:
        def run(self, spec):
            specs.append(spec)
            return JobResult(0, "COMPLETED", '{"host":"n1","compute_rails":4}', "", "ref")

    monkeypatch.setattr(fabric_probe, "build_scheduler", lambda *_a, **_kw: FakeScheduler())
    monkeypatch.setattr(host_exec, "connection", lambda _profile: object())
    monkeypatch.setattr(
        host_exec,
        "run_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("direct host runner reached")),
    )
    response = app_module.app.test_client().get("/api/fabric-probe")
    assert response.status_code == 200
    assert specs[0].launch_mode == "fanout" and specs[0].nodelist == "n1"
    assert specs[0].command.endswith("fabric_probe.sh")


def test_bug_report_bundle_is_blocked_before_direct_ssh(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(
        app_module,
        "_ssh_cat_remote_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("direct SSH reached")),
    )
    response = app_module.app.test_client().get("/api/bug-report-bundle?job_id=anything")
    assert response.status_code == 409
    assert response.get_json()["blocked"] is True


@pytest.mark.parametrize(
    "path",
    (
        "/api/tools/download-containers-stream",
        "/api/tools/install-hpcx-workspace",
        "/api/tools/install-dcgm-workspace-stream",
        "/api/tools/install-cuda-nccl-workspace",
        "/api/tools/install-nvloom-workspace-stream",
        "/api/tools/install-dgxc-benchmarking-workspace",
        "/api/tools/launch-grafana-bright-prometheus-stream",
    ),
)
def test_tools_mutations_are_blocked_before_any_local_or_remote_process(monkeypatch, path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("local process started")),
    )
    response = app_module.app.test_client().post(path, json={})
    assert response.status_code == 409
    assert response.get_json()["blocked"] is True


def test_tools_status_is_blocked_before_head_filesystem_probe(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(
        app_module.host_exec,
        "stat_dir",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("head filesystem probed")),
    )
    response = app_module.app.test_client().get(
        "/api/tools/workspace-toolchain-status?workspace=/tmp"
    )
    assert response.status_code == 409
    assert response.get_json()["blocked"] is True


def test_streaming_helper_applies_pythoncm_command_guard(monkeypatch, tmp_path):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    gen = app_module._merged_stdout_stream_generator(
        ["bash", "sysinfo.sh", "n1"], str(tmp_path), "unsafe", 30,
        linebuffer_env_var="TEST_LINEBUFFER", linebuffer_default="0",
    )
    with pytest.raises(RuntimeError, match="forbidden"):
        next(gen)


@pytest.mark.parametrize(
    ("path", "kind"),
    (
        ("/api/run-validation", "validation"),
        ("/api/run-validation-async", "validation"),
        ("/api/run-nemo-xrack-async", "nemo-xrack"),
        ("/api/run-llama-async", "llama"),
        ("/api/run-exemplar-llama-async", "llama-exemplar"),
        ("/api/run-sysinfo", "sysinfo"),
        ("/api/run-sysinfo-stream", "sysinfo"),
        ("/api/run-nccl-fullrack-async", "nccl-fullrack"),
        ("/api/run-llmb-nccl-async", "llmb-nccl"),
        ("/api/run-nvloom-async", "nvloom"),
        ("/api/run-clusterkit-async", "clusterkit"),
        ("/api/run-mnubergemm-stress-async", "mnubergemm"),
    ),
)
def test_unconverted_validation_routes_fail_before_any_host_launch(monkeypatch, path, kind):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(
        host_exec,
        "run_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("host launch reached")),
    )
    monkeypatch.setattr(
        host_exec,
        "popen_on_host",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("host launch reached")),
    )
    response = app_module.app.test_client().post(path, json={})
    payload = response.get_json()
    assert response.status_code == 501
    assert payload["scheduler"] == "pythoncm"
    assert payload["blocked"] is True
    assert kind in payload["error"]


def test_workflow_explicit_slurm_scheduler_is_rejected(monkeypatch):
    import workflow_engine

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    run = {"run_id": "audit-run", "params": {"target": "n1"}, "outputs": {}}
    step = {
        "id": "unsafe",
        "scheduler": {
            "kind": "slurm",
            "command": "hostname",
            "nodelist": "${target}",
        },
    }
    with pytest.raises(ValueError, match="forbidden"):
        workflow_engine._execute_scheduler_step(run, step)


def test_workflow_fanout_and_inline_use_pythoncm(monkeypatch):
    import workflow_engine

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(scheduler_runtime, "node_names", lambda: ["n1", "n2"])
    monkeypatch.setattr(workflow_engine, "_save_step_output", lambda *_a, **_kw: None)
    specs = []

    class FakeScheduler:
        kind = "pythoncm"

        def run(self, spec):
            specs.append(spec)
            nodes = (spec.nodelist or "").split(",")
            stdout = "".join(f"===== {node} =====\nOK\n" for node in nodes[:1] if node)
            return JobResult(
                0,
                "COMPLETED",
                stdout,
                "",
                "123e4567-e89b-42d3-a456-426614174000",
                node_results=[{"node": node, "returncode": 0} for node in nodes],
            )

    monkeypatch.setattr("schedulers.build_scheduler", lambda *_a, **_kw: FakeScheduler())
    monkeypatch.setattr(host_exec, "connection", lambda _profile: object())
    run = {"run_id": "audit", "params": {"nodelist": "n1,n2"}, "outputs": {}}
    fanout_step = {
        "id": "fanout",
        "run": "hostname",
        "fanout": {"nodes": "${nodelist}", "scheduler": "pythoncm"},
    }
    inline_step = {"id": "head", "run": "hostname", "timeout": 30}
    assert workflow_engine._execute_fanout_step(run, fanout_step)[0] is True
    assert workflow_engine._execute_inline_run(run, inline_step, run["params"], "hostname")[0] is True
    assert specs[0].launch_mode == "fanout"
    assert specs[0].nodelist == "n1,n2"
    assert specs[1].launch_mode == "head"


def test_workflow_inline_rejects_encoded_legacy_command(monkeypatch):
    import workflow_engine

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(scheduler_runtime, "node_names", lambda: ["n1"])
    monkeypatch.setattr(workflow_engine, "_save_step_output", lambda *_a, **_kw: None)
    ok, output = workflow_engine._execute_inline_run(
        {"run_id": "audit", "params": {}, "outputs": {}},
        {"id": "unsafe"},
        {},
        "srun hostname",
    )
    assert ok is False
    assert "forbidden" in output


@pytest.mark.parametrize(
    "kind",
    (
        "nccl-fullrack", "llmb-nccl", "nccl-2.31.2", "clusterkit", "nvloom",
        "mnubergemm", "validation", "nemo-xrack", "llama", "llama-exemplar",
        "gpt-oss-cpu", "exemplar-system-info", "sysinfo",
    ),
)
def test_every_validation_kind_fails_closed_until_programrunner_only(kind, monkeypatch, tmp_path):
    import pythoncm_validation

    with pytest.raises(pythoncm_validation.UnsupportedValidation):
        pythoncm_validation.build_spec(
            kind,
            nodes=["ink50-dh01-d01-11"],
            rack="ink50-dh01-d01",
            log_dir=str(tmp_path),
            scripts_dir="/opt/nvis/bin/scripts",
            timeout=60,
        )


def test_status_bootstrap_never_starts_cmsh_warmer_in_pythoncm(monkeypatch):
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
    monkeypatch.setattr(
        app_module,
        "_start_cmsh_rack_list_warm_thread",
        lambda: (_ for _ in ()).throw(AssertionError("cmsh warmer reached")),
    )
    app_module._ensure_rack_status_background_thread()


def test_pythoncm_workflow_and_preset_execution_surfaces_are_blocked(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    client = app_module.app.test_client()
    assert client.get("/api/workflows").get_json()["workflows"] == []
    assert client.post("/api/workflows/legacy/run", json={}).status_code == 409
    assert client.post("/api/workflows", json={"name": "unsafe"}).status_code == 409
    assert client.get("/api/validation-presets").get_json()["presets"] == []
    assert client.post("/api/validation-presets/prepare", json={}).status_code == 409
    assert client.post("/api/run-validation-preset-async", json={}).status_code == 501


def test_pythoncm_worker_persists_uuid_and_terminal_state(monkeypatch):
    ref = "123e4567-e89b-42d3-a456-426614174000"
    task_updates = []

    class FakeScheduler:
        def submit(self, spec):
            assert spec.launch_mode == "leader"
            return JobHandle(ref, "pythoncm")

        def wait(self, handle, **_kwargs):
            assert handle.job_id == ref
            return JobResult(0, "COMPLETED", "validation passed", "", ref)

        def cancel(self, _handle):
            raise AssertionError("completed command must not be cancelled")

    monkeypatch.setattr(app_module, "build_scheduler", lambda *_a, **_kw: FakeScheduler())
    monkeypatch.setattr(app_module.host_exec, "connection", lambda _profile: object())
    monkeypatch.setattr(
        app_module.validation_activities,
        "add_or_update_task",
        lambda activity_id, **fields: task_updates.append((activity_id, fields)),
    )
    monkeypatch.setattr(app_module.validation_activities, "update_activity", lambda *_a, **_kw: None)
    registry = {"activity": {"phase": "starting"}}
    spec = JobSpec(
        name="nccl-rack",
        nodes=2,
        nodelist="n1,n2",
        command="direct-command",
        output="/logs/result-%j.txt",
        launch_mode="leader",
        timeout=60,
    )
    app_module._pythoncm_validation_worker(
        "activity",
        spec,
        registry=registry,
        registry_lock=app_module.threading.Lock(),
        purge=lambda _job_id: None,
    )
    assert registry["activity"]["phase"] == "done"
    assert registry["activity"]["scheduler_ref"] == ref
    assert registry["activity"]["returncode"] == 0
    assert any(fields.get("scheduler_ref") == ref for _, fields in task_updates)
    assert task_updates[-1][1]["scheduler_state"] == "COMPLETED"
    assert task_updates[-1][1]["stdout_path"].endswith(f"result-{ref}.txt")


def test_log_probe_shell_trace_executes_no_cluster_command(tmp_path):
    script = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "rack_status.sh"
    marker = tmp_path / "forbidden-command-ran"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("cmsh", "sinfo", "squeue", "scontrol", "sacct", "sbatch", "srun", "scancel", "pdsh"):
        shim = fake_bin / name
        shim.write_text(f"#!/bin/sh\nprintf '%s\\n' {name} >> {marker}\nexit 99\n", encoding="utf-8")
        shim.chmod(0o755)
    log = tmp_path / "root-NCCL-1Rack-ink50-dh01-d01-1N-123.txt"
    log.write_text("NCCL test output\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    proc = subprocess.run(
        [
            "bash",
            str(script),
            "--no-squeue",
            "--no-cmsh",
            "--no-sinfo",
            "--probe-json",
            str(log),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()
