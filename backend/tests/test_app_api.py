# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for Flask API endpoints."""
import os
import threading
import time
from pathlib import Path

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

# Activity hydration moved out of app.py; patch the module that owns these names.
import activity_hydration
import app as app_module
import rack_locks
import rack_status_engine
import validation_activities
from api import dashboard, validation_runs
from app import app


@pytest.fixture
def isolated_validation_activities(tmp_path, monkeypatch):
    """Redirect validation activity store for API tests."""
    activities = tmp_path / "validation_activities.json"
    legacy = tmp_path / "multirack_jobs.json"
    archive = tmp_path / "validation_activities.sqlite3"
    monkeypatch.setattr(validation_activities, "ACTIVITIES_FILE", activities)
    monkeypatch.setattr(validation_activities, "LEGACY_JOBS_FILE", legacy)
    monkeypatch.setattr(validation_activities, "ACTIVITY_ARCHIVE_DB", archive)
    monkeypatch.setattr(validation_activities, "_archive_sync_signature", None)
    yield activities


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture
def log_dir(tmp_path):
    """An existing, empty directory for endpoints that validate ``log_dir``.

    Demo mode synthesises the rows, so the *contents* are irrelevant — but the
    path has to resolve to a real directory. This replaces a literal
    ``"var/logs/"``, which was relative and therefore only resolved when pytest
    happened to be invoked from the repository root.
    """
    d = tmp_path / "logs"
    d.mkdir()
    return str(d)


@pytest.fixture
def isolated_locks_file(tmp_path, monkeypatch):
    """Redirect the claims file so rack-locks tests don't leak into ./var/cache."""
    monkeypatch.setattr(rack_locks, "CLAIMS_FILE", tmp_path / "rack_locks.json")
    yield


class TestAuthEndpoints:
    def test_auth_check_when_disabled(self, client):
        resp = client.get("/api/auth-check")
        assert resp.status_code == 200

    def test_the_legacy_login_route_is_gone(self, client):
        """FR-18. Replaces a test that asserted `POST /api/login` returned 200 with `ok: true`.

        That behaviour is exactly what had to go: after the gate moved to tokens the route still
        succeeded and set a session nothing honoured, so a caller was told it had authenticated and
        then refused on every subsequent request.

        **405, not 404, and the reason is worth knowing.** With the rule deleted the path falls
        through to the SPA catch-all, which is GET-only — so a POST is "method not allowed" on that
        catch-all rather than "no such path". A GET does return a 404 with a JSON body, because the
        catch-all deliberately refuses to serve the HTML shell for anything under `/api/`. Either
        way there is no credential path here; asserting the exact code pins which refusal arrives,
        so a future routing change that resurrects a 200 fails loudly.
        """
        assert client.post("/api/login", json={"password": "anything"}).status_code == 405
        get = client.get("/api/login")
        assert get.status_code == 404
        assert get.headers.get("Content-Type", "").startswith("application/json")


class TestStatusEndpoint:
    def test_demo_status(self, client, log_dir):
        resp = client.get("/api/status", query_string={"log_dir": log_dir})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["rows"]) == 18
        assert "summary" in data
        assert "comments" in data

    def test_status_has_native_workers(self, client, log_dir):
        resp = client.get("/api/status", query_string={"log_dir": log_dir})
        data = resp.get_json()
        assert data["native_workers"]["mode"] == "demo"


