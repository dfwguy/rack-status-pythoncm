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
