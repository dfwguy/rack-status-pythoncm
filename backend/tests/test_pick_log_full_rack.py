#!/usr/bin/env python3
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Unit tests for parser-results full-rack (18N) log selection."""
from __future__ import annotations

import os
from pathlib import PurePosixPath

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module


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
