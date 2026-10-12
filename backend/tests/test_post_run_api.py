# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
import subprocess

import app as app_module
from api import reports as reports_module


def test_post_run_report_route_returns_frontend_contract(monkeypatch):
    report = {
        "generated_at": "2026-08-13T17:00:00+00:00",
        "history_path": "/tmp/post_run_nodes.json",
        "summary": {
            "nodes": 1,
            "participations": 2,
            "attributed_failures": 0,
            "flagged_nodes": 0,
            "drained_nodes": 0,
        },
        "racks": [],
        "nodes": [],
        "refresh": {
            "activities": 1,
            "merge": {"merged": 0},
            "accounting_error": "",
            "inventory_error": "",
        },
    }
    monkeypatch.setattr(
        reports_module,
        "_build_post_run_report",
        lambda **_kwargs: report,
    )

    with app_module.app.test_client() as client:
        response = client.get("/api/post-run-node-report?refresh=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["summary"]["nodes"] == 1
    assert payload["refresh"]["accounting_error"] == ""


def test_post_run_report_rejects_invalid_node_filter():
    with app_module.app.test_client() as client:
        response = client.get("/api/post-run-node-report?node=bad%20node")

    assert response.status_code == 400
    # Either layer may answer, and both are a 400 naming the problem. `bad node` contains a space,
    # so the authorization gate's identifier grammar refuses it before dispatch (FR-4) and the
    # view's own check answers only when the gate has already accepted the id. Asserting the view's
    # exact body asserted which layer got there first, not that a malformed node is refused.
    body = response.get_json() or {}
    assert body.get("ok") is False
    assert "node" in str(body) or body.get("reason") == "unresolvable_target", body


def test_companion_post_run_endpoints_are_registered():
    routes = {rule.rule for rule in app_module.app.url_map.iter_rules()}

    assert "/api/post-run-node-report" in routes
    assert "/api/post-run-node-report/mark" in routes
    assert "/api/node-issues" in routes
    assert "/api/deep-audits" in routes


def test_post_run_refresh_uses_configured_platform_slurm_commands(monkeypatch):
    class FakePlatform:
        def sacct_cmd(self, extra_args=""):
            return f"/configured/slurm/sacct {extra_args}"

        def sinfo_cmd(self, extra_args=""):
            return f"/configured/slurm/sinfo {extra_args}"

    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command.startswith("/configured/slurm/sacct "):
            stdout = (
                "147857|COMPLETED|ink50-dh01-j21-11|"
                "2026-08-13T15:00:00|2026-08-13T15:05:00|0:0|deep-audit|\n"
            )
        elif command.startswith("/configured/slurm/sinfo "):
            stdout = "ink50-dh01-j21-11|idle|none\n"
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(reports_module, "_get_platform", lambda: FakePlatform())
    monkeypatch.setattr(reports_module.host_exec, "run_slurm_on_host", fake_run)

    accounting = reports_module._post_run_accounting([
        {"tasks": [{"scheduler_ref": "147857"}]},
    ])
    inventory = reports_module._post_run_inventory()

    assert accounting[0]["job_id"] == "147857"
    assert accounting[0]["nodelist"] == "ink50-dh01-j21-11"
    assert inventory == [{
        "node": "ink50-dh01-j21-11",
        "state": "idle",
        "reason": "",
    }]
    assert calls[0].startswith("/configured/slurm/sacct ")
    assert calls[1].startswith("/configured/slurm/sinfo ")
    assert not any(command.startswith("sacct ") for command in calls)
    assert not any(command.startswith("sinfo ") for command in calls)
