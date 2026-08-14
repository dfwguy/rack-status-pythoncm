# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for NVFTP var/cache sync helpers."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"
os.environ["RACK_WEB_SECRET_KEY"] = "test-secret"

import nvftp_cache_sync as sync  # noqa: E402
import token_vault  # noqa: E402


#: Settings this module's tests run against. Held in an explicit table rather than
#: read back out of ``os.environ`` — see ``isolated`` below.
_SETTINGS = {
    "RACK_PROJECT_TOKEN": "proj-alpha",
    "RACK_NVFTP_SFTP_USER": "nv-test-user",
    "RACK_WEB_SECRET_KEY": "test-secret",
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "sample.json").write_text('{"a":1}', encoding="utf-8")
    vault = tmp_path / "secrets"
    vault.mkdir()
    monkeypatch.setattr(sync, "CACHE_DIR", cache)
    monkeypatch.setattr(sync, "SYNC_STATE_FILE", cache / "nvftp_sync_state.json")
    monkeypatch.setattr(token_vault, "NVFTP_VAULT_FILE", vault / "nvftp_token.vault.json")
    monkeypatch.setattr(token_vault, "FLOCK_FILE", vault / "nvftp_token.vault.flock")
    monkeypatch.setattr(token_vault, "VAULT_DIR", vault)
    for key, val in _SETTINGS.items():
        monkeypatch.setenv(key, val)
    token_vault.store_nvftp_token("nvftp-pass-token")

    def _eget_test(key: str, default: str = "") -> str:
        """Resolve a setting from the table above, falling back to ``os.environ``.

        Deliberately not a plain ``os.environ`` read. Once any other test module
        has imported ``app``, the catalog registers a change hook that
        re-materializes the active project into ``os.environ``, first restoring
        the baseline it captured at import — which *deletes* the keys that were
        unset back then, ``RACK_PROJECT_TOKEN`` among them. ``store_nvftp_token``
        above is such a change, so it would wipe the value set a line earlier and
        leave this module's tests configured differently depending on whether
        they ran alone or after the rest of the suite.
        """
        if key in _SETTINGS:
            return _SETTINGS[key]
        return (os.environ.get(key) or default).strip()

    monkeypatch.setattr(sync, "_eget", _eget_test)
    yield


def test_sync_enabled_when_configured():
    assert sync.sync_enabled() is True
    assert sync.skip_reason() is None


def test_sync_skipped_without_project_token(monkeypatch):
    monkeypatch.delenv("RACK_PROJECT_TOKEN", raising=False)

    def _eget_no_project(key: str, default: str = "") -> str:
        if key == "RACK_PROJECT_TOKEN":
            return ""
        return (os.environ.get(key) or default).strip()

    monkeypatch.setattr(sync, "_eget", _eget_no_project)
    assert sync.sync_enabled() is False
    out = sync.run_sync()
    assert out.get("skipped") is True


def test_run_sync_uploads_changed_file(monkeypatch):
    mock_sftp = MagicMock()
    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = mock_sftp

    with patch.object(sync, "_connect_sftp", return_value=mock_ssh):
        out = sync.run_sync()

    assert out["ok"] is True
    assert out["skipped"] is False
    assert "sample.json" in out["uploaded"]
    state = json.loads(sync.SYNC_STATE_FILE.read_text(encoding="utf-8"))
    assert "sample.json" in state["files"]
    assert state["remote_root"] == "/nvis-project-view/proj-alpha/cache"


