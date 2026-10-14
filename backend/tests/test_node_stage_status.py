# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A rack is validated by a run over all of its nodes; a run over fewer is each node's evidence.

On Nscale NC (2026-10-09) a one-node Stage-1 run on gpu19-su23 was written as
``cmods-NCCL-1Rack-V20-1N-119310.txt``, and the rack table -- which never looked at ``-<N>N-`` --
painted rack V20's Stage 1 done and archived it as a permanent acceptance pass; two one-node NeMo
pilots then painted its Stage 2 failed. These tests pin the split: full-rack logs drive the rack,
partial logs drive the nodes they ran on, activities say which one a live launch is, and
``/api/node-stages`` reports Stage 1 from those logs and Stage 2 from the bring-up framework.
"""
from __future__ import annotations

import os
from pathlib import PurePosixPath

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import native_status
import node_stages
import node_targets
import pytest
import results_history
import validation_activities as va

HOST = "sys1-gso1-p-phy-gpu19-su23"
RACK_HOSTS = [f"sys1-gso1-p-phy-gpu{n}-su23" for n in range(19, 37)]

NCCL_1N = f"""# NVIS: aggregated from llmb-run experiment microbenchmark_nccl_nccl_119310
# job=119310 rack=V20 nodes=1 tests=24
# Collective test starting: all_reduce_perf_mpi
#  Rank  0 Group  0 Pid 1875886 on {HOST} device  0 [0008:06:00] NVIDIA GB300
#  Rank  1 Group  0 Pid 1875887 on {HOST} device  1 [0009:06:00] NVIDIA GB300
# Avg bus bandwidth    : 219.516
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.delenv("RACK_FULL_RACK_NODES", raising=False)
    monkeypatch.setattr(va, "ACTIVITIES_FILE", tmp_path / "validation_activities.json")
    monkeypatch.setattr(va, "LEGACY_JOBS_FILE", tmp_path / "multirack_jobs.json")
    monkeypatch.setattr(va, "ACTIVITY_ARCHIVE_DB", tmp_path / "validation_activities.sqlite3")
    monkeypatch.setattr(va, "_archive_sync_signature", None)
    # Never ask a real Slurm; tests that care install their own answer.
    monkeypatch.setattr(native_status, "augment_running_set_for_job_ids", lambda *_a: None)


# ---------------------------------------------------------------- which logs speak for a rack


@pytest.mark.parametrize(
    ("name", "full", "partial"),
    [
        ("cmods-NCCL-1Rack-U22-18N-118993.txt", True, False),
        ("cmods-NCCL-1Rack-V20-1N-119310.txt", False, True),
        ("rkoike-NEMO-1Rack-V20-1N-125310.txt", False, True),
        ("rkoike-NEMO-1Rack-U20-16N-1.txt", False, True),
        ("cmods-nemo-r01-7.txt", True, False),  # no node count: older logs keep counting
    ],
)
def test_only_a_run_over_the_whole_rack_is_rack_evidence(name, full, partial):
    assert native_status._is_full_rack_log(name) is full
    assert native_status._is_partial_rack_log(name) is partial


def test_the_rack_size_is_a_setting(monkeypatch):
    monkeypatch.setenv("RACK_FULL_RACK_NODES", "16")
    assert native_status._is_full_rack_log("x-NCCL-1Rack-r01-16N-1.txt")
    assert not native_status._is_full_rack_log("x-NCCL-1Rack-r01-18N-1.txt")


def test_the_hosts_of_a_partial_run_come_from_its_content(monkeypatch):
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda *_a, **_k: (NCCL_1N, {}))
    assert native_status._hosts_in_partial_log(PurePosixPath("/l/x.txt")) == [HOST]

    nemo = f"+ echo x\nNODELIST: {HOST}\nINFO: LLM params model=nemotronh\n"
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda *_a, **_k: (nemo, {}))
    assert native_status._hosts_in_partial_log(PurePosixPath("/l/y.txt")) == [HOST]


