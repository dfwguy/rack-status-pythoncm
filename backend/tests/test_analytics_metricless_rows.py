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
"""A successful run that yields no averageable metrics must be reported, not dropped.

Observed on Nscale TX: a 4-rack multirack NeMo run passed every gate, contributed its 32-point
NCCL sweep to the Multirack chart, and then vanished from the Nemotron scaling table directly
below that chart -- because the step/TFLOPs averager found no samples in its window and the row
builder skipped any bucket without numbers. The operator saw their run in the chart and
concluded the table was stale, when in fact the run had been considered and silently discarded.

These pin the reporting contract: the bucket still appears, its means are null rather than zero,
and ``n_logs_no_metrics`` says how many runs could not be measured.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module  # noqa: E402
import native_status  # noqa: E402

FOUR_RACK_LOG = "root-NEMO-p05-r01+p05-r02+p05-r03+p05-r04-72N-131805.txt"

# What the parser returns for a run that completed but produced too few step samples: a verdict,
# and a dash for every number.
METRICLESS_NEMOTRON = {"rack": "p05-r01", "job_id": "131805", "status": "OK", "step_s": "-", "tflops_per_gpu": "-"}
MEASURED_NEMOTRON = {"rack": "p05-r01", "job_id": "131805", "status": "OK", "step_s": "4.22", "tflops_per_gpu": "1954.6"}
NCCL_SWEEP = {"all_reduce_busbw_series": [{"size_bytes": 1 << 30, "bus_bw_gbps": 812.42}]}


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def stub_analytics(monkeypatch, tmp_path):
    """Point the endpoint at one fake multirack log and control what the parser reports."""
    monkeypatch.setenv("RACK_STATUS_DEMO", "0")
    monkeypatch.setenv("RACK_ANALYTICS_DISABLED", "0")
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda *_a, **_k: tmp_path)
    # No single-rack discovery: this test is about the multirack path only.
    monkeypatch.setattr(native_status, "_host_log_entries", lambda *_a, **_k: [])
    monkeypatch.setattr(app_module, "_multirack_done_log_paths", lambda *_a, **_k: [Path(tmp_path / FOUR_RACK_LOG)])
    monkeypatch.setattr(app_module, "_parser_json_for_log_path", lambda *_a, **_k: ({}, None))
    monkeypatch.setattr(app_module, "_pick_nccl_row", lambda *_a, **_k: NCCL_SWEEP)
    monkeypatch.setattr(app_module, "_read_log_text_chunk", lambda *_a, **_k: "")
    monkeypatch.setattr(app_module, "_multirack_rack_count_from_basename", lambda *_a, **_k: 4)
    # Never touch the real cache file from a test.
    monkeypatch.setattr(app_module, "_analytics_aggregate_cache_put", lambda *_a, **_k: None)


def _aggregate(client) -> dict:
    res = client.post("/api/analytics/aggregate", json={"rows": []})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_a_metricless_run_is_reported_not_dropped(client, stub_analytics, monkeypatch):
    monkeypatch.setattr(app_module, "_pick_nemotron_row", lambda *_a, **_k: METRICLESS_NEMOTRON)
    mr = _aggregate(client)["multirack"]

    assert len(mr["nccl_series_by_log"]) == 1, "the NCCL sweep still feeds the chart"

    scaling = mr["nemotron_scaling"]
    assert len(scaling) == 1, "the bucket must appear even with nothing to average"
    row = scaling[0]
    assert row["n_racks"] == 4
    assert row["n_logs"] == 0, "no log contributed a measurement"
    assert row["n_logs_no_metrics"] == 1
    assert row["logs_no_metrics"] == [FOUR_RACK_LOG]
    assert row["mean_tflops_per_gpu"] is None, "null, never a misleading 0"
    assert row["mean_step_s"] is None


def test_the_throughput_table_reports_it_too(client, stub_analytics, monkeypatch):
    monkeypatch.setattr(app_module, "_pick_nemotron_row", lambda *_a, **_k: METRICLESS_NEMOTRON)
    rows = _aggregate(client)["multirack"]["nemotron_throughput_scaling"]
    assert len(rows) == 1
    assert rows[0]["n_logs_no_metrics"] == 1
    assert rows[0]["num_nodes"] is None, "geometry comes from a measured log; there is none"
    assert rows[0]["time_to_train_1t_days"] is None


def test_a_measured_run_still_reports_means(client, stub_analytics, monkeypatch):
    """The reporting path must not disturb a run that does have numbers."""
    monkeypatch.setattr(app_module, "_pick_nemotron_row", lambda *_a, **_k: MEASURED_NEMOTRON)
    row = _aggregate(client)["multirack"]["nemotron_scaling"][0]
    assert row["n_logs"] == 1
    assert row["n_logs_no_metrics"] == 0
    assert row["mean_tflops_per_gpu"] == pytest.approx(1954.6)
    assert row["mean_step_s"] == pytest.approx(4.22)
