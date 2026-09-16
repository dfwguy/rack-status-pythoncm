#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Unit tests for parser-results full-rack (18N) log selection."""
from __future__ import annotations

import os
from pathlib import PurePosixPath

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module


def test_campaign_rack_dir_maps_full_su5_su14_scope(tmp_path):
    legacy = tmp_path / "nscaletx-16k-validation"
    expected = {
        "ink50-dh02-e01": "SU5",
        "ink50-dh02-f02": "SU6",
        "ink50-dh02-g03": "SU7",
        "ink50-dh02-h04": "SU8",
        "ink50-dh02-i05": "SU9",
        "ink50-dh02-j06": "SU10",
        "ink50-dh02-k07": "SU11",
        "ink50-dh01-a08": "SU12",
        "ink50-dh01-b17": "SU13",
        "ink50-dh01-c24": "SU14",
    }
    for rack, su in expected.items():
        got = app_module._nscale_campaign_rack_dir(legacy, rack)
        assert got == tmp_path / su / f"rack{rack[-2:]}"


def test_node_count_from_log_basename():
    assert app_module._node_count_from_log_basename("cmods-NCCL-1Rack-ink50-dh01-h02-1N-75761.txt") == 1
    assert app_module._node_count_from_log_basename("cmods-NCCL-1Rack-ink50-dh01-h02-18N-75270.txt") == 18
    assert app_module._node_count_from_log_basename("root-NEMO-1Rack-bos01-b05-18N-294.txt") == 18
    assert app_module._node_count_from_log_basename("no-node-count.txt") is None


def test_pick_log_full_rack_ignores_newer_1n_and_mismatched_job(monkeypatch, tmp_path):
    """When full_rack=True, prefer 18N even if a newer 1N (and the row job_id) would win."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    partial = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-ink50-dh01-h02-1N-75761.txt"))
    full = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-ink50-dh01-h02-18N-75270.txt"))
    # Newest-first order (as find_log_paths_for_rack returns)
    monkeypatch.setattr(
        app_module,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [partial, full],
    )

    # Default: job_id wins → 1N file
    got = app_module.pick_log_for_rack(log_dir, "ink50-dh01-h02", "75761")
    assert got is not None and got.name.endswith("-1N-75761.txt")

    # full_rack: ignore 1N job_id, take newest 18N
    got_fr = app_module.pick_log_for_rack(
        log_dir, "ink50-dh01-h02", "75761", full_rack=True, full_rack_nodes=18
    )
    assert got_fr is not None and got_fr.name.endswith("-18N-75270.txt")


def test_pick_log_full_rack_empty_when_no_18n(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    partial = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-1N-99.txt"))
    monkeypatch.setattr(
        app_module,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [partial],
    )
    assert app_module.pick_log_for_rack(log_dir, "r01", None, full_rack=True) is None


def test_pick_log_full_rack_prefers_matching_18n_job(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-18N-100.txt"))
    newer = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-18N-200.txt"))
    monkeypatch.setattr(
        app_module,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [newer, older],
    )
    got = app_module.pick_log_for_rack(log_dir, "r01", "100", full_rack=True)
    assert got is not None and got.name.endswith("-18N-100.txt")


def test_campaign_log_discovery_and_job_pin(monkeypatch, tmp_path):
    """The 16K campaign's deterministic .out layout is first-class alongside legacy .txt."""
    legacy = tmp_path / "nscaletx-16k-validation"
    legacy.mkdir()
    campaign = tmp_path / "SU12" / "rack05" / "multiNodeNCCL"
    campaign.mkdir(parents=True)
    log = campaign / "nvftp-nccl-five-collective-28709.out"
    log.write_text("FIVE_COLLECTIVE_SWEEP_START job=28709 nodes=18\n")
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())

    got = app_module.pick_log_for_rack(legacy, "ink50-dh01-a05", "28709")
    assert got is not None and got.name == log.name
    assert app_module._rack_log_job_id(got) == "28709"
    assert app_module._rack_log_stage_kind(got) == "nccl"