def _scan(monkeypatch, entries, probes, texts):
    """Run the real log worker over fake files; return the state and the archive calls."""
    archived = []
    monkeypatch.setattr(native_status, "_glob_one_rack_logs", lambda _d: entries)
    monkeypatch.setattr(native_status, "_load_log_cache", lambda _p: {})
    monkeypatch.setattr(native_status, "_save_log_cache", lambda *_a: None)
    monkeypatch.setattr(native_status, "_rack_from_1rack_log", lambda name: name.split("-1Rack-")[1].split("-")[0])
    monkeypatch.setattr(native_status, "_archive_completed_native_probe", lambda p, **kw: archived.append((p.name, kw["rack"])))
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda p, **_k: (texts[PurePosixPath(p).name], {}))
    st = native_status.NativeRackTableState("k", PurePosixPath("/logs"))
    st._worker_logs(lambda p: probes[p.name])
    return st, archived


def test_a_one_node_pass_is_the_nodes_and_never_the_racks(monkeypatch):
    one = "cmods-NCCL-1Rack-V20-1N-119310.txt"
    st, archived = _scan(
        monkeypatch,
        [(PurePosixPath(f"/logs/{one}"), 1_000_000_000, 10)],
        {one: {"nccl_ok": True, "jid": 119310}},
        {one: NCCL_1N},
    )
    assert st.by_rack_stage_log == {} and st.by_rack_log == {}
    assert archived == [(one, None)], "a one-node pass must not be archived as rack acceptance"
    assert st.by_node_stage_log[HOST]["nccl"]["nccl_ok"] is True
    assert st.by_node_stage_log[HOST]["nccl"]["_rack"] == "V20"


def test_a_full_rack_run_still_drives_the_rack(monkeypatch):
    full = "cmods-NCCL-1Rack-U22-18N-118993.txt"
    st, archived = _scan(
        monkeypatch,
        [(PurePosixPath(f"/logs/{full}"), 1_000_000_000, 10)],
        {full: {"nccl_ok": True, "jid": 118993}},
        {},
    )
    assert st.by_rack_stage_log["U22"]["nccl"]["nccl_ok"] is True
    assert archived == [(full, "U22")]
    assert st.by_node_stage_log == {}, "a rack run does not need its log read for hosts"


def test_a_later_one_node_pass_does_not_hide_the_racks_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    for name, started in (
        ("cmods-NCCL-1Rack-V20-18N-100.txt", "2026-10-08T10:00:00Z"),
        ("cmods-NCCL-1Rack-V20-1N-119310.txt", "2026-10-09T10:00:00Z"),
    ):
        results_history.ingest_run({
            "source_log_path": f"/logs/{name}", "workload_kind": "nccl", "rack": "V20",
            "job_id": name.rsplit("-", 1)[1][:-4], "started_at": started, "status": "pass",
        })
    accepted = results_history.latest_successful_runs_by_rack(("nccl",), accept_log=native_status._is_full_rack_log)
    assert accepted["nccl"]["V20"]["job_id"] == "100"
    # Without the filter the one-node row wins -- the record the rack table used to read.
    assert results_history.latest_successful_runs_by_rack(("nccl",))["nccl"]["V20"]["job_id"] == "119310"


