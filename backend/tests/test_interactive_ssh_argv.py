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
"""Interactive terminal SSH must be bounded on every hop.

A compute-node shell is two SSH hops: out to the head node, then on to the tray. An interactive
session carries no deadline of its own, so a hop that accepts the connection and then never answers
leaves the operator with an empty terminal and no diagnosis — which is how an unreachable tray
presented on Nscale TX, indistinguishable from a broken UI.

The second hop is the one that matters and the easy one to forget: the first hop is healthy, so
nothing upstream times out.
"""
from __future__ import annotations

import shlex

import host_exec
import pytest


def _reset() -> None:
    """Force the next call to re-read the environment. ``_ensure_init`` re-runs when ``_CONN`` is None."""
    host_exec._CONN = None
    host_exec._CONTROL_SSH = None
    host_exec._CONFIG = None


def _peel(argv: list[str]) -> list[str]:
    """Peel one nesting level: the trailing remote command, re-split as the next shell sees it.

    Each leg quotes the whole of the leg inside it, so assertions have to unwrap rather than
    look for a token in the flat list. That is also the only way to catch an off-by-one in the
    quoting, which otherwise shows up as a shell on the wrong host.
    """
    return shlex.split(argv[-1])


def _legs(argv: list[str]) -> list[list[str]]:
    """Every leg of the chain, outermost first."""
    out = [argv]
    while len(argv) > 1 and argv[-1].lstrip().startswith(("ssh ", "ssh\t")):
        argv = _peel(argv)
        out.append(argv)
    return out


@pytest.fixture(autouse=True)
def _leave_transport_as_we_found_it():
    """Clear the cached transport after every test in this module.

    ``_reset()`` makes the *next* call re-read the environment, but monkeypatch restores the
    environment without clearing that cache -- so a test that ends in ssh mode leaves every later
    test in the suite pointed at this module's fake head node. Six ``test_workflow_composition``
    tests failed exactly that way when this file gained a second class; it had got away with it
    only because its last test happened to end in local mode, which is what the suite expects.
    """
    yield
    _reset()


def _argv(monkeypatch, **env):
    """Build the interactive argv for a compute node against a configured SSH head."""
    for k, v in {
        "RACK_EXEC_MODE": "ssh",
        "RACK_SSH_HOST": "10.0.0.1",
        "RACK_SSH_USER": "cmods",
        "RACK_SSH_PORT": "22",
        **env,
    }.items():
        monkeypatch.setenv(k, v)
    _reset()
    return host_exec.ssh_to_node_argv("ct07-01")


