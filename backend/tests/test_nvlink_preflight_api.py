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
"""The /api/nvlink-preflight-async route: target building, verdict, and job payload.

host_exec is stubbed, so these exercise the plumbing (what pdsh command gets built, what the
worker writes into the job) without touching a cluster.
"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module

READY = """\
n01: ### nvlink_preflight n01 (quick=0) ###
n01: [PASS] gpus_enumerate 4/4
n01: [PASS] nvlink_datapath forced-NVLink rc=0 busbw= 410.0 GB/s
n01: RESULT n01: PASS (NVLS-ready)
"""

WEDGED = """\
n02: ### nvlink_preflight n02 (quick=0) ###
n02: [PASS] gpus_enumerate 4/4
n02: [FAIL] nvlink_datapath forced-NVLink HANG/TIMEOUT(120s) but forced-RoCE rc=0 => NVSwitch SDN routing DEAD
n02: RESULT n02: FAIL (do not use until drained/reset)
"""


@pytest.fixture
def client(monkeypatch):
    # These tests verify the async action contract, not the optional external
    # Parquet sink.  A missing lake endpoint can otherwise consume most of the
    # ten-second polling deadline in Flask's synchronous after_request hook.
    monkeypatch.setenv("RACK_LAKE_DISABLED", "1")
    with app_module._reset_jobs_lock:
        app_module._reset_jobs.clear()

    class _InlineThread:
        """Run this route's worker inline so the full suite cannot starve it.

        The application deliberately has many async-action tests. Their 15-minute
        daemon reapers can leave hundreds of sleeping threads alive in one pytest
        process, making a ten-second scheduler-based assertion nondeterministic.
        Thread creation itself is not the contract under test here.
        """

        def __init__(self, *, target, args=(), kwargs=None, daemon=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(app_module, "threading", SimpleNamespace(Thread=_InlineThread))
    monkeypatch.setattr(app_module, "_reset_job_purge_later", lambda *_args, **_kwargs: None)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c
    with app_module._reset_jobs_lock:
        app_module._reset_jobs.clear()


@pytest.fixture
def stub_pdsh(monkeypatch):
    """Capture the command and return canned pdsh output."""
    seen: dict[str, object] = {}

    def _run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["profile"] = kwargs.get("profile")
        return SimpleNamespace(
            returncode=int(seen.get("rc", 0)), stdout=str(seen.get("out", "")), stderr=""
        )

    monkeypatch.setattr(app_module.host_exec, "run_on_host", _run)
    monkeypatch.setattr(app_module, "_script_exists", lambda _p: True)
    # Keep the target deterministic regardless of the active naming/geometry.
    monkeypatch.setattr(app_module, "_expand_pdsh_w_slurm_nodenames", lambda w: w)
    return seen


def _wait(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = client.get(f"/api/reset-job-status?job_id={job_id}").get_json()
        if str(data.get("phase")) in ("done", "error"):
            return data
        time.sleep(0.05)
    raise AssertionError("job never reached a terminal phase")


def test_rack_is_required(client):
    r = client.post("/api/nvlink-preflight-async", json={})
    assert r.status_code == 400
    assert "rack" in (r.get_json().get("error") or "")


def test_ready_rack_finishes_done(client, stub_pdsh):
    stub_pdsh["out"] = READY
    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01"}
    ).get_json()
    assert start["ok"] is True
    assert start["kind"] == "nvlink_preflight"
    done = _wait(client, start["job_id"])
    assert done["phase"] == "done"
    assert done["ok"] is True
    assert done["summary"]["ready"] is True
    assert [r["host"] for r in done["rows"]] == ["n01"]


def test_failed_node_finishes_error_with_a_reason(client, stub_pdsh):
    stub_pdsh["out"] = WEDGED
    # nvlink_preflight.sh exits 1 on FAIL, so pdsh returns non-zero: that is the normal
    # "not ready" path and must not be reported as a tooling failure.
    stub_pdsh["rc"] = 1
    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n02"}
    ).get_json()
    done = _wait(client, start["job_id"])
    # Assert on phase/summary, not on ``ok``: /api/reset-job-status sets ok=True to mean "the
    # status query succeeded", overwriting the job's own verdict. True for every job kind.
    assert done["phase"] == "error"
    assert done["summary"]["ready"] is False
    assert done["summary"]["failed"] == 1
    assert "1 node(s) FAILED" in done["error"]
    assert "NVSwitch SDN routing DEAD" in done["rows"][0]["first_failure"]


def test_the_command_runs_the_script_via_pdsh_on_the_script_profile(client, stub_pdsh):
    stub_pdsh["out"] = READY
    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01"}
    ).get_json()
    _wait(client, start["job_id"])
    cmd = " ".join(str(c) for c in stub_pdsh["cmd"])
    assert "pdsh" in cmd
    assert "nvlink_preflight.sh" in cmd
    assert "--full" in cmd, "default must be the full gate, not quick"
    # Root on the compute trays, like the other pdsh rack actions.
    assert stub_pdsh["profile"] == "script"


def test_quick_mode_passes_the_flag_and_reports_the_skip(client, stub_pdsh):
    stub_pdsh["out"] = (
        "n01: ### nvlink_preflight n01 (quick=1) ###\n"
        "n01: [PASS] gpus_enumerate 4/4\n"
        "n01: RESULT n01: PASS (NVLS-ready)\n"
    )
    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01", "quick": True}
    ).get_json()
    done = _wait(client, start["job_id"])
    assert "--quick" in " ".join(str(c) for c in stub_pdsh["cmd"])
    # Only failures block: quick mode passes, and the skipped probe is reported alongside.
    assert done["phase"] == "done"
    assert done["summary"]["ready"] is True
    assert done["summary"]["nodes_with_skips"] == 1
    assert done["rows"][0]["status"] == "pass"


def test_selected_nodes_scope_the_run(client, stub_pdsh):
    stub_pdsh["out"] = READY
    start = client.post(
        "/api/nvlink-preflight-async",
        json={"rack": "p5-r01", "specs": ["p5-r01-ct01", "p5-r01-ct02"]},
    ).get_json()
    assert start["pdsh_w"] == "p5-r01-ct01,p5-r01-ct02"
    _wait(client, start["job_id"])


def test_a_node_that_never_answers_blocks_ready(client, stub_pdsh):
    """Two nodes targeted, one silent: the rack must not report ready."""
    stub_pdsh["out"] = READY
    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01,n42"}
    ).get_json()
    done = _wait(client, start["job_id"])
    assert done["summary"]["ready"] is False
    assert done["summary"]["no_response"] == 1
    hosts = {r["host"]: r["status"] for r in done["rows"]}
    assert hosts["n42"] == "no_response", "a silent node must still appear in the table"


def test_disabled_by_env(client, stub_pdsh, monkeypatch):
    monkeypatch.setenv("RACK_NVLINK_PREFLIGHT_DISABLED", "1")
    r = client.post("/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01"})
    assert r.status_code == 403


def test_scheduler_runtime_error_reaches_terminal_error_instead_of_hanging(
    client, stub_pdsh, monkeypatch
):
    monkeypatch.setattr(
        app_module,
        "run_nvlink_preflight_pdsh",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("execution broker cookie expired")),
    )

    start = client.post(
        "/api/nvlink-preflight-async", json={"rack": "p5-r01", "target": "n01"}
    ).get_json()
    done = _wait(client, start["job_id"])

    assert done["phase"] == "error"
    assert "cookie expired" in done["error"]