def test_a_rack_with_only_one_node_runs_has_no_durable_acceptance(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    results_history.ingest_run({
        "source_log_path": "/logs/cmods-NCCL-1Rack-V20-1N-119310.txt", "workload_kind": "nccl",
        "rack": "V20", "job_id": "119310", "started_at": "2026-10-09T10:00:00Z", "status": "pass",
    })
    monkeypatch.setattr(native_status, "_current_stage_evidence_by_rack", dict)
    assert native_status._durable_completed_stage_logs_by_rack() == {}


# ---------------------------------------------------------------- what a launch is


def test_a_target_smaller_than_a_rack_is_a_node_launch():
    assert node_targets.launch_scope_for_target(HOST) == ("node", [HOST])
    assert node_targets.launch_scope_for_target(",".join(RACK_HOSTS)) == ("rack", [])
    assert node_targets.launch_scope_for_target("p17-r01-ct[01-18]")[0] == "rack"
    assert node_targets.launch_scope_for_target("")[0] == "rack"


def _live(scope, nodes, rack="V20", ref="125279"):
    aid = va.record_activity("nccl-fullrack", stage="stage1", scope=scope, rack=rack, racks=[rack],
                             log_dir="/logs", nodes=nodes)
    va.add_or_update_task(aid, ordinal=1, label="nccl", scheduler_ref=ref, work_phase="running", evidence="squeue")


def test_a_live_node_run_paints_its_node_and_not_its_rack():
    _live("node", [HOST])
    assert va.live_stage_jobs_by_rack() == {}
    assert va.live_stage_jobs_by_node() == {HOST: {"nccl": "125279"}}


def test_a_live_rack_run_still_paints_the_rack():
    _live("rack", [], rack="U22", ref="124921")
    assert va.live_stage_jobs_by_rack() == {"U22": {"nccl": "124921"}}
    assert va.live_stage_jobs_by_node() == {}


# ---------------------------------------------------------------- the per-node answer


def test_stage1_state_prefers_the_live_launch_over_the_previous_log():
    old = {"nccl_ok": False, "jid": 1, "_path": "/l/old.txt"}
    assert node_stages.stage1_state(old, "2", {"2"})["state"] == "ongoing"
    assert node_stages.stage1_state(old, "", set())["state"] == "fail"
    assert node_stages.stage1_state({"nccl_ok": True, "jid": 3}, "", set())["state"] == "done"
    assert node_stages.stage1_state(None, "", set()) == {"state": "pending"}


@pytest.mark.parametrize(
    ("status", "state"),
    [("DONE", "done"), ("FAIL", "fail"), ("RETRY", "ongoing"), ("RUNNING", "ongoing"), ("NOT_RUN", "pending")],
)
def test_stage2_is_the_bringup_verdict(status, state):
    assert node_stages.stage2_state({"status": status, "reason": ""})["state"] == state


def test_node_stages_joins_logs_activities_and_bringup(monkeypatch):
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: ({"125281"}, {}))
    other = "sys1-gso1-p-phy-gpu100-su21"
    _live("node", [other], rack="Q24", ref="125281")
    verdicts = {HOST: {"llm-pretrain-solo": {"status": "DONE", "reason": ""}},
                other: {"llm-pretrain-solo": {"status": "FAIL", "reason": "loss diverged"}}}
    monkeypatch.setattr(node_stages._bringup, "get", lambda wait=False: verdicts)
    logs = {HOST: {"nccl": {"nccl_ok": True, "jid": 119310, "_path": "/l/a.txt"}}}
    out = node_stages.node_stages([(HOST, "V20"), (other, "Q24")], logs)
    rows = {r["node"]: r for r in out["nodes"]}
    assert rows[HOST]["stage1"]["state"] == "done" and rows[HOST]["stage2"]["state"] == "done"
    assert rows[other]["stage1"] == {"state": "ongoing", "job_id": "125281"}
    assert rows[other]["stage2"] == {"state": "fail", "status": "FAIL", "reason": "loss diverged"}
    assert out["stage2_source"] == "bringup:llm-pretrain-solo" and out["stage2_loading"] is False


def test_stage2_reads_unknown_until_the_bringup_verdicts_land(monkeypatch):
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr(node_stages._bringup, "get", lambda wait=False: None)
    out = node_stages.node_stages([(HOST, "V20")], {})
    assert out["nodes"][0]["stage2"] == {"state": "unknown"} and out["stage2_loading"] is True


def _fresh_verdicts(monkeypatch, *, fail=False):
    import bringup

    calls: list[list[str]] = []

    def run(*, tests=(), timeout=None):
        calls.append(list(tests))
        if fail:
            raise RuntimeError("report_job-status.sh timed out")
        return f"----------------\n{HOST}\n----------------\n{tests[0]}: DONE\n"

    monkeypatch.setattr(bringup, "run_status_check_all", run)
    monkeypatch.setattr(bringup, "is_enabled", lambda: True)
    return node_stages._BringupVerdicts(), calls


