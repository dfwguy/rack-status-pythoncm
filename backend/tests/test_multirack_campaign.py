# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Exact-node multirack campaign API and launch targeting regressions."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module
import naming as naming_mod  # noqa: E402
import nvis_id_templates as nid


def _naming():
    return nid.get_nvis_naming(
        "ink50-dh01-[a-z][\\d+]",
        "ink50-dh01-[a-z][\\d+]-[\\d+]",
    )


def _campaign_env(monkeypatch, manifest: Path) -> None:
    values = {
        "RACK_MULTIRACK_CAMPAIGN_ID": "test-campaign",
        "RACK_MULTIRACK_CAMPAIGN_MANIFEST": f"local:{manifest}",
        "RACK_MULTIRACK_CAMPAIGN_NODES_PER_RACK": "2",
    }
    original_eget = app_module._eget
    monkeypatch.setattr(app_module, "_eget", lambda key, default=None: values.get(key, original_eget(key, default)))
    monkeypatch.setattr(app_module, "_eget_int", lambda key, default: int(values.get(key, default)))
    monkeypatch.setattr(app_module, "_nvis_naming", _naming)
    monkeypatch.setattr(naming_mod, "_nvis_naming", _naming)
def test_campaign_snapshot_uses_only_manifest_nodes(monkeypatch, tmp_path):
    manifest = tmp_path / "nodes.list"
    manifest.write_text(
        "ink50-dh01-d01-11\nink50-dh01-d01-12\n"
        "ink50-dh01-d02-11\nink50-dh01-d02-12\n",
        encoding="utf-8",
    )
    _campaign_env(monkeypatch, manifest)
    sinfo = """ink50-dh01-d01-11|idle
ink50-dh01-d01-12|idle
ink50-dh01-d01-99|drained
ink50-dh01-d02-11|idle
ink50-dh01-d02-12|drained*"""
    monkeypatch.setattr(
        app_module.host_exec,
        "run_slurm_on_host",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, sinfo, ""),
    )

    snapshot = app_module._multirack_campaign_snapshot()

    assert snapshot["ok"] is True
    assert snapshot["rack_count"] == 2
    assert snapshot["node_count"] == 4
    assert snapshot["eligible_count"] == 1
    rows = {row["rack"]: row for row in snapshot["racks"]}
    assert rows["ink50-dh01-d01"]["eligible"] is True
    assert rows["ink50-dh01-d02"]["eligible"] is False
    assert rows["ink50-dh01-d02"]["unavailable_nodes"] == [
        {"node": "ink50-dh01-d02-12", "state": "drained*"}
    ]


def test_llmb_campaign_launch_passes_exact_nodelist(monkeypatch, tmp_path):
    exact = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(
        app_module,
        "_multirack_campaign_snapshot",
        lambda: {
            "ok": True,
            "active": True,
            "id": "test-campaign",
            "racks": [{"rack": "ink50-dh01-d01", "eligible": True, "reason": "", "nodes": exact}],
        },
    )
    monkeypatch.setattr(app_module, "_nvis_naming", _naming)
    monkeypatch.setattr(naming_mod, "_nvis_naming", _naming)
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)
    monkeypatch.setattr(app_module, "_truthy_env", lambda _name: True)

    with app_module.app.app_context():
        prepared, err = app_module._prepare_run_llmb_nccl(
            {"racks": ["ink50-dh01-d01"], "campaign": "test-campaign", "dry_run": True}
        )

    assert err is None
    assert prepared is not None
    assert prepared["campaign"] == "test-campaign"
    assert prepared["nodes"] == exact
    assert prepared["cmd"][-2:] == ["--nodelist", ",".join(exact)]
    assert "ink50-dh01-d01 " not in prepared["display_cmd"]


def test_llmb_campaign_launch_rejects_ineligible_rack(monkeypatch, tmp_path):
    monkeypatch.setattr(
        app_module,
        "_multirack_campaign_snapshot",
        lambda: {
            "ok": True,
            "active": True,
            "id": "test-campaign",
            "racks": [{
                "rack": "ink50-dh01-d01",
                "eligible": False,
                "reason": "exact manifest nodes unavailable (drained=1)",
                "nodes": ["ink50-dh01-d01-11", "ink50-dh01-d01-12"],
            }],
        },
    )
    monkeypatch.setattr(app_module, "_nvis_naming", _naming)
    monkeypatch.setattr(naming_mod, "_nvis_naming", _naming)
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)

    with app_module.app.app_context():
        prepared, err = app_module._prepare_run_llmb_nccl({
            "racks": ["ink50-dh01-d01"],
            "campaign": "test-campaign",
        })

    assert prepared is None
    assert err is not None
    response, status = err
    assert status == 409
    assert "drained=1" in response.get_json()["error"]


def test_nemo_campaign_launch_passes_exact_nodelist(monkeypatch, tmp_path):
    exact = ["ink50-dh01-d01-11", "ink50-dh01-d01-12"]
    monkeypatch.setattr(app_module, "_nvis_naming", _naming)
    monkeypatch.setattr(naming_mod, "_nvis_naming", _naming)
    monkeypatch.setattr(
        app_module,
        "_multirack_campaign_snapshot",
        lambda: {
            "ok": True,
            "active": True,
            "id": "test-campaign",
            "racks": [{"rack": "ink50-dh01-d01", "eligible": True, "reason": "", "nodes": exact}],
        },
    )
    monkeypatch.setattr(app_module, "_resolve_log_dir", lambda _raw=None: tmp_path)
    monkeypatch.setattr(app_module, "_script_exists", lambda _path: True)

    with app_module.app.app_context():
        prepared, err = app_module._prepare_run_nemo_xrack({
            "racks": ["ink50-dh01-d01"],
            "campaign": "test-campaign",
        })

    assert err is None
    assert prepared is not None
    assert prepared["campaign"] == "test-campaign"
    assert prepared["nodes"] == exact
    assert prepared["arg"] == ",".join(exact)
    assert prepared["cmd"][-1] == ",".join(exact)
