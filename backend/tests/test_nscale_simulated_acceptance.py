# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Non-destructive, public-API acceptance sweep for the Nscale Rack Status build.

These are deliberately integration-shaped tests.  They enter through the same Flask routes
used by the SPA while replacing only external systems (BCM, Slurm, Grafana and
VictoriaMetrics) with deterministic responses.  No cluster command is executed.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import app as app_module
import grafana_embeds
import metrics_query
import post_run_report
import pytest
import validation_activities
import workflow_engine
from api import fabric_probe
from schedulers import JobHandle, JobResult


@pytest.fixture(autouse=True)
def _isolate_acceptance_state(monkeypatch, tmp_path):
    """Keep simulated durable/in-memory jobs out of the repo and other tests."""
    monkeypatch.setattr(validation_activities, "ACTIVITIES_FILE", tmp_path / "activities.json")
    monkeypatch.setattr(validation_activities, "LEGACY_JOBS_FILE", tmp_path / "legacy.json")
    monkeypatch.setattr(validation_activities, "ACTIVITY_ARCHIVE_DB", tmp_path / "activities.sqlite3")
    monkeypatch.setattr(validation_activities, "_archive_sync_signature", None)
    registries = (
        (app_module._validation_jobs_lock, app_module._validation_jobs),
        (app_module._stress_jobs_lock, app_module._stress_jobs),
        (app_module._xrack_jobs_lock, app_module._xrack_jobs),
        (app_module._llama_jobs_lock, app_module._llama_jobs),
    )
    for lock, registry in registries:
        with lock:
            registry.clear()
    yield
    for lock, registry in registries:
        with lock:
            registry.clear()
    with app_module._validation_launcher_processes_lock:
        app_module._validation_launcher_processes.clear()
    with workflow_engine._runs_lock:
        workflow_engine._active_runs.clear()
        workflow_engine._active_scheduler_jobs.clear()


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_rack_status_read_surfaces_through_public_api(monkeypatch, tmp_path):
    """Sweep the operator read paths used by Racks, reports, analytics and admin views."""
    monkeypatch.setenv("RACK_STATUS_DEMO", "1")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    monkeypatch.setattr(app_module, "_default_log_dir", lambda: Path(tmp_path))
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw: Path(tmp_path))

    expected = {
        "/api/status": "rows",
        "/api/cluster-overview": "rack_count",
        "/api/cluster-overview/live": "sections",
        "/api/post-run-node-report": "summary",
        "/api/analytics/history": "history",
        "/api/analytics/last": "single_rack",
        "/api/parser-results": "data",
        "/api/reports/generated": "reports",
        "/api/workflows": "workflows",
        "/api/tools/available": "tools",
        "/api/catalog/overview": "active",
        "/api/node-report/kinds": "kinds",
        "/api/validation-activities": "activities",
        "/api/scheduler-jobs": "jobs",
        "/api/scheduler-history": "jobs",
        "/api/scheduler-node-reasons": "nodes",
    }
    with _client() as client:
        for route, field in expected.items():
            response = client.get(route)
            assert response.status_code == 200, (route, response.get_data(as_text=True))
            payload = response.get_json()
            assert payload.get("ok") is True, (route, payload)
            assert field in payload, (route, payload)
        openapi = client.get("/api/openapi.json")
        assert openapi.status_code == 200
        assert "/api/status" in openapi.get_json()["paths"]


def test_packaged_nscale_config_is_catalog_isolated_and_contains_no_grafana_password():
    """Pin the deployment failure found by the independent runtime audit."""
    repo = Path(__file__).resolve().parents[2]
    expected = "NVIS_CATALOG_FILE=/app/var/store/catalog-nscale-tx.json"
    assert expected in (repo / "rack-config.env").read_text(encoding="utf-8")
    assert expected in (repo / ".env.example").read_text(encoding="utf-8")
    override = (repo / "deploy" / "nscale-tx.override.consolidated.yml").read_text(
        encoding="utf-8"
    )
    assert expected in override
    assert "nvis1234" not in override
    assert "RACK_GRAFANA_PASSWORD=${RACK_GRAFANA_PASSWORD:-}" in override


