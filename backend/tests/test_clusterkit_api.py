# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Full Flask request lifecycle with real services/storage and simulated Slurm I/O."""
from __future__ import annotations

import json
import shlex
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from .test_clusterkit_service import write_passing_summary


class SimulatedSlurm:
    def __init__(self):
        self.calls = []
        self.state = "PENDING"
        self.node_state = "idle"
        self.submit_error = False
        self.inventory_error = False
        self.recovered = False
        self.name = self.comment = ""

    @property
    def submissions(self):
        return [argv for argv in self.calls if argv[0] == "sbatch"]

    def run(self, command, **kwargs):
        assert kwargs["login_shell"] is False
        assert not any(key.startswith("SBATCH_") for key in kwargs["env"])
        argv = shlex.split(command)
        self.calls.append(argv)
        output = ""
        if argv[0] == "sinfo":
            if self.inventory_error:
                raise TimeoutError("simulated inventory timeout")
            nodes = argv[argv.index("-n") + 1].split(",")
            output = "\n".join(f"{node}|{self.node_state}|testq" for node in nodes)
        elif argv[0] == "sbatch":
            self.name = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--job-name="))
            self.comment = next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--comment="))
            if self.submit_error:
                raise TimeoutError("simulated lost submission response")
            output = "12345\n"
        elif argv[0] in {"sacct", "squeue"}:
            if any(arg.startswith("--name=") for arg in argv):
                if self.recovered:
                    output = f"12345|{self.name}|{self.comment}\n"
            else:
                output = self.state + "\n"
        else:
            raise AssertionError(f"Unexpected transport command: {command}")
        return subprocess.CompletedProcess(command, 0, output, "")


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    import app as app_module
    import config_env
    import host_exec
    import platforms
    import validation_activities as registry
    from catalog import store
    from transport.local import LocalExecutor

    root = tmp_path.resolve()
    results = root / "results"
    results.mkdir()
    hpcx = root / "hpcx-init.sh"
    hpcx.write_text("# fake HPC-X init, never executed\n")
    nodes = [f"sys1-gs01-p-phy-gpu{index}-su20" for index in range(1, 9)]
    profile = {
        "enabled": True, "partition": "testq", "hpcx_init": str(hpcx),
        "results_root": str(results), "allowed_node_counts": [2, 4, 8], "max_nodes": 8,
        "expected_architecture": "aarch64", "expected_link_layer": "Ethernet",
        "hca": "fake_rail:1", "node_allowlist": nodes,
        "topology_groups": {"test-pool": {"nodes": nodes, "kind": "group"}},
    }
    for key, value in {
        "RACK_EXEC_MODE": "local", "RACK_PLATFORM": "bcm",
        "RACK_SLURM_SKIP_MODULE_LOAD": "1", "RACK_SLURM_CONF": "",
        "RACK_CLUSTER_NAME": "simulated-nc", "RACK_LAKE_CAPTURE": "all",
        "RACK_REMOTE_BASE_PATH": str(Path(__file__).resolve().parents[2]),
        "RACK_WEB_AUTH_DISABLED": "0", "RACK_VALIDATION_ACTIVITIES_REFRESH_DISABLED": "1",
        "RACK_CLUSTERKIT_SITE_PROFILE": json.dumps(profile),
        "NVIS_CATALOG_FILE": str(root / "catalog.json"),
        "SBATCH_EXCLUSIVE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    for name in ("SBATCH", "SINFO", "SQUEUE", "SACCT", "SCONTROL", "SCANCEL"):
        monkeypatch.setenv(f"RACK_SLURM_{name}", name.lower())
    monkeypatch.setattr(store, "_instance", None)
    monkeypatch.setattr(platforms, "_instance", None)
    monkeypatch.setattr(host_exec, "_topology_provider", None)
    monkeypatch.setattr(config_env, "_job_env_overrides_path", lambda: root / "settings.json")
    for name in (
        "ACTIVITIES_FILE", "LEGACY_JOBS_FILE", "ACTIVITY_ARCHIVE_DB",
        "MULTIRACK_LEDGER_FILE", "MULTIRACK_LEDGER_MARKDOWN_FILE",
        "MULTIRACK_LEDGER_EXPORT_STATUS_FILE",
    ):
        monkeypatch.setattr(registry, name, root / name.lower())
    monkeypatch.setattr(registry, "_ledger_lake_disabled", lambda: True)
    monkeypatch.setattr(app_module, "_JOB_ENV_BOOTSTRAP_DONE", True)
    monkeypatch.setitem(app_module.app.config, "TESTING", True)
    monkeypatch.setitem(app_module.app.config, "SECRET_KEY", "test-secret-" * 8)
    sink = mock.Mock()
    sink.is_disabled.return_value = False
    sink.make_run_id.return_value = "simulated-request"
    monkeypatch.setattr(app_module, "lake_sink", sink)

    slurm = SimulatedSlurm()
    monkeypatch.setattr(LocalExecutor, "run", lambda _self, command, **kwargs: slurm.run(command, **kwargs))
    # Any accidental process launch or network connection fails the test immediately.
    deny = mock.Mock(side_effect=AssertionError("External execution is forbidden in HTTP tests"))
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr("socket.socket.connect", deny)
    with app_module.app.test_client() as client:
        with client.session_transaction() as session:
            session["rack_web_auth"] = "1"
        yield SimpleNamespace(
            client=client, app=app_module, root=root, results=results, hpcx=hpcx,
            nodes=nodes, profile=profile, registry=registry, slurm=slurm, sink=sink,
        )


def run_request(env, count=2):
    return {"scope": "preflight" if count == 1 else "explicit",
            "nodes": env.nodes[:count], "node_count": count}


def preview(env, count=2):
    response = env.client.post("/api/clusterkit/preview", json=run_request(env, count))
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def submit(env, count=2, *, plan=None):
    plan = plan or preview(env, count)
    response = env.client.post("/api/clusterkit/runs", json={
        "confirm": True, "confirmation_token": plan["confirmation_token"],
    }, headers={"X-Rack-Operator": "test-operator"})
    assert response.status_code == 202, response.get_json()
    return response.get_json()


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_previews_use_real_hooks_without_writes_or_submissions(api_env, monkeypatch, count):
    env = api_env
    monkeypatch.setattr(env.app, "_JOB_ENV_BOOTSTRAP_DONE", False)
    bootstrap = mock.Mock(side_effect=AssertionError("Preview must not materialize Settings"))
    monkeypatch.setattr(env.app, "_job_env_overrides_load", bootstrap)
    before = sorted(env.root.rglob("*"))
    ready = env.client.get("/api/clusterkit/readiness")
    assert ready.status_code == 200
    assert ready.get_json()["configuration_ready"] is True
    assert ready.get_json()["execution_context_ready"] is True
    inventory = env.client.get("/api/clusterkit/inventory")
    assert inventory.status_code == 200
    assert len(inventory.get_json()["inventory"]["nodes"]) == 8
    response = preview(env, count)
    plan = response["plan"]
    assert plan["expected_pair_count"] == count * (count - 1) // 2
    assert plan["node_count"] == count
    assert plan["entrypoint"].endswith("clusterkit_preflight.sbatch" if count == 1 else "clusterkit_pairwise.sbatch")
    assert plan["execution_ready"] and plan["submission_enabled"]
    assert "no-store" in ready.headers["Cache-Control"]
    assert sorted(env.root.rglob("*")) == before
    bootstrap.assert_not_called()
    env.sink.push_run.assert_not_called()
    assert not env.slurm.submissions


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_http_lifecycle_persists_results_and_keeps_job_ids_distinct(api_env, count):
    env = api_env
    plan = preview(env, count)
    result = submit(env, count, plan=plan)
    assert result["job_id"] == result["activity_id"] == plan["activity_id"]
    assert result["scheduler_job_id"] == "12345"
    assert result["phase"] == "running"
    assert env.registry.get_activity(result["job_id"])["operator"] == "test-operator"
    assert env.registry.ACTIVITY_ARCHIVE_DB.is_file()
    assert len(env.slurm.submissions) == 1
    assert shlex.split(plan["submission_command_display"]) == env.slurm.submissions[0]
    assert "--gres=none" in env.slurm.submissions[0]
    assert "--exclusive" not in env.slurm.submissions[0]
    status = env.client.get(result["status_url"])
    assert status.status_code == 200
    assert status.get_json()["phase"] == "running"
    env.slurm.state = "COMPLETED"
    collecting = env.client.get(result["status_url"]).get_json()
    assert collecting["phase"] == "running"
    assert collecting["work_phase"] == "collecting"
    root = Path(result["results_directory"])
    write_passing_summary(json.loads((root / "plan.json").read_text()), root)
    complete = env.client.get(result["status_url"])
    assert complete.status_code == 200
    body = complete.get_json()
    assert body["phase"] == "done" and body["returncode"] == 0
    assert body["summary"]["functional_status"] == "PASS"
    assert body["summary"]["performance_status"] == "UNVERIFIED"
    assert body["summary"]["expected_pair_count"] == count * (count - 1) // 2
    assert body["activity"]["tasks"][0]["scheduler_ref"] == "12345"
    artifacts = env.client.get(result["status_url"] + "/artifacts")
    assert artifacts.status_code == 200
    assert "summary.json" in {item["name"] for item in artifacts.get_json()["artifacts"]}
    downloaded = env.client.get(result["status_url"] + "/artifacts/summary.json")
    assert downloaded.status_code == 200
    assert downloaded.get_json() == body["summary"]
    assert len(env.slurm.submissions) == 1


@pytest.mark.parametrize("path,method", [
    ("/api/clusterkit/readiness", "GET"), ("/api/clusterkit/inventory", "GET"),
    ("/api/clusterkit/preview", "POST"), ("/api/clusterkit/runs", "POST"),
    ("/api/clusterkit/runs/test", "GET"),
    ("/api/clusterkit/runs/test/artifacts", "GET"),
    ("/api/clusterkit/runs/test/artifacts/summary.json", "GET"),
])
def test_all_routes_inherit_session_auth(api_env, path, method):
    with api_env.client.session_transaction() as session:
        session.clear()
    response = api_env.client.open(path, method=method, json=run_request(api_env))
    assert response.status_code == 401
    assert response.get_json()["error"] == "authentication required"
    assert api_env.slurm.calls == []
    assert list(api_env.results.iterdir()) == []


def test_disabled_profile_and_inventory_failure_do_not_submit(api_env, monkeypatch):
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", '{"enabled": false}')
    assert api_env.client.get("/api/clusterkit/readiness").get_json()["configuration_ready"] is False
    response = api_env.client.post("/api/clusterkit/preview", json=run_request(api_env))
    assert response.status_code == 409
    monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps(api_env.profile))
    api_env.slurm.inventory_error = True
    response = api_env.client.post("/api/clusterkit/preview", json=run_request(api_env))
    assert response.status_code == 409
    assert not api_env.slurm.submissions