def test_dashboard_activity_attributes_generic_single_rack_logs(monkeypatch, tmp_path):
    """UI launches keep rack provenance in the activity ledger, not in stdout basenames."""
    log_dir = tmp_path / "nscaletx-16k-validation"
    log_dir.mkdir()
    nccl = log_dir / "nvftp-nccl-five-collective-55421.out"
    nemo = log_dir / "nemo-26.04.01-55433.out"
    nccl.write_text("FIVE_COLLECTIVE_SWEEP_START nodes=18\n")
    nemo.write_text("INFO: nnodes=18 nproc_per_node=4\n")
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())
    monkeypatch.setattr(
        app_module.validation_activities,
        "list_activities",
        lambda **_kwargs: [
            {
                "rack": "ink50-dh02-h02",
                "tasks": [
                    {"scheduler_ref": "55421", "stdout_path": str(nccl)},
                    {"scheduler_ref": "55433", "stdout_path": str(nemo)},
                ],
            }
        ],
    )

    got = app_module.pick_logs_for_rack(log_dir, "ink50-dh02-h02", "55421")
    assert got == {
        "nccl": PurePosixPath(str(nccl)),
        "nemo": PurePosixPath(str(nemo)),
    }


def test_campaign_log_full_rack_reads_bounded_node_count(monkeypatch, tmp_path):
    legacy = tmp_path / "nscaletx-16k-validation"
    legacy.mkdir()
    campaign = tmp_path / "SU14" / "rack03" / "multiNodeNCCL"
    campaign.mkdir(parents=True)
    log = campaign / "nvftp-nccl-five-collective-17824.out"
    log.write_text(
        "NCCL_IB_PREFLIGHT_READY host=n1\n"
        "FIVE_COLLECTIVE_SWEEP_START job=17824 nodes=18\n"
        "FIVE_COLLECTIVE_SWEEP_END job=17824 rc=0 detail_rc=0\n"
    )
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())

    got = app_module.pick_log_for_rack(
        legacy, "ink50-dh01-c03", "17824", full_rack=True, full_rack_nodes=18
    )
    assert got is not None and got.name == log.name


def test_campaign_log_full_rack_rejects_partial(monkeypatch, tmp_path):
    legacy = tmp_path / "nscaletx-16k-validation"
    legacy.mkdir()
    campaign = tmp_path / "SU14" / "rack03" / "multiNodeNeMo"
    campaign.mkdir(parents=True)
    log = campaign / "nemo-26.04.01-17825.out"
    log.write_text("INFO: nnodes=2 nproc_per_node=4\nNEMO_VALIDATION_PASS nodes=2\n")
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())

    assert (
        app_module.pick_log_for_rack(
            legacy, "ink50-dh01-c03", "17825", full_rack=True, full_rack_nodes=18
        )
        is None
    )


def test_campaign_stage_pairing(monkeypatch, tmp_path):
    legacy = tmp_path / "nscaletx-16k-validation"
    legacy.mkdir()
    rack_root = tmp_path / "SU12" / "rack05"
    nccl_dir = rack_root / "multiNodeNCCL"
    nemo_dir = rack_root / "multiNodeNeMo"
    nccl_dir.mkdir(parents=True)
    nemo_dir.mkdir(parents=True)
    nccl = nccl_dir / "nvftp-nccl-five-collective-28709.out"
    nemo = nemo_dir / "nemo-26.04.01-28710.out"
    nccl.write_text("FIVE_COLLECTIVE_SWEEP_START nodes=18\n")
    nemo.write_text("INFO: nnodes=18 nproc_per_node=4\n")
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda raw: str(raw).strip())

    got = app_module.pick_logs_for_rack(
        legacy, "ink50-dh01-a05", "28709", full_rack=True, full_rack_nodes=18
    )
    assert got["nccl"].name == nccl.name
    assert got["nemo"].name == nemo.name


# --- stage-aware selection (NCCL and NeMo are separate jobs since llmb-run) ---------------


def _stage_logs(monkeypatch, log_dir, *basenames):
    """Wire find_log_paths_for_rack to return these basenames, newest first."""
    paths = [PurePosixPath(str(log_dir / b)) for b in basenames]
    monkeypatch.setattr(app_module, "find_log_paths_for_rack", lambda *_a, **_k: paths)
    return paths