def test_nvloom_simulated_submit_to_terminal_status_uses_documented_slurm_fallback(
    monkeypatch, tmp_path
):
    """Fire NVLoom through its real POST/GET routes without submitting a live allocation."""
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    prep = {
        "log_dir": str(tmp_path),
        "rk": "ink50-dh01-d01",
        "target": "ink50-dh01-d01-[11-12]",
        "cmd": ["bash", "run-nvloom-simulated.sh"],
        "display_cmd": "SIMULATED sbatch NVLoom ink50-dh01-d01-[11-12]",
        "timeout": 60,
        "env_updates": {},
    }
    monkeypatch.setattr(app_module, "_prepare_run_nvloom_singlerack", lambda _body: (prep, None))
    recorded: list[dict] = []
    monkeypatch.setattr(
        app_module,
        "_record_validation_activity",
        lambda _tool, **fields: recorded.append(fields) or fields["activity_id"],
    )

    def simulated_worker(job_id, *_args, **_kwargs):
        with app_module._stress_jobs_lock:
            app_module._stress_jobs[job_id].update(
                phase="done",
                work_phase="done",
                returncode=0,
                stdout="SIMULATED NVLoom PASS",
                stderr="",
                scheduler="slurm",
            )

    monkeypatch.setattr(app_module, "_stress_worker", simulated_worker)

    class ImmediateThread:
        def __init__(self, *, target, args=(), kwargs=None, **_rest):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(app_module.threading, "Thread", ImmediateThread)
    with app_module._stress_jobs_lock:
        app_module._stress_jobs.clear()
    try:
        with _client() as client:
            launched = client.post(
                "/api/run-nvloom-async",
                json={"rack": "ink50-dh01-d01", "simulate": True},
            )
            assert launched.status_code == 200
            launch_payload = launched.get_json()
            assert launch_payload["ok"] is True
            job_id = launch_payload["job_id"]
            status = client.get(f"/api/run-nvloom-status?job_id={job_id}")
            assert status.status_code == 200
            status_payload = status.get_json()
            assert status_payload["phase"] == "done"
            assert status_payload["returncode"] == 0
            assert status_payload["stdout"] == "SIMULATED NVLoom PASS"
        assert recorded and recorded[0].get("scheduler", "slurm") == "slurm"
    finally:
        with app_module._stress_jobs_lock:
            app_module._stress_jobs.clear()


def test_post_run_report_annotation_and_pythoncm_drain_are_simulated(monkeypatch):
    """Exercise the operator finding path, including its provider-routed drain hook."""
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(app_module, "_all_slurm_nodenames", lambda: ["node01"])
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        app_module,
        "_run_scontrol_node_update",
        lambda node, fields: calls.append((node, fields)) or {
            "ok": True,
            "returncode": 0,
            "stdout": "SIMULATED PythonCM drain accepted",
            "stderr": "",
        },
    )
    monkeypatch.setattr(post_run_report, "rack_for_node", lambda _node: "ink50-dh01-d01")
    import node_issues

    monkeypatch.setattr(node_issues, "record_issue", lambda **fields: {"issue_id": "sim-1", **fields})
    with _client() as client:
        response = client.post(
            "/api/post-run-node-report/mark",
            json={
                "node": "node01",
                "category": "network",
                "reason": "simulated Ethernet link degradation",
                "job_id": "123e4567-e89b-42d3-a456-426614174000",
                "drain": True,
            },
            headers={"X-Rack-Operator": "acceptance-simulator"},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True and payload["drained"] is True
    assert calls and calls[0][0] == "node01"


def test_analytics_async_simulated_run_completes(monkeypatch, tmp_path):
    """Start and poll the same asynchronous analytics lifecycle used by AnalyticsView."""
    monkeypatch.setenv("RACK_STATUS_DEMO", "1")
    monkeypatch.setenv("RACK_WEB_AUTH_DISABLED", "1")
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw: Path(tmp_path))
    with _client() as client:
        started = client.post("/api/analytics/aggregate-async", json={"rows": []})
        assert started.status_code == 200
        job_id = started.get_json()["job_id"]
        payload = client.get(f"/api/analytics/aggregate-status?job_id={job_id}").get_json()
        for _ in range(100):
            if payload.get("state") in {"done", "error"}:
                break
            import time

            time.sleep(0.01)
            payload = client.get(f"/api/analytics/aggregate-status?job_id={job_id}").get_json()
    assert payload["state"] == "done"
    assert payload["payload"]["single_rack"]["nccl_collectives"]


