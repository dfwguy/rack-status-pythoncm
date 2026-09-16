# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The preset dispatcher prepares only its exact Slurm stdout directory."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[2]
DISPATCHER = REPO / "bin/scripts/run-validation-preset.sh"


def _bash4() -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is unavailable")
    major = subprocess.run(
        [bash, "-c", 'printf %s "${BASH_VERSINFO[0]}"'],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not major.isdigit() or int(major) < 4:
        pytest.skip("bash 4+ is required")
    return bash


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _invoke_until_unknown_preset(tmp_path: Path, log_dir: Path, *, fail_chgrp: bool = False):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _executable(
        fake_bin / "scontrol",
        """#!/bin/bash
if [[ "$1 $2" == "show hostnames" ]]; then printf 'node01\n'; fi
exit 0
""",
    )
    sentinel = tmp_path / "sbatch-called"
    _executable(
        fake_bin / "sbatch",
        "#!/bin/bash\nprintf called >\"$SBATCH_SENTINEL\"\nexit 99\n",
    )
    if fail_chgrp:
        _executable(fake_bin / "chgrp", "#!/bin/bash\nexit 7\n")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "SBATCH_SENTINEL": str(sentinel),
        "RACK_SLURM_BIN": str(fake_bin),
        "RACK_SLURM_SKIP_MODULE_LOAD": "1",
        "NVIS_SLURM_SUBMIT_UID": str(os.getuid()),
        "NVIS_SLURM_SUBMIT_GID": str(os.getgid()),
    }
    proc = subprocess.run(
        [
            _bash4(),
            str(DISPATCHER),
            "--preset",
            "test-stop-after-log-dir",
            "--nodelist",
            "node01",
            "--log-dir",
            str(log_dir),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    return proc, sentinel


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_new_log_dir_is_submit_group_writable_and_setgid(tmp_path: Path):
    log_dir = tmp_path / "canonical" / "new-run"
    proc, sentinel = _invoke_until_unknown_preset(tmp_path, log_dir)
    assert proc.returncode == 2
    assert "LOG_DIR_READY" in proc.stdout
    assert "unsupported preset" in proc.stderr
    assert log_dir.is_dir()
    assert log_dir.stat().st_gid == os.getgid()
    assert _mode(log_dir) & 0o2030 == 0o2030
    assert not sentinel.exists()


def test_existing_log_dir_is_fixed_without_touching_children(tmp_path: Path):
    log_dir = tmp_path / "existing"
    log_dir.mkdir(mode=0o755)
    log_dir.chmod(0o2755)
    child = log_dir / "preserve.log"
    child.write_text("existing evidence\n", encoding="utf-8")
    child.chmod(0o640)
    nested = log_dir / "preserve-dir"
    nested.mkdir(mode=0o700)
    nested_mode = _mode(nested)

    proc, sentinel = _invoke_until_unknown_preset(tmp_path, log_dir)
    assert proc.returncode == 2
    assert "LOG_DIR_READY" in proc.stdout
    assert _mode(log_dir) & 0o2030 == 0o2030
    assert child.read_text(encoding="utf-8") == "existing evidence\n"
    assert _mode(child) == 0o640
    assert _mode(nested) == nested_mode
    assert not sentinel.exists()


def test_log_dir_group_failure_stops_before_sbatch(tmp_path: Path):
    log_dir = tmp_path / "cannot-fix"
    proc, sentinel = _invoke_until_unknown_preset(tmp_path, log_dir, fail_chgrp=True)
    assert proc.returncode == 2
    assert "cannot set log directory group" in proc.stderr
    assert "unsupported preset" not in proc.stderr
    assert not sentinel.exists()
