# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for validation_activities.py — durable validation activity registry."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import validation_activities as va


@pytest.fixture(autouse=True)
def isolated_activities_store(tmp_path, monkeypatch):
    """Point activity + legacy job files at tmp paths for each test."""
    activities = tmp_path / "validation_activities.json"
    legacy = tmp_path / "multirack_jobs.json"
    archive = tmp_path / "validation_activities.sqlite3"
    monkeypatch.setattr(va, "ACTIVITIES_FILE", activities)
    monkeypatch.setattr(va, "LEGACY_JOBS_FILE", legacy)
    monkeypatch.setattr(va, "ACTIVITY_ARCHIVE_DB", archive)
    monkeypatch.setattr(va, "_archive_sync_signature", None)
    yield activities, legacy


class TestParseSchedulerJobIds:
    def test_submitted_and_launched_lines(self):
        text = (
            "Stage 1/2: NCCL\n"
            "SUBMITTED: microbenchmark jobid=113894\n"
            "Stage 2/2: NeMo\n"
            "LAUNCHED: pretrain jobid=113895\n"
        )
        assert va.parse_scheduler_job_ids(text) == ["113894", "113895"]

    def test_bare_jobid_lines(self):
        assert va.parse_scheduler_job_ids("wrapper says jobid=42 done") == ["42"]


class TestInferFromLogBasename:
    def test_nccl_1rack(self):
        info = va.infer_from_log_basename("cmods-NCCL-1Rack-ink50-dh01-i02-18N-113894.txt")
        assert info["stage"] == "stage1"
        assert info["label"] == "nccl"

    def test_nemo_1rack(self):
        info = va.infer_from_log_basename("cmods-NEMO-1Rack-ink50-dh01-i02-18N-113895.txt")
        assert info["stage"] == "stage2"
        assert info["label"] == "nemo"


