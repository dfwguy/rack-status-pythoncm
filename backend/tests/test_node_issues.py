# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
import json
from unittest.mock import MagicMock

import node_issues
import workflow_engine
from transport.fanout import NodeResult


def test_punchcard_fixed_cells_become_node_rollups(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    rows = [
        ["Row h · SU5", "01", "03"],
        ["11", "11 ▲2 ⚑ ▦", "11 ▲1 ✖"],
        ["12", "12 ▦", "12 ▲3"],
    ]
    parsed = node_issues._parse_punchcard(rows, "ink50-dh01")
    assert parsed["ink50-dh01-h01-11"] == {
        "count": 2,
        "drained": False,
        "nccl": True,
        "maintenance": True,
        "cell_value": "11 ▲2 ⚑ ▦",
    }
    assert parsed["ink50-dh01-h03-11"]["drained"] is True


def test_local_findings_append_to_imported_count(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")

    def seed(data):
        data["punchcard"] = {
            "synced_at": "2026-08-13T00:00:00+00:00",
            "source": "test",
            "nodes": {"ink50-dh01-h01-11": {"count": 2, "drained": False}},
        }

    node_issues._mutate(seed)
    node_issues.record_issue(
        node="ink50-dh01-h01-11",
        rack="ink50-dh01-h01",
        category="hardware",
        code="kernel_xid",
        reason="Xid 79",
        severity="failure",
        source="hardware-deep-audit",
        validation_run_id="run-1",
        drained=True,
        slurm_reason="Drained for hardware deep audit",
    )
    result = node_issues.query(node="ink50-dh01-h01-11")
    rollup = result["nodes"]["ink50-dh01-h01-11"]
    assert rollup["issue_count"] == 3
    assert rollup["local_count"] == 1
    assert rollup["drained"] is True
    assert result["issues"][0]["reason"] == "Xid 79"


def test_invalidated_validator_run_is_retained_but_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    node_issues.record_issue(
        node="ink50-dh01-j21-11",
        rack="ink50-dh01-j21",
        category="hardware",
        code="gpu_count",
        reason="false zero-GPU result",
        severity="failure",
        source="hardware-deep-audit",
        validation_run_id="bad-run",
    )
    result = node_issues.invalidate_validation_runs(
        ["bad-run"], reason="validator omitted GPU GRES", operator="qa"
    )
    assert result["invalidated"] == 1
    assert node_issues.query(node="ink50-dh01-j21-11")["issues"] == []
    stored = node_issues.load()["issues"][0]
    assert stored["invalidated_at"]
    assert stored["invalidation_reason"] == "validator omitted GPU GRES"


def test_confirmed_workflow_failure_comments_drains_and_records(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    drain = MagicMock(return_value={"ok": True, "stdout": "updated"})
    payload = {
        "schema": 1,
        "kind": "network",
        "status": "fail",
        "codes": ["ib_counter"],
        "reason": "mlx5_3 link_downed=2",
        "warnings": [],
        "metrics": {"ib_bad_counters": 1},
    }
    result = NodeResult(
        "ink50-dh01-h08-29",
        "NVIS_SLURM_JOB_ID=147892\nNVIS_DEEP_AUDIT_JSON=" + json.dumps(payload) + "\n",
        "",
        2,
    )
    run = {
        "run_id": "abcdef0123456789",
        "workflow_id": "nvlink-network-deep-audit",
        "params": {"rack": "ink50-dh01-h08"},
    }
    step = {"issue_policy": {"category": "network", "drain_confirmed": True}}
    monkeypatch.setattr(workflow_engine, "_run_scontrol_node_update", drain)
    summary, output = workflow_engine._apply_node_issue_policy(run, step, [result])

    assert summary["failed"] == 1
    assert summary["drained"] == 1
    assert "Slurm drained" in output
    drain.assert_called_once()
    args = drain.call_args.args
    assert args[0] == "ink50-dh01-h08-29"
    assert args[1][0] == "state=drain"
    assert "validation abcdef01" in args[1][1]
    stored = node_issues.query(node="ink50-dh01-h08-29")
    assert stored["issues"][0]["code"] == "ib_counter"
    assert stored["issues"][0]["drained"] is True
    assert stored["issues"][0]["scheduler_ref"] == "147892"
    assert summary["nodes"][0]["scheduler_ref"] == "147892"


def test_inconclusive_transport_error_never_mass_drains(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    result = NodeResult("ink50-dh01-h03-11", "", "gateway timeout", 255)
    run = {"run_id": "run-2", "workflow_id": "hardware-deep-audit", "params": {}}
    step = {"issue_policy": {"category": "hardware", "drain_confirmed": True}}
    summary, _ = workflow_engine._apply_node_issue_policy(run, step, [result])
    assert summary["inconclusive"] == 1
    assert summary["drained"] == 0
    assert node_issues.query()["issues"] == []


def test_exact_node_slurm_prolog_failure_is_recorded_and_drained_without_mass_drain(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    drain = MagicMock(return_value={"ok": True, "stdout": "updated"})
    stderr = (
        "srun: Prolog failure on node ink50-dh01-d01-11\n"
        "srun: error: Nodes ink50-dh01-d01-11 are still not ready\n"
        "srun: error: Something is wrong with the boot of the nodes."
    )
    results = [
        NodeResult("ink50-dh01-d01-11", "", stderr, 255),
        # srun exposes the same global stderr to every unstarted task. The exact
        # hostname match must keep this peer inconclusive and out of Slurm updates.
        NodeResult("ink50-dh01-d01-12", "", stderr, 255),
    ]
    run = {
        "run_id": "4d64e837-b551-4def-a310-7d0138f9dda3",
        "workflow_id": "hardware-deep-audit",
        "params": {"rack": "ink50-dh01-d01"},
    }
    step = {"issue_policy": {"category": "hardware", "drain_confirmed": True}}

    monkeypatch.setattr(workflow_engine, "_run_scontrol_node_update", drain)
    summary, output = workflow_engine._apply_node_issue_policy(run, step, results)

    assert summary["failed"] == 1
    assert summary["inconclusive"] == 1
    assert summary["drained"] == 1
    failed, peer = summary["nodes"]
    assert failed["node"] == "ink50-dh01-d01-11"
    assert failed["status"] == "fail"
    assert failed["category"] == "control_plane"
    assert failed["codes"] == ["slurm_prolog_failure"]
    assert peer["node"] == "ink50-dh01-d01-12"
    assert peer["status"] == "inconclusive"
    drain.assert_called_once()
    assert drain.call_args.args[0] == "ink50-dh01-d01-11"
    assert "control_plane" in drain.call_args.args[1][1]
    stored = node_issues.query(node="ink50-dh01-d01-11")["issues"]
    assert len(stored) == 1
    assert stored[0]["category"] == "control_plane"
    assert stored[0]["code"] == "slurm_prolog_failure"
    assert "INCONCLUSIVE" in output  # only the unaffected peer
