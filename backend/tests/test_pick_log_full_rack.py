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
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module

# Log discovery moved out of app.py. Patch and call the module that OWNS these names --
# patching app's view would reach only the callers inside app.py (see backend-api.mdc).
import log_discovery
import naming
from api import parser_results


def test_node_count_from_log_basename():
    assert naming._node_count_from_log_basename("cmods-NCCL-1Rack-ink50-dh01-h02-1N-75761.txt") == 1
    assert naming._node_count_from_log_basename("cmods-NCCL-1Rack-ink50-dh01-h02-18N-75270.txt") == 18
    assert naming._node_count_from_log_basename("root-NEMO-1Rack-bos01-b05-18N-294.txt") == 18
    assert naming._node_count_from_log_basename("no-node-count.txt") is None


def test_pick_log_full_rack_ignores_newer_1n_and_mismatched_job(monkeypatch, tmp_path):
    """When full_rack=True, prefer 18N even if a newer 1N (and the row job_id) would win."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    partial = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-ink50-dh01-h02-1N-75761.txt"))
    full = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-ink50-dh01-h02-18N-75270.txt"))
    # Newest-first order (as find_log_paths_for_rack returns)
    monkeypatch.setattr(
        log_discovery,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [partial, full],
    )

    # Default: job_id wins → 1N file
    got = log_discovery.pick_log_for_rack(log_dir, "ink50-dh01-h02", "75761")
    assert got is not None and got.name.endswith("-1N-75761.txt")

    # full_rack: ignore 1N job_id, take newest 18N
    got_fr = log_discovery.pick_log_for_rack(
        log_dir, "ink50-dh01-h02", "75761", full_rack=True, full_rack_nodes=18
    )
    assert got_fr is not None and got_fr.name.endswith("-18N-75270.txt")


def test_pick_log_full_rack_empty_when_no_18n(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    partial = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-1N-99.txt"))
    monkeypatch.setattr(
        log_discovery,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [partial],
    )
    assert log_discovery.pick_log_for_rack(log_dir, "r01", None, full_rack=True) is None


def test_pick_log_full_rack_prefers_matching_18n_job(monkeypatch, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    older = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-18N-100.txt"))
    newer = PurePosixPath(str(log_dir / "cmods-NCCL-1Rack-r01-18N-200.txt"))
    monkeypatch.setattr(
        log_discovery,
        "find_log_paths_for_rack",
        lambda *_a, **_k: [newer, older],
    )
    got = log_discovery.pick_log_for_rack(log_dir, "r01", "100", full_rack=True)
    assert got is not None and got.name.endswith("-18N-100.txt")


# --- stage-aware selection (NCCL and NeMo are separate jobs since llmb-run) ---------------


def _stage_logs(monkeypatch, log_dir, *basenames):
    """Wire find_log_paths_for_rack to return these basenames, newest first."""
    paths = [PurePosixPath(str(log_dir / b)) for b in basenames]
    monkeypatch.setattr(log_discovery, "find_log_paths_for_rack", lambda *_a, **_k: paths)
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
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", None)
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
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", None)
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
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", "100")
    assert got["nemo"].name.endswith("-100.txt"), "job_id must win for its own stage"
    assert got["nccl"].name.endswith("-299.txt"), "the other stage keeps its newest log"


def test_pick_logs_combined_log_resolves_to_one_file(monkeypatch, tmp_path):
    """Pre-llmb format: one job, both stages in one file. Must not be parsed twice."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    paths = _stage_logs(monkeypatch, log_dir, "root-NEMO-1Rack-bos01-b05-18N-294.txt")
    got = log_discovery.pick_logs_for_rack(log_dir, "bos01-b05", None)
    primary = parser_results._primary_stage_log(got, None)
    assert primary is not None and primary.name == paths[0].name
    assert parser_results._parser_secondary_stages(got, primary) == []


def test_pick_logs_falls_back_when_no_stage_marker(monkeypatch, tmp_path):
    """Foreign/legacy naming keeps the old single-log behaviour rather than returning nothing."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _stage_logs(monkeypatch, log_dir, "root-CK-clusterkit-r01-18N-42.txt")
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", None)
    assert parser_results._primary_stage_log(got, None) is not None


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
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", None, full_rack=True, full_rack_nodes=18)
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
    got = log_discovery.pick_logs_for_rack(log_dir, "r01", None)
    primary = parser_results._primary_stage_log(got, None)
    secondary = parser_results._parser_secondary_stages(got, primary)
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
