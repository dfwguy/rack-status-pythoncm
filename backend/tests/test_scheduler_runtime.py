# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

from __future__ import annotations

import scheduler_runtime


UUID = "123e4567-e89b-42d3-a456-426614174000"


def test_pythoncm_reference_and_bracket_expansion(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    assert scheduler_runtime.valid_reference(UUID)
    assert scheduler_runtime.reference_kind(UUID) == "pythoncm"
    assert scheduler_runtime.valid_reference("12345")
    assert scheduler_runtime.reference_kind("12345") == "slurm"
    assert scheduler_runtime.expand_nodelist("rack-ct[01-03],rack-ct07") == [
        "rack-ct01", "rack-ct02", "rack-ct03", "rack-ct07",
    ]


def test_nodes_use_structured_pythoncm_scheduler_state(monkeypatch):
    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(scheduler_runtime.bcm_query, "nodes", lambda: {
        "ok": True,
        "data": [
            {"hostname": "rack-ct01", "rack_id": "rack"},
            {"hostname": "rack-ct02", "rack_id": "rack"},
        ],
    })
    monkeypatch.setattr(scheduler_runtime.bcm_query, "node_states", lambda: {
        "ok": True,
        "data": [
            {"hostname": "rack-ct01", "scheduler_state": "idle", "reason": ""},
            {"hostname": "rack-ct02", "scheduler_state": "drained", "reason": "audit"},
        ],
    })
    assert scheduler_runtime.nodes() == [
        {"node": "rack-ct01", "state": "idle", "reason": "", "rack": "rack"},
        {"node": "rack-ct02", "state": "drained", "reason": "audit", "rack": "rack"},
    ]


def test_unknown_is_not_guessed_idle_when_authenticated_state_fails(monkeypatch):
    monkeypatch.setattr(scheduler_runtime.bcm_query, "nodes", lambda: {
        "ok": True, "data": [{"hostname": "rack-ct01", "rack_id": "rack"}],
    })
    monkeypatch.setattr(scheduler_runtime.bcm_query, "node_states", lambda: {
        "ok": False, "error": "authentication failed",
    })
    assert scheduler_runtime.nodes()[0]["state"] == "unknown"


def test_pythoncm_queue_compatibility_uses_registry_not_slurm(monkeypatch):
    import slurm_query

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(scheduler_runtime, "jobs", lambda active_only=False: [{
        "job_id": UUID, "name": "audit", "state": "RUNNING", "elapsed": "00:00:03",
        "nodelist": "rack-ct01", "nodes": ["rack-ct01"],
    }])
    monkeypatch.setattr(slurm_query.host_exec, "run_slurm_on_host", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("Slurm must not execute"))
    ))
    result = slurm_query._run_squeue()
    assert result["ok"] is True
    assert result["scheduler"] == "pythoncm"
    assert UUID in result["stdout"]


def test_native_status_pythoncm_paths_never_execute_slurm(monkeypatch):
    import native_status

    monkeypatch.setenv("RACK_SCHEDULER", "pythoncm")
    monkeypatch.setattr(scheduler_runtime, "node_state_counts", lambda: {("rack", "idle"): 2})
    monkeypatch.setattr(scheduler_runtime, "active_job_times", lambda wanted=None: ({UUID}, {UUID: "00:01:00"}))
    monkeypatch.setattr(native_status.host_exec, "run_slurm_on_host", lambda *_a, **_k: (
        (_ for _ in ()).throw(AssertionError("Slurm must not execute"))
    ))
    assert native_status.collect_sinfo_counts() == {("rack", "idle"): 2}
    assert native_status.load_squeue_jobs_map() == ({UUID}, {UUID: "00:01:00"})
