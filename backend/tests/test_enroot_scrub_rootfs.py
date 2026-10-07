# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""``enroot_scrub_rootfs.sh`` restores what enroot's hooks wrote, and nothing the build made.

Author: Rafael M. Koike

The helper exists because ``enroot start --rw`` + ``enroot export`` baked a builder's passwd
entries, home directory and timezone into a NeMo+NCCL image. Two directions matter equally: every
hook-written path must come back to the base (or vanish where the base had none), and the build's
own output -- the new NCCL library, a rebuilt binary -- must survive untouched, or the scrub would
quietly ship the stock image. A builder name reaches ``rm -rf``, so a path-shaped one is refused.

Needs squashfs-tools; skipped where they are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRUB = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "enroot_scrub_rootfs.sh"

pytestmark = pytest.mark.skipif(
    not (shutil.which("mksquashfs") and shutil.which("unsquashfs")),
    reason="needs squashfs-tools (mksquashfs/unsquashfs)",
)


def _base_tree(root: Path) -> None:
    (root / "etc" / "apt" / "apt.conf.d").mkdir(parents=True)
    (root / "etc" / "passwd").write_text("root:x:0:0:root:/root:/bin/bash\n")
    (root / "etc" / "group").write_text("root:x:0:\n")
    shadow = root / "etc" / "shadow"
    shadow.write_text("root:*:20000:0:99999:7:::\n")
    shadow.chmod(0o640)
    (root / "etc" / "localtime").symlink_to("../usr/share/zoneinfo/Etc/UTC")
    (root / "home").mkdir()
    (root / "usr" / "lib").mkdir(parents=True)
    (root / "usr" / "lib" / "libnccl.so.2.29.3").write_bytes(b"old-nccl")
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    (root / "usr" / "local" / "bin" / "all_reduce_perf_mpi").write_bytes(b"old-perf")


def _image(tmp_path: Path) -> tuple[Path, Path]:
    """A base .sqsh and a rootfs created from it, then mutated as the hooks + a build would."""
    base_tree = tmp_path / "base"
    _base_tree(base_tree)
    base_sqsh = tmp_path / "base.sqsh"
    subprocess.run(["mksquashfs", str(base_tree), str(base_sqsh), "-noappend", "-all-root", "-quiet"],
                   check=True, capture_output=True)
    rootfs = tmp_path / "rootfs"
    shutil.copytree(base_tree, rootfs, symlinks=True)
    # What enroot's runtime hooks write.
    with open(rootfs / "etc" / "passwd", "a") as fh:
        fh.write("builder:x:1100:1003::/home/builder:/bin/bash\n")
    with open(rootfs / "etc" / "group", "a") as fh:
        fh.write("cmods:x:1003:\n")
    with open(rootfs / "etc" / "shadow", "a") as fh:
        fh.write("builder:!:20000::::::\n")
    (rootfs / "etc" / "localtime").unlink()
    (rootfs / "etc" / "localtime").symlink_to("../usr/share/zoneinfo/America/New_York")
    (rootfs / "home" / "builder").mkdir()
    (rootfs / "home" / "builder" / ".bashrc").write_text("# builder's shell\n")
    (rootfs / "etc" / "apt" / "apt.conf.d" / "enroot").write_text("// enroot hook\n")
    # What the build itself produced: must survive the scrub.
    (rootfs / "usr" / "lib" / "libnccl.so.2.29.3").unlink()
    (rootfs / "usr" / "lib" / "libnccl.so.2.30.7").write_bytes(b"new-nccl")
    (rootfs / "usr" / "local" / "bin" / "all_reduce_perf_mpi").write_bytes(b"new-perf")
    (rootfs / "etc" / "nvis-image-build").write_text("nccl_source=v2.30.7-1\n")
    return base_sqsh, rootfs


def _scrub(rootfs: Path, base_sqsh: Path, builder: str) -> subprocess.CompletedProcess:
    # check=False: refusals (exit 2) are outcomes the tests assert on, not errors.
    return subprocess.run(["bash", str(SCRUB), str(rootfs), str(base_sqsh), builder],
                          capture_output=True, text=True, check=False)


def test_hook_writes_return_to_the_base(tmp_path):
    base_sqsh, rootfs = _image(tmp_path)
    res = _scrub(rootfs, base_sqsh, "builder")
    assert res.returncode == 0, res.stdout + res.stderr
    base = tmp_path / "base"
    for rel in ("etc/passwd", "etc/group", "etc/shadow"):
        assert (rootfs / rel).read_bytes() == (base / rel).read_bytes(), rel
    assert os.readlink(rootfs / "etc" / "localtime") == "../usr/share/zoneinfo/Etc/UTC"
    assert not (rootfs / "home" / "builder").exists()
    assert not (rootfs / "etc" / "apt" / "apt.conf.d" / "enroot").exists()
    # passwd, group, shadow, localtime restored; the builder's home and apt hook removed.
    assert "SCRUB_DONE restored=4 removed=2" in res.stdout


def test_the_build_output_survives(tmp_path):
    base_sqsh, rootfs = _image(tmp_path)
    assert _scrub(rootfs, base_sqsh, "builder").returncode == 0
    assert (rootfs / "usr" / "lib" / "libnccl.so.2.30.7").read_bytes() == b"new-nccl"
    assert not (rootfs / "usr" / "lib" / "libnccl.so.2.29.3").exists()
    assert (rootfs / "usr" / "local" / "bin" / "all_reduce_perf_mpi").read_bytes() == b"new-perf"
    assert (rootfs / "etc" / "nvis-image-build").read_text() == "nccl_source=v2.30.7-1\n"
    assert (rootfs / "home").is_dir()  # only the builder's directory goes, never home/ itself


@pytest.mark.parametrize("builder", ["../etc", "a/b", "-rf"])
def test_a_path_shaped_builder_is_refused_and_nothing_changes(tmp_path, builder):
    base_sqsh, rootfs = _image(tmp_path)
    res = _scrub(rootfs, base_sqsh, builder)
    assert res.returncode == 2
    assert "invalid builder name" in res.stdout
    assert (rootfs / "home" / "builder" / ".bashrc").exists()
    assert (rootfs / "etc").is_dir()


def test_a_directory_that_is_not_a_rootfs_is_refused(tmp_path):
    base_sqsh, _ = _image(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    res = _scrub(empty, base_sqsh, "builder")
    assert res.returncode == 2
    assert "not a rootfs" in res.stdout
