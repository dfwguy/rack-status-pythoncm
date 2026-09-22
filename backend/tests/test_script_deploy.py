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
"""Pushing the shipped script layers to the head.

Two things are worth pinning here and the rest is detail. **What must never be in the
archive** -- an operator's `custom/` and a project's own copy -- because extraction cannot
harm what it does not contain, and that is the only reason this is safe to re-run. And
**that the payload is chunked**, because the unchunked version passes every test against a
local connection and fails on the first real cluster.
"""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import script_deploy


@pytest.fixture
def source(tmp_path):
    """A miniature `bin/scripts/` with one of everything that must be handled."""
    root = tmp_path / "scripts"
    (root / "lib").mkdir(parents=True)
    (root / "platform" / "gb200-nvl72").mkdir(parents=True)
    (root / "custom").mkdir()
    (root / "__pycache__").mkdir()

    (root / "sysinfo.sh").write_text("#!/bin/bash\necho hi\n")
    (root / "sysinfo.sh").chmod(0o755)
    (root / "lib" / "fabric.inc.sh").write_text("# helper\n")
    (root / "platform" / "gb200-nvl72" / "sysinfo.sh").write_text("#!/bin/bash\necho gb200\n")
    (root / "custom" / "operator-added.sh").write_text("# theirs, not ours\n")
    (root / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"\x00")
    (root / "notes.sh.bak").write_text("old\n")
    return root


def _members(blob: bytes) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        return {m.name: m for m in tar.getmembers()}


class TestWhatGoesIntoTheArchive:
    def test_the_shared_set_lands_under_multi_platform(self, source):
        _blob, manifest = script_deploy.build_bundle(source)
        assert "multi-platform/sysinfo.sh" in manifest
        assert "multi-platform/lib/fabric.inc.sh" in manifest

    def test_a_platform_layer_lands_beside_it_not_inside_it(self, source):
        # `platform/` is a container of layers, not a layer. Copying it wholesale would bury
        # every platform's scripts inside the default set, where nothing would ever find them.
        _blob, manifest = script_deploy.build_bundle(source)
        assert "gb200-nvl72/sysinfo.sh" in manifest
        assert not [n for n in manifest if n.startswith("multi-platform/platform/")]

    def test_the_layout_matches_the_tree_so_one_extraction_places_everything(self, source):
        _blob, manifest = script_deploy.build_bundle(source)
        # Every path is <layer>/<relative>, which is exactly what `script_tree` resolves
        # against once extracted at the tree root.
        assert {n.split("/", 1)[0] for n in manifest} == {"multi-platform", "gb200-nvl72"}


class TestWhatMustNeverBeSent:
    def test_an_operators_custom_directory_is_not_in_the_archive(self, source):
        # The strongest form of "a deploy does not clobber operator scripts": extraction
        # cannot touch a path the archive does not contain.
        _blob, manifest = script_deploy.build_bundle(source)
        assert not [n for n in manifest if "custom" in n]

    def test_build_artifacts_and_editor_leftovers_are_skipped(self, source):
        _blob, manifest = script_deploy.build_bundle(source)
        assert not [n for n in manifest if "__pycache__" in n or n.endswith(".pyc")]
        assert not [n for n in manifest if ".bak" in n]

    def test_there_is_no_delete_pass(self, source):
        # Matching `deploy-scripts.sh`, which has no --delete: a file only on the head is
        # somebody's, and a tar extraction cannot remove it.
        blob, _ = script_deploy.build_bundle(source)
        assert all(m.isfile() or m.isdir() for m in _members(blob).values())


class TestTheExecutableBitSurvives:
    def test_a_script_stays_runnable_on_the_far_side(self, source):
        # A shipped script that arrives without +x is a launch failure at the far end of a
        # chain, reported as something else entirely.
        blob, _ = script_deploy.build_bundle(source)
        assert _members(blob)["multi-platform/sysinfo.sh"].mode & 0o111


class TestItIsChunked:
    def test_a_payload_larger_than_one_command_is_split(self, monkeypatch, tmp_path):
        # The ceiling is MAX_ARG_STRLEN (128 KiB for ONE argv element), not ARG_MAX, and
        # write_bytes puts the base64 inline. An unchunked send of today's ~280 KiB bundle
        # would fail with E2BIG on the first real cluster and never locally.
        assert script_deploy.CHUNK_BYTES <= 96 * 1024

        writes: list[tuple[str, int]] = []
        conn = _FakeHead(writes)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(script_deploy.script_tree, "clear_cache", lambda: None)
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 200_000, ["multi-platform/a.sh"])
        )

        summary = script_deploy.deploy()
        assert summary["ok"] is True
        assert len(writes) == 4  # 200_000 / 65_536, rounded up
        assert all(size <= script_deploy.CHUNK_BYTES for _path, size in writes)

    def test_the_parts_are_named_so_they_reassemble_in_order(self, monkeypatch):
        # `cat part-*` relies on the shell's lexical glob order, which only matches write
        # order while the numbers are zero-padded.
        writes: list[tuple[str, int]] = []
        conn = _FakeHead(writes)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(script_deploy.script_tree, "clear_cache", lambda: None)
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 200_000, ["multi-platform/a.sh"])
        )

        script_deploy.deploy()
        names = [Path(p).name for p, _ in writes]
        assert names == sorted(names) == ["part-0000", "part-0001", "part-0002", "part-0003"]