class TestSharedClusterSnapshotAPI:
    @pytest.fixture(autouse=True)
    def isolate_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RACK_CLUSTER_SNAPSHOT_DB", str(tmp_path / "snapshots.sqlite3"))
        monkeypatch.setenv("RACK_STATUS_BACKGROUND_DISABLED", "1")
        monkeypatch.setenv("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES", "1")
        # Exercise real-cluster non-blocking wait=False (module default is DEMO=1).
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.setattr(app_module.nvftp_cache_sync, "ensure_background_thread", lambda: None)
        monkeypatch.setattr(dashboard, "_start_cmsh_rack_list_warm_thread", lambda: None)
        monkeypatch.setattr(dashboard, "_resolve_log_dir", lambda _raw: Path("/logs"))
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block",
            lambda: {"ok": True, "stdout": ""},
        )
        # Avoid ~1.5s filesystem reads on every _eget during API shell assembly.
        monkeypatch.setattr(app_module, "_job_env_overrides_load", dict)
        # Local mode reachability is a no-op; do not stub it so SSH-mode regressions surface.

    @staticmethod
    def inventory():
        return {
            "ok": True,
            "returncode": 0,
            "updated_at": "2026-07-22T15:00:00+00:00",
            "log_dir": "/logs",
            "script": "collector",
            "stderr": "",
            "legend": "",
            "rows": [
                {
                    "rack": "p5-r01",
                    "nemo": "done",
                    "last_job": "1",
                    "sinfo": "idle=16",
                    "ct_down_unk": "",
                }
            ],
            "summary": {"done": 1, "fail": 0, "ongoing": 0, "pending": 0},
            "footnotes": [],
            "comments": {},
            "physical_rf0": {f"node-{i:02d}": f"10.0.0.{i + 1}" for i in range(16)},
            "native_workers": {"mode": "test"},
        }

    @staticmethod
    def _poll_json(client, route, *, want_state, timeout=5.0):
        deadline = time.monotonic() + timeout
        body = None
        while time.monotonic() < deadline:
            body = client.get(f"{route}?log_dir=/logs").get_json()
            if (body or {}).get("snapshot", {}).get("state") == want_state:
                return body
            time.sleep(0.05)
        assert body is not None
        assert body["snapshot"]["state"] == want_state
        return body

    @pytest.mark.parametrize("first_route", ["/api/status", "/api/cluster-overview"])
    def test_both_route_orders_share_snapshot_id_and_counts(
        self, client, monkeypatch, first_route
    ):
        calls = 0

        def collect(_log_dir, include_raw=False):
            nonlocal calls
            calls += 1
            return self.inventory()

        monkeypatch.setattr(rack_status_engine, "run_rack_status", collect)
        # Kick refresh on the first route, then poll both until fresh so async
        # wait=False responses still share one snapshot id / inventory counts.
        client.get(f"{first_route}?log_dir=/logs")
        second_route = (
            "/api/cluster-overview" if first_route == "/api/status" else "/api/status"
        )
        first = self._poll_json(client, first_route, want_state="fresh")
        second = self._poll_json(client, second_route, want_state="fresh")
        status = first if first_route == "/api/status" else second
        overview = second if first_route == "/api/status" else first

        assert status["snapshot"]["id"] == overview["snapshot"]["id"]
        assert status["snapshot"]["state"] == overview["snapshot"]["state"] == "fresh"
        assert len(status["rows"]) == overview["rack_count"] == 1
        assert len(status["physical_rf0"]) == overview["tray_hosts"] == 16
        assert overview["status_updated_at"] == status["updated_at"]
        assert calls == 1

    def test_failed_inventory_is_structured_unavailable_not_zero(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            rack_status_engine,
            "run_rack_status",
            lambda *_args, **_kwargs: {
                "ok": False,
                "returncode": 1,
                "error": "inventory failed",
            },
        )

        client.get("/api/cluster-overview?log_dir=/logs")
        overview = self._poll_json(client, "/api/cluster-overview", want_state="failed")
        status = self._poll_json(client, "/api/status", want_state="failed")

        assert overview["ok"] is True
        assert overview["snapshot"]["state"] == "failed"
        assert overview["rack_count"] is None
        assert overview["tray_hosts"] is None
        assert overview["validation_summary"] is None
        assert status["snapshot"]["state"] == "failed"
        assert status["updated_at"] is None

    def test_confirmed_empty_inventory_returns_real_zero(self, client, monkeypatch):
        empty = self.inventory()
        empty.update(
            rows=[],
            physical_rf0={},
            summary={"done": 0, "fail": 0, "ongoing": 0, "pending": 0},
        )
        monkeypatch.setattr(rack_status_engine, "run_rack_status", lambda *_args, **_kwargs: empty)

        client.get("/api/cluster-overview?log_dir=/logs")
        overview = self._poll_json(client, "/api/cluster-overview", want_state="fresh")
        assert overview["snapshot"]["state"] == "fresh"
        assert overview["rack_count"] == 0
        assert overview["tray_hosts"] == 0

    @pytest.mark.parametrize("nodes, expected", [
        ([{"id": "GPU01", "rack": None, "status": "up", "ip": ""}], 1),
        ([], 0),
        (None, None),
    ])
    def test_node_roster_counts_without_racks_or_rf0(self, client, monkeypatch, nodes, expected):
        payload = self.inventory()
        # A stale management map must not override an authoritative empty/null roster.
        payload.update(rows=[], nodes=nodes, node_inventory_error="unavailable" if nodes is None else "")
        monkeypatch.setattr(rack_status_engine, "run_rack_status", lambda *_a, **_kw: payload)
        client.get("/api/status?log_dir=/logs")
        status = self._poll_json(client, "/api/status", want_state="fresh")
        overview = self._poll_json(client, "/api/cluster-overview", want_state="fresh")
        assert status["nodes"] == nodes
        assert overview["rack_count"] == 0
        assert overview["tray_hosts"] == expected
        assert overview["snapshot"]["id"] == status["snapshot"]["id"]
        if nodes:
            assert overview["cuda_nccl_build_host_candidates"] == ["GPU01"]

    def test_overview_counts_platform_gpus_per_node(self, client, monkeypatch):
        payload = self.inventory()
        payload.update(
            rows=[],
            nodes=[
                {"id": "GPU01", "rack": None, "status": "up", "ip": ""},
                {"id": "GPU02", "rack": None, "status": "up", "ip": ""},
            ],
        )
        monkeypatch.setattr(rack_status_engine, "run_rack_status", lambda *_a, **_kw: payload)
        monkeypatch.setattr(
            dashboard,
            "_eget_int",
            lambda name, default: {"GPUS_PER_NODE": 8, "RACK_CLUSTER_GPU_PER_TRAY": 2}.get(
                name, default
            ),
        )

        client.get("/api/cluster-overview?log_dir=/logs")
        overview = self._poll_json(client, "/api/cluster-overview", want_state="fresh")

        assert overview["tray_hosts"] == 2
        assert overview["estimated_gpu_count"] == 16

    def test_cold_overview_returns_metadata_while_initializing(self, client, monkeypatch):
        gate = threading.Event()
        probe_calls = {"slurm": 0, "bcm": 0, "squeue": 0}

        def slow_collect(_log_dir, include_raw=False):
            assert gate.wait(10)
            return self.inventory()

        monkeypatch.delenv("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES", raising=False)
        monkeypatch.setattr(rack_status_engine, "run_rack_status", slow_collect)

        def count_slurm():
            probe_calls["slurm"] += 1
            return ("1.0", None)

        def count_bcm():
            probe_calls["bcm"] += 1
            return ("10.0", None)

        def count_squeue():
            probe_calls["squeue"] += 1
            return {"ok": True, "stdout": ""}

        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", count_slurm)
        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", count_bcm)
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block", count_squeue)

        try:
            t0 = time.monotonic()
            res = client.get("/api/cluster-overview?log_dir=/logs")
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0
            assert not gate.is_set()
            body = res.get_json()
            assert body["ok"] is True
            assert body["snapshot"]["state"] == "initializing"
            assert body["rack_count"] is None
            assert body["tray_hosts"] is None
            assert body["validation_summary"] is None
            assert body["cluster_name"]
            assert probe_calls == {"slurm": 0, "bcm": 0, "squeue": 0}
        finally:
            gate.set()

    def test_cold_status_returns_initializing_quickly(self, client, monkeypatch):
        gate = threading.Event()

        def slow_collect(_log_dir, include_raw=False):
            assert gate.wait(10)
            return self.inventory()

        monkeypatch.setattr(rack_status_engine, "run_rack_status", slow_collect)
        try:
            t0 = time.monotonic()
            res = client.get("/api/status?log_dir=/logs")
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0
            assert not gate.is_set()
            body = res.get_json()
            assert body["ok"] is True
            assert body["snapshot"]["state"] == "initializing"
            assert body["updated_at"] is None
            assert body["rows"] == []
        finally:
            gate.set()

    def test_canonical_collect_does_not_rebuild_via_live_merge(self, monkeypatch):
        merge_calls = 0

        def collect(_log_dir, include_raw=False):
            return self.inventory()

        def merge(_out, _log_dir):
            nonlocal merge_calls
            merge_calls += 1

        monkeypatch.setattr(rack_status_engine, "run_rack_status", collect)
        monkeypatch.setattr(dashboard, "_merge_live_native_workers_into_status", merge)
        payload = rack_status_engine._collect_canonical_rack_status(Path("/logs"))
        assert payload["ok"] is True
        assert merge_calls == 0

    def test_overview_never_runs_live_probes_with_fresh_snapshot(self, client, monkeypatch):
        """Fresh inventory must not block on BCM/Slurm/queue or SSH reachability probes."""
        probe_calls = {"bcm": 0, "slurm": 0, "queue": 0}
        reach_probe_calls = {"n": 0}
        monkeypatch.setattr(
            rack_status_engine, "run_rack_status", lambda *_a, **_k: self.inventory()
        )

        # Seed a fresh shared snapshot while probes are still fixture-disabled.
        client.get("/api/cluster-overview?log_dir=/logs")
        seeded = self._poll_json(client, "/api/cluster-overview", want_state="fresh")
        assert seeded["snapshot"]["state"] == "fresh"

        def block_bcm():
            probe_calls["bcm"] += 1
            time.sleep(5)
            return ("BCM blocked", "")

        def block_slurm():
            probe_calls["slurm"] += 1
            time.sleep(5)
            return ("slurm blocked", "")

        def block_queue():
            probe_calls["queue"] += 1
            time.sleep(5)
            return {"ok": True, "stdout": "JOBID"}

        def boom_reach_probe():
            reach_probe_calls["n"] += 1
            time.sleep(5)
            return False, "ssh reachability must not run from base overview"

        monkeypatch.delenv("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES", raising=False)
        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", block_bcm)
        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", block_slurm)
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block", block_queue)

        he = app_module.host_exec
        # Nested context restores env + every setattr'd global on exit so later
        # tests re-read local mode (plain assignments would leave ssh init globals).
        with monkeypatch.context() as m:
            m.setenv("RACK_EXEC_MODE", "ssh")
            m.setenv("RACK_SSH_HOST", "10.0.0.1")
            m.setenv("RACK_SSH_USER", "root")
            m.setenv("RACK_SSH_REACHABILITY_TTL_SEC", "15")
            m.setattr(he, "_CONN", None)
            m.setattr(he, "_CONTROL_SSH", None)
            m.setattr(he, "_CONFIG", None)
            m.setattr(he, "_reach_ok", None)
            m.setattr(he, "_reach_detail", "")
            m.setattr(he, "_reach_checked_at", 0.0)
            m.setattr(he, "_reach_wall_at", 0.0)
            m.setattr(he, "_probe_reachable", boom_reach_probe)

            started = time.monotonic()
            body = client.get("/api/cluster-overview?log_dir=/logs").get_json()
            assert time.monotonic() - started < 1.0
            assert body["cluster_name"]
            assert body["snapshot"]["state"] == "fresh"
            assert body["rack_count"] == 1
            assert body["tray_hosts"] == 16
            assert "squeue" in body
            assert body["squeue"]["ok"] is False
            assert "live details" in (body["squeue"].get("error") or "").lower()
            assert probe_calls == {"bcm": 0, "slurm": 0, "queue": 0}
            assert reach_probe_calls["n"] == 0
            # Unknown cached reachability must not invent a down/up result.
            assert body.get("cluster_reachable") is None
            assert body.get("exec_mode") == "ssh"

            # Stale cached value is returned as-is without re-probing.
            m.setattr(he, "_reach_ok", True)
            m.setattr(he, "_reach_detail", "")
            m.setattr(he, "_reach_checked_at", time.monotonic() - 10_000)
            started = time.monotonic()
            warm = client.get("/api/cluster-overview?log_dir=/logs").get_json()
            assert time.monotonic() - started < 1.0
            assert reach_probe_calls["n"] == 0
            assert warm.get("cluster_reachable") is True

        # Context exited: env is local again. Clear any ensure_init values written
        # after monkeypatch saved the originals, then prove local re-init.
        m_outer = monkeypatch
        m_outer.setattr(he, "_CONN", None)
        m_outer.setattr(he, "_CONTROL_SSH", None)
        m_outer.setattr(he, "_CONFIG", None)
        m_outer.setattr(he, "_reach_ok", None)
        m_outer.setattr(he, "_reach_detail", "")
        m_outer.setattr(he, "_reach_checked_at", 0.0)
        m_outer.setattr(he, "_reach_wall_at", 0.0)
        assert (os.environ.get("RACK_EXEC_MODE") or "local").strip().lower() == "local"
        assert he.exec_kind() == "local"

    def test_host_exec_is_local_after_ssh_cold_overview(self, client, monkeypatch):
        """Order-sensitive guard: SSH-cold overview must not leak ssh init globals.

        Run after ``test_overview_never_runs_live_probes_with_fresh_snapshot``. With
        plain host_exec global assignments, ssh init globals can leak even though
        monkeypatch restored ``RACK_EXEC_MODE=local``.
        """
        assert (os.environ.get("RACK_EXEC_MODE") or "local").strip().lower() == "local"
        # Do not clear host_exec caches here — that would hide the leak.
        assert app_module.host_exec.exec_kind() == "local"