class TestRecordAndList:
    def test_record_list_get_delete(self):
        aid = va.record_activity(
            "llmb-nccl",
            stage="stage1",
            scope="rack",
            rack="p5-r01",
            racks=["p5-r01"],
            log_dir="/logs",
            operator="alice",
            command_display="run_nccl_mpi.sh --dry-run",
            dry_run=True,
        )
        assert aid
        rec = va.get_activity(aid)
        assert rec is not None
        assert rec["activity_id"] == aid
        assert rec["job_id"] == aid
        assert rec["tool"] == "llmb-nccl"
        assert rec["kind"] == "llmb"
        assert rec["rack"] == "p5-r01"
        assert rec["operator"] == "alice"
        assert rec["phase"] == "starting"

        listed = va.list_activities(log_dir="/logs", rack="p5-r01")
        assert len(listed) == 1
        assert listed[0]["activity_id"] == aid

        va.update_activity(aid, phase="done", returncode=0)
        assert va.get_activity(aid)["phase"] == "done"

        assert va.delete_activity(aid) is True
        assert va.get_activity(aid) is None
        assert va.delete_activity(aid) is False

    def test_scope_and_tool_filters(self):
        va.record_activity("nemo-xrack", scope="multirack", racks=["a", "b"], log_dir="/logs")
        va.record_activity("llama", scope="rack", rack="p5-r01", log_dir="/logs")
        assert len(va.list_activities(scope="multirack")) == 1
        assert len(va.list_activities(tool="llama")) == 1

    def test_completed_rollup_survives_prune_restart_and_cache_replacement(
        self, isolated_activities_store, monkeypatch
    ):
        activities_file, _legacy = isolated_activities_store
        monkeypatch.setattr(va, "MAX_ACTIVITIES", 2)

        # More completed racks than the live JSON cap.  Every record must still
        # remain available to the append-preserving acceptance rollup.
        for ordinal, rack in enumerate(("r01", "r02", "r03"), start=1):
            aid = va.record_activity(
                "mnubergemm",
                rack=rack,
                racks=[rack],
                log_dir="/logs",
                tasks=[
                    {
                        "task_id": f"task-{ordinal}",
                        "ordinal": 1,
                        "label": "mnubergemm",
                        "scheduler_ref": str(100 + ordinal),
                        "scheduler_state": "COMPLETED",
                        "work_phase": "done",
                    }
                ],
            )
            va.update_activity(aid, phase="done", returncode=0, work_phase="done")

        assert len(json.loads(activities_file.read_text(encoding="utf-8"))) == 2
        rollup = va.completion_rollup(tool="mnubergemm")
        assert rollup["by_tool"]["mnubergemm"]["completed_racks"] == ["r01", "r02", "r03"]

        # Simulate a replacement/restart with a brand-new empty live cache.
        activities_file.write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(va, "_archive_sync_signature", None)
        assert va.list_activities(limit=0) == []
        after_restart = va.completion_rollup(tool="mnubergemm")
        assert after_restart["by_tool"]["mnubergemm"]["completed_rack_count"] == 3

    def test_completed_rollup_merges_fingerprinted_results_history(self, monkeypatch):
        """Pre-registry NCCL/NeMo passes remain visible without synthetic activity rows."""
        monkeypatch.setattr(
            va.results_history,
            "latest_successful_runs_by_rack",
            lambda _kinds: {
                "nccl": {
                    "r01": {
                        "run_id": "nccl-r01",
                        "source_log_path": "/logs/cmods-NCCL-1Rack-r01-18N-1.txt",
                        "started_at": "2026-08-17T01:00:00+00:00",
                    }
                },
                "nemo": {
                    "r01": {
                        "run_id": "nemo-r01",
                        "source_log_path": "/logs/cmods-NEMO-1Rack-r01-18N-2.txt",
                        "started_at": "2026-08-17T02:00:00+00:00",
                    }
                },
            },
        )

        rollup = va.completion_rollup(log_dir="/logs")

        assert rollup["by_tool"]["nccl-fullrack"]["completed_rack_count"] == 1
        assert rollup["by_tool"]["llmb-nemo-combined"]["completed_rack_count"] == 1
        assert rollup["by_tool"]["nccl-fullrack"]["evidence_by_rack"]["r01"]["source"] == "results_history"

    def test_open_activity_is_not_pruned_before_background_refresh(
        self, isolated_activities_store, monkeypatch
    ):
        monkeypatch.setattr(va, "MAX_ACTIVITIES", 1)
        open_id = va.record_activity(
            "llmb-nemo-combined",
            rack="r01",
            tasks=[
                {
                    "task_id": "live",
                    "ordinal": 1,
                    "label": "nemo",
                    "scheduler_ref": "99",
                    "work_phase": "running",
                }
            ],
        )
        done_id = va.record_activity(
            "mnubergemm",
            rack="r02",
            tasks=[
                {
                    "task_id": "done",
                    "ordinal": 1,
                    "label": "mnubergemm",
                    "scheduler_ref": "100",
                    "work_phase": "done",
                }
            ],
        )
        va.update_activity(done_id, phase="done", work_phase="done", returncode=0)
        assert va.get_activity(open_id) is not None


class TestTasks:
    def test_add_or_update_task_and_stdout_sync(self):
        aid = va.record_activity("llmb-nemo-combined", rack="p5-r01", log_dir="/logs")
        stdout = (
            "run_llmb_nemo.sh\n"
            "Stage 1/2: NCCL\nSUBMITTED: nccl jobid=100\n"
            "Stage 2/2: NeMo\nSUBMITTED: nemo jobid=101\n"
        )
        va.update_activity(aid, stdout=stdout)
        rec = va.get_activity(aid)
        tasks = rec.get("tasks") or []
        assert len(tasks) == 2
        labels = {t["label"]: t["scheduler_ref"] for t in tasks}
        assert labels["nccl"] == "100"
        assert labels["nemo"] == "101"

    def test_sync_single_jobid(self):
        aid = va.record_activity("nccl-fullrack", rack="p5-r01", log_dir="/logs")
        va.sync_tasks_from_stdout(aid, "SUBMITTED: jobid=555\n")
        tasks = va.get_activity(aid).get("tasks") or []
        assert len(tasks) == 1
        assert tasks[0]["scheduler_ref"] == "555"

    def test_rollup_work_phase_prefers_running(self):
        assert va.rollup_work_phase([
            {"work_phase": "done"},
            {"work_phase": "running"},
        ]) == "running"
        assert va.rollup_work_phase([
            {"work_phase": "done"},
            {"work_phase": "collecting"},
        ]) == "collecting"

    def test_find_results_log_for_job(self, tmp_path):
        p = tmp_path / "cmods-NCCL-1Rack-r01-18N-42.txt"
        p.write_text("x", encoding="utf-8")
        (tmp_path / "other.txt").write_text("y", encoding="utf-8")
        found = va.find_results_log_for_job(tmp_path, "42", label="nccl")
        assert found == str(p)
        assert va.find_results_log_for_job(tmp_path, "99", label="nccl") is None


