import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    fake_app = SimpleNamespace(_run_scontrol_node_update=drain)
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
        "NVIS_DEEP_AUDIT_JSON=" + json.dumps(payload) + "\n",
        "",
        2,
    )
    run = {
        "run_id": "abcdef0123456789",
        "workflow_id": "nvlink-network-deep-audit",
        "params": {"rack": "ink50-dh01-h08"},
    }
    step = {"issue_policy": {"category": "network", "drain_confirmed": True}}
    with patch.dict(sys.modules, {"app": fake_app}):
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


def test_inconclusive_transport_error_never_mass_drains(tmp_path, monkeypatch):
    monkeypatch.setattr(node_issues, "ISSUES_FILE", tmp_path / "issues.json")
    result = NodeResult("ink50-dh01-h03-11", "", "gateway timeout", 255)
    run = {"run_id": "run-2", "workflow_id": "hardware-deep-audit", "params": {}}
    step = {"issue_policy": {"category": "hardware", "drain_confirmed": True}}
    summary, _ = workflow_engine._apply_node_issue_policy(run, step, [result])
    assert summary["inconclusive"] == 1
    assert summary["drained"] == 0
    assert node_issues.query()["issues"] == []