def test_the_refresh_asks_the_bringup_report_for_the_stage2_job_only(monkeypatch):
    monkeypatch.delenv("RACK_NODE_STAGE2_BRINGUP_JOB", raising=False)
    verdicts, calls = _fresh_verdicts(monkeypatch)
    out = verdicts.get(wait=True)
    assert calls == [["llm-pretrain-solo"]]
    assert out[HOST]["llm-pretrain-solo"]["status"] == "DONE"
    verdicts.get(wait=True)
    assert len(calls) == 1  # fresh: served from the cache


def test_a_changed_stage2_job_expires_the_cached_verdicts(monkeypatch):
    """The cache holds one job's column; another job's answer must not be read from it."""
    monkeypatch.delenv("RACK_NODE_STAGE2_BRINGUP_JOB", raising=False)
    verdicts, calls = _fresh_verdicts(monkeypatch)
    verdicts.get(wait=True)
    monkeypatch.setenv("RACK_NODE_STAGE2_BRINGUP_JOB", "llm-pretrain")
    out = verdicts.get(wait=True)
    assert calls == [["llm-pretrain-solo"], ["llm-pretrain"]]
    assert "llm-pretrain" in out[HOST]


def test_a_failed_refresh_waits_out_the_ttl_instead_of_rerunning_every_call(monkeypatch):
    monkeypatch.delenv("RACK_NODE_STAGE2_BRINGUP_JOB", raising=False)
    verdicts, calls = _fresh_verdicts(monkeypatch, fail=True)
    assert verdicts.get(wait=True) is None
    verdicts.get(wait=True)
    assert len(calls) == 1
    assert "timed out" in verdicts.error
    # ...and is tried again once the retry wait has passed.
    verdicts._at -= node_stages._RETRY_AFTER_ERROR_SEC
    verdicts.get(wait=True)
    assert len(calls) == 2


def test_the_stage2_job_is_a_setting(monkeypatch):
    monkeypatch.setenv("RACK_NODE_STAGE2_BRINGUP_JOB", "llm-pretrain")
    assert node_stages.stage2_bringup_job() == "llm-pretrain"
    monkeypatch.delenv("RACK_NODE_STAGE2_BRINGUP_JOB")
    assert node_stages.stage2_bringup_job() == "llm-pretrain-solo"


def test_a_node_run_submitted_as_another_user_still_reads_as_running(monkeypatch):
    """The service's own squeue misses the operator's jobs; the node view asks Slurm by job id."""
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    asked = []

    def augment(running, _times, want):
        asked.extend(want)
        running.update(j for j in want if j == "125824")

    monkeypatch.setattr(native_status, "augment_running_set_for_job_ids", augment)
    monkeypatch.setattr(node_stages._bringup, "get", lambda wait=False: {})
    _live("node", [HOST], ref="125824")
    out = node_stages.node_stages([(HOST, "V20")], {})
    assert asked == ["125824"]
    assert out["nodes"][0]["stage1"] == {"state": "ongoing", "job_id": "125824"}


def test_the_snapshot_carries_each_nodes_evidence_and_status_does_not_serve_it(monkeypatch):
    """The per-node evidence rides the canonical snapshot (cross-process, survives restarts)."""
    one = "cmods-NCCL-1Rack-V20-1N-119310.txt"
    st, _ = _scan(
        monkeypatch,
        [(PurePosixPath(f"/logs/{one}"), 1_000_000_000, 10)],
        {one: {"nccl_ok": True, "jid": 119310, "nemo_state": "done"}},
        {one: NCCL_1N},
    )
    payload = st.node_stage_logs_payload()
    assert payload == {HOST: {"nccl": {"jid": 119310, "nccl_ok": True, "llm_ok": None,
                                       "_path": f"/logs/{one}", "_mtime": 1.0, "_rack": "V20",
                                       "fail_reason": ""}}}


