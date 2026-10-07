# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the /api/p2p gateway blueprint (backend/api/p2p_fleet.py)."""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from app import app
from catalog import models, seed
from catalog.context import get_active
from catalog.store import TABLE_PROJECTS, TinyDBRepo, get_repo, set_repo


@pytest.fixture
def client(tmp_path):
    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": "gb200", "RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


def _set_active_p2p(enabled: bool) -> None:
    """Flip `p2p_enabled` on the active cluster, the way the Edit Cluster dialog would."""
    repo = get_repo()
    active = get_active(repo)
    project = models.Project.from_dict(repo.get(TABLE_PROJECTS, active["project"]))
    for cluster in project.clusters:
        if cluster.id == active["cluster"]:
            cluster.p2p_enabled = enabled
    repo.upsert(TABLE_PROJECTS, project.to_dict())


def _configure_active_p2p(**overrides) -> None:
    """Save P2P settings for the active cluster (defaults + overrides), the way the settings PUT does."""
    import p2p_config

    active = get_active(get_repo())
    settings = models.P2pSettings.from_dict({"cluster_id": active["cluster"], **overrides})
    p2p_config.save_settings(settings)


class TestP2pStatus:
    def test_status_404_when_p2p_disabled(self, client):
        # Default seed leaves P2P off, so the gateway must not disclose a fleet for it.
        resp = client.get("/api/p2p/status")
        assert resp.status_code == 404
        assert resp.get_json()["ok"] is False

    def test_status_greenfield_when_enabled(self, client):
        _set_active_p2p(True)
        resp = client.get("/api/p2p/status")
        assert resp.status_code == 200
        body = resp.get_json()
        # Enabled but unprovisioned: real catalog, empty coverage, object store down.
        assert body["provisioned"] is False
        assert body["object_store_healthy"] is False
        assert body["coverage"]["nodes"] == []
        assert body["operations"] == []
        # The shipped catalog manifest is surfaced, with runtime observations blanked.
        assert isinstance(body["catalog"]["images"], list)
        assert body["catalog"]["images"], "expected the shipped catalog manifest to be non-empty"
        first = body["catalog"]["images"][0]
        assert first["canonical_present"] is False
        assert first["persistent_import"] is None
        assert {"order", "name", "filename", "bytes", "sha256"} <= set(first)

    def test_status_catalog_resolves_per_cluster(self, client, tmp_path):
        _set_active_p2p(True)
        manifest = tmp_path / "cluster-catalog.json"
        manifest.write_text(
            '{"schema_version": 1, "catalog_name": "cluster X", "images": '
            '[{"order": 1, "name": "ClusterOnly", "filename": "x.sqsh", "bytes": 5, "sha256": "ab"}]}',
            encoding="utf-8",
        )
        _configure_active_p2p(catalog_path=str(manifest))
        body = client.get("/api/p2p/status").get_json()
        # The active cluster's own manifest is surfaced, not the shipped default.
        assert [i["name"] for i in body["catalog"]["images"]] == ["ClusterOnly"]

    def test_status_catalog_falls_back_when_path_unreadable(self, client):
        _set_active_p2p(True)
        _configure_active_p2p(catalog_path="/no/such/cluster-catalog.json")
        body = client.get("/api/p2p/status").get_json()
        # A configured-but-unreadable path falls back to the shipped catalog, not an empty list.
        assert body["catalog"]["images"], "expected fallback to the shipped manifest"

    def test_status_serves_live_snapshot_when_fresh(self, client, tmp_path, monkeypatch):
        import datetime
        import json

        import p2p_snapshot

        _set_active_p2p(True)
        cluster_id = get_active(get_repo())["cluster"]
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        snap = {
            "provisioned": True,
            "object_store_healthy": True,
            "catalog": {"images": [{"name": "Live", "filename": "l.sqsh", "order": 1, "bytes": 1,
                                    "sha256": "a", "canonical_present": True, "persistent_import": None}]},
            "coverage": {"generated_at": now, "required_count": 1, "required_images": ["l.sqsh"],
                         "by_su": [], "nodes": [{"node": "n1", "su": 1}]},
            "fleet": [], "sync": None, "catalog_exact_duplicates": [],
            "preflight_generated_at": None, "operations": [], "generated_at": now,
        }
        d = tmp_path / "snaps"
        d.mkdir()
        (d / f"{cluster_id}.json").write_text(json.dumps(snap), encoding="utf-8")
        monkeypatch.setattr(p2p_snapshot, "_SNAPSHOT_DIR", d)
        p2p_snapshot.invalidate_cache()
        body = client.get("/api/p2p/status").get_json()
        # The live snapshot is served verbatim: provisioned, real coverage, its own catalog.
        assert body["provisioned"] is True
        assert body["coverage"]["nodes"][0]["node"] == "n1"
        assert [i["name"] for i in body["catalog"]["images"]] == ["Live"]

    def test_operations_400_when_settings_incomplete(self, client):
        _set_active_p2p(True)  # enabled but nothing saved: provisioning blockers remain
        resp = client.post("/api/p2p/operations", json={"action": "preflight"})
        assert resp.status_code == 400
        assert resp.get_json()["provisioning_blockers"]

    def _complete_settings(self):
        _configure_active_p2p(manager_ip="10.0.0.1", appserver_ip="10.0.0.2",
                              inventory_pattern="n[01-09]")

    def test_operations_rejects_unsupported_action(self, client):
        _set_active_p2p(True)
        self._complete_settings()
        resp = client.post("/api/p2p/operations", json={"action": "sync"})
        assert resp.status_code == 400
        assert "unsupported" in resp.get_json()["message"]

    def test_operations_requires_image_for_distribute(self, client):
        _set_active_p2p(True)
        self._complete_settings()
        resp = client.post("/api/p2p/operations", json={"action": "distribute"})
        assert resp.status_code == 400
        assert "requires at least one image" in resp.get_json()["message"]

    def test_operations_enqueues(self, client, tmp_path, monkeypatch):
        import p2p_operations

        _set_active_p2p(True)
        self._complete_settings()
        monkeypatch.setattr(p2p_operations, "_OPS_DIR", tmp_path / "ops")
        resp = client.post("/api/p2p/operations",
                           json={"action": "distribute", "images": ["nemo.sqsh"], "sus": [1]})
        assert resp.status_code == 202
        op = resp.get_json()
        assert op["action"] == "distribute" and op["state"] == "running"
        assert op["images"] == ["nemo.sqsh"] and op["id"].startswith("distribute-")
        assert "request" not in op  # the worker's private block is not leaked to the client
        cluster_id = get_active(get_repo())["cluster"]
        written = list((tmp_path / "ops" / cluster_id).glob("*.json"))
        assert len(written) == 1  # the worker has a request to claim
        import json as _json
        req = _json.loads(written[0].read_text())
        assert req["request"]["rollout"]["wave_size"] == 50  # the cluster's campaign pacing travels with it

    def test_operation_log_round_trip(self, client, tmp_path, monkeypatch):
        import p2p_operations

        _set_active_p2p(True)
        self._complete_settings()
        monkeypatch.setattr(p2p_operations, "_OPS_DIR", tmp_path / "ops")
        op_id = client.post("/api/p2p/operations", json={"action": "preflight"}).get_json()["id"]
        cluster_id = get_active(get_repo())["cluster"]
        (tmp_path / "ops" / cluster_id / f"{op_id}.log").write_text("line one\nline two\n", encoding="utf-8")
        body = client.get(f"/api/p2p/operations/{op_id}/log").get_json()
        assert body["ok"] and "line two" in body["content"]

    def test_operation_log_404_when_missing(self, client, tmp_path, monkeypatch):
        import p2p_operations

        _set_active_p2p(True)
        self._complete_settings()
        monkeypatch.setattr(p2p_operations, "_OPS_DIR", tmp_path / "ops")
        assert client.get("/api/p2p/operations/nope-123/log").status_code == 404


