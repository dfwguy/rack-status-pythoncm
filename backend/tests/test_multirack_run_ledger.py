# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable human + Parquet-backed ledger for multi-rack validation runs."""
from __future__ import annotations

import ast
import json
import os
import queue
import sys
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import lake_sink
import validation_activities as va


def test_changed_backend_modules_parse_with_production_python_310_grammar():
    """Prevent local Python 3.12 from accepting syntax the 3.10 image cannot import."""
    backend_dir = Path(__file__).resolve().parents[1]
    parse_kwargs = {"feature_version": (3, 10)} if sys.version_info >= (3, 11) else {}
    for name in ("validation_activities.py", "app.py"):
        path = backend_dir / name
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), **parse_kwargs)


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "ACTIVITIES_FILE", tmp_path / "validation_activities.json")
    monkeypatch.setattr(va, "LEGACY_JOBS_FILE", tmp_path / "multirack_jobs.json")
    monkeypatch.setattr(va, "MULTIRACK_LEDGER_FILE", tmp_path / "multirack_runs.json")
    monkeypatch.setattr(
        va,
        "MULTIRACK_LEDGER_MARKDOWN_FILE",
        tmp_path / "MULTIRACK_VALIDATION_RUNS.md",
    )
    monkeypatch.setattr(
        va,
        "MULTIRACK_LEDGER_EXPORT_STATUS_FILE",
        tmp_path / "multirack_parquet_status.json",
    )
    monkeypatch.setenv("RACK_MULTIRACK_LEDGER_LAKE_DISABLED", "1")


def test_scheduler_outcome_and_corrective_action_are_rendered():
    aid = va.record_activity(
        "nemo-xrack",
        stage="multirack",
        scope="multirack",
        rack="ink50-dh01-e01",
        racks=["ink50-dh01-e01", "ink50-dh01-e02"],
        command_display="run-nemo-xrack.sh e01,e02",
        scheduler="slurm",
    )
    va.update_activity(aid, phase="done", returncode=0, stdout="Submitted batch job 139896")
    task = va.get_activity(aid)["tasks"][0]
    va.add_or_update_task(
        aid,
        ordinal=1,
        label="validation",
        task_id=task["task_id"],
        scheduler_ref="139896",
        scheduler_state="FAILED",
        work_phase="failed",
        evidence="sacct",
        phase="error",
    )
    rec = va.annotate_activity(
        aid,
        issue_summary="H03 GPUs entered Reset after Xid 149 NETIR_LINK_DOWN",
        outcome_summary="No acceptance result was produced",
        corrective_action="Drain H03 and perform a coordinated GPU recovery",
        corrective_action_status="in_progress",
        operator="ops",
    )

    assert rec is not None
    assert va.ledger_record(rec)["outcome"] == "failed"
    text = va.read_multirack_ledger_markdown()
    assert "## 139896 — FAILED" in text
    assert "Xid 149" in text
    assert "coordinated GPU recovery" in text
    assert "[in_progress]" in text


def test_archive_survives_live_activity_dismissal():
    aid = va.record_activity(
        "nemo-xrack",
        scope="multirack",
        racks=["r01", "r02"],
    )
    assert va.delete_activity(aid) is True
    assert va.get_activity(aid) is None

    archived = json.loads(va.MULTIRACK_LEDGER_FILE.read_text(encoding="utf-8"))
    assert aid in archived["runs"]
    assert aid in va.read_multirack_ledger_markdown()


def test_single_rack_activity_is_not_added_to_multirack_ledger():
    va.record_activity("llama", scope="rack", rack="r01", racks=["r01"])
    records = va.rebuild_multirack_ledger()
    assert records == []
    assert "No multi-rack runs recorded" in va.read_multirack_ledger_markdown()


def test_parquet_export_uses_lakewriter_sink(monkeypatch):
    captured = {}

    def fake_make_run_id(workload, rack, job_id):
        return f"run-{workload}-{rack}-{job_id}"

    def fake_push_run(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "rows": 2}

    monkeypatch.setattr(lake_sink, "make_run_id", fake_make_run_id)
    monkeypatch.setattr(lake_sink, "push_run", fake_push_run)
    rec = {
        "activity_id": "activity-1",
        "scope": "multirack",
        "rack": "r01",
        "racks": ["r01", "r02"],
        "tool": "nemo-xrack",
        "phase": "done",
        "work_phase": "failed",
        "tasks": [
            {
                "scheduler_ref": "139888",
                "scheduler_state": "FAILED",
                "work_phase": "failed",
            }
        ],
    }

    result = va.export_multirack_activity_to_lake(rec)

    assert result["ok"] is True
    assert captured["workload"] == "multirack-ledger"
    assert captured["job_id"] == "139888"
    assert captured["parsed"]["outcome"] == "failed"
    assert captured["parsed"]["scheduler_job_ids"] == "139888"
    assert "139888" in captured["log_text"]


def test_stale_pending_parquet_export_is_recovered_after_restart(monkeypatch):
    rec = {
        "activity_id": "activity-stale",
        "scope": "multirack",
        "rack": "r01",
        "racks": ["r01", "r02"],
        "tool": "nemo-xrack",
        "phase": "running",
        "tasks": [{"scheduler_ref": "42", "work_phase": "running"}],
    }
    fingerprint = va._ledger_export_fingerprint(rec)
    va.MULTIRACK_LEDGER_EXPORT_STATUS_FILE.write_text(
        json.dumps(
            {
                "activity-stale": {
                    "pending_fingerprint": fingerprint,
                    "pending_pid": os.getpid() + 10000,
                }
            }
        ),
        encoding="utf-8",
    )
    work_queue = queue.Queue()
    monkeypatch.setattr(va, "_ledger_export_queue", work_queue)
    monkeypatch.setattr(va, "_ensure_ledger_export_worker", lambda: None)
    monkeypatch.delenv("RACK_MULTIRACK_LEDGER_LAKE_DISABLED", raising=False)

    va._schedule_multirack_exports([rec])
    assert work_queue.qsize() == 1
    status = json.loads(va.MULTIRACK_LEDGER_EXPORT_STATUS_FILE.read_text(encoding="utf-8"))
    assert status["activity-stale"]["pending_pid"] == os.getpid()

    # The same live process does not queue the identical snapshot again.
    va._schedule_multirack_exports([rec])
    assert work_queue.qsize() == 1
