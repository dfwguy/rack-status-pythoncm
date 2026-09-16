# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Durable acceptance-completion queries for RackStatus."""
from __future__ import annotations

import native_status
import results_history


def test_latest_successful_run_survives_later_failure_and_reopen(tmp_path, monkeypatch):
    db = tmp_path / "results.db"
    monkeypatch.setenv("RACK_RESULTS_DB", str(db))

    results_history.ingest_run(
        {
            "source_log_path": "/logs/nemo-r01-100.txt",
            "workload_kind": "nemo",
            "rack": "r01",
            "job_id": "100",
            "started_at": "2026-08-16T10:00:00Z",
            "status": "pass",
        }
    )
    results_history.ingest_run(
        {
            "source_log_path": "/logs/nemo-r01-101.txt",
            "workload_kind": "nemo",
            "rack": "r01",
            "job_id": "101",
            "started_at": "2026-08-17T10:00:00Z",
            "status": "fail",
        }
    )

    # Every call opens SQLite afresh.  This verifies both monotonic completion
    # semantics and restart/reopen persistence.
    completed = results_history.latest_successful_runs_by_rack(("nemo",))
    assert completed["nemo"]["r01"]["job_id"] == "100"
    assert completed["nemo"]["r01"]["status"] == "pass"


def test_authoritative_current_failure_overrides_monotonic_pass(monkeypatch):
    monkeypatch.setattr(
        results_history,
        "latest_successful_runs_by_rack",
        lambda _kinds: {
            "nccl": {"r01": {
                "job_id": "100", "status": "pass", "source_log_path": "/logs/nccl-100.out",
            }},
            "nemo": {"r01": {
                "job_id": "101", "status": "pass", "source_log_path": "/logs/nemo-101.out",
            }},
        },
    )
    monkeypatch.setattr(
        results_history,
        "latest_runs_by_rack",
        lambda _kinds, *, source=None: {
            "nccl": {"r01": {
                "job_id": "102", "status": "fail", "source_log_path": "/logs/nccl-102.out",
            }},
            "nemo": {},
        },
    )

    stages = native_status._durable_completed_stage_logs_by_rack()
    assert stages["r01"]["nccl"]["nccl_ok"] is False
    assert stages["r01"]["nccl"]["jid"] == 102
    assert stages["r01"]["nccl"]["_authoritative_current"] is True
    assert stages["r01"]["nemo"]["llm_ok"] is True


def test_authoritative_incomplete_attempt_does_not_reuse_old_pass(monkeypatch):
    monkeypatch.setattr(
        results_history,
        "latest_successful_runs_by_rack",
        lambda _kinds: {
            "nccl": {"r01": {"job_id": "100", "status": "pass"}},
            "nemo": {},
        },
    )
    monkeypatch.setattr(
        results_history,
        "latest_runs_by_rack",
        lambda _kinds, *, source=None: {
            "nccl": {"r01": {"job_id": "103", "status": "unknown"}},
            "nemo": {},
        },
    )

    probe = native_status._durable_completed_stage_logs_by_rack()["r01"]["nccl"]
    assert probe["jid"] == 103
    assert probe["nccl_ok"] is None
    assert probe["nemo_state"] == "pending"
    assert probe["_durable_completion"] is False
    assert probe["_authoritative_current"] is True


def test_latest_runs_by_rack_can_select_named_current_source(tmp_path, monkeypatch):
    monkeypatch.setenv("RACK_RESULTS_DB", str(tmp_path / "results.db"))
    for job_id, status, source in (
        ("100", "pass", "nscale16k_settled_evidence"),
        ("101", "fail", "nscale16k_current_rack_evidence"),
    ):
        results_history.ingest_run({
            "source_log_path": f"/logs/nccl-{job_id}.out",
            "workload_kind": "nccl",
            "rack": "r01",
            "job_id": job_id,
            "started_at": f"2026-08-17T10:0{int(job_id) - 100}:00Z",
            "status": status,
            "source": source,
        })

    current = results_history.latest_runs_by_rack(
        ("nccl",), source="nscale16k_current_rack_evidence"
    )
    assert current["nccl"]["r01"]["job_id"] == "101"
    assert current["nccl"]["r01"]["status"] == "fail"
