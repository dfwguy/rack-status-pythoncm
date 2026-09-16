# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Strict external acceptance-evidence import coverage."""
from __future__ import annotations

import pytest

import external_validation_evidence as evidence


def test_canonical_su_mapping():
    assert evidence.canonical_rack_id(1, 1) == "ink50-dh02-a01"
    assert evidence.canonical_rack_id(11, 24) == "ink50-dh02-k24"
    assert evidence.canonical_rack_id(12, 17) == "ink50-dh01-a17"
    assert evidence.canonical_rack_id(14, 8) == "ink50-dh01-c08"


def test_only_qualified_passes_are_imported():
    snapshot = {
        "complete": True,
        "rack_nccl": {"latest_by_rack": [
            {"outcome": "PASS", "su": 12, "rack_number": 17, "job_id": "6984",
             "main_log": "/logs/nccl.out", "mtime_ns": 1, "node_count": 18,
             "parquet_exact": True, "source_sha256": "abc"},
            {"outcome": "FAIL", "su": 12, "rack_number": 2,
             "main_log": "/logs/fail.out"},
        ]},
        "rack_nemo": {"latest_by_rack": []},
        "rack_mnubergemm": {"latest_by_rack": [{
            "outcome": "PASS", "su": 9, "rack_number": 4, "job_id": "24646",
            "main_log": "/logs/mnubergemm.out", "mtime_ns": 2, "node_count": 18,
            "parquet_exact": True, "source_sha256": "def",
        }]},
    }
    rows = evidence.accepted_records(snapshot)
    assert [(r["rack"], r["workload_kind"], r["status"]) for r in rows] == [
        ("ink50-dh01-a17", "nccl", "pass"),
        ("ink50-dh02-i04", "mnubergemm", "pass"),
    ]


def test_incomplete_snapshot_is_rejected():
    with pytest.raises(ValueError, match="incomplete"):
        evidence.accepted_records({"complete": False})


def test_current_rows_retain_failures_and_use_stable_rack_stage_identity():
    snapshot = {
        "complete": True,
        "rack_nccl": {"latest_by_rack": [{
            "outcome": "FAIL", "su": 12, "rack_number": 1, "job_id": "46113",
            "main_log": "/logs/nccl-46113.out", "mtime_ns": 10, "terminal": True,
            "terminal_rc": 143,
        }]},
        "rack_nemo": {"latest_by_rack": [{
            "outcome": "PASS", "su": 12, "rack_number": 1, "job_id": "45832",
            "main_log": "/logs/nemo-45832.out", "mtime_ns": 9,
            "parquet_exact": True,
        }]},
        "rack_mnubergemm": {"latest_by_rack": []},
    }
    rows = evidence.current_rack_records(snapshot)
    assert [(r["workload_kind"], r["status"], r["job_id"]) for r in rows] == [
        ("nccl", "fail", "46113"),
        ("nemo", "pass", "45832"),
    ]
    assert all(r["source"] == "nscale16k_current_rack_evidence" for r in rows)

    updated = {**snapshot, "rack_nccl": {"latest_by_rack": [{
        "outcome": "PASS", "su": 12, "rack_number": 1, "job_id": "54568",
        "main_log": "/logs/nccl-54568.out", "mtime_ns": 20,
        "parquet_exact": True,
    }]}}
    replacement = evidence.current_rack_records(updated)[0]
    assert replacement["run_id"] == rows[0]["run_id"]
    assert replacement["status"] == "pass"
    assert replacement["job_id"] == "54568"
