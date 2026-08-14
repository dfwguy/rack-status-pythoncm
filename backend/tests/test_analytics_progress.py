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
def client(app_module):
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _login(client, app_module):
    """Bypass auth the same way the other API tests do."""
    if getattr(app_module, "_auth_mode", lambda: "none")() == "none":
        return
    client.post("/api/login", json={"password": os.environ.get("RACK_WEB_AUTH_TOKEN", "")})


def test_async_start_returns_a_job_id_and_status_reports_done(client, app_module):
    """Demo mode short-circuits the compute, so this exercises the job lifecycle itself."""
    _login(client, app_module)
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
    _login(client, app_module)
    res = client.get("/api/analytics/aggregate-status?job_id=nope")
    assert res.status_code == 404


def test_the_payload_is_handed_over_once(client, app_module):
    """Polling must not pin a full aggregate in memory for every abandoned tab."""
    _login(client, app_module)
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
    _login(client, app_module)
    res = client.post("/api/analytics/aggregate-async", json={"rows": "not-a-list"})
    assert res.status_code == 400
    assert "rows" in (res.get_json().get("error") or "")


def test_the_blocking_route_still_works(client, app_module):
    """API callers that never learned about the job endpoints must be unaffected."""
    _login(client, app_module)
    body = client.post("/api/analytics/aggregate", json={"rows": []}).get_json()
    assert body.get("ok") is not False


# --- shaping a scanner record like parser.sh --------------------------------------------------


def test_scan_record_is_shaped_like_parser_json(app_module, tmp_path):
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
        "# Collective test concluded: all_reduce_perf_mpi\n" + _steps(30),
        encoding="utf-8",
    )
    rec = logscan.scan(str(p), inventory=False)
    shaped = app_module._parser_json_from_scan(rec, "p05-r01", "4242")

    nccl = shaped["nccl_peak_out_of_place_busbw_gbps"][0]
    nemo = shaped["nemotron_5h_56b_train_window"][0]
    assert nccl["all_reduce_gbps"] == "916.0"
    assert nccl["all_reduce_busbw_series"], "the chart series must survive the shaping"
    assert nemo["status"] == "OK"
    assert nemo["tflops_per_gpu"] == pytest.approx(1954.6, rel=1e-3)
    # A batch scan never races the writer, so a gap is a lost marker rather than a partial read.
    assert nccl["sweep_complete"] is True
    assert nccl["sweep_running"] is False


def test_the_batch_lookup_path_actually_runs(app_module, tmp_path):
    """Exercise _parser_json_for_log_path THROUGH the batch, not just the shaping helper.

    Testing the shaper alone missed a real break: the surrounding lookup called a module-level
    naming function as if it were a method, so every batch hit raised and the whole aggregate
    failed at 100% with 'NvisNaming object has no attribute job_id_from_log_basename'.
    """
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4242.txt"
    p.write_text(_steps(30), encoding="utf-8")
    rec = logscan.scan(str(p), inventory=False)

    app_module._analytics_scan_ctx.batch = {str(app_module._rpath(p)): rec}
    try:
        parsed, err = app_module._parser_json_for_log_path(tmp_path, p)
    finally:
        app_module._analytics_scan_ctx.batch = None

    assert err is None
    assert parsed and parsed.get("from_logscan") is True
    assert parsed["nemotron_5h_56b_train_window"][0]["job_id"] == "4242"


def test_missing_measurements_shape_as_dashes(app_module, tmp_path):
    """parser.sh renders "no result" as ---; the pickers and the UI both expect that."""
    p = tmp_path / "empty.txt"
    p.write_text("nothing interesting here\n", encoding="utf-8")
    shaped = app_module._parser_json_from_scan(logscan.scan(str(p), inventory=False), "r", "1")
    nccl = shaped["nccl_peak_out_of_place_busbw_gbps"][0]
    assert nccl["all_reduce_gbps"] == "---"
    assert nccl["alltoall_gbps"] == "---"
