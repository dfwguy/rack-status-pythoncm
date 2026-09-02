# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable acceptance-completion queries for RackStatus."""
from __future__ import annotations

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
