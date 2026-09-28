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
"""Deployment hardening: what must never leave this install, and how we reach the head.

Two unrelated controls, kept together because both are about the deploy path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import script_deploy
from transport import ssh

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "sysinfo.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    (root / "lib").mkdir()
    (root / "lib" / "helper.sh").write_text("# helper\n", encoding="utf-8")
    return root


class TestOnlyRegularFilesAreShipped:
    """`tar` reproduces what it is given, and the far side extracts into a shared tree.

    A symlink in `bin/scripts/` is recreated on the head still pointing where it pointed
    here — so a link named `sysinfo.sh` aimed at `/etc/passwd` becomes a file in the scripts
    tree that reads outside it. Refused at build time, which is the side that can name the
    offending file.
    """

    def test_a_clean_tree_builds(self, tree):
        _blob, manifest = script_deploy.build_bundle(tree)
        assert sorted(manifest) == [
            "multi-platform/lib/helper.sh", "multi-platform/sysinfo.sh",
        ]

    def test_a_symlink_at_the_top_level_is_refused(self, tree):
        os.symlink("/etc/passwd", tree / "evil.sh")
        with pytest.raises(script_deploy.UnsafeScript, match="symlink"):
            script_deploy.build_bundle(tree)

    def test_a_symlink_in_a_subdirectory_is_refused(self, tree):
        os.symlink("/etc/shadow", tree / "lib" / "evil.sh")
        with pytest.raises(script_deploy.UnsafeScript, match="symlink"):
            script_deploy.build_bundle(tree)

    def test_the_refusal_names_the_file_and_its_target(self, tree):
        os.symlink("/etc/passwd", tree / "evil.sh")
        with pytest.raises(script_deploy.UnsafeScript) as caught:
            script_deploy.build_bundle(tree)
        # Actionable: the operator has to find and remove it in *this* install.
        assert "evil.sh" in str(caught.value)
        assert "/etc/passwd" in str(caught.value)

    def test_a_symlinked_directory_is_refused_too(self, tree):
        # Following it would ship whatever it points at, under a name that hides the source.
        (tree.parent / "outside").mkdir()
        os.symlink(tree.parent / "outside", tree / "linkdir")
        with pytest.raises(script_deploy.UnsafeScript):
            script_deploy.build_bundle(tree)

    def test_a_refusal_ships_nothing_at_all(self, tree, monkeypatch):
        # Partial is not an option: the archive is the unit, and half a script tree on a
        # head node is worse than none.
        os.symlink("/etc/passwd", tree / "evil.sh")
        monkeypatch.setattr(script_deploy.host_exec, "connection", lambda _p: None)
        with pytest.raises(script_deploy.UnsafeScript):
            script_deploy.build_bundle(tree)

    def test_the_shipped_tree_has_no_symlinks_today(self):
        """The live check. It passes now, which is exactly when to add it."""
        scripts = REPO / "bin" / "scripts"
        links = [p for p in scripts.rglob("*") if p.is_symlink()]
        assert links == [], f"symlinks in the deployed script tree: {links}"


class TestHostKeyVerification:
    """Off by default, and the default is a trade rather than an oversight."""

    def test_the_default_accepts_any_host_key(self, monkeypatch):
        # These deployments are brought up against appliances whose keys change when they
        # are reimaged. An install that refuses to run until somebody seeds a known_hosts
        # is one operators disable permanently, which is a worse end state.
        monkeypatch.setattr("config_env._eget", lambda _n, d="": d)
        assert ssh.host_key_opts() == ["-o", "StrictHostKeyChecking=no"]

    def test_a_configured_known_hosts_turns_verification_on(self, monkeypatch):
        monkeypatch.setattr(
            "config_env._eget",
            lambda n, d="": "/etc/nvis/known_hosts" if n == "RACK_SSH_KNOWN_HOSTS" else d,
        )
        opts = ssh.host_key_opts()
        assert "StrictHostKeyChecking=yes" in opts
        assert "UserKnownHostsFile=/etc/nvis/known_hosts" in opts

    def test_it_also_stops_an_agent_offering_other_identities(self, monkeypatch):
        # Otherwise "which credential reached the head" is unanswerable from the config.
        monkeypatch.setattr(
            "config_env._eget",
            lambda n, d="": "/etc/nvis/known_hosts" if n == "RACK_SSH_KNOWN_HOSTS" else d,
        )
        assert "IdentitiesOnly=yes" in ssh.host_key_opts()

    def test_whitespace_only_is_treated_as_unset(self, monkeypatch):
        # A blank value in an env file must not half-enable a security control.
        monkeypatch.setattr(
            "config_env._eget", lambda n, d="": "   " if n == "RACK_SSH_KNOWN_HOSTS" else d
        )
        assert ssh.host_key_opts() == ["-o", "StrictHostKeyChecking=no"]

    def test_compute_node_hops_are_deliberately_not_verified(self, monkeypatch):
        """The asymmetry is a decision, so it is pinned rather than left to be 'fixed'.

        A node hop runs inside the cluster fabric, where nodes are reimaged as a matter of
        routine; a pinned known_hosts for two thousand of them would fail every bring-up. The
        link worth verifying is the one leaving the app across a tunnel or jumphost.
        test_ssh_host_key_paths.py covers each path's argv; this keeps the reason in place.
        """
        from transport.hop import HopExecutor
        from transport.ssh import SSHConfig, SSHExecutor

        monkeypatch.setattr(
            "config_env._eget",
            lambda n, d="": "/etc/nvis/known_hosts" if n == "RACK_SSH_KNOWN_HOSTS" else d,
        )
        head = SSHExecutor(SSHConfig(host="head"))
        assert "StrictHostKeyChecking=no" in HopExecutor(head, "tray-01")._ssh_prefix()
        source = (REPO / "backend" / "transport" / "hop.py").read_text(encoding="utf-8")
        assert "asymmetry" in source, "the reason must stay written down next to the decision"


class TestEveryShellScriptCarriesItsLicence:
    def test_bin_scripts_have_spdx_headers(self):
        missing = []
        for path in sorted((REPO / "bin").glob("*.sh")):
            head = path.read_text(encoding="utf-8", errors="replace")[:900]
            if "SPDX-FileCopyrightText" not in head:
                missing.append(path.name)
        assert missing == [], f"shell scripts without an SPDX header: {missing}"

    def test_the_shebang_is_still_first(self):
        """The header goes *after* the shebang, or the script stops being executable."""
        for path in sorted((REPO / "bin").glob("*.sh")):
            first = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
            assert first.startswith("#!"), f"{path.name} lost its shebang"
