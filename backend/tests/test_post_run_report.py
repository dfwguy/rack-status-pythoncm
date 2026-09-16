# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
import post_run_report


def test_split_hostlist_handles_nscale_noncontiguous_trays():
    assert post_run_report.split_hostlist("ink50-dh01-j21-[11-12,28-29]") == [
        "ink50-dh01-j21-11",
        "ink50-dh01-j21-12",
        "ink50-dh01-j21-28",
        "ink50-dh01-j21-29",
    ]


def test_multinode_failure_is_not_assigned_to_every_allocated_node():
    activities = [{
        "activity_id": "activity-1",
        "tool": "llmb-nccl",
        "scope": "rack",
        "created_at": "2026-08-13T01:00:00Z",
        "issue_summary": "rank on ink50-dh01-j21-12 emitted an RDMA error",
        "tasks": [{"scheduler_ref": "147999"}],
    }]
    accounting = [{
        "job_id": "147999",
        "state": "FAILED",
        "nodelist": "ink50-dh01-j21-[11-12]",
        "started_at": "2026-08-13T01:00:00Z",
    }]
    events = post_run_report.events_from_accounting(activities, accounting)
    by_node = {row["node"]: row for row in events}
    assert by_node["ink50-dh01-j21-11"]["failed_job"] is True
    assert by_node["ink50-dh01-j21-11"]["attributed_failure"] is False
    assert by_node["ink50-dh01-j21-12"]["attributed_failure"] is True
    assert by_node["ink50-dh01-j21-12"]["category"] == "network"


def test_repeat_flag_requires_three_distinct_runs():
    node = "ink50-dh01-j21-11"
    events = [
        {
            "node": node,
            "rack": "ink50-dh01-j21",
            "job_id": str(148000 + idx),
            "activity_id": f"activity-{idx}",
            "started_at": f"2026-08-13T0{idx}:00:00Z",
            "attributed_failure": True,
            "failed_job": True,
        }
        for idx in range(1, 4)
    ]
    report = post_run_report.build_report(
        events=events,
        inventory=[{"node": node, "state": "idle", "reason": ""}],
        findings=[],
        audit_results=[],
    )
    assert report["nodes"][0]["flagged"] is True
    assert report["nodes"][0]["distinct_failure_runs"] == 3


def test_superseded_indexed_failures_remain_history_without_inflating_current_flags():
    node = "ink50-dh01-j21-11"
    report = post_run_report.build_report(
        events=[
            {"node": node, "job_id": "100", "activity_id": "old", "started_at": "2026-09-01T00:00:00Z", "failed_job": True, "attributed_failure": True, "authoritative": False},
            {"node": node, "job_id": "101", "activity_id": "new", "started_at": "2026-09-02T00:00:00Z", "failed_job": False, "attributed_failure": False, "authoritative": True},
        ],
        inventory=[{"node": node, "state": "idle", "reason": ""}],
        findings=[],
        audit_results=[],
    )
    row = report["nodes"][0]
    assert row["participation_count"] == 2
    assert row["failure_count"] == 0
    assert row["flagged"] is False
    assert len(row["runs"]) == 2


def test_deep_audit_is_visible_as_a_job_participation_with_output_linkage():
    node = "ink50-dh01-d01-32"
    report = post_run_report.build_report(
        events=[],
        inventory=[{"node": node, "state": "idle", "reason": ""}],
        findings=[],
        audit_results=[{
            "node": node,
            "rack": "ink50-dh01-d01",
            "kind": "hardware",
            "workflow_id": "hardware-deep-audit",
            "run_id": "ae74eb2d-d38e-4a38-b6e3-0f1dfb474e1e",
            "run_state": "completed",
            "scheduler_ref": "147892",
            "status": "pass",
            "started_at": "2026-08-13T19:17:24Z",
            "updated_at": "2026-08-13T19:17:27Z",
        }],
    )

    row = report["nodes"][0]
    assert row["participation_count"] == 1
    assert row["hardware_status"]["scheduler_ref"] == "147892"
    assert row["runs"][0]["job_id"] == "147892"
    assert row["runs"][0]["activity_id"].startswith("ae74eb2d")
    assert row["runs"][0]["output_run_id"].startswith("ae74eb2d")
    assert row["runs"][0]["output_step_id"] == "audit"


def test_merge_preserves_exact_participation_after_accounting_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(post_run_report, "HISTORY_FILE", tmp_path / "post_run.json")
    event = {
        "node": "ink50-dh01-j21-11",
        "rack": "ink50-dh01-j21",
        "job_id": "147999",
        "activity_id": "activity-1",
        "started_at": "2026-08-13T01:00:00Z",
        "state": "COMPLETED",
    }
    first = post_run_report.merge_events([event])
    second = post_run_report.merge_events([{**event, "state": "FAILED"}])
    assert first["after"] == 1
    assert second["after"] == 1
    assert post_run_report.load_events()[0]["state"] == "FAILED"