def test_iter_cache_files_skips_flock_and_tmp(isolated, tmp_path, monkeypatch):
    cache = tmp_path / "extra-cache"
    cache.mkdir()
    (cache / "good.json").write_text("{}", encoding="utf-8")
    (cache / "skip.flock").write_text("", encoding="utf-8")
    (cache / "skip.tmp").write_text("", encoding="utf-8")
    sub = cache / "nested"
    sub.mkdir()
    (sub / "inner.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(sync, "CACHE_DIR", cache)
    names = {rel for _, rel in sync._iter_cache_files()}
    assert names == {"good.json", "nested/inner.txt"}


def test_run_sync_force_uploads_unchanged_file(monkeypatch):
    mock_sftp = MagicMock()
    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = mock_sftp
    (sync.CACHE_DIR / "sample.json").write_text('{"a":1}', encoding="utf-8")
    st = sync._load_state()
    st["files"] = {"sample.json": "6:1"}
    st["remote_root"] = "/nvis-project-view/proj-alpha/cache"
    sync._save_state(st)

    with patch.object(sync, "_connect_sftp", return_value=mock_ssh):
        out = sync.run_sync(force=True)

    assert out["ok"] is True
    assert "sample.json" in out["uploaded"]
    assert out.get("forced") is True


def test_remote_root_change_resets_tracking(monkeypatch):
    mock_sftp = MagicMock()
    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = mock_sftp
    sync._save_state(
        {
            "files": {"sample.json": "6:1"},
            "remote_root": "/nvis-project-view/old-project/cache",
        }
    )

    def _root(_project: str) -> str:
        return "/nvis-project-view/proj-alpha/cache"

    monkeypatch.setattr(sync, "_sync_remote_root", _root)
    with patch.object(sync, "_connect_sftp", return_value=mock_ssh):
        out = sync.run_sync()

    assert out["ok"] is True
    assert "sample.json" in out["uploaded"]


def test_project_remote_dir_without_base_prefix(monkeypatch):
    def _eget_no_base(key: str, default: str = "") -> str:
        if key == "RACK_NVFTP_REMOTE_BASE":
            return ""
        return (os.environ.get(key) or default).strip()

    monkeypatch.setattr(sync, "_eget", _eget_no_base)
    assert sync._project_remote_dir("proj-alpha") == "/nvis-project-view/proj-alpha"
    assert sync._sync_remote_root("proj-alpha") == "/nvis-project-view/proj-alpha/cache"


def test_iter_sync_files_includes_rack_comments(isolated, tmp_path, monkeypatch):
    comments = tmp_path / "rack_comments.json"
    comments.write_text('{"r1": "note"}', encoding="utf-8")
    monkeypatch.setattr(sync, "_comments_file", lambda: comments)
    rels = {rel for _, rel in sync._iter_sync_files()}
    assert "rack_comments.json" in rels
    assert "sample.json" in rels


def test_sanitize_project_token():
    assert sync._sanitize_project_token("my-site_01") == "my-site_01"
    with pytest.raises(ValueError):
        sync._sanitize_project_token("  ")


def test_ensure_background_thread_starts_when_sync_disabled(monkeypatch):
    monkeypatch.setattr(sync, "sync_enabled", lambda: False)
    sync._bg_thread = None
    with patch.object(sync, "_background_loop", side_effect=lambda: time.sleep(60)):
        sync.ensure_background_thread()
        first = sync._bg_thread
        assert first is not None
        assert first.is_alive()
        sync.ensure_background_thread()
        assert sync._bg_thread is first


def test_ensure_background_thread_restarts_dead_thread():
    sync._bg_thread = None
    with patch.object(sync, "_background_loop"):
        sync.ensure_background_thread()
        dead = sync._bg_thread
        dead.join(timeout=2)
        assert not dead.is_alive()
        with patch.object(sync, "_background_loop", side_effect=lambda: time.sleep(60)):
            sync.ensure_background_thread()
            assert sync._bg_thread is not None
            assert sync._bg_thread is not dead
            assert sync._bg_thread.is_alive()


def test_log_sync_result_logs_failure(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        sync._log_sync_result({"ok": False, "error": "connect failed"})
    assert "connect failed" in caplog.text


def test_format_connect_error_auth_failed():
    err = sync._format_connect_error(
        Exception("Authentication failed."),
        user="nv-wat-123",
        host="transfer.nvftp.nvidia.com",
    )
    assert "nv-wat-123" in err
    assert "token" in err.lower()


def test_test_project_directory_upload():
    mock_sftp = MagicMock()
    mock_stat = MagicMock()
    mock_stat.st_size = 128
    mock_sftp.stat.return_value = mock_stat
    mock_dst = MagicMock()
    mock_cm = MagicMock()
    mock_cm.__enter__.return_value = mock_dst
    mock_cm.__exit__.return_value = None
    mock_sftp.open.return_value = mock_cm
    mock_ssh = MagicMock()
    mock_ssh.open_sftp.return_value = mock_sftp

    with patch.object(sync, "_connect_sftp", return_value=mock_ssh):
        out = sync.test_project_directory(
            project_token="proj-alpha",
            sftp_user="nv-test-user",
            token="nvftp-pass-token",
        )

    assert out["ok"] is True
    assert "/nvis-project-view/proj-alpha" in out["remote_dir"]
    assert "connectivity-test.json" in out["remote_file"]
    mock_dst.write.assert_called()