class TestBothHopsAreBounded:
    """The same properties as :class:`TestAnyNumberOfLegs`, driven through the real env plumbing.

    Kept separate because these go via ``host_exec.ssh_to_node_argv`` and therefore cover the
    config translation as well as the composition.
    """

    def test_connect_timeout_is_set_on_every_hop(self, monkeypatch):
        legs = _legs(_argv(monkeypatch))
        assert len(legs) == 2, legs
        # One per leg. Bounding only the outer one means the inner ssh can still hang forever.
        for leg in legs:
            assert "ConnectTimeout=15" in leg, leg

    def test_keepalives_are_set_on_every_hop(self, monkeypatch):
        for leg in _legs(_argv(monkeypatch)):
            assert "ServerAliveInterval=15" in leg, leg
            assert "ServerAliveCountMax=3" in leg, leg

    def test_the_inner_hop_carries_them_not_just_the_outer(self, monkeypatch):
        inner = _peel(_argv(monkeypatch))
        assert "ConnectTimeout=15" in inner, inner
        assert "ServerAliveInterval=15" in inner, inner

    def test_the_node_is_the_innermost_target(self, monkeypatch):
        """Options must be inserted before the target, not appended after it."""
        assert _peel(_argv(monkeypatch))[-1] == "ct07-01"

    def test_a_password_prompt_is_still_possible(self, monkeypatch):
        """BatchMode would break the one place where being asked for a password is legitimate."""
        for leg in _legs(_argv(monkeypatch)):
            assert not any("BatchMode" in a for a in leg), leg

    def test_a_direct_target_is_bounded_too(self, monkeypatch):
        """App already on the head: one ssh leg to the node, still bounded."""
        for k in ("RACK_SSH_HOST", "RACK_SSH_USER"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("RACK_EXEC_MODE", "local")
        _reset()
        argv = host_exec.ssh_to_node_argv("ct07-01")
        inner = _peel(argv)
        assert "ConnectTimeout=15" in inner, argv
        assert inner[-1] == "ct07-01", argv


class TestAnyNumberOfLegs:
    """The chain, not a hardcoded "head then node", decides how many ssh legs there are.

    The terminal used to hand-build exactly two, so a deployment with anything in front of the
    head node had a working head-node shell and no compute shell. These build the executor
    chain directly, since the point is the composition rather than the env plumbing.
    """

    @staticmethod
    def _chain(n_hops: int):
        from transport.hop import HopExecutor
        from transport.ssh import SSHConfig, SSHExecutor

        ex = SSHExecutor(SSHConfig(host="head", user="cmods", extra_opts=["-J", "bastion"]))
        for i in range(n_hops):
            ex = HopExecutor(ex, f"leg{i}" if i < n_hops - 1 else "tray-01")
        return ex

    def test_one_leg_ends_at_the_destination(self):
        argv = self._chain(0).interactive_argv()
        assert argv[-1] == "cmods@head"

    @pytest.mark.parametrize("hops", [1, 2, 3, 5])
    def test_the_innermost_host_survives_every_nesting_level(self, hops):
        """The real assertion: peel the quoting *hops* times and the tray is still there.

        Eyeballing the rendered string proves nothing -- each added leg quotes the whole of the
        one inside it, so an off-by-one in the quoting shows up only as a shell on the wrong
        host, or a command the far end cannot parse.
        """
        argv = self._chain(hops).interactive_argv()
        for _ in range(hops):
            argv = _peel(argv)
        assert argv[-1] == "tray-01", argv
        assert argv[0] == "ssh"

    @pytest.mark.parametrize("hops", [1, 2, 3])
    def test_every_leg_is_bounded(self, hops):
        """A middle leg that goes silent must not hang: the legs in front of it are healthy."""
        argv = self._chain(hops).interactive_argv()
        seen = 0
        while True:
            assert "ConnectTimeout=15" in argv, f"unbounded leg at depth {seen}: {argv}"
            seen += 1
            if seen > hops:
                break
            argv = _peel(argv)

    def test_the_outermost_leg_keeps_the_jump(self):
        """``extra_opts`` belongs to the leg that leaves this machine, at any depth."""
        argv = self._chain(3).interactive_argv()
        assert "-J" in argv and "bastion" in argv
        assert "bastion" not in _peel(argv), "the jump must not be repeated inside"

    def test_a_local_base_needs_no_ssh_at_all(self):
        """App already on the head: one leg to the tray, no outer ssh."""
        from transport.hop import HopExecutor
        from transport.local import LocalExecutor

        argv = HopExecutor(LocalExecutor(), "tray-01").interactive_argv()
        assert argv[:2] == ["bash", "-lc"]
        assert shlex.split(argv[-1])[-1] == "tray-01"

    def test_a_per_leg_user_is_honoured(self):
        from transport.hop import HopExecutor
        from transport.ssh import SSHConfig, SSHExecutor

        head = SSHExecutor(SSHConfig(host="head", user="cmods"))
        argv = HopExecutor(HopExecutor(head, "sw", user="admin"), "tray-01").interactive_argv()
        assert argv[-1].split()[-1] == "admin@sw" or "admin@sw" in argv[-1]


class TestNodeAccessIsDeclaredNotHardcoded:
    """Relay vs direct is a property of the deployment, not of the node's hostname.

    ``Connection.hop`` briefly decided this inline with one customer's tray regex and one
    customer's key path, which meant ``RACK_DIRECT_NODE_SSH`` did nothing on every other
    cluster. Now the policy lives in ``host_exec.node_connection``.
    """

    def test_relay_is_the_default(self, monkeypatch):
        """Nodes usually trust only the head, so the leg must run FROM the head."""
        monkeypatch.delenv("RACK_DIRECT_NODE_SSH", raising=False)
        argv = _argv(monkeypatch)
        assert any(a.startswith("cmods@") for a in argv), argv
        assert shlex.split(argv[-1])[-1] == "ct07-01"

    def test_direct_skips_the_relay_for_any_hostname(self, monkeypatch):
        """No hostname pattern: the setting either applies to this deployment or it does not."""
        argv = _argv(monkeypatch, RACK_DIRECT_NODE_SSH="1", RACK_NODE_SSH_USER="root")
        assert argv[-1] == "root@ct07-01", argv
        assert not any(a.startswith("cmods@") for a in argv), "direct must not relay via the head"


class TestTheOuterHopCanActuallyReachTheHead:
    """The first hop must be built the way the transport says, not re-derived.

    Observed on Nscale TX 2026-08-19: the head-node terminal connected while every compute-node
    terminal died with ``ssh: connect to host 192.168.10.2 port 22: Operation timed out`` -- the
    *head's* address, from the second hop's own error. Cause: ``ssh_to_node_argv`` hand-built its
    outer hop and omitted ``extra_opts``, where ``RACK_SSH_OPTS`` (and therefore ``-J``) lands. Two
    code paths to the same host, only one of which knew how to get there.

    Invisible on the deployed OVA, because the container reaches the head directly and
    ``extra_opts`` is empty -- so only a local run against a jumped cluster exposes it.
    """

    def _split_at_head(self, argv: list[str]) -> tuple[list[str], list[str]]:
        """(outer hop through the destination, the command run on the head)."""
        head_at = next(i for i, a in enumerate(argv) if a.startswith("cmods@"))
        return argv[: head_at + 1], argv[head_at + 1 :]

    def test_ssh_opts_reach_the_outer_hop(self, monkeypatch):
        argv = _argv(monkeypatch, RACK_SSH_OPTS="-J bastion")
        outer, inner = self._split_at_head(argv)
        assert "-J" in outer and "bastion" in outer, outer
        # The jump belongs to the hop that leaves this machine; the head reaches trays directly.
        assert "bastion" not in inner, inner

    def test_the_outer_hop_never_drops_what_the_head_node_terminal_uses(self, monkeypatch):
        """Both terminals reach the same host, so the compute path must be a superset.

        Asserted as a superset rather than an equality so it keeps holding as options are added:
        any future option that reaches the head-node terminal and not this one fails here.
        """
        node_argv = _argv(monkeypatch, RACK_SSH_OPTS="-J bastion")
        head_argv = host_exec.ssh_to_headnode_argv()  # same cached config, no reset between
        outer, _ = self._split_at_head(node_argv)
        missing = [a for a in head_argv if a not in outer]
        assert not missing, f"outer hop dropped {missing} that the head-node terminal sets"

    def test_the_destination_is_still_the_head_not_the_node(self, monkeypatch):
        argv = _argv(monkeypatch, RACK_SSH_OPTS="-J bastion")
        outer, _ = self._split_at_head(argv)
        assert outer[-1] == "cmods@10.0.0.1"
        assert _peel(argv)[-1] == "ct07-01", "the node is the inner leg's target, not the outer's"
