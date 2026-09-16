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
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import app as app_module
from app import app

import rack_locks
import validation_activities


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

    def test_login_when_disabled(self, client):
        resp = client.post("/api/login", json={"password": "anything"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True


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
        monkeypatch.setattr(app_module, "_start_cmsh_rack_list_warm_thread", lambda: None)
        monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw: Path("/logs"))
        monkeypatch.setattr(
            app_module,
            "_cluster_squeue_overview_block",
            lambda: {"ok": True, "stdout": ""},
        )
        # Avoid ~1.5s filesystem reads on every _eget during API shell assembly.
        monkeypatch.setattr(app_module, "_job_env_overrides_load", lambda: {})
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

        monkeypatch.setattr(app_module, "run_rack_status", collect)
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
            app_module,
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
        monkeypatch.setattr(app_module, "run_rack_status", lambda *_args, **_kwargs: empty)

        client.get("/api/cluster-overview?log_dir=/logs")
        overview = self._poll_json(client, "/api/cluster-overview", want_state="fresh")
        assert overview["snapshot"]["state"] == "fresh"
        assert overview["rack_count"] == 0
        assert overview["tray_hosts"] == 0

    def test_cold_overview_returns_metadata_while_initializing(self, client, monkeypatch):
        gate = threading.Event()
        probe_calls = {"slurm": 0, "bcm": 0, "squeue": 0}

        def slow_collect(_log_dir, include_raw=False):
            assert gate.wait(10)
            return self.inventory()

        monkeypatch.delenv("RACK_CLUSTER_OVERVIEW_DISABLE_PROBES", raising=False)
        monkeypatch.setattr(app_module, "run_rack_status", slow_collect)

        def count_slurm():
            probe_calls["slurm"] += 1
            return ("1.0", None)

        def count_bcm():
            probe_calls["bcm"] += 1
            return ("10.0", None)

        def count_squeue():
            probe_calls["squeue"] += 1
            return {"ok": True, "stdout": ""}

        monkeypatch.setattr(app_module, "_cluster_probe_slurm_version", count_slurm)
        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", count_bcm)
        monkeypatch.setattr(app_module, "_cluster_squeue_overview_block", count_squeue)

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

        monkeypatch.setattr(app_module, "run_rack_status", slow_collect)
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

        monkeypatch.setattr(app_module, "run_rack_status", collect)
        monkeypatch.setattr(app_module, "_merge_live_native_workers_into_status", merge)
        payload = app_module._collect_canonical_rack_status(Path("/logs"))
        assert payload["ok"] is True
        assert merge_calls == 0

    def test_overview_never_runs_live_probes_with_fresh_snapshot(self, client, monkeypatch):
        """Fresh inventory must not block on BCM/Slurm/queue or SSH reachability probes."""
        probe_calls = {"bcm": 0, "slurm": 0, "queue": 0}
        reach_probe_calls = {"n": 0}
        monkeypatch.setattr(
            app_module, "run_rack_status", lambda *_a, **_k: self.inventory()
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
        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", block_bcm)
        monkeypatch.setattr(app_module, "_cluster_probe_slurm_version", block_slurm)
        monkeypatch.setattr(app_module, "_cluster_squeue_overview_block", block_queue)

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
        monkeypatch.setattr(app_module, "_start_cmsh_rack_list_warm_thread", lambda: None)
        monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw: Path("/logs"))
        monkeypatch.setattr(app_module.host_exec, "reachability_state", lambda: (True, ""))
        monkeypatch.setattr(app_module, "_job_env_overrides_load", lambda: {})
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

        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(app_module, "_cluster_probe_slurm_version", collect_slurm)
        monkeypatch.setattr(app_module, "_cluster_squeue_overview_block", collect_queue)

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

        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(
            app_module, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(
            app_module,
            "_cluster_squeue_overview_block",
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

        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", collect_bcm)
        monkeypatch.setattr(
            app_module, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(
            app_module,
            "_cluster_squeue_overview_block",
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

        monkeypatch.setattr(app_module, "_cluster_probe_bcm_cmsh_version", boom_bcm)
        monkeypatch.setattr(
            app_module, "_cluster_probe_slurm_version", lambda: ("slurm 1", "")
        )
        monkeypatch.setattr(
            app_module,
            "_cluster_squeue_overview_block",
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

        monkeypatch.setattr(app_module, "_rack_status_background_enabled", enabled)
        monkeypatch.setattr(app_module, "_rack_status_background_interval_sec", lambda: 5)
        monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(app_module.host_exec, "cluster_reachable", lambda force=False: False)
        monkeypatch.setattr(
            app_module,
            "get_cluster_snapshot",
            lambda *args, **kwargs: snapshot_calls.append((args, kwargs)),
        )

        app_module._rack_status_background_loop()

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
    def test_get_snapshot_shape(self, client, isolated_locks_file):
        resp = client.get("/api/rack-locks", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["me"] == "alice"
        assert "now" in data and "locks" in data
        assert isinstance(data["locks"], dict)

    def test_post_creates_claim(self, client, isolated_locks_file):
        resp = client.post(
            "/api/rack-locks/p5-r01",
            headers={"X-Rack-Operator": "alice"},
            json={"note": "planned"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "alice"
        assert data["claim"]["note"] == "planned"

        snap = client.get("/api/rack-locks", headers={"X-Rack-Operator": "alice"}).get_json()
        assert "p5-r01" in snap["locks"]
        assert snap["locks"]["p5-r01"]["operator"] == "alice"
        assert snap["locks"]["p5-r01"]["kind"] == "claim"

    def test_post_without_operator_header_400(self, client, isolated_locks_file):
        resp = client.post("/api/rack-locks/p5-r01", json={})
        assert resp.status_code == 400

    def test_post_conflict_returns_409(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "bob"})
        assert resp.status_code == 409

    def test_post_force_takeover_succeeds(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.post(
            "/api/rack-locks/p5-r01",
            headers={"X-Rack-Operator": "bob"},
            json={"force": True, "note": "alice left"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["claim"]["operator"] == "bob"
        assert data["claim"]["stolen_from"] == "alice"
        assert data["claim"]["note"] == "alice left"

    def test_post_invalid_rack_400(self, client, isolated_locks_file):
        resp = client.post(
            "/api/rack-locks/not-a-rack",
            headers={"X-Rack-Operator": "alice"},
        )
        assert resp.status_code == 400

    def test_delete_by_owner_succeeds(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["removed"] is True

    def test_delete_by_other_returns_403(self, client, isolated_locks_file):
        client.post("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "bob"})
        assert resp.status_code == 403

    def test_delete_nonexistent_returns_ok_not_removed(self, client, isolated_locks_file):
        resp = client.delete("/api/rack-locks/p5-r01", headers={"X-Rack-Operator": "alice"})
        assert resp.status_code == 200
        assert resp.get_json()["removed"] is False


class TestRunLlmbNccl:
    """Async + status endpoints for the NCCL Microbenchmark (LLMB) tool."""

    def test_single_rack_starts_job(self, client, log_dir):
        resp = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
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

    def test_multi_rack_starts_job(self, client, log_dir, monkeypatch):
        racks = ["p5-r01", "p5-r02"]
        monkeypatch.setattr(
            app_module,
            "_multirack_campaign_snapshot",
            lambda: {
                "ok": True,
                "active": True,
                "id": "test-exact-campaign",
                "racks": [
                    {
                        "rack": rack,
                        "eligible": True,
                        "reason": "",
                        "nodes": [f"{rack}-ct01", f"{rack}-ct02"],
                    }
                    for rack in racks
                ],
            },
        )
        resp = client.post(
            "/api/run-llmb-nccl-async",
            json={
                "racks": ["p5-r01", "p5-r02", "p5-r01"],
                "campaign": "test-exact-campaign",
                "log_dir": log_dir,
            },
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
        start = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
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
        start = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
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
        start = client.post(
            "/api/run-llmb-nccl-async",
            json={"rack": "p5-r01", "log_dir": log_dir, "dry_run": True},
        ).get_json()
        job_id = start["job_id"]
        resp = client.delete(f"/api/validation-activities/{job_id}")
        assert resp.status_code == 200
        listed = client.get("/api/validation-activities", query_string={"log_dir": log_dir}).get_json()
        assert listed["activities"] == []

    def test_multirack_jobs_compat_alias(
        self, client, log_dir, isolated_validation_activities, monkeypatch
    ):
        racks = ["p5-r01", "p5-r02"]
        monkeypatch.setattr(
            app_module,
            "_multirack_campaign_snapshot",
            lambda: {
                "ok": True,
                "active": True,
                "id": "test-exact-campaign",
                "racks": [
                    {
                        "rack": rack,
                        "eligible": True,
                        "reason": "",
                        "nodes": [f"{rack}-ct01", f"{rack}-ct02"],
                    }
                    for rack in racks
                ],
            },
        )
        start = client.post(
            "/api/run-nemo-xrack-async",
            json={
                "racks": racks,
                "campaign": "test-exact-campaign",
                "log_dir": log_dir,
                "dry_run": True,
            },
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

        monkeypatch.setattr(app_module, "_run_scontrol_show_job", _fake_scontrol)
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
        assert app_module._task_needs_slurm_hydration({"scheduler_ref": "123"}) is True

    def test_task_needs_hydration_when_slurm_still_running(self):
        task = {
            "scheduler_ref": "123",
            "stdout_path": "/logs/out",
            "scheduler_state": "RUNNING",
            "work_phase": "running",
        }
        assert app_module._task_needs_slurm_hydration(task) is True

    def test_task_skips_hydration_when_work_terminal(self):
        task = {
            "scheduler_ref": "123",
            "stdout_path": "/logs/cmods-NCCL-1Rack-r01-18N-123.txt",
            "results_log_path": "/logs/cmods-NCCL-1Rack-r01-18N-123.txt",
            "scheduler_state": "COMPLETED",
            "work_phase": "done",
        }
        assert app_module._task_needs_slurm_hydration(task) is False

    def test_lazy_refresh_reconciles_terminal_task_to_parent(
        self, isolated_validation_activities
    ):
        aid = validation_activities.record_activity(
            "llmb-nccl", rack="p5-r01", log_dir="/logs"
        )
        validation_activities.add_or_update_task(
            aid,
            ordinal=1,
            label="nccl",
            scheduler_ref="123",
            scheduler_state="COMPLETED",
            stdout_path="/logs/nccl-123.out",
            results_log_path="/logs/nccl-123.out",
            phase="done",
            work_phase="done",
        )
        validation_activities.update_activity(aid, phase="running", work_phase="done")

        rec = validation_activities.get_activity(aid)
        app_module._refresh_validation_activities_lazy([rec])

        stored = validation_activities.get_activity(aid)
        assert stored["phase"] == "done"
        assert stored["work_phase"] == "done"
        assert stored["finished_at"]

    def test_hydrate_activity_task_logs(self, isolated_validation_activities, monkeypatch):
        aid = validation_activities.record_activity("llmb-nccl", rack="p5-r01", log_dir="/logs")
        validation_activities.sync_tasks_from_stdout(aid, "jobid=999\n")
        validation_activities.update_activity(aid, phase="done")

        monkeypatch.setattr(app_module.native_status, "load_squeue_jobs_map", lambda: (set(), {}))
        monkeypatch.setattr(
            app_module.native_status, "augment_running_set_for_job_ids", lambda *a, **k: None
        )
        monkeypatch.setattr(
            app_module,
            "_run_scontrol_show_job",
            lambda jid: {
                "ok": True,
                "stdout": f"JobId={jid} JobState=COMPLETED StdOut=/logs/llmb-nccl_nodes_{jid}.out",
            },
        )
        app_module._hydrate_activity_task_logs(aid)
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
        monkeypatch.setattr(
            app_module,
            "_hydrate_activity_task_logs",
            lambda aid, _rec=None: refreshed.append(aid),
        )
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
            app_module,
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
        app_module._hydrate_activity_task_logs(aid)
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

        app_module._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["work_phase"] == "collecting"
        assert task["collecting_since"], "entering collecting must stamp the clock"

        # Backdate the stamp past the grace period: the collector never delivered.
        self._backdate_collecting(aid)
        app_module._hydrate_activity_task_logs(aid)
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
        app_module._hydrate_activity_task_logs(aid)
        assert validation_activities.get_activity(aid)["tasks"][0]["work_phase"] == "collecting"
        self._backdate_collecting(aid)
        app_module._hydrate_activity_task_logs(aid)
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
        assert app_module._task_needs_slurm_hydration(task) is False
        # Still inside the grace period → keep polling for the collector.
        fresh = dict(task, work_phase="collecting", collecting_since=None)
        assert app_module._task_needs_slurm_hydration(fresh) is True

    def test_collecting_grace_expired_helper(self, monkeypatch):
        assert app_module._collecting_grace_expired(None) is False
        assert app_module._collecting_grace_expired("not-a-timestamp") is False
        assert app_module._collecting_grace_expired("2020-01-01T00:00:00+00:00") is True
        # Naive timestamps are treated as UTC rather than crashing.
        assert app_module._collecting_grace_expired("2020-01-01T00:00:00") is True
        monkeypatch.setenv("RACK_VALIDATION_COLLECT_GRACE_SEC", "999999999")
        assert app_module._collecting_grace_expired("2020-01-01T00:00:00+00:00") is False

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
            app_module, "_run_scontrol_show_job", lambda jid: {"ok": False, "stdout": ""}
        )
        monkeypatch.setattr(
            app_module,
            "_query_scheduler_state_and_stdout",
            lambda jid: (None, None, "missing"),
        )
        app_module._hydrate_activity_task_logs(aid)
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
            app_module,
            "_query_scheduler_state_and_stdout",
            lambda jid: ("COMPLETED", f"/tmp/llmb-nccl_nodes_{jid}.out", "sacct"),
        )
        app_module._hydrate_activity_task_logs(aid)
        task = validation_activities.get_activity(aid)["tasks"][0]
        assert task["results_log_path"] == str(results)
        assert task["work_phase"] == "done"
        activity = validation_activities.get_activity(aid)
        assert activity["work_phase"] == "done"
        assert activity["phase"] == "done"
