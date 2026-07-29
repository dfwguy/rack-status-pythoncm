# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Unit tests for the per-node bring-up results module (backend/bringup.py).

The runners shell out to Travis's scripts; here ``host_exec.run_on_host`` is stubbed
with captured real output so the parsing/merge logic is exercised without a cluster.
"""
import subprocess

import bringup

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


def test_parse_daily_report_csv():
    cols, rows = bringup.parse_daily_report_csv(DAILY_CSV)
    assert cols == ["dcgm", "nvbandwidth", "allreduce", "allreduce-nonvlink"]
    assert set(rows) == {"ink50-dh01-h01-11", "ink50-dh01-h01-12"}
    r = rows["ink50-dh01-h01-11"]
    assert r["slurm"] == "down*"
    assert r["cells"]["nvbandwidth"] == "357.23"
    assert r["cells"]["allreduce-nonvlink"] == "2.28"
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