class TestItProvesTheTargetWorksBeforeWritingAnything:
    """Both of these shipped broken and left 1.1 MB of tarball chunks in git.

    The transport is asymmetric: with no SSH host configured `write_bytes` and `makedirs`
    still succeed -- against the *local* filesystem -- while `run` raises. And under a test
    harness that stubs `subprocess`, `run` returns a confident zero for a command that never
    executed. Either way the old order staged the bundle first and only then discovered it
    could not extract it, leaving the parts behind because cleanup needs the same execution.
    """

    def test_an_unconfigured_target_is_refused_before_a_byte_is_written(self, monkeypatch):
        writes = []

        class Unconfigured(_FakeHead):
            def run(self, cmd, timeout=120, **_kw):
                raise RuntimeError("No SSH host is configured.")

        conn = Unconfigured(writes)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")

        summary = script_deploy.deploy()
        assert summary["ok"] is False
        assert "cannot run commands on the target" in summary["error"]
        # The point of the ordering: nothing was written, so there is nothing to clean up.
        assert writes == []

    def test_an_exit_code_without_output_is_not_accepted_as_proof(self, monkeypatch):
        # A stubbed `subprocess` answers 0 with empty stdout for everything. Trusting the
        # status would stage a real 280 KB bundle for an extraction that cannot happen --
        # which is exactly how the chunks reached the repository.
        writes = []

        class Liar(_FakeHead):
            def run(self, cmd, timeout=120, **_kw):
                import subprocess

                self.commands.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, "", "")

        conn = Liar(writes)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")

        summary = script_deploy.deploy()
        assert summary["ok"] is False
        assert "without running the command" in summary["error"]
        assert writes == []

    def test_a_real_target_echoes_the_nonce_and_proceeds(self, monkeypatch):
        writes = []
        conn = _FakeHead(writes)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(script_deploy.script_tree, "clear_cache", lambda: None)
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 100, ["multi-platform/a.sh"])
        )
        assert script_deploy.deploy()["ok"] is True
        assert writes


class TestSuccessIsVerifiedNotAsserted:
    def test_staging_still_present_after_extract_is_a_failure(self, monkeypatch):
        # The extract command removes its own staging directory last, so the directory
        # surviving means nothing landed however the exit code read. Same rule as
        # `verify-deploy.sh`: "up" is not "working".
        conn = _FakeHead([], staging_survives=True)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(script_deploy.script_tree, "clear_cache", lambda: None)
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 100, ["multi-platform/a.sh"])
        )
        summary = script_deploy.deploy()
        assert summary["ok"] is False
        assert "still exists" in summary["error"]


class TestItReportsRatherThanRaises:
    def test_a_dry_run_never_touches_the_cluster(self, monkeypatch):
        # The wizard previews this on the step BEFORE SSH has been proven, so a preview that
        # needed the head would be blank exactly when it is most useful.
        def fail(*_a, **_k):
            raise AssertionError("a preview must not open a connection")

        monkeypatch.setattr(script_deploy.host_exec, "connection", fail)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        summary = script_deploy.deploy(dry_run=True)
        assert summary["ok"] is True and summary["dry_run"] is True
        assert summary["files"] > 0

    def test_an_unreachable_head_comes_back_as_a_result_not_an_exception(self, monkeypatch):
        conn = _FakeHead([], makedirs_error="ssh: connect to host 10.0.0.1: timed out")
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        summary = script_deploy.deploy()
        assert summary["ok"] is False
        # The operator needs to know it was the connection, not the scripts.
        assert "timed out" in summary["error"]

    def test_a_failed_extract_cleans_up_its_staging_directory(self, monkeypatch):
        # Otherwise every failed attempt leaves a few hundred KB of parts on the one
        # filesystem nobody watches.
        conn = _FakeHead([], extract_rc=1)
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: conn)
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 10, ["multi-platform/a.sh"])
        )
        summary = script_deploy.deploy()
        assert summary["ok"] is False
        assert any("rm -rf" in c for c in conn.commands)

    def test_the_layer_cache_is_cleared_so_the_next_lookup_sees_the_tree(self, monkeypatch):
        # The tree may have come into existence just now. Without this the bootstrap that
        # runs next in the wizard still resolves against the flat legacy path and cannot
        # find the defaults this just sent.
        cleared = []
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: _FakeHead([]))
        monkeypatch.setattr(script_deploy.script_tree, "tree_root", lambda: "/srv/scripts")
        monkeypatch.setattr(script_deploy.script_tree, "clear_cache", lambda: cleared.append(1))
        monkeypatch.setattr(
            script_deploy, "build_bundle", lambda *_a: (b"x" * 10, ["multi-platform/a.sh"])
        )
        script_deploy.deploy()
        assert cleared == [1]


class _FakeHead:
    """A head node that records what was written and run."""

    def __init__(self, writes, *, makedirs_error=None, extract_rc=0, staging_survives=False):
        self.writes = writes
        self.commands: list[str] = []
        self._makedirs_error = makedirs_error
        self._extract_rc = extract_rc
        self._staging_survives = staging_survives

    def makedirs(self, _path):
        return self._makedirs_error

    def write_bytes(self, path, data):
        self.writes.append((path, len(data)))

    def is_dir(self, _path):
        # A working extract removes its own staging directory, so the default is "gone".
        return self._staging_survives

    def run(self, cmd, timeout=120, **_kw):
        import subprocess

        script = cmd[-1]
        self.commands.append(script)
        # Echo the readiness nonce back, the way a host that really ran the command does.
        if cmd[:2] == ["printf", "%s"]:
            return subprocess.CompletedProcess(cmd, 0, cmd[2], "")
        rc = self._extract_rc if "tar -xzf" in script else 0
        return subprocess.CompletedProcess(cmd, rc, "", "tar: short read" if rc else "")