def test_observability_metrics_logs_events_and_ethernet_contract(monkeypatch):
    """Serve all observability views from a deterministic Nscale Grafana map."""
    views = {}
    for key in (
        "metrics",
        "logs",
        "events",
        "infra-ethernet",
        "infra-ib-nvlink",
        "infra-storage",
    ):
        views[key] = {
            "route": f"/observability/{key}",
            "segment": key,
            "sections": [
                {
                    "title": f"Nscale {key}",
                    "uid": f"nscale-{key}",
                    "panel_ids": [1],
                    "panel_titles": {"1": f"{key} health"},
                }
            ],
        }
    simulated_map = {
        "schema_version": 1,
        "generated": "2026-08-25T00:00:00Z",
        "grafana": {"site": "Nscale TX"},
        "views": views,
        "unmapped": [],
        "keep_as_is": [],
        "_source_path": "SIMULATED/grafana_embed_map.json",
    }
    monkeypatch.setattr(grafana_embeds, "load_map", lambda force=False: simulated_map)
    monkeypatch.setenv("RACK_GRAFANA_PUBLIC_URL", "https://nscale-grafana.example.invalid")
    with _client() as client:
        whole = client.get("/api/observability/dashboards")
        assert whole.status_code == 200
        whole_payload = whole.get_json()
        assert whole_payload["ok"] is True
        assert whole_payload["totals"] == {"views": 6, "sections": 6, "panels": 6, "unmapped": 0}
        for view in views:
            response = client.get(f"/api/observability/dashboards/{view}?rack=ink50-dh01-d01")
            assert response.status_code == 200
            payload = response.get_json()
            assert payload["ok"] is True
            assert payload["view"] == view
            assert payload["panels" if "panels" in payload else "sections"]
            panel = payload["sections"][0]["panels"][0]
            assert "var-rack=ink50-dh01-d01" in panel["embed_url"]


def test_observability_health_and_native_metrics_with_simulated_upstreams(monkeypatch):
    """Validate Grafana health plus VictoriaMetrics catalog/health/chart shaping."""
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(
        grafana_embeds,
        "load_map",
        lambda force=False: {"views": {"metrics": {}}, "unmapped": [], "keep_as_is": []},
    )

    def grafana_response(url, _timeout, method="GET"):
        if url.endswith("/api/health"):
            return 200, {}, json.dumps({"version": "simulated", "database": "ok"}).encode(), ""
        if "/api/search" in url:
            return 200, {}, json.dumps([{"uid": "nscale"}]).encode(), ""
        return 200, {}, b"", ""

    monkeypatch.setattr(grafana_embeds, "_grafana_request", grafana_response)
    monkeypatch.setattr(
        metrics_query,
        "_instant",
        lambda _expr: [{"metric": {}, "value": [0, "3943"]}],
    )
    monkeypatch.setattr(
        metrics_query,
        "_range",
        lambda _expr, _minutes, _step: [
            {"metric": {"rack": "ink50-dh01-d01"}, "values": [[1, "18"], [2, "18"]]},
            {"metric": {"rack": "ink50-dh01-d02"}, "values": [[1, "18"], [2, "17"]]},
        ],
    )
    with _client() as client:
        health = client.get("/api/observability/health").get_json()
        assert health["ok"] is True
        assert health["reachable"] is True
        assert health["authenticated"] is True
        assert health["embedding_allowed"] is True
        metrics_health = client.get("/api/metrics/health").get_json()
        assert metrics_health == {
            "ok": True,
            "upstream": metrics_query.base_url(),
            "error": None,
            "dcgm_hosts": 3943,
        }
        catalog = client.get("/api/metrics/catalog?group=infra").get_json()
        assert catalog["ok"] is True and catalog["charts"]
        assert catalog["scheduler"] == "pythoncm"
        chart_ids = {row["id"] for row in catalog["charts"]}
        assert "bcm_devices" in chart_ids
        assert "slurm_nodes" not in chart_ids
        chart = client.get("/api/metrics/chart/gpu_temp_by_rack?minutes=5").get_json()
        assert chart["ok"] is True
        assert chart["labels"] == ["1", "2"]
        assert len(chart["datasets"]) == 2