def test_group_preview_uses_configured_membership(api_env):
    response = api_env.client.post("/api/clusterkit/preview", json={
        "scope": "group", "group": "test-pool", "node_count": 4,
    })
    assert response.status_code == 200
    assert response.get_json()["plan"]["nodes"] == sorted(api_env.nodes)[:4]
    assert not api_env.slurm.submissions


@pytest.mark.parametrize("body", [None, [], {}, {"confirm": "true"}, {"confirm": True},
    {"confirm": True, "confirmation_token": "forged"},
    {"confirm": True, "confirmation_token": "valid-ascii.\u00e9"},
    {"confirm": True, "confirmation_token": "forged", "hca": "override:1"}])
def test_confirmation_rejects_invalid_payloads(api_env, body):
    response = api_env.client.post("/api/clusterkit/runs", json=body)
    assert response.status_code in {400, 409}
    assert response.get_json()["ok"] is False
    assert not api_env.slurm.submissions


@pytest.mark.parametrize("change", ["policy", "context", "availability", "recipe", "expired"])
def test_changed_or_expired_preview_rejected_before_submission(api_env, monkeypatch, change):
    env = api_env
    plan = preview(env)
    if change == "policy":
        monkeypatch.setenv("RACK_CLUSTERKIT_SITE_PROFILE", json.dumps({**env.profile, "max_nodes": 4}))
    elif change == "context":
        monkeypatch.setenv("RACK_CLUSTER_NAME", "different-cluster")
    elif change == "availability":
        env.slurm.node_state = "idle+drain"
    elif change == "recipe":
        env.hpcx.write_text("# changed fake init\n")
    else:
        import clusterkit_service
        verifier = clusterkit_service.verify_preview
        future = clusterkit_service.time.time() + 1000
        monkeypatch.setattr(clusterkit_service, "verify_preview",
                            lambda token, secret, **kwargs: verifier(token, secret, now=future))
    response = env.client.post("/api/clusterkit/runs", json={
        "confirm": True, "confirmation_token": plan["confirmation_token"],
    })
    assert response.status_code == 409, response.get_json()
    assert not env.slurm.submissions


