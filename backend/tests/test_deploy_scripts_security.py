# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "bin" / "deploy-scripts.sh"


def _run(tmp_path: Path, *args: str, **updates: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for name in (
        "RACK_SSH_HOST",
        "RACK_SSH_USER",
        "RACK_SSH_KEY",
        "RACK_SSH_PORT",
        "RACK_KNOWN_HOSTS_PATH",
    ):
        env.pop(name, None)
    env.update(updates)
    return subprocess.run(
        [str(SCRIPT), "--dry-run", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_requires_explicit_non_root_user(tmp_path):
    result = _run(tmp_path, "head.example")
    assert result.returncode == 2
    assert "RACK_SSH_USER is required" in result.stderr

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("head.example ssh-ed25519 AAAA-test\n", encoding="utf-8")
    result = _run(
        tmp_path,
        "root@head.example",
        RACK_KNOWN_HOSTS_PATH=str(known_hosts),
    )
    assert result.returncode == 2
    assert "root SSH deployments are forbidden" in result.stderr


def test_requires_pinned_regular_known_hosts(tmp_path):
    result = _run(tmp_path, "svc@head.example")
    assert result.returncode == 2
    assert "RACK_KNOWN_HOSTS_PATH is required" in result.stderr

    real = tmp_path / "known_hosts"
    real.write_text("head.example ssh-ed25519 AAAA-test\n", encoding="utf-8")
    linked = tmp_path / "linked-known-hosts"
    linked.symlink_to(real)
    result = _run(
        tmp_path,
        "svc@head.example",
        RACK_KNOWN_HOSTS_PATH=str(linked),
    )
    assert result.returncode == 2
    assert "non-symlink" in result.stderr


def test_rsync_transport_enforces_pinned_host_key_checking(tmp_path):
    known_hosts = tmp_path / "known hosts"
    known_hosts.write_text("head.example ssh-ed25519 AAAA-test\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    args_file = tmp_path / "rsync-args"
    fake_rsync = fake_bin / "rsync"
    fake_rsync.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RSYNC_ARGS_FILE\"\n",
        encoding="utf-8",
    )
    fake_rsync.chmod(0o700)

    result = _run(
        tmp_path,
        "svc@head.example",
        RACK_KNOWN_HOSTS_PATH=str(known_hosts),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        RSYNC_ARGS_FILE=str(args_file),
    )
    assert result.returncode == 0, result.stderr
    argv = args_file.read_text(encoding="utf-8").splitlines()
    transport = argv[argv.index("-e") + 1]
    assert "StrictHostKeyChecking=yes" in transport
    assert "UserKnownHostsFile=" in transport
    assert "StrictHostKeyChecking=no" not in transport
    assert argv[-1].startswith("svc@head.example:")
