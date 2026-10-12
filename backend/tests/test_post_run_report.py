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


# --- grouping follows the cluster's mapping, not the hostname --------------------------------------

NC_NODES = [f"sys1-gso1-p-phy-gpu{n}-su{su}" for su in (19, 20) for n in (1, 2, 3)]


def _snapshot(mode, *, placed=None, units=None):
    """The slice of a cluster snapshot payload the report reads."""
    placed = placed or {}
    return {
        "rack_mapping_mode": mode,
        "nodes": [
            {"id": n, "rack": placed.get(n), "rack_unit": str(i + 11) if placed.get(n) else ""}
            for i, n in enumerate(NC_NODES)
        ],
        "rows": [{"rack": r} for r in ("U20", "U21")],
        "scalable_units": units or [],
    }


def _report(placement):
    return post_run_report.build_report(
        events=[],
        inventory=[{"node": n, "state": "idle", "reason": ""} for n in NC_NODES],
        findings=[],
        audit_results=[],
        placement=placement,
    )


def test_translate_groups_by_the_providers_rack_not_the_hostname_prefix():
    # On Nscale NC the -suN suffix is the SU, so the old prefix rule grouped gpuN across SUs.
    placed = {n: ("U20" if "su19" in n else "U21") for n in NC_NODES}
    report = _report(post_run_report.placement_from_snapshot(_snapshot("translate", placed=placed)))
    groups = {g["rack"]: [n["node"] for n in g["nodes"]] for g in report["racks"]}
    assert set(groups) == {"U20", "U21"}
    assert groups["U20"] == [n for n in NC_NODES if "su19" in n]
    assert report["rack_mapping_mode"] == "translate"
    assert all(g["kind"] == "rack" for g in report["racks"])


def test_translate_groups_carry_their_scalable_unit_and_the_racks_page_order():
    placed = {n: ("U20" if "su19" in n else "U21") for n in NC_NODES}
    units = [{"id": "su19", "label": "SU19", "racks": ["U20"]}, {"id": "su20", "label": "SU20", "racks": ["U21"]}]
    report = _report(post_run_report.placement_from_snapshot(_snapshot("translate", placed=placed, units=units)))
    assert [g["rack"] for g in report["racks"]] == ["U20", "U21"]
    assert report["racks"][0]["scalable_unit"] == {"id": "su19", "label": "SU19"}


def test_pool_puts_every_node_in_one_pool_card_and_invents_no_rack():
    report = _report(post_run_report.placement_from_snapshot(_snapshot("pool")))
    assert len(report["racks"]) == 1
    pool = report["racks"][0]
    assert (pool["kind"], pool["label"]) == ("pool", "Node pool")
    assert len(pool["nodes"]) == len(NC_NODES)
    assert {n["rack"] for n in report["nodes"]} == {""}


def test_a_pool_cluster_needs_no_snapshot_to_show_its_pool():
    # No inventory yet (cold snapshot): the pool card is still right, because it asserts no placement.
    report = _report(post_run_report.placement_from_snapshot({"rack_mapping_mode": "pool"}))
    assert [g["kind"] for g in report["racks"]] == ["pool"]


def test_a_node_the_mapping_could_not_place_is_unplaced_never_folded_into_a_rack():
    placed = {NC_NODES[0]: "U20"}
    report = _report(post_run_report.placement_from_snapshot(_snapshot("translate", placed=placed)))
    kinds = {g["rack"]: g["kind"] for g in report["racks"]}
    assert kinds == {"U20": "rack", "unplaced": "unplaced"}
    unplaced = next(g for g in report["racks"] if g["kind"] == "unplaced")
    assert len(unplaced["nodes"]) == len(NC_NODES) - 1


def test_without_a_snapshot_a_non_pool_cluster_shows_everything_unplaced():
    report = _report(post_run_report.placement_from_snapshot(None))
    assert [g["kind"] for g in report["racks"]] == ["unplaced"]


def test_a_rack_stored_on_an_old_event_does_not_override_the_current_mapping():
    # History written when racks were hostname-derived carries a stale rack; placement wins.
    node = NC_NODES[0]
    event = {"node": node, "rack": "sys1-gso1-p-phy-gpu1", "job_id": "1", "activity_id": "a",
             "started_at": "2026-10-01T00:00:00Z", "attributed_failure": False}
    report = post_run_report.build_report(
        events=[event],
        inventory=[{"node": node, "state": "idle", "reason": ""}],
        findings=[], audit_results=[],
        placement=post_run_report.placement_from_snapshot(_snapshot("translate", placed={node: "U20"})),
    )
    assert report["nodes"][0]["rack"] == "U20"


def test_the_rack_filter_applies_to_the_mapped_rack_and_the_pool():
    placed = {n: ("U20" if "su19" in n else "U21") for n in NC_NODES}
    placement = post_run_report.placement_from_snapshot(_snapshot("translate", placed=placed))
    filtered = post_run_report.build_report(
        events=[], inventory=[{"node": n, "state": "idle", "reason": ""} for n in NC_NODES],
        findings=[], audit_results=[], rack="U21", placement=placement,
    )
    assert {n["rack"] for n in filtered["nodes"]} == {"U21"}
    pool = post_run_report.build_report(
        events=[], inventory=[{"node": n, "state": "idle", "reason": ""} for n in NC_NODES],
        findings=[], audit_results=[], rack="pool",
        placement=post_run_report.placement_from_snapshot(_snapshot("pool")),
    )
    assert len(pool["nodes"]) == len(NC_NODES)


def test_nodes_inside_a_group_sort_naturally_not_lexically():
    nodes = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in (10, 9, 100, 1)]
    placement = post_run_report.placement_from_snapshot(
        {"rack_mapping_mode": "translate", "nodes": [{"id": n, "rack": "U20"} for n in nodes], "rows": [{"rack": "U20"}]}
    )
    report = post_run_report.build_report(
        events=[], inventory=[{"node": n, "state": "idle", "reason": ""} for n in nodes],
        findings=[], audit_results=[], placement=placement,
    )
    assert [n["node"] for n in report["racks"][0]["nodes"]] == [f"sys1-gso1-p-phy-gpu{n}-su19" for n in (1, 9, 10, 100)]