class TestP2pProvision:
    def test_provision_404_when_disabled(self, client):
        resp = client.post("/api/p2p/provision", json={})
        assert resp.status_code == 404

    def test_provision_refuses_incomplete_settings(self, client):
        _set_active_p2p(True)  # enabled, but addresses never configured
        resp = client.post("/api/p2p/provision", json={})
        assert resp.status_code == 400
        body = resp.get_json()
        assert any("manager_ip" in b for b in body["provisioning_blockers"])

    def test_provision_starts_the_reconcile_run(self, client, monkeypatch):
        _set_active_p2p(True)
        _configure_active_p2p(manager_ip="10.41.112.3", appserver_ip="192.168.10.4",
                              inventory_source="slurm", inventory_pattern="nc-[01-24]")
        calls = {}

        def _fake_start(workflow_id, params=None):
            calls["workflow_id"] = workflow_id
            calls["params"] = params
            return {"run_id": "run-abc", "state": "pending"}

        import workflow_engine

        monkeypatch.setattr(workflow_engine, "start_workflow_run", _fake_start)
        resp = client.post("/api/p2p/provision", json={})
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["ok"] is True
        assert body["run_id"] == "run-abc"
        assert body["poll"] == "/api/workflow-runs/run-abc"
        # It triggers the shipped reconcile workflow, scoped to the active cluster.
        assert calls["workflow_id"] == "nvis-p2p-provision"
        assert calls["params"]["cluster_id"]

    def test_reconcile_workflow_is_shipped_and_wellformed(self):
        import workflow_engine

        wf = workflow_engine.get_workflow("nvis-p2p-provision")
        assert wf is not None, "nvis-p2p-provision.yaml must ship in var/workflows/"
        step_ids = [s.get("id") for s in wf.get("steps", [])]
        assert "preflight" in step_ids
        # The first mutating work must sit behind the read-only preflight.
        assert step_ids[0] == "start" and "preflight" in step_ids


