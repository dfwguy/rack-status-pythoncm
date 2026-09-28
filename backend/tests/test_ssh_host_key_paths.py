# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Every ssh the app starts honours ``RACK_SSH_KNOWN_HOSTS`` -- not only the control connection.

The setting was honoured by one argv builder. The web terminal, hop legs and bug-report
collection each hard-coded ``StrictHostKeyChecking=no``, so a deployment that turned
verification on still had a head-node terminal that accepted any key. These tests pin each path,
with the setting unset (the permissive default, unchanged) and set (verified), plus the one
deliberate exception: legs to compute nodes, which are never verified.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import host_exec
import pytest

REPO = Path(__file__).resolve().parents[2]
KNOWN = "/etc/nvis/known_hosts"


def _verification(monkeypatch, value: str | None) -> None:
    """Pin what ``_eget`` answers for the setting, independent of the catalog overlay."""
    monkeypatch.setattr(
        "config_env._eget",
        lambda n, d="": (value if value is not None else d) if n == "RACK_SSH_KNOWN_HOSTS" else d,
    )


def _verified(argv: list[str]) -> bool:
    joined = " ".join(argv)
    if "StrictHostKeyChecking=yes" in joined:
        assert f"UserKnownHostsFile={KNOWN}" in joined, argv
        assert "StrictHostKeyChecking=no" not in joined, argv
        return True
    assert "StrictHostKeyChecking=no" in joined, argv
    return False


def _reset() -> None:
    host_exec._CONN = None
    host_exec._CONTROL_SSH = None
    host_exec._CONFIG = None


@pytest.fixture(autouse=True)
def _leave_transport_as_we_found_it():
    yield
    _reset()


class TestSharedOptions:
    def test_an_interactive_session_does_not_record_unverified_keys(self, monkeypatch):
        from transport.ssh import host_key_opts

        _verification(monkeypatch, None)
        assert host_key_opts(record=False) == [
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        ]

    def test_record_changes_nothing_once_verification_is_on(self, monkeypatch):
        from transport.ssh import host_key_opts

        _verification(monkeypatch, KNOWN)
        assert host_key_opts(record=False) == host_key_opts()


class TestHeadNodeTerminal:
    """The path this change exists for: the terminal ignored the setting entirely."""

    def _argv(self, monkeypatch, known: str | None) -> list[str]:
        from transport.ssh import SSHConfig, SSHExecutor

        _verification(monkeypatch, known)
        return SSHExecutor(SSHConfig(host="head", user="cmods")).interactive_argv()

    def test_unset_keeps_todays_behaviour(self, monkeypatch):
        argv = self._argv(monkeypatch, None)
        assert not _verified(argv)
        assert "UserKnownHostsFile=/dev/null" in argv

    def test_set_verifies_the_head(self, monkeypatch):
        assert _verified(self._argv(monkeypatch, KNOWN))

    def test_through_the_real_config_plumbing(self, monkeypatch, tmp_path):
        known = tmp_path / "known_hosts"
        known.write_text("")
        for k, v in {
            "RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "10.0.0.1", "RACK_SSH_USER": "cmods",
            "RACK_SSH_KNOWN_HOSTS": str(known),
        }.items():
            monkeypatch.setenv(k, v)
        _reset()
        argv = host_exec.ssh_to_headnode_argv()
        assert "StrictHostKeyChecking=yes" in argv
        assert f"UserKnownHostsFile={known}" in argv


class TestHops:
    def _local_hop(self, **kw):
        from transport.hop import HopExecutor
        from transport.local import LocalExecutor

        return HopExecutor(LocalExecutor(), "head", **kw)

    def _remote_hop(self, **kw):
        from transport.hop import HopExecutor
        from transport.ssh import SSHConfig, SSHExecutor

        return HopExecutor(SSHExecutor(SSHConfig(host="head")), "tray-01", **kw)

    @pytest.mark.parametrize("known", [None, KNOWN])
    def test_a_gateway_leg_from_this_process_follows_the_setting(self, monkeypatch, known):
        _verification(monkeypatch, known)
        hop = self._local_hop(gateway=True)
        assert _verified(hop._ssh_prefix()) is (known is not None)
        assert _verified(hop._interactive_ssh_prefix()) is (known is not None)

    def test_a_node_leg_from_this_process_is_never_verified(self, monkeypatch):
        """The app on the head node: its control connection is local, so node hops start here.

        Verifying them would demand every compute node's key and break each fan-out.
        """
        _verification(monkeypatch, KNOWN)
        hop = self._local_hop()
        assert not _verified(hop._ssh_prefix())
        assert not _verified(hop._interactive_ssh_prefix())

    def test_a_leg_that_runs_on_the_head_is_never_verified(self, monkeypatch):
        """The app's known_hosts file is not on the head, and nodes are reimaged routinely."""
        _verification(monkeypatch, KNOWN)
        for hop in (self._remote_hop(), self._remote_hop(gateway=True)):
            assert not _verified(hop._ssh_prefix())
            assert "UserKnownHostsFile=/dev/null" in hop._ssh_prefix()

    def test_a_wrapped_local_base_still_counts_as_this_process(self, monkeypatch):
        from transport.compose import SudoExecutor
        from transport.hop import HopExecutor
        from transport.local import LocalExecutor

        _verification(monkeypatch, KNOWN)
        hop = HopExecutor(SudoExecutor(LocalExecutor()), "head", gateway=True)
        assert _verified(hop._ssh_prefix())

    def test_a_declared_chain_marks_its_hops_as_gateways(self, monkeypatch):
        from transport import build_chain

        _verification(monkeypatch, KNOWN)
        conn = build_chain([{"kind": "ssh", "host": "bastion"}, {"kind": "ssh", "host": "head"}])
        head_leg = conn.executor
        bastion_leg = head_leg.base
        assert head_leg.gateway and bastion_leg.gateway
        assert _verified(bastion_leg._ssh_prefix()), "app -> bastion leaves this process"
        assert not _verified(head_leg._ssh_prefix()), "bastion -> head runs on the bastion"

    def test_a_topology_via_endpoint_is_a_gateway(self):
        from transport.hop import HopExecutor
        from transport.topology import build_executors

        built = build_executors(
            [
                {"id": "app", "type": "local"},
                {"id": "head", "type": "ssh", "host": "head", "via": "app"},
            ]
        )
        assert isinstance(built["head"], HopExecutor)
        assert built["head"].gateway

    def test_fanout_node_hops_are_not_gateways(self):
        from transport.base import Connection
        from transport.local import LocalExecutor

        assert Connection(LocalExecutor()).hop("tray-01").executor.gateway is False