class TestClusterOverviewLiveAPI:
    """Independent /api/cluster-overview/live progressive probe endpoint."""

    @pytest.fixture(autouse=True)
    def isolate_live(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RACK_CLUSTER_SNAPSHOT_DB", str(tmp_path / "snapshots.sqlite3"))
        monkeypatch.setenv(
            "RACK_CLUSTER_OVERVIEW_LIVE_DB", str(tmp_path / "cluster_overview_live.sqlite3")
        )
        monkeypatch.setenv("RACK_STATUS_BACKGROUND_DISABLED", "1")
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.delenv("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES", raising=False)
        monkeypatch.setattr(app_module.nvftp_cache_sync, "ensure_background_thread", lambda: None)
        monkeypatch.setattr(dashboard, "_start_cmsh_rack_list_warm_thread", lambda: None)
        monkeypatch.setattr(dashboard, "_resolve_log_dir", lambda _raw: Path("/logs"))
        monkeypatch.setattr(app_module.host_exec, "reachability_state", lambda: (True, ""))
        monkeypatch.setattr(app_module, "_job_env_overrides_load", dict)
        # Force a fresh live-service singleton for this DB path.
        app_module._cluster_overview_live_service_cache = None
        yield
        app_module._cluster_overview_live_service_cache = None

    @staticmethod
    def _poll_live(client, *, want_state, section="bcm", timeout=5.0, refresh=False):
        """Poll until EVERY requested section reaches *want_state*.

        ``section`` accepts one name or several. The bcm/slurm/queue collectors run in
        independent threads and finish in any order, so waiting on one and then asserting on
        the others is a race — pass every section you are about to assert on.
        """
        wanted = (section,) if isinstance(section, str) else tuple(section)
        deadline = time.monotonic() + timeout
        body = None
        q = "?refresh=1" if refresh else ""
        while time.monotonic() < deadline:
            body = client.get(f"/api/cluster-overview/live{q}").get_json()
            secs = ((body or {}).get("sections") or {})
            if all(((secs.get(s) or {}).get("state") == want_state) for s in wanted):
                return body
            time.sleep(0.05)
        assert body is not None
        for s in wanted:
            assert body["sections"][s]["state"] == want_state, f"section {s!r} never reached {want_state!r}"
        return body

    def test_overview_live_returns_initializing_then_fresh(self, client, monkeypatch):
        started = {"bcm": threading.Event(), "slurm": threading.Event(), "queue": threading.Event()}
        release = threading.Event()

        def collect_bcm():
            started["bcm"].set()
            assert release.wait(5)
            return ("BCM 11.0", "")

        def collect_slurm():
            started["slurm"].set()
            assert release.wait(5)
            return ("slurm 25.05.0", "")

        def collect_queue():
            started["queue"].set()
            assert release.wait(5)
            return {"ok": True, "stdout": "JOBID NAME\n1 x", "stderr": "", "returncode": 0}

        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", collect_slurm)
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block", collect_queue)

        try:
            t0 = time.monotonic()
            cold = client.get("/api/cluster-overview/live").get_json()
            assert time.monotonic() - t0 < 1.0
            assert cold["ok"] is True
            assert cold["reachable"] is True
            assert cold["sections"]["bcm"]["state"] == "initializing"
            assert cold["sections"]["slurm"]["state"] == "initializing"
            assert cold["sections"]["queue"]["state"] == "initializing"
            assert started["bcm"].wait(1)
            assert started["slurm"].wait(1)
            assert started["queue"].wait(1)
        finally:
            release.set()

        # Wait for all three, not just bcm: they are collected by independent threads and can
        # go fresh in any order, so polling bcm alone then asserting on slurm/queue is a race.
        fresh = self._poll_live(client, want_state="fresh", section=("bcm", "slurm", "queue"))
        assert fresh["sections"]["bcm"]["value"] == "BCM 11.0"
        assert fresh["sections"]["slurm"]["value"] == "slurm 25.05.0"
        assert fresh["sections"]["queue"]["value"]["ok"] is True

        # Cached re-read is immediate and stays fresh.
        t1 = time.monotonic()
        cached = client.get("/api/cluster-overview/live").get_json()
        assert time.monotonic() - t1 < 1.0
        assert cached["sections"]["bcm"]["state"] == "fresh"
        assert cached["sections"]["bcm"]["value"] == "BCM 11.0"

    def test_overview_live_refresh_forces_recollect(self, client, monkeypatch):
        values = {"bcm": "BCM first"}

        def collect_bcm():
            return (values["bcm"], "")

        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block",
            lambda: {"ok": True, "stdout": "", "stderr": "", "returncode": 0},
        )

        self._poll_live(client, want_state="fresh", section="bcm")
        values["bcm"] = "BCM refreshed"
        client.get("/api/cluster-overview/live?refresh=1")
        deadline = time.monotonic() + 5.0
        body = None
        while time.monotonic() < deadline:
            body = client.get("/api/cluster-overview/live").get_json()
            if ((body or {}).get("sections") or {}).get("bcm", {}).get("value") == "BCM refreshed":
                break
            time.sleep(0.05)
        assert body is not None
        assert body["sections"]["bcm"]["value"] == "BCM refreshed"
        assert body["sections"]["bcm"]["state"] == "fresh"

    def test_overview_live_unreachable_returns_cached_without_refresh(
        self, client, monkeypatch
    ):
        calls = {"bcm": 0}

        def collect_bcm():
            calls["bcm"] += 1
            return ("BCM cached", "")

        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block",
            lambda: {"ok": True, "stdout": "", "stderr": "", "returncode": 0},
        )

        self._poll_live(client, want_state="fresh", section="bcm")
        assert calls["bcm"] >= 1
        baseline = calls["bcm"]

        monkeypatch.setattr(
            app_module.host_exec,
            "reachability_state",
            lambda: (False, "vpn down"),
        )
        t0 = time.monotonic()
        body = client.get("/api/cluster-overview/live?refresh=1").get_json()
        assert time.monotonic() - t0 < 1.0
        assert body["ok"] is True
        assert body["reachable"] is False
        assert "vpn" in (body.get("unreachable_detail") or "").lower()
        assert body["sections"]["bcm"]["state"] == "fresh"
        assert body["sections"]["bcm"]["value"] == "BCM cached"
        assert calls["bcm"] == baseline

    def test_overview_live_sanitizes_collector_errors(self, client, monkeypatch):
        def boom_bcm():
            raise RuntimeError("probe failed password=supersecret token=leak123")

        monkeypatch.setattr(dashboard, "_cluster_probe_bcm_cmsh_version", boom_bcm)
        monkeypatch.setattr(dashboard, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(dashboard, "_cluster_squeue_overview_block",
            lambda: {"ok": True, "stdout": "", "stderr": "", "returncode": 0},
        )

        client.get("/api/cluster-overview/live")
        deadline = time.monotonic() + 5.0
        body = None
        err = ""
        while time.monotonic() < deadline:
            body = client.get("/api/cluster-overview/live").get_json()
            err = ((body or {}).get("sections") or {}).get("bcm", {}).get("last_error") or ""
            if err:
                break
            time.sleep(0.05)
        assert body is not None
        assert err
        # Immediate retry may flip state back to initializing; metadata must stay sanitized.
        assert body["sections"]["bcm"]["state"] in ("failed", "initializing")
        assert body["sections"]["bcm"]["value"] is None
        assert "supersecret" not in err
        assert "leak123" not in err
        assert "[redacted]" in err
        assert "supersecret" not in str(body)
        assert "leak123" not in str(body)


class TestRackStatusBackgroundLoop:
    def test_skips_collect_when_cluster_unreachable(self, monkeypatch):
        """VPN/SSH outage must not launch slow inventory collects every cycle."""
        snapshot_calls: list[object] = []
        enabled_ticks = {"n": 0}

        def enabled() -> bool:
            # Enter once (pre-sleep + post-sleep), then exit on next loop top.
            enabled_ticks["n"] += 1
            return enabled_ticks["n"] <= 2

        monkeypatch.setattr(dashboard, "_rack_status_background_enabled", enabled)
        monkeypatch.setattr(dashboard, "_rack_status_background_interval_sec", lambda: 5)
        monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(app_module.host_exec, "cluster_reachable", lambda force=False: False)
        monkeypatch.setattr(
            dashboard,
            "get_cluster_snapshot",
            lambda *args, **kwargs: snapshot_calls.append((args, kwargs)),
        )

        dashboard._rack_status_background_loop()

        assert snapshot_calls == []
        assert enabled_ticks["n"] == 3


class TestParserResultsDemo:
    def test_demo_parser_results(self, client):
        resp = client.get("/api/parser-results?rack=p5-r01&job_id=123")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "data" in data
        nccl = data["data"]["nccl_peak_out_of_place_busbw_gbps"]
        assert len(nccl) > 0
        assert "all_reduce_busbw_series" in nccl[0]
        nemo = data["data"]["nemotron_5h_56b_train_window"]
        assert len(nemo) > 0
        assert "step_time_series" in nemo[0]

    def test_parser_results_deterministic(self, client):
        r1 = client.get("/api/parser-results?rack=p5-r01&job_id=456").get_json()
        r2 = client.get("/api/parser-results?rack=p5-r01&job_id=456").get_json()
        assert r1["data"] == r2["data"]

    def test_parser_results_different_per_rack(self, client):
        r1 = client.get("/api/parser-results?rack=p5-r01&job_id=1").get_json()
        r2 = client.get("/api/parser-results?rack=p5-r02&job_id=1").get_json()
        peak1 = r1["data"]["nccl_peak_out_of_place_busbw_gbps"][0]["peak_busbw_gbps"]
        peak2 = r2["data"]["nccl_peak_out_of_place_busbw_gbps"][0]["peak_busbw_gbps"]
        assert peak1 != peak2


class TestCommentsAPI:
    def test_get_comments(self, client):
        resp = client.get("/api/comments")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "comments" in data


class TestSPAServing:
    def test_spa_index(self, client):
        from pathlib import Path
        spa_index = Path(__file__).resolve().parent.parent.parent / "static" / "spa" / "index.html"
        if not spa_index.is_file():
            pytest.skip("SPA not built (static/spa/ missing in CI)")
        resp = client.get("/spa/")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()

    def test_client_route_falls_back_to_index(self, client):
        """A deep link the user typed is the SPA's job to route, so it must get the app."""
        from pathlib import Path
        spa_index = Path(__file__).resolve().parent.parent.parent / "static" / "spa" / "index.html"
        if not spa_index.is_file():
            pytest.skip("SPA not built (static/spa/ missing in CI)")
        resp = client.get("/compute/nodes")
        assert resp.status_code == 200
        assert b"html" in resp.data.lower()

    def test_missing_shell_503s_instead_of_raising(self, client, tmp_path, monkeypatch):
        """A deployment with no frontend built into it must say so, not throw a 500.

        The old code checked whether index.html existed and then, for a client route, fell through
        to ``send_file(index.html)`` -- handing off the file it had just established was absent.
        The operator got a stack trace for what is really "nobody built the UI". This is the
        failure that broke the backend CI job, because that job does not build the frontend.
        """
        import api._common as common

        monkeypatch.setattr(common, "_SPA_DIR", tmp_path / "empty-spa")
        resp = client.get("/compute/nodes")
        assert resp.status_code == 503, "a missing bundle is unavailability, not a server error"
        body = resp.data.decode().lower()
        assert "frontend not built" in body
        assert "npm run build" in body, "the message has to say how to fix it"

    def test_missing_build_artifact_404s_instead_of_serving_html(self, client):
        """A stale index.html asking for a deleted bundle must fail honestly.

        Falling back to index.html here returns HTML for a .js request, so the browser
        rejects it on MIME type and the dashboard renders blank -- blaming the app for
        what is really a half-shipped deploy.
        """
        resp = client.get("/assets/index-DoesNotExist.js")
        assert resp.status_code == 404
        assert "text/html" not in (resp.content_type or "")
        assert resp.get_json()["ok"] is False


class TestRackLocksAPI:
    """Rack locks, with TWO REAL PRINCIPALS rather than a spoofable header.

    These tests used to distinguish operators with `X-Rack-Operator: alice` / `: bob`. FR-16 made
    the authenticated principal win over that header, because a lock claim is *evidence* of who
    took a rack and a header anyone can set made it unprovable. So the header no longer selects an
    operator, and the conflict / takeover / delete-by-other scenarios collapsed to one identity.

    Rewritten on two enrolled accounts, which is what the product actually does — and what AC-28e
    asks for. Each request carries that principal's own token; nothing here is faked.
    """

    @pytest.fixture
    def alice(self):
        from identity import testing as identity_testing

        return identity_testing.enrol("alice", display_name="Alice")

    @pytest.fixture
    def bob(self):
        from identity import testing as identity_testing

        return identity_testing.enrol("bob", display_name="Bob")

    @staticmethod
    def _as(principal, **kwargs):
        """Request kwargs that authenticate as `principal`."""
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = principal.bearer
        return {"headers": headers, **kwargs}

    def test_get_snapshot_shape(self, authed_client, isolated_locks_file, alice):
        resp = authed_client.get("/api/rack-locks", **self._as(alice))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["me"] == "alice", (
            "`me` should be the authenticated principal, not a client-supplied label"
        )
        assert "now" in data and "locks" in data
        assert isinstance(data["locks"], dict)

    def test_post_creates_claim(self, authed_client, isolated_locks_file, alice):
        resp = authed_client.post(
            "/api/rack-locks/p5-r01", **self._as(alice, json={"note": "planned"})
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "alice"
        assert data["claim"]["note"] == "planned"

        snap = authed_client.get("/api/rack-locks", **self._as(alice)).get_json()
        assert "p5-r01" in snap["locks"]
        assert snap["locks"]["p5-r01"]["operator"] == "alice"
        assert snap["locks"]["p5-r01"]["kind"] == "claim"

    def test_a_spoofed_operator_header_cannot_change_the_claim(
        self, authed_client, isolated_locks_file, alice
    ):
        """FR-16, at the route. This replaces `test_post_without_operator_header_400`: that test
        asserted a 400 when no header was sent, and with a real principal the operator is never
        empty, so the branch it covered is unreachable. The property worth testing now is the
        opposite one -- that sending a header does not let alice claim a rack as somebody else.
        """
        resp = authed_client.post(
            "/api/rack-locks/p5-r01",
            **self._as(alice, headers={"X-Rack-Operator": "somebody-else"}),
        )
        assert resp.status_code == 200
        assert resp.get_json()["claim"]["operator"] == "alice"

    def test_post_conflict_returns_409(self, authed_client, isolated_locks_file, alice, bob):
        authed_client.post("/api/rack-locks/p5-r01", **self._as(alice))
        resp = authed_client.post("/api/rack-locks/p5-r01", **self._as(bob))
        assert resp.status_code == 409

    def test_post_force_takeover_succeeds(self, authed_client, isolated_locks_file, alice, bob):
        authed_client.post("/api/rack-locks/p5-r01", **self._as(alice))
        resp = authed_client.post(
            "/api/rack-locks/p5-r01",
            **self._as(bob, json={"force": True, "note": "alice left"}),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "bob"
        assert data["claim"]["stolen_from"] == "alice"
        assert data["claim"]["note"] == "alice left"

    def test_post_invalid_rack_400(self, authed_client, isolated_locks_file, alice):
        resp = authed_client.post("/api/rack-locks/not-a-rack", **self._as(alice))
        assert resp.status_code == 400

    def test_delete_by_owner_succeeds(self, authed_client, isolated_locks_file, alice):
        authed_client.post("/api/rack-locks/p5-r01", **self._as(alice))
        resp = authed_client.delete("/api/rack-locks/p5-r01", **self._as(alice))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["removed"] is True

    def test_delete_by_other_returns_403(self, authed_client, isolated_locks_file, alice, bob):
        authed_client.post("/api/rack-locks/p5-r01", **self._as(alice))
        resp = authed_client.delete("/api/rack-locks/p5-r01", **self._as(bob))
        assert resp.status_code == 403


def _launch(client, url: str, kind: str, body: dict):
    """POST a legacy launch through the reviewed-target protocol (REQ-6).

    `prepare` resolves the target and returns a digest; the launch echoes it back and the server
    re-resolves and compares. Since 2026-08-24 the ten legacy launch endpoints require that binding,
    so a test that posts a bare body is testing the refusal, not the launch.

    A helper rather than a fixture because it takes the kind and body per call, and it deliberately
    fetches the digest instead of hard-coding one: a hard-coded digest would stop testing the
    binding the first time a prepare's resolved command changed for an unrelated reason.
    """
    prep = client.post("/api/launch/prepare", json={"kind": kind, **body}).get_json()
    assert prep and prep.get("ok"), f"prepare failed for kind={kind}: {prep}"
    return client.post(url, json={**body, "plan_hash": prep["plan_hash"]})


class TestReviewedLaunchBinding:
    """REQ-6 on the legacy launch endpoints: what the operator reviewed is what runs.

    Before this, `plan_hash` was enforced on the validation-preset path only -- one endpoint out of
    thirteen -- and the legacy launches relied on a client-side confirmation dialog, which detects
    nothing when the target has changed since it was shown. Found by a PLC requirements validation.
    """

    def test_a_launch_without_a_digest_is_refused(self, client, log_dir):
        resp = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["ok"] is False
        # The refusal carries the digest, so a client can review and retry without a second round.
        assert body.get("plan_hash")

    def test_a_stale_digest_is_refused(self, client, log_dir):
        resp = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True, "plan_hash": "0" * 64},
        )
        assert resp.status_code == 409
        assert "changed since it was reviewed" in (resp.get_json().get("error") or "")

    def test_the_prepared_digest_is_accepted(self, client, log_dir):
        resp = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        )
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()["ok"] is True

    def test_changing_the_target_after_review_invalidates_the_digest(self, client, log_dir):
        """The point of the gate: a digest earned for one target must not launch another."""
        prep = client.post(
            "/api/launch/prepare",
            json={"kind": "llmb-nccl", "rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        assert prep["ok"] is True
        resp = client.post(
            "/api/run-llmb-nccl-async",
            # Same digest, different rack.
            json={"rack": "p5-r02", "log_dir": log_dir, "dry_run": True,
                  "plan_hash": prep["plan_hash"]},
        )
        assert resp.status_code == 409

    def test_a_malformed_body_still_gets_its_own_error_not_a_digest_complaint(self, client, log_dir):
        """Gate order matters: the prepare's own 400 must win, or every bad request looks like a
        review problem and the operator is sent to fix the wrong thing."""
        resp = client.post("/api/run-llmb-nccl-async", json={"log_dir": log_dir})
        assert resp.status_code == 400

    def test_prepare_rejects_an_unknown_kind(self, client, log_dir):
        resp = client.post("/api/launch/prepare", json={"kind": "not-a-tool", "rack": "p5-r01"})
        assert resp.status_code == 400
        assert "kinds" in resp.get_json(), "the error should name the valid kinds"

    def test_prepare_does_not_launch(self, client, log_dir, isolated_validation_activities):
        """A review step that started the job would be worse than no review step."""
        prep = client.post(
            "/api/launch/prepare",
            json={"kind": "llmb-nccl", "rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        assert prep["ok"] is True
        assert "job_id" not in prep
        listed = client.get(
            "/api/validation-activities", query_string={"log_dir": log_dir}
        ).get_json()
        assert listed["activities"] == []

    def test_explicit_node_validation_does_not_require_a_rack(
        self, client, log_dir, monkeypatch
    ):
        """A real Slurm hostname is sufficient when BCM has no rack assignments."""
        node = "compute-001"

        def resolve(rack, override):
            assert rack == ""
            assert override == node
            return node

        monkeypatch.setattr(validation_runs, "validation_target_for_rack", resolve)
        monkeypatch.setattr(validation_runs, "_script_exists", lambda _path: True)
        response = client.post(
            "/api/launch/prepare",
            json={"kind": "validation", "target": node, "log_dir": log_dir},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["plan"]["rack"] == ""
        assert body["plan"]["target"] == node

        recorded = {}
        monkeypatch.setattr(
            validation_runs, "_record_validation_activity",
            lambda _tool, **fields: recorded.update(fields),
        )
        monkeypatch.setattr(validation_runs.threading.Thread, "start", lambda _thread: None)
        monkeypatch.setattr(validation_runs, "_validation_jobs", {})
        launch = client.post(
            "/api/run-validation-async",
            json={
                "target": node,
                "log_dir": log_dir,
                "plan_hash": body["plan_hash"],
            },
        )

        assert launch.status_code == 200
        assert recorded["scope"] == "node"
        assert recorded["rack"] == ""
        assert recorded["racks"] == []

    def test_off_mode_restores_the_legacy_contract(self, client, log_dir, monkeypatch):
        """The compatibility escape hatch, for a site with its own automation posting here."""
        monkeypatch.setenv("RACK_LAUNCH_REVIEW", "off")
        resp = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        )
        assert resp.status_code == 200

    def test_warn_mode_allows_an_unbound_launch_but_still_refuses_a_stale_one(
        self, client, log_dir, monkeypatch
    ):
        """`warn` is for migrating callers, not for disabling the check that matters: a MISMATCH is
        a target that changed, not a caller that never learned the protocol."""
        monkeypatch.setenv("RACK_LAUNCH_REVIEW", "warn")
        unbound = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        )
        assert unbound.status_code == 200
        stale = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True, "plan_hash": "0" * 64},
        )
        assert stale.status_code == 409


class TestRunLlmbNccl:
    """Async + status endpoints for the NCCL Microbenchmark (LLMB) tool."""

    def test_single_rack_starts_job(self, client, log_dir):
        resp = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data.get("job_id")
        assert data.get("racks") == ["p5-r01"]
        # Multi-rack NCCL now defaults to the direct path (run_nccl_mpi.sh), not the
        # llmb-run container. RACK_LLMB_NCCL_USE_CONTAINER=1 switches back.
        assert "run_nccl_mpi.sh" in (data.get("command_display") or "")
        assert "--dry-run" in (data.get("command_display") or "")

    def test_multi_rack_starts_job(self, client, log_dir):
        resp = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"racks": ["p5-r01", "p5-r02", "p5-r01"], "log_dir": log_dir},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        # Duplicates removed, order preserved.
        assert data.get("racks") == ["p5-r01", "p5-r02"]

    def test_missing_rack_returns_400(self, client, log_dir):
        # A valid log_dir keeps this assertion about the *missing rack*; with an
        # unresolvable one it would pass on the wrong 400.
        resp = client.post("/api/run-llmb-nccl-async", json={"log_dir": log_dir})
        assert resp.status_code == 400
        assert resp.get_json()["ok"] is False

    def test_status_requires_job_id(self, client):
        resp = client.get("/api/run-llmb-nccl-status")
        assert resp.status_code == 400

    def test_status_for_started_job(self, client, log_dir):
        start = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        job_id = start["job_id"]
        resp = client.get(f"/api/run-llmb-nccl-status?job_id={job_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data.get("phase") in {"starting", "running", "done", "error", "timeout"}

    def test_status_unknown_job_404(self, client):
        resp = client.get("/api/run-llmb-nccl-status?job_id=does-not-exist")
        assert resp.status_code == 404


class TestValidationActivitiesAPI:
    """Durable validation activity registry HTTP surface."""

    def test_list_empty(self, client, log_dir, isolated_validation_activities):
        resp = client.get("/api/validation-activities", query_string={"log_dir": log_dir})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["activities"] == []

    def test_async_launch_creates_activity(self, client, log_dir, isolated_validation_activities):
        start = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        assert start["ok"] is True
        job_id = start["job_id"]
        listed = client.get(
            "/api/validation-activities",
            query_string={"log_dir": log_dir, "rack": "p5-r01"},
        ).get_json()
        assert listed["ok"] is True
        ids = [a["activity_id"] for a in listed["activities"]]
        assert job_id in ids
        detail = client.get(f"/api/validation-activities/{job_id}").get_json()
        assert detail["ok"] is True
        assert detail["activity"]["tool"] == "llmb-nccl"

    def test_delete_activity(self, client, log_dir, isolated_validation_activities):
        start = _launch(
            client, "/api/run-llmb-nccl-async", "llmb-nccl",
            {"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        job_id = start["job_id"]
        resp = client.delete(f"/api/validation-activities/{job_id}")
        assert resp.status_code == 200
        listed = client.get("/api/validation-activities", query_string={"log_dir": log_dir}).get_json()
        assert listed["activities"] == []

    def test_multirack_jobs_compat_alias(self, client, log_dir, isolated_validation_activities):
        start = _launch(
            client, "/api/run-nemo-xrack-async", "nemo-xrack",
            {"racks": ["p5-r01", "p5-r02"], "log_dir": log_dir, "dry_run": True},
        ).get_json()
        assert start["ok"] is True
        resp = client.get("/api/multirack-jobs", query_string={"log_dir": log_dir})
        assert resp.status_code == 200
        jobs = resp.get_json().get("jobs") or []
        assert any(j.get("job_id") == start["job_id"] for j in jobs)

    def test_multirack_ledger_and_corrective_action_api(
        self, client, isolated_validation_activities
    ):
        aid = validation_activities.record_activity(
            "nemo-xrack",
            scope="multirack",
            rack="p5-r01",
            racks=["p5-r01", "p5-r02"],
        )
        response = client.post(
            f"/api/validation-activities/{aid}/corrective-actions",
            json={
                "issue_summary": "scheduler failed the workload",
                "action": "fixed the generated output filename",
                "status": "complete",
                "result": "submit dry-run passed",
            },
        )
        assert response.status_code == 200
        activity = response.get_json()["activity"]
        assert activity["issue_summary"] == "scheduler failed the workload"
        assert activity["corrective_actions"][0]["status"] == "complete"

        ledger = client.get("/api/validation-activities/ledger")
        assert ledger.status_code == 200
        assert ledger.content_type.startswith("text/markdown")
        assert "fixed the generated output filename" in ledger.get_data(as_text=True)

    def test_corrective_action_api_requires_content(
        self, client, isolated_validation_activities
    ):
        aid = validation_activities.record_activity(
            "nemo-xrack", scope="multirack", racks=["p5-r01", "p5-r02"]
        )
        response = client.post(
            f"/api/validation-activities/{aid}/corrective-actions", json={}
        )
        assert response.status_code == 400

    def test_terminal_activity_hydrates_slurm_stdout(
        self, client, log_dir, isolated_validation_activities, monkeypatch
    ):
        """Done wrapper activities still resolve Slurm StdOut for child tasks on list refresh."""
        aid = validation_activities.record_activity(
            "llmb-nccl",
            rack="p5-r01",
            log_dir=log_dir,
            operator="tester",
        )
        validation_activities.update_activity(
            aid,
            phase="done",
            stdout="SUBMITTED: microbenchmark jobid=114305\n",
        )

        def _fake_scontrol(job_id: str) -> dict:
            return {
                "ok": True,
                "stdout": (
                    f"JobId={job_id} JobState=RUNNING "
                    f"StdOut=/logs/llmb-nccl_nodes_{job_id}.out"
                ),
            }

        monkeypatch.setattr(activity_hydration, "_run_scontrol_show_job", _fake_scontrol)
        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )

        listed = client.get(
            "/api/validation-activities",
            query_string={"log_dir": log_dir, "rack": "p5-r01"},
        ).get_json()
        assert listed["ok"] is True
        rec = next(a for a in listed["activities"] if a["activity_id"] == aid)
        assert rec["phase"] == "done"
        tasks = rec.get("tasks") or []
        assert len(tasks) == 1
        assert tasks[0]["scheduler_ref"] == "114305"
        assert tasks[0]["stdout_path"] == "/logs/llmb-nccl_nodes_114305.out"
        assert tasks[0]["scheduler_state"] == "RUNNING"
        assert tasks[0]["work_phase"] == "running"
        assert rec.get("work_phase") == "running"

        detail = client.get(f"/api/validation-activities/{aid}").get_json()
        assert detail["activity"]["tasks"][0]["stdout_path"] == "/logs/llmb-nccl_nodes_114305.out"


class TestValidationActivitySlurmHydration:
    def test_task_needs_hydration_when_stdout_missing(self):
        assert activity_hydration._task_needs_slurm_hydration({"scheduler_ref": "123"}) is True

    def test_task_needs_hydration_when_slurm_still_running(self):
        task = {
            "scheduler_ref": "123",
            "stdout_path": "/logs/out",
            "scheduler_state": "RUNNING",
            "work_phase": "running",
        }
        assert activity_hydration._task_needs_slurm_hydration(task) is True

    def test_task_skips_hydration_when_work_terminal(self):
        task = {
            "scheduler_ref": "123",
            "stdout_path": "/logs/cmods-NCCL-1Rack-r01-18N-123.txt",
            "results_log_path": "/logs/cmods-NCCL-1Rack-r01-18N-123.txt",
            "scheduler_state": "COMPLETED",
            "work_phase": "done",
        }
        assert activity_hydration._task_needs_slurm_hydration(task) is False

    def test_hydrate_activity_task_logs(self, isolated_validation_activities, monkeypatch):
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir="/logs")
        validation_activities.sync_tasks_from_stdout(aid, "jobid=999\n")
        validation_activities.update_activity(aid, phase="done")

        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )
        monkeypatch.setattr(
            activity_hydration,
            "_run_scontrol_show_job",
            lambda jid: {
                "ok": True,
                "stdout": f"JobId={jid} JobState=COMPLETED StdOut=/logs/llmb-nccl_nodes_{jid}.out",
            },
        )
        activity_hydration._hydrate_activity_task_logs(aid)
        tasks = validation_activities.get_activity(aid).get("tasks") or []
        assert tasks[0]["stdout_path"] == "/logs/llmb-nccl_nodes_999.out"
        assert tasks[0]["scheduler_state"] == "COMPLETED"
        # Intermediate llmb StdOut without aggregated results → collecting
        assert tasks[0]["work_phase"] == "collecting"

    def test_background_cycle_refreshes_all_racks_without_browser_filters(
        self, isolated_validation_activities, monkeypatch
    ):
        activity_ids = []
        for ordinal, rack in enumerate(("r01", "r02", "r03"), start=1):
            aid = validation_activities.record_activity(
                "llmb-nemo-combined", rack=rack, log_dir="/logs"
            )
            validation_activities.add_or_update_task(
                aid,
                ordinal=1,
                label="nemo",
                scheduler_ref=str(200 + ordinal),
                scheduler_state="RUNNING",
                work_phase="running",
            )
            activity_ids.append(aid)

        refreshed = []
        # The cycle under test lives in app.py and calls the name IMPORTED there, so app's binding is
        # the one that has to be replaced. activity_hydration's own is patched too, for any call the
        # module makes internally -- patching one of the two silently exercises the real hydration.
        stub = lambda aid, _rec=None: refreshed.append(aid)  # noqa: E731
        monkeypatch.setattr(app_module, "_hydrate_activity_task_logs", stub)
        monkeypatch.setattr(activity_hydration, "_hydrate_activity_task_logs", stub)
        monkeypatch.setenv("RACK_VALIDATION_ACTIVITIES_REFRESH_BUDGET_SEC", "60")
        monkeypatch.setenv(
            "RACK_VALIDATION_ACTIVITIES_REFRESH_LOCK_FILE",
            str(isolated_validation_activities.with_suffix(".refresh.lock")),
        )
        assert app_module._refresh_validation_activities_cycle() == 3
        # The module-level refresh daemon may concurrently hydrate another
        # activity while this test's monkeypatch is installed.  What matters is
        # that a whole-store cycle reaches every intended rack without browser
        # filters; unrelated concurrent refreshes are allowed.
        assert set(activity_ids).issubset(refreshed)

    def _stub_slurm(self, monkeypatch, state="COMPLETED", stdout_tpl="/logs/llmb-nccl_nodes_{jid}.out"):
        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )
        monkeypatch.setattr(
            activity_hydration,
            "_run_scontrol_show_job",
            lambda jid: {
                "ok": True,
                "stdout": f"JobId={jid} JobState={state} StdOut={stdout_tpl.format(jid=jid)}",
            },
        )

    def _backdate_collecting(self, aid, when="2020-01-01T00:00:00+00:00"):
        """Age the task's collecting stamp past the grace period, in place.

        Addressed by task_id: matching on (ordinal, label) would append a second
        task, because sync_tasks_from_stdout labels a lone job id "validation".
        """
        task = validation_activities.get_activity(aid)["tasks"][0]
        validation_activities.add_or_update_task(
            aid,
            ordinal=int(task["ordinal"]),
            label=str(task["label"]),
            task_id=str(task["task_id"]),
            collecting_since=when,
        )

    def test_results_log_found_on_remote_exec_host(
        self, isolated_validation_activities, monkeypatch
    ):
        """The log dir lives on the execution host; discovery must not use a local scan.

        Regression: in SSH mode the container cannot see the log dir, so llmb tasks
        (whose StdOut is the intermediate llmb-nccl_*.out) never bound their results
        and sat in "collecting" forever.
        """
        aid = validation_activities.record_activity(
            "llmb-nccl", rack="ink50-dh01-h01", log_dir="/home/cmods/benchmark/harish"
        )
        validation_activities.sync_tasks_from_stdout(aid, "jobid=127326\n")
        self._stub_slurm(monkeypatch)
        agg = "/home/cmods/benchmark/harish/cmods-NCCL-1Rack-ink50-dh01-h01-18N-127326.txt"
        monkeypatch.setattr(
            app_module.host_exec,
            "stat_dir",
            lambda base, **kw: {agg: object(), f"{base}/llmb-nccl_x_127326.out": object()},
        )
        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["results_log_path"] == agg
        assert task["work_phase"] == "done"
        assert task.get("collecting_since") is None

    def test_collecting_is_stamped_then_resolves_after_grace(
        self, isolated_validation_activities, monkeypatch
    ):
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir="/logs")
        validation_activities.sync_tasks_from_stdout(aid, "jobid=999\n")
        self._stub_slurm(monkeypatch)
        monkeypatch.setattr(app_module.host_exec, "stat_dir", lambda base, **kw: {})

        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["work_phase"] == "collecting"
        assert task["collecting_since"], "entering collecting must stamp the clock"

        # Backdate the stamp past the grace period: the collector never delivered.
        self._backdate_collecting(aid)
        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["work_phase"] == "done"  # Slurm said COMPLETED
        assert task["phase"] == "done"
        assert len(validation_activities.get_activity(aid)["tasks"]) == 1

    def test_collecting_grace_expiry_uses_scheduler_verdict(
        self, isolated_validation_activities, monkeypatch
    ):
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir="/logs")
        validation_activities.sync_tasks_from_stdout(aid, "jobid=998\n")
        self._stub_slurm(monkeypatch, state="TIMEOUT")
        monkeypatch.setattr(app_module.host_exec, "stat_dir", lambda base, **kw: {})
        activity_hydration._hydrate_activity_task_logs(aid)
        assert validation_activities.get_activity(aid)["tasks"][0]["work_phase"] == "collecting"
        self._backdate_collecting(aid)
        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["work_phase"] == "failed"

    def test_grace_expired_task_stops_being_refreshed(self):
        task = {
            "scheduler_ref": "123",
            "stdout_path": "/logs/llmb-nccl_nodes_123.out",
            "scheduler_state": "COMPLETED",
            "work_phase": "done",
            "collecting_since": "2020-01-01T00:00:00+00:00",
        }
        assert activity_hydration._task_needs_slurm_hydration(task) is False
        # Still inside the grace period → keep polling for the collector.
        fresh = dict(task, work_phase="collecting", collecting_since=None)
        assert activity_hydration._task_needs_slurm_hydration(fresh) is True

    def test_collecting_grace_expired_helper(self, monkeypatch):
        assert activity_hydration._collecting_grace_expired(None) is False
        assert activity_hydration._collecting_grace_expired("not-a-timestamp") is False
        assert activity_hydration._collecting_grace_expired("2020-01-01T00:00:00+00:00") is True
        # Naive timestamps are treated as UTC rather than crashing.
        assert activity_hydration._collecting_grace_expired("2020-01-01T00:00:00") is True
        monkeypatch.setenv("RACK_VALIDATION_COLLECT_GRACE_SEC", "999999999")
        assert activity_hydration._collecting_grace_expired("2020-01-01T00:00:00+00:00") is False

    def test_hydrate_fail_closed_when_scontrol_and_sacct_missing(
        self, isolated_validation_activities, monkeypatch
    ):
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir="/tmp")
        validation_activities.sync_tasks_from_stdout(aid, "jobid=42\n")
        validation_activities.add_or_update_task(
            aid, ordinal=1, label="nccl", scheduler_state="RUNNING", work_phase="running"
        )
        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )
        monkeypatch.setattr(
            activity_hydration, "_run_scontrol_show_job", lambda jid: {"ok": False, "stdout": ""}
        )
        monkeypatch.setattr(
            activity_hydration,
            "_query_scheduler_state_and_stdout",
            lambda jid: (None, None, "missing"),
        )
        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["scheduler_state"] == "UNKNOWN"
        assert task["work_phase"] == "unknown"

    def test_hydrate_binds_results_log_and_marks_done(
        self, isolated_validation_activities, tmp_path, monkeypatch
    ):
        log_dir = tmp_path
        results = log_dir / "cmods-NCCL-1Rack-p5-r01-18N-77.txt"
        results.write_text("# Collective test starting: all_reduce_perf\n", encoding="utf-8")
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir=str(log_dir))
        validation_activities.sync_tasks_from_stdout(aid, "jobid=77\n")
        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )
        monkeypatch.setattr(
            activity_hydration,
            "_query_scheduler_state_and_stdout",
            lambda jid: ("COMPLETED", f"/tmp/llmb-nccl_nodes_{jid}.out", "sacct"),
        )
        activity_hydration._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["results_log_path"] == str(results)
        assert task["work_phase"] == "done"
        assert validation_activities.get_activity(aid)["work_phase"] == "done"