class TestP2pSettings:
    def test_get_returns_defaults_with_blockers_when_unset(self, client):
        _set_active_p2p(True)
        resp = client.get("/api/p2p/settings")
        assert resp.status_code == 200
        body = resp.get_json()
        # Sensible defaults, addresses blank, and the blockers name what provisioning still needs.
        assert body["install_root"] == "/cluster/nvis-p2p"
        assert body["manager_ip"] == ""
        assert any("manager_ip" in b for b in body["provisioning_blockers"])

    def test_put_then_get_round_trips(self, client):
        _set_active_p2p(True)
        put = client.put(
            "/api/p2p/settings",
            json={"manager_ip": "10.41.112.3", "appserver_ip": "192.168.10.4",
                  "inventory_source": "slurm", "inventory_pattern": "nc-[01-24]"},
        )
        assert put.status_code == 200
        assert put.get_json()["provisioning_blockers"] == []
        got = client.get("/api/p2p/settings").get_json()
        assert got["manager_ip"] == "10.41.112.3"
        assert got["appserver_ip"] == "192.168.10.4"

    def test_settings_404_when_disabled(self, client):
        assert client.get("/api/p2p/settings").status_code == 404
        assert client.put("/api/p2p/settings", json={}).status_code == 404


class TestP2pSettingsModel:
    def test_round_trip_and_render_env(self):
        s = models.P2pSettings.from_dict(
            {"cluster_id": "c1", "manager_ip": "10.0.0.1", "appserver_ip": "10.0.0.2", "service_uid": 4200}
        )
        d = s.to_dict()
        assert d["id"] == "c1" and d["cluster_id"] == "c1"
        assert models.P2pSettings.from_dict(d).service_uid == 4200
        env = s.to_render_env()
        # The document maps onto exactly the NVIS_* vars render-site-config.py requires.
        assert env["NVIS_MANAGER_IP"] == "10.0.0.1"
        assert env["NVIS_SERVICE_UID"] == "4200"
        # Secrets are file paths derived under install_root, never values in the document.
        assert env["NVIS_POSTGRES_PASSWORD_FILE"].endswith("/secrets/postgres-password")
        assert not any("password" in k.lower() and not k.endswith("_FILE") for k in env)

    def test_blockers_and_defaults(self):
        assert models.P2pSettings(cluster_id="c1").provisioning_blockers()  # addresses blank
        ready = models.P2pSettings(cluster_id="c1", manager_ip="1.1.1.1", appserver_ip="1.1.1.2",
                                   inventory_source="slurm", inventory_pattern="n[01-09]")
        assert ready.provisioning_blockers() == []

    def test_rollout_campaign_defaults_round_trip(self):
        s = models.P2pSettings(cluster_id="c1")
        assert s.rollout_wave_size == 50 and s.rollout_parallelism == 48
        assert s.rollout_poll_seconds == 60 and s.rollout_max_launches == 3
        assert s.rollout_max_unreachable == 3 and s.rollout_timeout_seconds == 1800
        custom = models.P2pSettings.from_dict(
            {"cluster_id": "c1", "rollout_wave_size": 25, "rollout_parallelism": 100,
             "rollout_poll_seconds": 20, "rollout_timeout_seconds": 3600}
        )
        back = models.P2pSettings.from_dict(custom.to_dict())
        assert back.rollout_wave_size == 25 and back.rollout_parallelism == 100
        assert back.rollout_poll_seconds == 20 and back.rollout_timeout_seconds == 3600
        # Runtime pacing is not a render input -- it must not leak into the NVIS_* env.
        assert not any("ROLLOUT" in k for k in custom.to_render_env())

    def test_rollout_validation_bounds(self):
        assert models.P2pSettings(cluster_id="c1").validate() == []
        over = models.P2pSettings(cluster_id="c1", rollout_parallelism=200)
        assert any("rollout_parallelism" in p for p in over.validate())
        zero = models.P2pSettings(cluster_id="c1", rollout_wave_size=0, rollout_poll_seconds=0)
        probs = zero.validate()
        assert any("rollout_wave_size" in p for p in probs)
        assert any("rollout_poll_seconds" in p for p in probs)