def test_pick_logs_resolves_one_log_per_stage(monkeypatch, tmp_path):
    """The whole point: the NeMo job being newer must not cost us the NCCL log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(
        monkeypatch,
        log_dir,
        "cmods-NEMO-1Rack-r01-18N-114257.txt",  # newest
        "cmods-NCCL-1Rack-r01-18N-114256.txt",
    )
    got = app_module.pick_logs_for_rack(log_dir, "r01", None)
    assert got["nccl"].name.endswith("-NCCL-1Rack-r01-18N-114256.txt")
    assert got["nemo"].name.endswith("-NEMO-1Rack-r01-18N-114257.txt")


def test_pick_logs_takes_the_newest_of_each_stage(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(
        monkeypatch,
        log_dir,
        "cmods-NEMO-1Rack-r01-18N-300.txt",
        "cmods-NCCL-1Rack-r01-18N-299.txt",
        "cmods-NEMO-1Rack-r01-18N-100.txt",
        "cmods-NCCL-1Rack-r01-18N-99.txt",
    )
    got = app_module.pick_logs_for_rack(log_dir, "r01", None)
    assert got["nemo"].name.endswith("-300.txt")
    assert got["nccl"].name.endswith("-299.txt")


def test_pick_logs_job_id_pins_only_its_own_stage(monkeypatch, tmp_path):
    """A job id names one stage; the other stage must still resolve to its own newest log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(
        monkeypatch,
        log_dir,
        "cmods-NEMO-1Rack-r01-18N-300.txt",
        "cmods-NCCL-1Rack-r01-18N-299.txt",
        "cmods-NEMO-1Rack-r01-18N-100.txt",
    )
    got = app_module.pick_logs_for_rack(log_dir, "r01", "100")
    assert got["nemo"].name.endswith("-100.txt"), "job_id must win for its own stage"
    assert got["nccl"].name.endswith("-299.txt"), "the other stage keeps its newest log"


def test_pick_logs_combined_log_resolves_to_one_file(monkeypatch, tmp_path):
    """Pre-llmb format: one job, both stages in one file. Must not be parsed twice."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    paths = _stage_logs(monkeypatch, log_dir, "root-NEMO-1Rack-bos01-b05-18N-294.txt")
    got = app_module.pick_logs_for_rack(log_dir, "bos01-b05", None)
    primary = app_module._primary_stage_log(got, None)
    assert primary is not None and primary.name == paths[0].name
    assert app_module._parser_secondary_stages(got, primary) == []


def test_pick_logs_falls_back_when_no_stage_marker(monkeypatch, tmp_path):
    """Foreign/legacy naming keeps the old single-log behaviour rather than returning nothing."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(monkeypatch, log_dir, "root-CK-clusterkit-r01-18N-42.txt")
    got = app_module.pick_logs_for_rack(log_dir, "r01", None)
    assert app_module._primary_stage_log(got, None) is not None


def test_pick_logs_honours_full_rack_filter(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(
        monkeypatch,
        log_dir,
        "cmods-NCCL-1Rack-r01-1N-500.txt",  # newest but partial
        "cmods-NCCL-1Rack-r01-18N-499.txt",
        "cmods-NEMO-1Rack-r01-18N-498.txt",
    )
    got = app_module.pick_logs_for_rack(log_dir, "r01", None, full_rack=True, full_rack_nodes=18)
    assert got["nccl"].name.endswith("-18N-499.txt")
    assert got["nemo"].name.endswith("-18N-498.txt")


def test_secondary_stages_lists_only_distinct_files(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(
        monkeypatch,
        log_dir,
        "cmods-NEMO-1Rack-r01-18N-2.txt",
        "cmods-NCCL-1Rack-r01-18N-1.txt",
    )
    got = app_module.pick_logs_for_rack(log_dir, "r01", None)
    primary = app_module._primary_stage_log(got, None)
    secondary = app_module._parser_secondary_stages(got, primary)
    assert [s for s, _ in secondary] == ["nccl"], "NeMo is primary, so only NCCL needs a parse"


def test_unknown_api_endpoint_404s_instead_of_serving_the_spa():
    """A frontend calling an endpoint this build lacks must hear so, not get HTML at 200.

    This is how the Validation activities panel came to read "0 tracked" with its backend not
    deployed: the catch-all answered /api/validation-activities with index.html.
    """
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        resp = c.get("/api/there-is-no-such-endpoint")
        assert resp.status_code == 404
        assert "text/html" not in (resp.content_type or "")
        assert resp.get_json()["ok"] is False


def test_stage_kind_of_log_bn():
    from native_status import _stage_kind_of_log_bn

    assert _stage_kind_of_log_bn("cmods-NCCL-1Rack-r01-18N-1.txt") == "nccl"
    assert _stage_kind_of_log_bn("cmods-NEMO-1Rack-r01-18N-2.txt") == "nemo"
    assert _stage_kind_of_log_bn("root-CK-clusterkit-r01.txt") is None
