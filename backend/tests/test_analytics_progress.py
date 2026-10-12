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
"""The analytics aggregate reports how far through the log corpus it is.

It used to be a POST that blocked for minutes behind an indeterminate spinner. The work is a
known number of logs -- the server enumerates them before it parses anything -- so it can be run
as a job that reports done/total per file.

These tests pin the job lifecycle and the batch plumbing the progress rides on, including the
--paths-from route that keeps a 4000-file fleet clear of ARG_MAX.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCANNER = REPO_ROOT / "bin" / "scripts" / "nvis_logscan.py"

_spec = importlib.util.spec_from_file_location("nvis_logscan_p", SCANNER)
assert _spec and _spec.loader
logscan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(logscan)


def _steps(n: int) -> str:
    return "Step Time : 4.22s GPU utilization: 1954.6MODEL_TFLOP/s/GPU\n" * n


# --- the scanner side: many files, one process, one record each ------------------------------


def test_paths_from_file_avoids_a_giant_argv(tmp_path):
    """A directory scan is capped at 4000 files and 1020 paths is already ~150 KB of argv."""
    logs = []
    for i in range(5):
        p = tmp_path / f"cmods-NEMO-1Rack-p05-r{i:02d}-18N-{i}.txt"
        p.write_text(_steps(20), encoding="utf-8")
        logs.append(str(p))
    listing = tmp_path / "paths.txt"
    listing.write_text("\n".join(logs) + "\n", encoding="utf-8")

    proc = subprocess.run(
        ["python3", str(SCANNER), "--ndjson", "--no-inventory", "--paths-from", str(listing)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    recs = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(recs) == 5
    assert {r["path"] for r in recs} == set(logs)


def test_paths_from_stdin(tmp_path):
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-1.txt"
    p.write_text(_steps(20), encoding="utf-8")
    proc = subprocess.run(
        ["python3", str(SCANNER), "--ndjson", "--no-inventory", "--paths-from", "-"],
        input=f"{p}\n",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip())["path"] == str(p)


def test_no_paths_at_all_is_an_error(tmp_path):
    proc = subprocess.run(
        ["python3", str(SCANNER), "--ndjson"], capture_output=True, text=True, timeout=60, check=False
    )
    assert proc.returncode != 0
    assert "no paths" in (proc.stderr or "").lower()


def test_ndjson_emits_one_record_per_file_as_it_goes(tmp_path):
    """Progress is per file, so the stream has to be one record per file and in order."""
    paths = []
    for i in range(4):
        p = tmp_path / f"log{i}.txt"
        p.write_text(_steps(15), encoding="utf-8")
        paths.append(str(p))
    proc = subprocess.run(
        ["python3", str(SCANNER), "--ndjson", "--no-inventory", *paths],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == len(paths)
    assert [json.loads(ln)["path"] for ln in lines] == paths, "order must match the request"


# --- the app side: job lifecycle --------------------------------------------------------------


@pytest.fixture()
def app_module():
    import sys

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import app as app_module

    return app_module


@pytest.fixture()
def analytics_module(app_module):
    """The module that now owns the analytics helpers.

    Depends on ``app_module`` only so ``backend/`` is on ``sys.path`` first; the helpers themselves no
    longer live in ``app``.
    """
    from api import analytics

    return analytics


@pytest.fixture()
def client(app_module):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_async_start_returns_a_job_id_and_status_reports_done(client, app_module):
    """Demo mode short-circuits the compute, so this exercises the job lifecycle itself."""
    started = client.post("/api/analytics/aggregate-async", json={"rows": []}).get_json()
    assert started["ok"] is True
    job_id = started["job_id"]

    body = None
    for _ in range(100):
        body = client.get(f"/api/analytics/aggregate-status?job_id={job_id}").get_json()
        if body.get("state") in ("done", "error"):
            break
        import time as _t

        _t.sleep(0.05)
    assert body is not None
    assert body["state"] == "done", body
    assert body["payload"], "the finished job must hand back the aggregate"


def test_status_for_an_unknown_job_is_404(client, app_module):
    res = client.get("/api/analytics/aggregate-status?job_id=nope")
    assert res.status_code == 404


def test_the_payload_is_handed_over_once(client, app_module):
    """Polling must not pin a full aggregate in memory for every abandoned tab."""
    job_id = client.post("/api/analytics/aggregate-async", json={"rows": []}).get_json()["job_id"]
    for _ in range(100):
        body = client.get(f"/api/analytics/aggregate-status?job_id={job_id}").get_json()
        if body.get("state") == "done":
            break
        import time as _t

        _t.sleep(0.05)
    again = client.get(f"/api/analytics/aggregate-status?job_id={job_id}").get_json()
    assert again["state"] == "collected"
    assert "payload" not in again


def test_bad_body_is_rejected_before_a_job_is_created(client, app_module):
    res = client.post("/api/analytics/aggregate-async", json={"rows": "not-a-list"})
    assert res.status_code == 400
    assert "rows" in (res.get_json().get("error") or "")


def test_the_blocking_route_still_works(client, app_module):
    """API callers that never learned about the job endpoints must be unaffected."""
    body = client.post("/api/analytics/aggregate", json={"rows": []}).get_json()
    assert body.get("ok") is not False


# --- shaping a scanner record like parser.sh --------------------------------------------------


def test_scan_record_is_shaped_like_parser_json(analytics_module, tmp_path):
    """Downstream pickers read parser.sh's field names; the batch payload must match them."""
    p = tmp_path / "cmods-NCCL-1Rack-p05-r01-18N-4242.txt"
    hdr = (
        "#       size         count      type   redop    root     time   algbw   busbw  #wrong"
        "     time   algbw   busbw  #wrong"
    )
    row = (
        "   1073741824     268435456     float     sum      -1   100.00    0.00  916.00"
        "       0   100.00    0.00  916.00       0"
    )
    p.write_text(
        "# Collective test starting: all_reduce_perf_mpi\n"
        f"{hdr}\n{row}\n"
        "# Out of bounds values : 0 OK\n"
        "# Avg bus bandwidth    : 916.00\n"
        "# Collective test concluded: all_reduce_perf_mpi\n" + _steps(30),
        encoding="utf-8",
    )
    rec = logscan.scan(str(p), inventory=False)
    shaped = analytics_module._parser_json_from_scan(rec, "p05-r01", "4242")

    nccl = shaped["nccl_peak_out_of_place_busbw_gbps"][0]
    nemo = shaped["nemotron_5h_56b_train_window"][0]
    assert nccl["all_reduce_gbps"] == "916.0"
    assert nccl["all_reduce_busbw_series"], "the chart series must survive the shaping"
    assert nemo["status"] == "OK"
    assert nemo["tflops_per_gpu"] == pytest.approx(1954.6, rel=1e-3)
    # A batch scan never races the writer, so a gap is a lost marker rather than a partial read.
    assert nccl["sweep_complete"] is True
    assert nccl["sweep_running"] is False