class TestP2pSnapshot:
    def _fresh(self, **extra):
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        base = {"provisioned": True, "object_store_healthy": True,
                "catalog": {"images": []}, "coverage": {"nodes": []}, "generated_at": now}
        base.update(extra)
        return base

    def _write(self, directory, cluster_id, snap):
        import json

        directory.mkdir(exist_ok=True)
        (directory / f"{cluster_id}.json").write_text(json.dumps(snap), encoding="utf-8")

    def test_fresh_snapshot_loads(self, tmp_path, monkeypatch):
        import p2p_snapshot

        d = tmp_path / "snaps"
        self._write(d, "c1", self._fresh(marker="live"))
        monkeypatch.setattr(p2p_snapshot, "_SNAPSHOT_DIR", d)
        p2p_snapshot.invalidate_cache()
        assert p2p_snapshot.load_snapshot("c1")["marker"] == "live"

    def test_absent_snapshot_is_none(self, tmp_path, monkeypatch):
        import p2p_snapshot

        monkeypatch.setattr(p2p_snapshot, "_SNAPSHOT_DIR", tmp_path / "nope")
        p2p_snapshot.invalidate_cache()
        assert p2p_snapshot.load_snapshot("c1") is None

    def test_stale_snapshot_is_none(self, tmp_path, monkeypatch):
        import p2p_snapshot

        d = tmp_path / "snaps"
        self._write(d, "c1", self._fresh(generated_at="2000-01-01T00:00:00Z"))
        monkeypatch.setattr(p2p_snapshot, "_SNAPSHOT_DIR", d)
        p2p_snapshot.invalidate_cache()
        assert p2p_snapshot.load_snapshot("c1") is None

    def test_malformed_snapshot_is_none(self, tmp_path, monkeypatch):
        import p2p_snapshot

        d = tmp_path / "snaps"
        d.mkdir()
        (d / "c1.json").write_text('{"partial": true}', encoding="utf-8")  # missing required keys
        monkeypatch.setattr(p2p_snapshot, "_SNAPSHOT_DIR", d)
        p2p_snapshot.invalidate_cache()
        assert p2p_snapshot.load_snapshot("c1") is None