def test_duplicate_confirmation_submits_exactly_once(api_env):
    plan = preview(api_env)
    submit(api_env, plan=plan)
    response = api_env.client.post("/api/clusterkit/runs", json={
        "confirm": True, "confirmation_token": plan["confirmation_token"],
    })
    assert response.status_code == 409
    assert len(api_env.slurm.submissions) == 1


def test_uncertain_acceptance_recovers_by_activity_without_retry(api_env):
    api_env.slurm.submit_error = True
    result = submit(api_env)
    assert result["submission_state"] == "SUBMISSION_UNKNOWN"
    pending = api_env.client.get(result["status_url"]).get_json()
    assert pending["phase"] == "running" and pending["work_phase"] == "submission_unknown"
    api_env.slurm.recovered = True
    recovered = api_env.client.get(result["status_url"])
    assert recovered.status_code == 200
    assert recovered.get_json()["scheduler_job_id"] == "12345"
    assert len(api_env.slurm.submissions) == 1


@pytest.mark.parametrize("mode", ["missing", "invalid", "failed-job"])
def test_failure_evidence_cannot_become_success(api_env, monkeypatch, mode):
    result = submit(api_env)
    api_env.slurm.state = "FAILED" if mode == "failed-job" else "COMPLETED"
    if mode == "invalid":
        (Path(result["results_directory"]) / "summary.json").write_text('{"functional_status": "PASS"}')
    response = api_env.client.get(result["status_url"])
    if mode == "missing":
        assert response.get_json()["work_phase"] == "collecting"
        monkeypatch.setattr("clusterkit_service._age_seconds", lambda _: 10000)
        response = api_env.client.get(result["status_url"])
    assert response.status_code == 200
    body = response.get_json()
    assert body["phase"] == "error" and body["returncode"] == 1
    assert body["error"]
    assert len(api_env.slurm.submissions) == 1


def test_missing_activity_is_rebuilt_from_durable_submission(api_env):
    result = submit(api_env)
    api_env.registry.ACTIVITIES_FILE.unlink()
    response = api_env.client.get(result["status_url"])
    assert response.status_code == 200
    assert response.get_json()["scheduler_job_id"] == "12345"
    assert api_env.registry.get_activity(result["job_id"]) is not None
    assert len(api_env.slurm.submissions) == 1


def test_unknown_activity_and_artifact_boundaries(api_env):
    response = api_env.client.get(f"/api/clusterkit/runs/{uuid.uuid4()}")
    assert response.status_code == 404
    result = submit(api_env)
    root = Path(result["results_directory"])
    (root / "linked.json").symlink_to(api_env.hpcx)
    for name, expected in (("missing.json", 404), ("linked.json", 409), ("..%2Fhpcx-init.sh", 400)):
        response = api_env.client.get(result["status_url"] + "/artifacts/" + name)
        assert response.status_code == expected
