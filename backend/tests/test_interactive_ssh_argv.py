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

import host_exec


def _reset() -> None:
    """Force the next call to re-read the environment. ``_ensure_init`` re-runs when ``_CONN`` is None."""
    host_exec._CONN = None
    host_exec._CONTROL_SSH = None
    host_exec._CONFIG = None


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
    def test_connect_timeout_is_set_on_every_hop(self, monkeypatch):
        argv = _argv(monkeypatch)
        # One per hop. A single occurrence means the inner ssh can still hang forever.
        assert argv.count("ConnectTimeout=15") == 2, argv

    def test_keepalives_are_set_on_every_hop(self, monkeypatch):
        argv = _argv(monkeypatch)
        assert argv.count("ServerAliveInterval=15") == 2, argv
        assert argv.count("ServerAliveCountMax=3") == 2, argv

    def test_the_inner_hop_carries_them_not_just_the_outer(self, monkeypatch):
        argv = _argv(monkeypatch)
        # Everything after the head node target is the command run ON the head — the second hop.
        head_at = next(i for i, a in enumerate(argv) if a.startswith("cmods@"))
        inner = argv[head_at:]
        assert "ConnectTimeout=15" in inner, inner
        assert "ServerAliveInterval=15" in inner, inner

    def test_the_node_is_still_the_final_argument(self, monkeypatch):
        """Options must be inserted before the target, not appended after it."""
        assert _argv(monkeypatch)[-1] == "ct07-01"

    def test_a_password_prompt_is_still_possible(self, monkeypatch):
        """BatchMode would break the one place where being asked for a password is legitimate."""
        argv = _argv(monkeypatch)
        assert not any("BatchMode" in a for a in argv), argv

    def test_a_direct_target_is_bounded_too(self, monkeypatch):
        """With no SSH head configured the shell goes straight to the node; same reasoning."""
        for k in ("RACK_SSH_HOST", "RACK_SSH_USER"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("RACK_EXEC_MODE", "local")
        _reset()
        argv = host_exec.ssh_to_node_argv("ct07-01")
        assert "ConnectTimeout=15" in argv, argv