class TestPickResultsLog:
    """Basename selection, so a remote log dir can be listed by the caller."""

    NCCL = "cmods-NCCL-1Rack-ink50-dh01-h01-18N-127326.txt"
    NEMO = "cmods-NEMO-1Rack-ink50-dh01-h01-18N-127326.txt"

    def test_picks_by_label(self):
        names = [self.NEMO, self.NCCL, "llmb-nccl_x_127326.out"]
        assert va.pick_results_log(names, "127326", label="nccl") == self.NCCL
        assert va.pick_results_log(names, "127326", label="nemo") == self.NEMO
        assert va.pick_results_log(names, "127326", label="stage2") == self.NEMO

    def test_ignores_non_aggregated_and_other_jobs(self):
        assert va.pick_results_log(["llmb-nccl_x_127326.out"], "127326", label="nccl") is None
        assert va.pick_results_log([self.NCCL], "999", label="nccl") is None

    def test_job_id_must_match_whole_field(self):
        """A longer job id ending in the same digits must not be mistaken for a match."""
        longer = "cmods-NCCL-1Rack-r01-18N-1127326.txt"
        assert va.pick_results_log([longer], "127326", label="nccl") is None

    def test_rejects_non_numeric_job_id(self):
        assert va.pick_results_log([self.NCCL], "", label="nccl") is None
        assert va.pick_results_log([self.NCCL], "abc", label="nccl") is None

    def test_falls_back_to_any_aggregated_log(self):
        assert va.pick_results_log([self.NEMO], "127326", label="mnubergemm") == self.NEMO

    def test_tolerates_blank_entries(self):
        assert va.pick_results_log(["", None, self.NCCL], "127326", label="nccl") == self.NCCL