def test_node_stages_route_reads_the_snapshot(monkeypatch):
    from types import SimpleNamespace

    import app as app_module
    from api import node_stages as route

    snap = SimpleNamespace(payload={
        "nodes": [{"id": HOST, "rack": "V20"}, {"id": "sys1-gso1-p-phy-gpu20-su23", "rack": "V20"},
                  {"id": "other", "rack": "Q7"}],
        "node_stage_logs": {HOST: {"nccl": {"nccl_ok": True, "jid": 119310}}},
    })

    monkeypatch.setattr(route, "get_cluster_snapshot", lambda _d, wait=False: snap)
    monkeypatch.setattr(native_status, "load_squeue_jobs_map", lambda: (set(), {}))
    monkeypatch.setattr(node_stages._bringup, "get", lambda wait=False: {})
    monkeypatch.setattr("api.dashboard._ensure_rack_status_background_thread", lambda: None)
    monkeypatch.setattr("api.dashboard._start_cmsh_rack_list_warm_thread", lambda: None)
    monkeypatch.setattr(route, "_resolve_log_dir", lambda _raw: PurePosixPath("/logs"))
    r = app_module.app.test_client().get("/api/node-stages?rack=V20")
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"]
    assert [n["node"] for n in body["nodes"]] == [HOST, "sys1-gso1-p-phy-gpu20-su23"]
    assert body["nodes"][0]["stage1"]["state"] == "done"


def test_node_stages_route_says_retry_while_the_inventory_loads(monkeypatch):
    import app as app_module
    from api import node_stages as route

    class _Empty:
        payload = None

    monkeypatch.setattr(route, "get_cluster_snapshot", lambda _d, wait=False: _Empty())
    monkeypatch.setattr("api.dashboard._ensure_rack_status_background_thread", lambda: None)
    monkeypatch.setattr("api.dashboard._start_cmsh_rack_list_warm_thread", lambda: None)
    monkeypatch.setattr(route, "_resolve_log_dir", lambda _raw: PurePosixPath("/logs"))
    r = app_module.app.test_client().get("/api/node-stages")
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    assert r.status_code == 503 and r.headers.get("Retry-After") == "30"


# ---------------------------------------------------------------- a failed run says so, and why

FABRIC_HOST = "sys1-gso1-p-phy-gpu100-su31"
# Nscale NC Slurm 125831: the NVLink fabric was not up, every test died at "Using devices", and the
# log never printed a rank line -- so it was evidence for nobody and the node stayed "pending".
NCCL_FABRIC_DOWN = f"""# NVIS: aggregated from llmb-run experiment microbenchmark_nccl_nccl_fp8_125831
# job=125831 rack=X24 nodes=1 tests=24
# --- 1_1_LOG_all_reduce_env1_args1_job125831_iter1 ---
# nccl-tests version 2.17.8 nccl-headers=22902 nccl-library=22903
# Collective test starting: all_reduce_perf_mpi
#
# Using devices
{FABRIC_HOST}: Test CUDA failure util.cu:555 'system not yet initialized'
 .. {FABRIC_HOST} pid 3252989: Test failure common.cu:1187
"""

# What collect_llmb_nccl_log.sh writes for a job that never started (Slurm 133870).
NCCL_NOT_STARTED = f"""# NVIS: llmb-run job 133870 left no nccl-tests output; it did not start on its nodes
# job=133870 rack=U24 nodes=1 tests=0
NODELIST: {HOST}
# NVIS-FAILURE: container did not start (pyxis): No such file or directory: /opt/containers/x.sqsh
"""


