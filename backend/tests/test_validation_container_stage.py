# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The image stage trusts the node's verified cache before it looks for a source to copy from.

On Nscale NC (2026-10-13) a 48-node NeMo preset launch failed its stage job (Slurm 192718) in two
seconds: every node held the P2P-verified image in /opt/containers, but the stage checked the
shared source first, and /cluster/containers exists only on the app server. With rsync off the
source is never read, so it must not be required.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

STAGE = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "validation_container_stage.sbatch"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None or subprocess.run(["stat", "-c", "%s", "/"], capture_output=True, check=False).returncode != 0,
    reason="needs GNU stat and flock (the CI image has both)",
)


def _node_block() -> str:
    """The per-node script the sbatch hands to ``srun ... bash -euo pipefail -c '...'``."""
    text = STAGE.read_text(encoding="utf-8")
    start = text.index("bash -euo pipefail -c '") + len("bash -euo pipefail -c '")
    return text[start:text.index("\n  '\n", start)]


def _stage(tmp_path: Path, *, source: Path, dest: Path, payload: bytes, rsync: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NVIS_STAGE_SOURCE": str(source),
        "NVIS_STAGE_DEST": str(dest),
        "NVIS_STAGE_RSYNC": rsync,
        "NVIS_STAGE_EXPECTED_SIZE": str(len(payload)),
        "NVIS_STAGE_EXPECTED_SHA256": hashlib.sha256(payload).hexdigest(),
    }
    return subprocess.run(["bash", "-euo", "pipefail", "-c", _node_block()], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=30, check=False)


def test_a_verified_cache_is_ready_when_the_source_is_not_visible(tmp_path):
    payload = b"squashfs bytes"
    dest = tmp_path / "opt-containers" / "image.sqsh"
    dest.parent.mkdir()
    dest.write_bytes(payload)
    r = _stage(tmp_path, source=tmp_path / "cluster" / "missing.sqsh", dest=dest, payload=payload, rsync="0")
    assert r.returncode == 0, r.stderr
    assert "IMAGE_STAGE_READY" in r.stdout and "cached=yes" in r.stdout


def test_a_missing_cache_without_rsync_names_the_cache_not_the_source(tmp_path):
    dest = tmp_path / "opt-containers" / "image.sqsh"
    r = _stage(tmp_path, source=tmp_path / "cluster" / "missing.sqsh", dest=dest, payload=b"x", rsync="0")
    assert r.returncode == 3
    assert f"path={dest}" in r.stderr and "rsync=0" in r.stderr


def test_a_corrupt_cache_is_not_trusted(tmp_path):
    dest = tmp_path / "opt-containers" / "image.sqsh"
    dest.parent.mkdir()
    dest.write_bytes(b"other bytes!!!")
    r = _stage(tmp_path, source=tmp_path / "missing.sqsh", dest=dest, payload=b"squashfs bytes", rsync="0")
    assert r.returncode == 3
    assert "IMAGE_STAGE_CORRUPT" in r.stderr


def test_a_copy_still_requires_the_source(tmp_path):
    dest = tmp_path / "opt-containers" / "image.sqsh"
    r = _stage(tmp_path, source=tmp_path / "cluster" / "missing.sqsh", dest=dest, payload=b"x", rsync="1")
    assert r.returncode == 2
    assert "source size" in r.stderr and "actual=missing" in r.stderr
