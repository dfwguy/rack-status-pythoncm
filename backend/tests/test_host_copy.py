# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for host_exec.copy_tree_to_host (the transport copy primitive).

Local exec mode makes the "remote" host the local filesystem, so the full bundle -> chunk ->
reassemble -> extract path runs end to end without SSH.
"""
from __future__ import annotations

import os

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import host_exec


def test_copies_a_tree_and_lands_every_file(tmp_path):
    src = tmp_path / "src"
    (src / "scripts").mkdir(parents=True)
    (src / "configs").mkdir()
    (src / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (src / "configs" / "manager.yaml").write_text("key: value\n")
    (src / "top.txt").write_text("top\n")
    # A file that must be skipped, and a __pycache__ dir that must not ride along.
    (src / "junk.pyc").write_bytes(b"\x00\x01")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    dest = tmp_path / "dest"
    err = host_exec.copy_tree_to_host(src, str(dest))
    assert err is None, err

    assert (dest / "scripts" / "run.sh").read_text() == "#!/bin/sh\necho hi\n"
    assert (dest / "configs" / "manager.yaml").read_text() == "key: value\n"
    assert (dest / "top.txt").read_text() == "top\n"
    # skip rules held
    assert not (dest / "junk.pyc").exists()
    assert not (dest / "__pycache__").exists()
    # the staging dir cleaned itself up
    assert not any(p.name.startswith(".nvis-p2p-stage-") for p in dest.iterdir())


def test_missing_source_is_an_error_not_a_crash(tmp_path):
    err = host_exec.copy_tree_to_host(tmp_path / "does-not-exist", str(tmp_path / "d"))
    assert err and "not a directory" in err


def test_binary_content_survives_the_bundle(tmp_path):
    # A file larger than one chunk, with non-text bytes, must arrive byte-identical.
    src = tmp_path / "src"
    src.mkdir()
    blob = bytes(range(256)) * 1024  # 256 KiB, spans several 64 KiB chunks
    (src / "bin.dat").write_bytes(blob)
    dest = tmp_path / "dest"
    assert host_exec.copy_tree_to_host(src, str(dest)) is None
    assert (dest / "bin.dat").read_bytes() == blob