def test_the_batch_lookup_path_actually_runs(analytics_module, tmp_path):
    """Exercise _parser_json_for_log_path THROUGH the batch, not just the shaping helper.

    Testing the shaper alone missed a real break: the surrounding lookup called a module-level
    naming function as if it were a method, so every batch hit raised and the whole aggregate
    failed at 100% with 'NvisNaming object has no attribute job_id_from_log_basename'.
    """
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4242.txt"
    p.write_text(_steps(30), encoding="utf-8")
    rec = logscan.scan(str(p), inventory=False)

    import core
    import logscan_cache

    logscan_cache._analytics_scan_ctx.batch = {str(core.rpath(p)): rec}
    try:
        parsed, err = analytics_module._parser_json_for_log_path(tmp_path, p)
    finally:
        logscan_cache._analytics_scan_ctx.batch = None

    assert err is None
    assert parsed and parsed.get("from_logscan") is True
    assert parsed["nemotron_5h_56b_train_window"][0]["job_id"] == "4242"


def test_missing_measurements_shape_as_dashes(analytics_module, tmp_path):
    """parser.sh renders "no result" as ---; the pickers and the UI both expect that."""
    p = tmp_path / "empty.txt"
    p.write_text("nothing interesting here\n", encoding="utf-8")
    shaped = analytics_module._parser_json_from_scan(logscan.scan(str(p), inventory=False), "r", "1")
    nccl = shaped["nccl_peak_out_of_place_busbw_gbps"][0]
    assert nccl["all_reduce_gbps"] == "---"
    assert nccl["alltoall_gbps"] == "---"