class TestAdoptLog:
    def test_adopt_external_nccl_log(self):
        path = "/logs/cmods-NCCL-1Rack-p5-r01-18N-999.txt"
        aid = va.adopt_log(path, log_dir="/logs", rack="p5-r01", operator="external")
        assert aid
        rec = va.get_activity(aid)
        assert rec["source"] == "external"
        assert rec["phase"] == "done"
        tasks = rec.get("tasks") or []
        assert len(tasks) == 1
        assert tasks[0]["scheduler_ref"] == "999"
        assert tasks[0]["results_log_path"] == path

        # Idempotent — same path returns existing id.
        assert va.adopt_log(path, log_dir="/logs", rack="p5-r01") == aid

    def test_readoption_reconciles_exact_external_metadata_and_archive(self):
        path = "/cluster/OVA/logs/SU11/rack17/multiNodeMNU/mnubergemm-26.03-v1.17-g001-55952.out"
        aid = va.adopt_log(
            path,
            log_dir="/cluster/OVA/logs/nscaletx-16k-validation",
            rack="mnubergemm-26.03-v1.17-g001-55952",
        )

        corrected_dir = "/cluster/OVA/logs/SU11/rack17/multiNodeMNU"
        assert va.adopt_log(
            path, log_dir=corrected_dir, rack="ink50-dh02-k17"
        ) == aid

        rec = va.get_activity(aid)
        assert rec["activity_id"] == aid
        assert rec["rack"] == "ink50-dh02-k17"
        assert rec["racks"] == ["ink50-dh02-k17"]
        assert rec["log_dir"] == corrected_dir
        assert len(rec["tasks"]) == 1
        assert rec["tasks"][0]["scheduler_ref"] == "55952"

        with sqlite3.connect(va.ACTIVITY_ARCHIVE_DB) as conn:
            assert conn.execute("SELECT COUNT(*) FROM validation_activities").fetchone()[0] == 1
            archived = json.loads(
                conn.execute(
                    "SELECT payload FROM validation_activities WHERE activity_id = ?", (aid,)
                ).fetchone()[0]
            )
        assert archived["rack"] == "ink50-dh02-k17"
        assert archived["log_dir"] == corrected_dir

    def test_readoption_never_rewrites_non_external_activity(self):
        path = "/logs/mnubergemm-26.03-v1.17-g001-55952.out"
        aid = va.record_activity(
            "mnubergemm",
            rack="ink50-dh02-i21",
            racks=["ink50-dh02-i21"],
            log_dir="/logs/original",
            source="ui",
            tasks=[{"stdout_path": path, "results_log_path": path, "scheduler_ref": "55952"}],
        )

        assert va.adopt_log(path, log_dir="/logs/rewrite", rack="ink50-dh02-k17") == aid
        rec = va.get_activity(aid)
        assert rec["rack"] == "ink50-dh02-i21"
        assert rec["racks"] == ["ink50-dh02-i21"]
        assert rec["log_dir"] == "/logs/original"


class TestLegacyMigration:
    def test_migrates_multirack_jobs_json(self, isolated_activities_store):
        activities_file, legacy_file = isolated_activities_store
        jid = str(uuid.uuid4())
        legacy_file.write_text(
            json.dumps(
                {
                    jid: {
                        "kind": "nemo",
                        "racks": ["p5-r01", "p5-r02"],
                        "log_dir": "/logs",
                        "operator": "bob",
                        "command_display": "run_nemo_xrack.sh",
                        "phase": "running",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                }
            ),
            encoding="utf-8",
        )
        rows = va.list_activities(log_dir="/logs")
        assert len(rows) == 1
        assert rows[0]["activity_id"] == jid
        assert rows[0]["tool"] == "nemo-xrack"
        assert rows[0]["scope"] == "multirack"
        assert activities_file.is_file()


class TestLiveStageJobsByRack:
    def test_maps_open_nccl_task_to_rack(self):
        aid = va.record_activity(
            "llmb-nccl",
            stage="stage1",
            scope="rack",
            rack="ink50-dh01-i15",
            racks=["ink50-dh01-i15"],
            log_dir="/logs",
        )
        va.add_or_update_task(
            aid,
            ordinal=1,
            label="nccl",
            scheduler_ref="117122",
            work_phase="running",
            evidence="squeue",
        )
        live = va.live_stage_jobs_by_rack()
        assert live.get("ink50-dh01-i15", {}).get("nccl") == "117122"

    def test_ignores_terminal_work_phase(self):
        aid = va.record_activity(
            "llmb-nccl",
            stage="stage1",
            scope="rack",
            rack="r01",
            racks=["r01"],
            log_dir="/logs",
        )
        va.add_or_update_task(
            aid,
            ordinal=1,
            label="nccl",
            scheduler_ref="1",
            work_phase="failed",
            evidence="sacct",
        )
        assert va.live_stage_jobs_by_rack() == {}


class TestMultirackJobsShim:
    def test_record_job_and_list_jobs_scope(self):
        jid = va.record_job(
            "nemo",
            ["p5-r01", "p5-r02"],
            "/logs",
            operator="alice",
            command_display="xrack",
            job_id=str(uuid.uuid4()),
        )
        multirack = va.list_jobs(log_dir="/logs")
        assert len(multirack) == 1
        assert multirack[0]["activity_id"] == jid

        va.record_job("llmb", ["p5-r03"], "/logs")
        assert len(va.list_jobs(log_dir="/logs")) == 1  # single-rack llmb excluded