class TestP2pCatalogModel:
    def test_round_trip(self):
        c = models.P2pCatalog.from_dict({"cluster_id": "c1", "images": [
            {"order": 1, "name": "NeMo", "filename": "nemo.sqsh", "bytes": 10, "sha256": "a" * 64}]})
        d = c.to_dict()
        assert d["id"] == "c1"
        back = models.P2pCatalog.from_dict(d)
        assert back.images[0].filename == "nemo.sqsh" and back.validate() == []

    def test_validation(self):
        bad = models.P2pCatalog.from_dict({"cluster_id": "c1", "images": [
            {"name": "x", "filename": "../evil", "sha256": "zz"},
            {"name": "", "filename": "ok.sqsh"},
            {"name": "d1", "filename": "dup.sqsh"}, {"name": "d2", "filename": "dup.sqsh"}]})
        probs = bad.validate()
        assert any("bare *.sqsh" in p for p in probs)
        assert any("64 hex" in p for p in probs)
        assert any("name is required" in p for p in probs)
        assert any("duplicate" in p for p in probs)


class TestP2pCatalogApi:
    def test_get_seeds_from_shipped_when_none_authored(self, client):
        _set_active_p2p(True)
        body = client.get("/api/p2p/catalog").get_json()
        assert body["ok"] and body["authored"] is False and body["images"]

    def test_put_then_get_and_status_reflect_it(self, client):
        _set_active_p2p(True)
        imgs = [{"order": 1, "name": "Only", "filename": "only.sqsh", "bytes": 5, "sha256": "b" * 64}]
        put = client.put("/api/p2p/catalog", json={"images": imgs})
        assert put.status_code == 200 and put.get_json()["authored"] is True
        got = client.get("/api/p2p/catalog").get_json()
        assert got["authored"] is True and [i["filename"] for i in got["images"]] == ["only.sqsh"]
        # The authored catalog is what the fleet status surfaces, even unprovisioned.
        status = client.get("/api/p2p/status").get_json()
        assert [i["name"] for i in status["catalog"]["images"]] == ["Only"]

    def test_put_rejects_invalid(self, client):
        _set_active_p2p(True)
        bad = client.put("/api/p2p/catalog",
                         json={"images": [{"name": "x", "filename": "x.sqsh", "sha256": "nope"}]})
        assert bad.status_code == 400 and "64 hex" in bad.get_json()["message"]


