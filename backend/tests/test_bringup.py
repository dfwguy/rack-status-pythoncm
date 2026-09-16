# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Unit tests for the per-node bring-up results module (backend/bringup.py).

The runners shell out to Travis's scripts; here ``host_exec.run_on_host`` is stubbed
with captured real output so the parsing/merge logic is exercised without a cluster.
"""
import subprocess
import threading
from types import SimpleNamespace

import app as app_module
import bringup
import pytest


@pytest.fixture(autouse=True)
def _clear_bringup_caches():
    bringup._daily_cache.clear()
    with bringup._fleet_cache_condition:
        bringup._fleet_cache.clear()
        bringup._fleet_errors.clear()
        bringup._fleet_refreshing.clear()
    yield

# Captured from the live head node (daily-report.sh -c / status-check.sh <node>).
DAILY_CSV = (
    "node,slurm,dcgm,nvbandwidth,allreduce,allreduce-nonvlink\n"
    "ink50-dh01-h01-11,down*,DONE,357.23,688.68,2.28\n"
    "ink50-dh01-h01-12,drained*,DONE,349.74,687.97,NOT_RUN\n"
)

STATUS_CHECK = (
    "----------------\n"
    "ink50-dh01-h01-11\n"
    "----------------\n"
    "dcgm: DONE\n"
    "nvbandwidth: DONE\n"
    "allreduce: DONE\n"
    "allreduce-nonvlink: FAIL -- below floor (43): 2.28\n"
)


def _fake_run(daily=DAILY_CSV, status=STATUS_CHECK):
    def run(cmd, **_kw):
        joined = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        out = daily if "daily-report.sh" in joined else status if "status-check.sh" in joined else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


# The fleet form of status-check.sh: dshbak -c groups every node sharing a verdict set behind
# one Slurm nodeset. Captured from the live head node (566 nodes come back in ~0.5 s).
FLEET_STATUS = (
    "----------------\n"
    "ink50-dh01-h01-[11,14-15]\n"
    "----------------\n"
    "allreduce: DONE\n"
    "allreduce-nonvlink: FAIL -- run error\n"
    "dcgm: NOT_RUN\n"
    "nvbandwidth: DONE\n"
    "----------------\n"
    "ink50-dh01-h02-11,ink50-dh01-h02-[28-29]\n"
    "----------------\n"
    "allreduce: NOT_RUN\n"
    "allreduce-nonvlink: NOT_RUN\n"
    "dcgm: NOT_RUN\n"
    "nvbandwidth: NOT_RUN\n"
)


def test_parse_daily_report_csv():
    cols, rows = bringup.parse_daily_report_csv(DAILY_CSV)
    assert cols == ["dcgm", "nvbandwidth", "allreduce", "allreduce-nonvlink"]
    assert set(rows) == {"ink50-dh01-h01-11", "ink50-dh01-h01-12"}
    r = rows["ink50-dh01-h01-11"]
    assert r["slurm"] == "down*"
    assert r["cells"]["nvbandwidth"] == "357.23"
    assert r["cells"]["allreduce-nonvlink"] == "2.28"


def test_parse_daily_report_rejects_decoration_only_node():
    decorated = DAILY_CSV + "************************************************************,,,,,,\n"
    _cols, rows = bringup.parse_daily_report_csv(decorated)
    assert "************************************************************" not in rows
    assert rows["ink50-dh01-h01-12"]["cells"]["allreduce-nonvlink"] == "NOT_RUN"


def test_parse_status_check_grouped_output():
    sc = bringup.parse_status_check(STATUS_CHECK)
    assert sc["dcgm"]["status"] == "DONE"
    assert sc["allreduce-nonvlink"]["status"] == "FAIL"
    assert "below floor" in sc["allreduce-nonvlink"]["reason"]
    # The bare node-name header and dashed separators must not be parsed as columns.
    assert "ink50-dh01-h01-11" not in sc


def test_node_summary_merges_status_and_metric(monkeypatch):
    bringup._daily_cache.clear()
    monkeypatch.setattr(bringup.host_exec, "run_on_host", _fake_run())
    summary = bringup.node_summary("ink50-dh01-h01-11")

    assert summary["node"] == "ink50-dh01-h01-11"
    assert summary["present"] is True
    assert summary["slurm_state"] == "down*"

    by_name = {j["name"]: j for j in summary["jobs"]}
    # dcgm: status word, no metric.
    assert by_name["dcgm"]["status"] == "DONE"
    assert by_name["dcgm"]["metric"] is None
    # allreduce: numeric metric + authoritative DONE from status-check.
    assert by_name["allreduce"]["status"] == "DONE"
    assert by_name["allreduce"]["metric"] == "688.68"
    assert by_name["allreduce"]["metric_label"] == "busbw GB/s"
    # A numeric cell that FAILED keeps its number, and the FAIL/reason comes from
    # status-check (the daily-report cell alone cannot express pass/fail).
    nv = by_name["allreduce-nonvlink"]
    assert nv["status"] == "FAIL"
    assert nv["metric"] == "2.28"
    assert "below floor" in nv["reason"]


def test_node_summary_case_insensitive_and_absent(monkeypatch):
    bringup._daily_cache.clear()
    monkeypatch.setattr(bringup.host_exec, "run_on_host", _fake_run())
    # Uppercase request resolves to the real (lowercase) node key.
    summary = bringup.node_summary("INK50-DH01-H01-11")
    assert summary["node"] == "ink50-dh01-h01-11"
    assert summary["present"] is True

    bringup._daily_cache.clear()
    monkeypatch.setattr(bringup.host_exec, "run_on_host", _fake_run(status=""))
    absent = bringup.node_summary("ink50-dh01-h99-99")
    assert absent["present"] is False
    # Columns still come from the daily-report header; every job reads unknown/blank.
    assert [j["name"] for j in absent["jobs"]] == ["dcgm", "nvbandwidth", "allreduce", "allreduce-nonvlink"]


def test_daily_report_cache(monkeypatch):
    bringup._daily_cache.clear()
    monkeypatch.setenv("RACK_BRINGUP_CACHE_TTL_SEC", "60")
    calls = {"n": 0}

    def run(cmd, **_kw):
        calls["n"] += 1
        return subprocess.CompletedProcess(cmd, 0, stdout=DAILY_CSV, stderr="")

    monkeypatch.setattr(bringup.host_exec, "run_on_host", run)
    bringup.run_daily_report()
    bringup.run_daily_report()
    assert calls["n"] == 1  # second call served from cache


def test_report_scan_concurrency_is_bounded(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen.update(kw.get("env_extra") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout=FLEET_STATUS, stderr="")

    monkeypatch.setattr(bringup.host_exec, "run_on_host", run)
    monkeypatch.setenv("RACK_BRINGUP_SCAN_JOBS", "12")
    bringup.run_status_check_all()
    assert seen["SCAN_JOBS_OVERRIDE"] == "12"


def test_fleet_cache_has_independent_ttl(monkeypatch):
    monkeypatch.setenv("RACK_BRINGUP_CACHE_TTL_SEC", "30")
    monkeypatch.setenv("RACK_BRINGUP_FLEET_CACHE_TTL_SEC", "300")
    assert bringup._cache_ttl() == 30
    assert bringup._fleet_cache_ttl() == 300


def test_fleet_summary_single_flight_on_cold_cache(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    payload = {"home": "/cluster/bringup", "columns": [], "metric_labels": {}, "nodes": []}

    def build():
        calls["n"] += 1
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(bringup, "_build_fleet_summary", build)
    results = []
    workers = [threading.Thread(target=lambda: results.append(bringup.fleet_summary())) for _ in range(4)]
    for worker in workers:
        worker.start()
    assert entered.wait(1)
    release.set()
    for worker in workers:
        worker.join(2)

    assert calls["n"] == 1
    assert len(results) == 4
    assert all(result["nodes"] == [] for result in results)


def test_stale_fleet_returns_while_one_refresh_runs(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    payload = {"home": "/cluster/bringup", "columns": [], "metric_labels": {}, "nodes": []}
    bringup._fleet_cache[bringup.bringup_home()] = (0.0, payload)
    calls = {"n": 0}

    def build():
        calls["n"] += 1
        entered.set()
        assert release.wait(2)
        return payload

    monkeypatch.setattr(bringup, "_build_fleet_summary", build)
    first = bringup.fleet_summary()
    second = bringup.fleet_summary()

    assert entered.wait(1)
    assert first["cache"]["refreshing"] is True
    assert second["cache"]["refreshing"] is True
    assert calls["n"] == 1
    release.set()
    with bringup._fleet_cache_condition:
        assert bringup._fleet_cache_condition.wait_for(
            lambda: bringup.bringup_home() not in bringup._fleet_refreshing,
            timeout=2,
        )


def test_bringup_home_and_enabled(monkeypatch):
    monkeypatch.delenv("RACK_BRINGUP_HOME", raising=False)
    assert bringup.bringup_home() == "/home/cmods/bringup"
    monkeypatch.setenv("RACK_BRINGUP_HOME", "/opt/bringup/")
    assert bringup.bringup_home() == "/opt/bringup"

    monkeypatch.delenv("RACK_BRINGUP_DISABLED", raising=False)
    assert bringup.is_enabled() is True
    monkeypatch.setenv("RACK_BRINGUP_DISABLED", "1")
    assert bringup.is_enabled() is False


def test_demo_node_summary():
    summary = bringup.demo_node_summary("demo-1")
    assert summary["node"] == "demo-1"
    assert summary["present"] is True
    assert any(j["status"] == "FAIL" for j in summary["jobs"])


def test_absent_optional_bringup_is_a_successful_empty_state(monkeypatch):
    monkeypatch.delenv("RACK_STATUS_DEMO", raising=False)
    fake = SimpleNamespace(
        is_enabled=lambda: True,
        config=lambda: {"enabled": True, "present": False, "home": "/home/cmods/bringup"},
        bringup_home=lambda: "/home/cmods/bringup",
    )
    monkeypatch.setattr(app_module, "_bringup", fake)
    client = app_module.app.test_client()

    node = client.get("/api/bringup/node?node=ink50-dh01-a01-11")
    assert node.status_code == 200
    assert node.get_json()["data"] == {
        "node": "ink50-dh01-a01-11",
        "present": False,
        "slurm_state": "",
        "home": "/home/cmods/bringup",
        "jobs": [],
    }

    fleet = client.post("/api/bringup/fleet", json={"nodes": ["ink50-dh01-a01-11"]})
    assert fleet.status_code == 200
    assert fleet.get_json()["data"]["nodes"] == []


class TestFleet:
    """The fleet view: grouped nodesets expanded, then merged with the metric CSV."""

    def test_grouped_nodesets_are_expanded_to_every_node(self):
        fleet = bringup.parse_status_check_fleet(FLEET_STATUS)
        assert set(fleet) == {
            "ink50-dh01-h01-11", "ink50-dh01-h01-14", "ink50-dh01-h01-15",
            "ink50-dh01-h02-11", "ink50-dh01-h02-28", "ink50-dh01-h02-29",
        }
        # Every node in a group carries that group's verdicts, including the reason.
        for node in ("ink50-dh01-h01-11", "ink50-dh01-h01-15"):
            assert fleet[node]["allreduce"]["status"] == "DONE"
            assert fleet[node]["allreduce-nonvlink"]["status"] == "FAIL"
            assert fleet[node]["allreduce-nonvlink"]["reason"] == "run error"
        assert fleet["ink50-dh01-h02-28"]["allreduce"]["status"] == "NOT_RUN"

    def test_every_node_gets_the_same_column_count(self):
        """A short node means a block was mis-attributed; uniformity is the signal."""
        fleet = bringup.parse_status_check_fleet(FLEET_STATUS)
        assert {len(v) for v in fleet.values()} == {4}

    def test_fleet_summary_merges_status_with_metric(self, monkeypatch):
        monkeypatch.setattr(bringup.host_exec, "run_on_host", _fake_run(status=FLEET_STATUS))
        bringup._daily_cache.clear()
        data = bringup.fleet_summary()

        assert data["columns"] == ["dcgm", "nvbandwidth", "allreduce", "allreduce-nonvlink"]
        assert data["metric_labels"]["allreduce"] == "busbw GB/s"
        by_node = {n["node"]: n for n in data["nodes"]}

        # h01-11 is in both sources: status from status-check, metric from the CSV.
        jobs = {j["name"]: j for j in by_node["ink50-dh01-h01-11"]["jobs"]}
        assert jobs["allreduce"]["status"] == "DONE"
        assert jobs["allreduce"]["metric"] == "688.68"
        assert by_node["ink50-dh01-h01-11"]["slurm_state"] == "down*"

        # A FAIL keeps its number in the CSV, so the metric must survive alongside the FAIL.
        assert jobs["allreduce-nonvlink"]["status"] == "FAIL"
        assert jobs["allreduce-nonvlink"]["metric"] == "2.28"
        assert jobs["allreduce-nonvlink"]["reason"] == "run error"

    def test_nodes_known_to_only_one_source_still_appear(self, monkeypatch):
        monkeypatch.setattr(bringup.host_exec, "run_on_host", _fake_run(status=FLEET_STATUS))
        bringup._daily_cache.clear()
        names = {n["node"] for n in bringup.fleet_summary()["nodes"]}
        assert "ink50-dh01-h01-12" in names  # CSV only (not in the status groups)
        assert "ink50-dh01-h02-28" in names  # status only (not in the CSV)

    def test_demo_fleet_summary_spreads_values(self):
        data = bringup.demo_fleet_summary([f"demo-r01-{i:02d}" for i in range(1, 19)])
        assert len(data["nodes"]) == 18
        statuses = {j["status"] for n in data["nodes"] for j in n["jobs"]}
        assert {"DONE", "FAIL", "NOT_RUN"} <= statuses
        assert any(n["slurm_state"] == "down*" for n in data["nodes"])