def test_pythoncm_fabric_and_ethernet_probe_through_public_api(monkeypatch):
    """Run the node-local fabric probe contract through a fake ProgramRunner result."""
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setenv("RACK_CLUSTER_ID", "nscale-tx")
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "Nscale")
    monkeypatch.setattr(fabric_probe.scheduler_runtime, "node_names", lambda: ["node01"])
    submitted = []

    class SimulatedScheduler:
        def run(self, spec):
            submitted.append(spec)
            return JobResult(
                returncode=0,
                state="COMPLETED",
                stdout=json.dumps(
                    {
                        "link_layer": "Ethernet",
                        "compute_rails": ["mlx5_0", "mlx5_1"],
                        "storage_interfaces": ["bond0"],
                        "nccl_ib_hca": "mlx5_0,mlx5_1",
                        "nccl_socket_ifname": "bond0",
                    }
                ),
                stderr="",
                job_id="123e4567-e89b-42d3-a456-426614174000",
            )

    monkeypatch.setattr(fabric_probe, "build_scheduler", lambda *_args, **_kwargs: SimulatedScheduler())
    monkeypatch.setattr(fabric_probe.host_exec, "connection", lambda _profile: object())
    with _client() as client:
        response = client.get("/api/fabric-probe?node=node01")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["fabric"]["link_layer"] == "Ethernet"
    assert payload["fabric"]["compute_rails"] == ["mlx5_0", "mlx5_1"]
    assert submitted[0].nodelist == "node01"
    assert submitted[0].launch_mode == "fanout"
    assert "ssh" not in submitted[0].command.lower()