@pytest.mark.parametrize(
    ("text", "host", "reason"),
    [
        (NCCL_FABRIC_DOWN, FABRIC_HOST, "NVLink fabric not initialized (CUDA: system not yet initialized)"),
        (NCCL_NOT_STARTED, HOST, "container did not start (pyxis): No such file or directory: /opt/containers/x.sqsh"),
        (NCCL_1N, HOST, ""),
    ],
)
def test_a_partial_log_names_its_hosts_and_why_it_failed(monkeypatch, text, host, reason):
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda *_a, **_k: (text, {}))
    assert native_status._scan_partial_log(PurePosixPath("/l/x.txt")) == ([host], reason)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("h: Test NCCL failure common.cu:1084 'remote process exited or there was a network error'",
         "NCCL failure: remote process exited or there was a network error"),
        ("h: Test CUDA failure common.cu:900 'out of memory'", "CUDA failure: out of memory"),
        ("h: Test timeout common.cu:1 ", "nccl-tests timed out"),
        ("# Avg bus bandwidth : 219.5", ""),
    ],
)
def test_the_failure_reason_reads_like_an_operator_would_say_it(text, reason):
    assert native_status.stage_failure_reason(text) == reason


def test_a_node_whose_run_failed_reads_fail_with_the_reason(monkeypatch):
    name = "cmods-NCCL-1Rack-X24-1N-125831.txt"
    st, _ = _scan(
        monkeypatch,
        [(PurePosixPath(f"/logs/{name}"), 1_000_000_000, 10)],
        {name: {"nccl_ok": False, "jid": 125831}},
        {name: NCCL_FABRIC_DOWN},
    )
    probe = st.node_stage_logs_payload()[FABRIC_HOST]["nccl"]
    assert node_stages.stage1_state(probe, "", set()) == {
        "state": "fail", "job_id": "125831", "log": f"/logs/{name}",
        "reason": "NVLink fabric not initialized (CUDA: system not yet initialized)",
    }


def test_a_pass_carries_no_reason():
    assert "reason" not in node_stages.stage1_state({"nccl_ok": True, "jid": 1, "fail_reason": "x"}, "", set())


def test_logs_scanned_before_failure_lines_counted_are_read_once_more(monkeypatch):
    """A log cached with no hosts by the older scan must be re-read, or its node stays "pending"."""
    name = "cmods-NCCL-1Rack-X24-1N-125831.txt"
    old = {f"/logs/{name}": {"mtime_ns": 1_000_000_000, "size": 10, "hosts": [],
                             "probe": {"nccl_ok": False, "jid": 125831}}}
    saved = []
    monkeypatch.setattr(native_status, "_glob_one_rack_logs", lambda _d: [(PurePosixPath(f"/logs/{name}"), 1_000_000_000, 10)])
    monkeypatch.setattr(native_status, "_load_log_cache", lambda _p: old)
    monkeypatch.setattr(native_status, "_save_log_cache", lambda _p, disk, _alive: saved.append(disk))
    monkeypatch.setattr(native_status, "_rack_from_1rack_log", lambda _n: "X24")
    monkeypatch.setattr(native_status, "_archive_completed_native_probe", lambda *_a, **_k: None)
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda *_a, **_k: (NCCL_FABRIC_DOWN, {}))
    st = native_status.NativeRackTableState("k", PurePosixPath("/logs"))
    st._worker_logs(lambda _p: pytest.fail("the probe itself is still cached"))
    assert st.by_node_stage_log[FABRIC_HOST]["nccl"]["fail_reason"].startswith("NVLink fabric not initialized")
    entry = saved[-1][f"/logs/{name}"]
    assert entry["hosts"] == [FABRIC_HOST] and entry["scan"] == native_status._PARTIAL_LOG_SCAN_VERSION

    # Scanned once, never again: the next pass reads the cache, not the file.
    monkeypatch.setattr(native_status, "_load_log_cache", lambda _p: saved[-1])
    monkeypatch.setattr(native_status.host_exec, "read_text", lambda *_a, **_k: pytest.fail("re-read a scanned log"))
    st2 = native_status.NativeRackTableState("k", PurePosixPath("/logs"))
    st2._worker_logs(lambda _p: pytest.fail("the probe itself is still cached"))
    assert st2.by_node_stage_log[FABRIC_HOST]["nccl"]["fail_reason"].startswith("NVLink fabric")