class TestBugReportCollection:
    def test_unset_is_unverified(self, monkeypatch):
        from api import bug_report

        monkeypatch.delenv("RACK_BUG_REPORT_SSH_OPTS", raising=False)
        _verification(monkeypatch, None)
        assert not _verified(bug_report._bug_report_ssh_argv_prefix())

    def test_set_verifies(self, monkeypatch):
        from api import bug_report

        monkeypatch.delenv("RACK_BUG_REPORT_SSH_OPTS", raising=False)
        _verification(monkeypatch, KNOWN)
        argv = bug_report._bug_report_ssh_argv_prefix()
        assert _verified(argv)
        assert "BatchMode=yes" in argv

    def test_an_explicit_override_still_wins(self, monkeypatch):
        from api import bug_report

        monkeypatch.setenv("RACK_BUG_REPORT_SSH_OPTS", "-o ProxyJump=head")
        _verification(monkeypatch, KNOWN)
        assert bug_report._bug_report_ssh_argv_prefix() == ["ssh", "-o", "ProxyJump=head"]


class TestDeployScripts:
    """bin/deploy-scripts.sh mirrors host_key_opts in shell; this keeps the two in step."""

    def _run(self, tmp_path: Path, known: str | None) -> str:
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        log = tmp_path / "calls.log"
        for name in ("ssh", "rsync"):
            tool = fake_bin / name
            tool.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\nexit 1\n')
            tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        env = {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "RACK_SSH_HOST": "head",
        }
        if known is not None:
            env["RACK_SSH_KNOWN_HOSTS"] = known
        result = subprocess.run(
            ["bash", str(REPO / "bin" / "deploy-scripts.sh"), "--dry-run"],
            env=env, capture_output=True, text=True, timeout=60, check=False,
        )
        # Asserted on every run, not only the unset case: macOS's bash 3.2 tolerates what bash 5
        # rejects under `set -u`, so "it made the right calls" alone passed on a laptop while the
        # script died on the first line that touched the variable on every Linux host.
        assert "unbound variable" not in result.stderr, result.stderr
        return log.read_text() if log.exists() else ""

    def test_unset_is_unverified(self, tmp_path):
        calls = self._run(tmp_path, None)
        assert "StrictHostKeyChecking=no" in calls
        assert "StrictHostKeyChecking=yes" not in calls

    def test_set_verifies_on_every_call(self, tmp_path):
        calls = self._run(tmp_path, KNOWN).splitlines()
        assert calls, "the script made no ssh/rsync call"
        for line in calls:
            assert "StrictHostKeyChecking=yes" in line, line
            assert f"UserKnownHostsFile={KNOWN}" in line, line
            assert "StrictHostKeyChecking=no" not in line, line

    def test_a_path_with_spaces_is_refused_rather_than_split(self, tmp_path):
        assert self._run(tmp_path, "/etc/my hosts") == ""


def test_every_app_side_path_honours_host_key_opts():
    """No backend module decides host-key behaviour except transport/ssh.py.

    An allow-list that may only shrink: a new ssh argv builder that hard-codes its own options is
    the exact gap this change closed, and it would pass every other test here.
    """
    allowed: set[str] = set()
    offenders = []
    for path in sorted((REPO / "backend").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("backend/tests/") or rel == "backend/transport/ssh.py":
            continue
        if re.search(r"StrictHostKeyChecking|UserKnownHostsFile", path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert sorted(set(offenders) - allowed) == [], (
        "take host-key options from transport.ssh.host_key_opts() (or in_cluster_host_key_opts "
        f"for a leg that runs on a cluster host): {offenders}"
    )