# --- NCCL logs are not NeMo runs, and NCCL scales get their own table --------------------------


def _nccl_log(path: Path, alltoall: float = 134.75) -> Path:
    hdr = (
        "#       size         count      type   redop    root     time   algbw   busbw  #wrong"
        "     time   algbw   busbw  #wrong"
    )
    row = (
        f"   1073741824     268435456     float     sum      -1   100.00    0.00  {alltoall:.2f}"
        f"       0   100.00    0.00  {alltoall:.2f}       0"
    )
    path.write_text(
        "# Collective test starting: alltoall_perf_mpi\n"
        f"{hdr}\n{row}\n"
        "# Out of bounds values : 0 OK\n"
        "# Avg bus bandwidth    : 134.75\n"
        "# Collective test concluded: alltoall_perf_mpi\n",
        encoding="utf-8",
    )
    return path


def test_an_nccl_log_yields_no_nemotron_row_so_it_is_never_a_no_metrics_nemo_run(analytics_module, tmp_path):
    """parser.sh emits a placeholder Nemotron row for any file. The batch shaping used to copy it, so
    52 NCCL logs reached the Nemotron scaling table as "successful runs with no step timings"."""
    rec = logscan.scan(str(_nccl_log(tmp_path / "nccl-mpi_a2a-30n-c256_181274.out")), inventory=False)
    shaped = analytics_module._parser_json_from_scan(rec, "a2a-30n-c256", "181274")
    assert shaped["nemotron_5h_56b_train_window"] == []
    # The NCCL half is untouched.
    assert shaped["nccl_peak_out_of_place_busbw_gbps"][0]["alltoall_gbps"] == "134.75"