def test_validation_activity_aggregates_status_and_cancels_every_native_child(monkeypatch):
    """One public activity UUID owns all fallback tasks; status/cancel must aggregate them."""
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    activity_id = validation_activities.record_activity(
        "nvloom",
        scheduler="slurm",
        execution_provider="slurm",
        fallback=True,
        fallback_reason="NVLoom requires reviewed Slurm allocation semantics",
        nodes=["ink50-dh01-h04-11", "ink50-dh01-h04-12"],
    )
    for ordinal, ref in enumerate(("81001", "81002"), start=1):
        validation_activities.add_or_update_task(
            activity_id,
            ordinal=ordinal,
            label=f"NVLoom part {ordinal}",
            scheduler_ref=ref,
            scheduler_state="RUNNING",
            phase="running",
            work_phase="running",
        )
    monkeypatch.setattr(app_module, "_hydrate_activity_task_logs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_module.scheduler_runtime, "live_status", lambda ref: ("RUNNING", f"/logs/{ref}.out"))
    monkeypatch.setattr(app_module.scheduler_runtime, "job", lambda ref: {"nodelist": f"node-{ref}"})
    monkeypatch.setattr(app_module.scheduler_runtime, "reference_kind", lambda ref: "slurm" if ref.isdigit() else "pythoncm")
    cancelled: list[str] = []
    monkeypatch.setattr(app_module.scheduler_runtime, "cancel", cancelled.append)

    with _client() as client:
        detail = client.get(f"/api/scheduler-job-detail?job_id={activity_id}")
        assert detail.status_code == 200
        detail_payload = detail.get_json()
        assert detail_payload["scheduler"] == "slurm"
        assert detail_payload["scheduler_refs"] == ["81001", "81002"]
        assert len(detail_payload["tasks"]) == 2

        response = client.post("/api/scheduler-cancel", json={"job_id": activity_id})
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["ok"] is True
        assert payload["scheduler_refs"] == ["81001", "81002"]
        assert len(payload["tasks"]) == 2

    assert cancelled == ["81001", "81002"]
    persisted = validation_activities.get_activity(activity_id)
    assert persisted["phase"] == "cancelled"
    assert persisted["fallback_reason"]
    assert persisted["nodes"] == ["ink50-dh01-h04-11", "ink50-dh01-h04-12"]


def test_sync_validation_failure_returns_recoverable_activity_and_provider(monkeypatch, tmp_path):
    """A synchronous launcher timeout must not strand an undiscoverable durable record."""
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    prep = {
        "log_dir": str(tmp_path),
        "rk": "ink50-dh01-h04",
        "target": "ink50-dh01-h04-[11-12]",
        "cmd": ["bash", "simulated-timeout.sh"],
        "display_cmd": "SIMULATED timeout",
        "timeout": 1,
    }
    monkeypatch.setattr(app_module, "_prepare_run_validation", lambda _body: (prep, None))
    monkeypatch.setattr(
        app_module.host_exec,
        "run_on_host",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("simulated-timeout.sh", 1)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_scontrol_hostnames_preserving_case",
        lambda _target: ["ink50-dh01-h04-11", "ink50-dh01-h04-12"],
    )
    with _client() as client:
        response = client.post("/api/run-validation", json={"rack": "ink50-dh01-h04"})
    assert response.status_code == 504
    payload = response.get_json()
    assert payload["scheduler"] == "slurm"
    assert payload["fallback"] is True and payload["fallback_reason"]
    persisted = validation_activities.get_activity(payload["job_id"])
    assert persisted["phase"] == "timeout"
    assert persisted["execution_provider"] == "slurm"


def test_pre_cancelled_validation_never_spawns_fallback_wrapper(monkeypatch, tmp_path):
    """Cancellation before the worker turn closes the pre-sbatch submission window."""
    activity_id = validation_activities.record_activity(
        "nvloom",
        scheduler="slurm",
        execution_provider="slurm",
        fallback=True,
        fallback_reason="simulated Slurm recipe gap",
    )
    validation_activities.update_activity(
        activity_id,
        phase="cancelled",
        cancel_requested=True,
    )
    called = {"popen": 0}

    def forbidden_popen(*_args, **_kwargs):
        called["popen"] += 1
        raise AssertionError("pre-cancelled activity must not spawn a launcher")

    monkeypatch.setattr(app_module.host_exec, "popen_on_host", forbidden_popen)
    with app_module._stress_jobs_lock:
        app_module._stress_jobs[activity_id] = {"job_id": activity_id, "phase": "starting"}
    try:
        app_module._stress_worker(
            activity_id,
            ["bash", "forbidden.sh"],
            str(tmp_path),
            1,
            "forbidden.sh",
            persist=True,
        )
    finally:
        with app_module._stress_jobs_lock:
            app_module._stress_jobs.pop(activity_id, None)
    assert called["popen"] == 0
    assert validation_activities.get_activity(activity_id)["phase"] == "cancelled"


def test_workflow_cancel_dispatches_native_child_and_cannot_be_overwritten(monkeypatch, tmp_path):
    """Exercise submit/status/cancel through Flask while a fake scheduler wait is blocked."""
    workflow_dir = tmp_path / "workflows"
    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", workflow_dir)
    monkeypatch.setattr(workflow_engine, "RUNS_DIR", workflow_dir / "runs")
    with workflow_engine._runs_lock:
        workflow_engine._active_runs.clear()
        workflow_engine._active_scheduler_jobs.clear()

    submitted = threading.Event()
    released = threading.Event()
    cancel_calls: list[str] = []

    class SimulatedScheduler:
        kind = "pythoncm"

        def submit(self, _spec):
            submitted.set()
            return JobHandle("123e4567-e89b-42d3-a456-426614174001", "pythoncm")

        def wait(self, handle, **_kwargs):
            released.wait(3)
            return JobResult(0, "COMPLETED", "SIMULATED workflow PASS", "", handle.job_id)

        def cancel(self, handle):
            cancel_calls.append(handle.job_id)
            released.set()

    import schedulers

    monkeypatch.setattr(schedulers, "build_scheduler", lambda *_args, **_kwargs: SimulatedScheduler())
    workflow_engine.save_workflow({
        "id": "nscale-pythoncm-cancel",
        "name": "Nscale PythonCM cancel acceptance",
        "steps": [{
            "id": "probe",
            "type": "automated",
            "scheduler": {
                "kind": "pythoncm",
                "command": "echo simulated",
                "pythoncm_direct": True,
                "nodelist": "ink50-dh01-h04-11",
            },
            "on_success": "done",
        }],
    })
    with _client() as client:
        started = client.post("/api/workflows/nscale-pythoncm-cancel/run", json={"params": {}})
        assert started.status_code == 200
        run_id = started.get_json()["run_id"]
        assert submitted.wait(2)
        live = client.get(f"/api/workflow-runs/{run_id}").get_json()["run"]
        assert live["active_scheduler"]["provider"] == "pythoncm"
        assert live["active_scheduler"]["scheduler_ref"].endswith("4001")
        cancelled_response = client.post(f"/api/workflow-runs/{run_id}/cancel")
        assert cancelled_response.status_code == 200
        assert cancelled_response.get_json()["cancel_dispatched"] is True

    deadline = time.monotonic() + 3
    final = workflow_engine.get_run_state(run_id)
    while not final.get("history") and time.monotonic() < deadline:
        time.sleep(0.01)
        final = workflow_engine.get_run_state(run_id)
    assert final["state"] == "cancelled"
    assert final["history"][-1]["cancelled"] is True
    assert final["history"][-1]["scheduler"]["provider"] == "pythoncm"
    assert cancel_calls == ["123e4567-e89b-42d3-a456-426614174001"]


def test_workflow_cancel_while_submit_is_blocked_kills_handle_before_wait(monkeypatch, tmp_path):
    """Close the submit-time race where no native reference exists at cancel time."""
    workflow_dir = tmp_path / "workflows-submit-race"
    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", workflow_dir)
    monkeypatch.setattr(workflow_engine, "RUNS_DIR", workflow_dir / "runs")
    with workflow_engine._runs_lock:
        workflow_engine._active_runs.clear()
        workflow_engine._active_scheduler_jobs.clear()

    submitting = threading.Event()
    release_submit = threading.Event()
    cancel_calls: list[str] = []
    wait_calls: list[str] = []

    class BlockingSubmitScheduler:
        kind = "slurm"

        def submit(self, _spec):
            submitting.set()
            release_submit.wait(3)
            return JobHandle("82001", "slurm")

        def wait(self, handle, **_kwargs):
            wait_calls.append(handle.job_id)
            return JobResult(0, "COMPLETED", "must not run", "", handle.job_id)

        def cancel(self, handle):
            cancel_calls.append(handle.job_id)

    import schedulers

    monkeypatch.setattr(schedulers, "build_scheduler", lambda *_args, **_kwargs: BlockingSubmitScheduler())
    workflow_engine.save_workflow({
        "id": "nscale-submit-cancel-race",
        "name": "Nscale submit cancellation race",
        "steps": [{
            "id": "allocation",
            "type": "automated",
            "scheduler": {"kind": "slurm", "command": "echo simulated"},
            "on_success": "done",
        }],
    })
    with _client() as client:
        started = client.post("/api/workflows/nscale-submit-cancel-race/run", json={})
        run_id = started.get_json()["run_id"]
        assert submitting.wait(2)
        response = client.post(f"/api/workflow-runs/{run_id}/cancel")
        assert response.status_code == 200
        assert response.get_json()["state"] == "cancelled"
        release_submit.set()

    deadline = time.monotonic() + 3
    final = workflow_engine.get_run_state(run_id)
    while not final.get("history") and time.monotonic() < deadline:
        time.sleep(0.01)
        final = workflow_engine.get_run_state(run_id)
    assert final["state"] == "cancelled"
    assert final["history"][-1]["scheduler"]["scheduler_ref"] == "82001"
    assert cancel_calls == ["82001"]
    assert wait_calls == []