class TestP2pProvisionWorkflow:
    def test_native_stage_step_targets_the_app_server_current_dir(self, monkeypatch):
        import host_exec
        import workflow_engine

        # Registered at import of p2p_provision (via api.p2p_fleet).
        assert "p2p_stage_assets" in workflow_engine._NATIVE_STEPS
        assert "p2p_run_on_appserver" in workflow_engine._NATIVE_STEPS

        seen = {}

        def _fake_copy(local, remote, *, conn=None, **kw):
            seen["local"] = str(local)
            seen["remote"] = remote
            seen["has_conn"] = conn is not None

        monkeypatch.setattr(host_exec, "copy_tree_to_host", _fake_copy)
        fn = workflow_engine._NATIVE_STEPS["p2p_stage_assets"]
        run = {"run_id": "r1", "params": {"NVIS_INSTALL_ROOT": "/cluster/nvis-p2p", "P2P_APPSERVER_HOST": "10.10.20.22"}}
        ok, output = fn(run, {"id": "stage-assets"})
        assert ok, output
        # Staged under {install_root}/current (what the rendered compose references), over an
        # explicit connection to the app server -- not the default control connection.
        assert seen["remote"] == "/cluster/nvis-p2p/current"
        assert seen["has_conn"] is True
        assert seen["local"].endswith("deploy/p2p")

    def test_stage_refuses_without_an_app_server_host(self):
        import workflow_engine

        fn = workflow_engine._NATIVE_STEPS["p2p_stage_assets"]
        ok, output = fn({"run_id": "r1", "params": {"NVIS_INSTALL_ROOT": "/x"}}, {"id": "s"})
        assert ok is False and "P2P_APPSERVER_HOST" in output

    def test_run_on_appserver_refuses_without_a_host(self):
        import workflow_engine

        fn = workflow_engine._NATIVE_STEPS["p2p_run_on_appserver"]
        ok, output = fn({"run_id": "r1", "params": {}}, {"id": "x", "run": "echo hi"})
        assert ok is False and "P2P_APPSERVER_HOST" in output

    def test_native_dispatch_reports_an_unknown_step(self):
        import workflow_engine

        ok, output = workflow_engine._execute_native_step(
            {"run_id": "r1", "params": {}}, {"id": "x", "native": "no_such_native"}
        )
        assert ok is False
        assert "no native step registered" in output

    def test_reconcile_workflow_runs_every_step_on_the_app_server(self):
        import workflow_engine

        wf = workflow_engine.get_workflow("nvis-p2p-provision")
        steps = {s["id"]: s for s in wf.get("steps", [])}
        step_ids = list(steps)
        # Read-only preflight first, staging before render, secrets before render, bring-up last.
        for expected in ("start", "preflight", "stage-assets", "secrets", "render", "bring-up", "done"):
            assert expected in step_ids, f"missing step {expected}"
        assert step_ids.index("secrets") < step_ids.index("render") < step_ids.index("bring-up")
        assert steps["stage-assets"]["native"] == "p2p_stage_assets"
        # Every executing step runs on the app server, not the default control connection.
        for sid in ("preflight", "secrets", "render", "bring-up"):
            assert steps[sid].get("native") == "p2p_run_on_appserver", sid


class TestP2pSettingsContract:
    """The model must match render-site-config.py's real contract (learned from the NC dry-run)."""

    def test_catalog_path_defaults_to_a_json_file(self):
        env = models.P2pSettings(cluster_id="c1").to_render_env()
        assert env["NVIS_CATALOG_PATH"].endswith(".json"), env["NVIS_CATALOG_PATH"]

    def test_inventory_source_is_an_absolute_path(self):
        # Blank -> a default under install_root the reconcile writes; the renderer needs a path.
        s = models.P2pSettings(cluster_id="c1", install_root="/cluster/nvis-p2p")
        src = s.to_render_env()["NVIS_INVENTORY_SOURCE"]
        assert src.startswith("/cluster/nvis-p2p/") and " " not in src
        # An explicit path is honoured.
        s2 = models.P2pSettings(cluster_id="c1", inventory_source="/data/nodes.txt")
        assert s2.to_render_env()["NVIS_INVENTORY_SOURCE"] == "/data/nodes.txt"

    def test_inventory_source_is_not_a_provisioning_blocker_but_pattern_is(self):
        s = models.P2pSettings(cluster_id="c1", manager_ip="1.1.1.1", appserver_ip="1.1.1.2")
        # pattern missing -> blocked; source missing -> not (it defaults)
        assert any("inventory_pattern" in b for b in s.provisioning_blockers())
        assert not any("inventory_source" in b for b in s.provisioning_blockers())

    def test_appserver_connection_falls_back_to_appserver_ip(self):
        s = models.P2pSettings(cluster_id="c1", appserver_ip="10.10.20.22")
        conn = s.appserver_connection()
        assert conn == {"host": "10.10.20.22", "user": "root", "key": ""}
        s2 = models.P2pSettings(
            cluster_id="c1", appserver_ip="10.10.20.22",
            appserver_ssh_host="mgmt.example", appserver_ssh_user="svc", appserver_ssh_key="/k",
        )
        assert s2.appserver_connection() == {"host": "mgmt.example", "user": "svc", "key": "/k"}

    def test_ssh_fields_round_trip(self):
        d = models.P2pSettings(cluster_id="c1", appserver_ssh_user="svc", appserver_ssh_key="/k").to_dict()
        back = models.P2pSettings.from_dict(d)
        assert back.appserver_ssh_user == "svc" and back.appserver_ssh_key == "/k"