def test_a_nemo_log_too_short_to_average_still_gets_its_row_so_no_metrics_stays_honest(analytics_module, tmp_path):
    """The real "no metrics" case: a NeMo run that logged steps but too few survived the warmup."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4242.txt"
    p.write_text(_steps(3), encoding="utf-8")
    shaped = analytics_module._parser_json_from_scan(logscan.scan(str(p), inventory=False), "p05-r01", "4242")
    rows = shaped["nemotron_5h_56b_train_window"]
    assert len(rows) == 1
    assert rows[0]["tflops_per_gpu"] is None and rows[0]["median_step_time"] is None


def _nc(**peaks):
    base = {k: "---" for k in (
        "all_reduce_gbps", "all_gather_gbps", "alltoall_gbps", "reduce_scatter_gbps", "sendrecv_gbps",
    )}
    base.update({k: str(v) for k, v in peaks.items()})
    return base


def test_nccl_scaling_groups_by_node_count_not_by_a_rack_count_read_from_the_name(analytics_module, monkeypatch, tmp_path):
    nodes = {"pass36.out": 36, "a2a30.out": 30, "ref6.out": 6, "ref6b.out": 6, "mystery.out": None}
    monkeypatch.setattr(analytics_module, "_nccl_log_node_count", lambda lp: nodes[Path(str(lp)).name])
    bucket: dict = {}
    add = lambda name, **peaks: analytics_module._nccl_scaling_append_entry(  # noqa: E731
        bucket, tmp_path / name, _nc(**peaks)
    )
    add("pass36.out", all_reduce_gbps=798, alltoall_gbps=135)
    add("a2a30.out", alltoall_gbps=134.75)
    add("ref6.out", alltoall_gbps=100)
    add("ref6b.out", alltoall_gbps=140)
    add("mystery.out", all_reduce_gbps=50)
    rows = analytics_module._nccl_scaling_rows(bucket)

    assert [r["num_nodes"] for r in rows] == [6, 30, 36, None]  # unknown scale sorts last
    by = {r["num_nodes"]: r for r in rows}
    assert by[6]["n_logs"] == 2 and by[6]["mean_alltoall_gbps"] == 120.0
    assert by[36]["mean_all_reduce_gbps"] == 798.0 and by[36]["mean_alltoall_gbps"] == 135.0
    # A collective no log in the row measured is a dash, never a zero.
    assert by[36]["mean_all_gather_gbps"] is None


def test_an_nccl_log_with_no_measured_collective_is_counted_apart_not_averaged(analytics_module, monkeypatch, tmp_path):
    monkeypatch.setattr(analytics_module, "_nccl_log_node_count", lambda lp: 36)
    bucket: dict = {}
    analytics_module._nccl_scaling_append_entry(bucket, tmp_path / "ok.out", _nc(all_reduce_gbps=800))
    analytics_module._nccl_scaling_append_entry(bucket, tmp_path / "dead.out", _nc())
    (row,) = analytics_module._nccl_scaling_rows(bucket)
    assert row["n_logs"] == 1 and row["n_logs_no_metrics"] == 1
    assert row["logs_no_metrics"] == ["dead.out"]
    assert row["mean_all_reduce_gbps"] == 800.0  # not dragged toward zero by the dead log


def test_geometry_failure_does_not_lose_the_bandwidth(analytics_module, monkeypatch, tmp_path):
    def boom(lp):
        raise OSError("log gone")

    monkeypatch.setattr(analytics_module, "_nccl_log_node_count", boom)
    bucket: dict = {}
    analytics_module._nccl_scaling_append_entry(bucket, tmp_path / "x.out", _nc(all_reduce_gbps=700))
    (row,) = analytics_module._nccl_scaling_rows(bucket)
    assert row["num_nodes"] is None and row["mean_all_reduce_gbps"] == 700.0


def test_the_demo_aggregate_carries_nccl_scaling(analytics_module):
    demo = analytics_module._demo_analytics_aggregate()
    assert {r["num_nodes"] for r in demo["multirack"]["nccl_scaling"]} == {18, 36}


def test_the_scanner_reads_the_nccl_launchers_node_count(tmp_path):
    """MPI NCCL logs carry neither SLURM_NNODES nor NODELIST; the launcher's own header is the only
    declared scale, and it was why those runs could not be placed on a scaling table."""
    p = _nccl_log(tmp_path / "nccl-mpi_U22+U23_138496.out")
    p.write_text(
        "nccl_allreduce: nodes=36 np=144 gpus/node=4 sizes=8..16G rounds=1\n" + p.read_text(),
        encoding="utf-8",
    )
    assert logscan.scan(str(p), inventory=False)["geometry"]["nccl_nodes"] == "36"


def test_node_count_precedence_is_header_then_slurm_then_the_file_name(analytics_module, monkeypatch, tmp_path):
    def with_geometry(**g):
        monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: {"geometry": g})

    with_geometry(nccl_nodes="36", slurm_nnodes="1")
    assert analytics_module._nccl_log_node_count(tmp_path / "x-18N-1.txt") == 36  # the launcher's declaration wins
    with_geometry(slurm_nnodes="18")
    assert analytics_module._nccl_log_node_count(tmp_path / "x-30n-1.txt") == 18
    with_geometry()
    assert analytics_module._nccl_log_node_count(tmp_path / "nccl-mpi_a2a-30n-c256_181274.out") == 30
    assert analytics_module._nccl_log_node_count(tmp_path / "rkoike-NCCL-1Rack-gpu55-su19-18N-118993.txt") == 18
    # Nothing declared anywhere: unknown, never a guess.
    assert analytics_module._nccl_log_node_count(tmp_path / "nccl-mpi_U22+U23_138496.out") is None
    monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: None)
    assert analytics_module._nccl_log_node_count(tmp_path / "nccl-mpi_a2a-6n-c256_1.out") == 6


# --- per-collective single-rack series and GPUs on the scaling rows ----------------------------


def test_the_scanner_reads_the_nccl_launchers_rank_count(tmp_path):
    p = _nccl_log(tmp_path / "nccl-mpi_U22+U23_138496.out")
    p.write_text("nccl_allreduce: nodes=36 np=144 gpus/node=4 sizes=8..16G rounds=1\n" + p.read_text(), encoding="utf-8")
    geom = logscan.scan(str(p), inventory=False)["geometry"]
    assert (geom["nccl_nodes"], geom["nccl_np"]) == ("36", "144")


def test_gpu_count_comes_from_the_header_or_is_unknown(analytics_module, monkeypatch, tmp_path):
    monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: {"geometry": {"nccl_np": "144"}})
    assert analytics_module._nccl_log_gpu_count(tmp_path / "x.out") == 144
    monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: {"geometry": {}})
    assert analytics_module._nccl_log_gpu_count(tmp_path / "x.out") is None
    monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: None)
    assert analytics_module._nccl_log_gpu_count(tmp_path / "x.out") is None


def test_a_scaling_row_reports_the_gpus_of_its_newest_log_that_states_them(analytics_module, monkeypatch, tmp_path):
    gpus = {"old.out": 144, "new.out": None}
    monkeypatch.setattr(analytics_module, "_nccl_log_node_count", lambda lp: 36)
    monkeypatch.setattr(analytics_module, "_nccl_log_gpu_count", lambda lp: gpus[Path(str(lp)).name])
    monkeypatch.setattr(analytics_module, "_log_mtime_or_zero", lambda lp: 2 if Path(str(lp)).name == "new.out" else 1)
    bucket: dict = {}
    for name in ("old.out", "new.out"):
        analytics_module._nccl_scaling_append_entry(bucket, tmp_path / name, _nc(all_reduce_gbps=800))
    (row,) = analytics_module._nccl_scaling_rows(bucket)
    assert row["num_gpus"] == 144  # the newest log did not say, so the next newest that did


def _pts(*bw):
    return [{"size_bytes": 1 << (20 + i), "bus_bw_gbps": v} for i, v in enumerate(bw)]


def test_collective_series_come_from_the_scan_row_when_it_carries_them(analytics_module, monkeypatch, tmp_path):
    monkeypatch.setattr(analytics_module, "_logscan_record", lambda lp: pytest.fail("no second scan needed"))
    nc = {"busbw_series_by_collective": {"all_reduce": _pts(10, 900), "alltoall": _pts(5, 690), "sendrecv": _pts(1)}}
    out = analytics_module._collective_series_for_log(tmp_path / "x.out", nc)
    assert set(out) == {"all_reduce", "alltoall"}  # only the switchable collectives, only when measured
    assert out["alltoall"][-1]["bus_bw_gbps"] == 690


def test_parser_rows_take_the_extra_collectives_from_the_scan_and_keep_all_reduce(analytics_module, monkeypatch, tmp_path):
    """parser.sh reports only all_reduce; the whole-file scan has the rest."""
    monkeypatch.setattr(
        analytics_module, "_logscan_record",
        lambda lp: {"nccl": {"series_by_collective": {"all_gather": _pts(8, 647)}}},
    )
    out = analytics_module._collective_series_for_log(tmp_path / "x.out", {"all_reduce_busbw_series": _pts(10, 909)})
    assert out["all_gather"][-1]["bus_bw_gbps"] == 647
    assert out["all_reduce"][-1]["bus_bw_gbps"] == 909  # never regresses when the scan lacks it


def test_a_failed_scan_still_leaves_the_all_reduce_series(analytics_module, monkeypatch, tmp_path):
    def boom(lp):
        raise OSError("ssh dropped")

    monkeypatch.setattr(analytics_module, "_logscan_record", boom)
    out = analytics_module._collective_series_for_log(tmp_path / "x.out", {"all_reduce_busbw_series": _pts(10, 909)})
    assert list(out) == ["all_reduce"]


def test_the_series_block_is_median_plus_each_rack_off_it(analytics_module):
    block = analytics_module._busbw_series_block(
        {"U20": _pts(10, 900), "U21": _pts(10, 1000), "U22": _pts(10, 800)},
        {"U20": "a.txt", "U21": "b.txt"},
    )
    assert block["sizes_bytes"] == [1 << 20, 1 << 21]
    assert block["median_bus_bw_gbps"] == [10.0, 900.0]
    by = {r["rack"]: r for r in block["per_rack"]}
    assert by["U21"]["pct_vs_median"] == [0.0, 11.11]
    assert by["U21"]["log_file"] == "b.txt" and by["U22"]["log_file"] == ""
