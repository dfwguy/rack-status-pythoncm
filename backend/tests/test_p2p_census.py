# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The fleet peer census: one honest state per node, from nothing site-specific.

Author: Rafael M. Koike

``census-peer-node.sh`` decides, per node, whether a root-level fix is needed first (``needs-prep``),
a peer re-deploy (``needs-peer``), nothing (``healthy``), or whether the node cannot be reached. On
Nscale NC that split is what turned 772 broken nodes into two fleet-wide fixes instead of 772
investigations. Every value the first version hard-coded -- the service gid, the pinned daemon hash,
a BCM-only allowlist path, the roster -- is now a parameter or derived from ``artifacts.lock``, and
these tests run it with values that are deliberately NOT Nscale's.

The node is a stand-in: ``ssh`` runs the probe locally, and ``id``/``sudo``/``systemctl``/
``sha256sum`` report whatever state the test sets.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

P2P = Path(__file__).resolve().parents[2] / "deploy" / "p2p"
NODE = P2P / "scripts" / "census-peer-node.sh"
FLEET = P2P / "scripts" / "census-peer-fleet.sh"
PIN = "e" * 64
GID = "4242"  # not Nscale's 1003, on purpose

STUBS = {
    # ssh: drop options and the host, run the remote command locally (stdin carries the probe).
    "ssh": '#!/bin/bash\nwhile [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) break;; esac; done\n'
           'host=$1; shift\ncase " $NODE_DOWN " in *" $host "*) exit 255;; esac\nexec bash -c "$*"\n',
    "id": '#!/bin/bash\ncase "$1" in -g) echo "$NODE_GID";; -un) echo svc;; *) echo "uid=1(svc)";; esac\n',
    "sudo": '#!/bin/bash\n[ "$1" = -n ] && shift\n[ "$NODE_SUDO" = yes ] || exit 1\n[ $# -eq 0 ] && exit 0\nexec "$@"\n',
    "systemctl": '#!/bin/bash\n[ "$1" = is-active ] || exit 0\ncase "$2" in nvis-p2p-peer) echo "$NODE_UNIT";;'
                 ' nvis-p2p-catalog-refresh.timer) echo "$NODE_TIMER";; esac\n',
    "sha256sum": '#!/bin/bash\n[ -e "$1" ] && echo "$NODE_DAEMON_SHA  $1"\n',
}


@pytest.fixture
def fleet(tmp_path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in STUBS.items():
        (stubs / name).write_text(body)
        (stubs / name).chmod(0o755)
    base, containers = tmp_path / "peer", tmp_path / "containers"
    (base / "bin").mkdir(parents=True)
    (base / "run").mkdir()
    (base / "bin" / "dfdaemon").write_text("daemon")
    containers.mkdir()
    allow = tmp_path / "allowlist.py"
    allow.write_text('legitUsers = [\n    "svc",\n]\n')
    # A real unix socket stands in for the daemon's. Bound under a short directory -- socket paths
    # are capped near 104 bytes on macOS, which pytest's tmp_path exceeds -- and linked into place;
    # `test -S` follows the link.
    short = tempfile.mkdtemp(prefix="p2ps", dir="/tmp")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(f"{short}/s")
    (base / "run" / "dfdaemon.sock").symlink_to(f"{short}/s")
    healthy = {
        "NODE_GID": GID, "NODE_SUDO": "yes", "NODE_UNIT": "active", "NODE_TIMER": "active",
        "NODE_DAEMON_SHA": PIN, "NODE_DOWN": "",
    }
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("NVIS_", "NODE_"))},
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "NVIS_SERVICE_GID": GID,
        "NVIS_P2P_EXPECTED_DAEMON_SHA": PIN,
        "NVIS_P2P_BASE": str(base),
        "NVIS_P2P_CONTAINER_DIR": str(containers),
        **healthy,
    }
    yield {"env": env, "base": base, "containers": containers, "allow": allow, "tmp": tmp_path}
    sock.close()
    shutil.rmtree(short, ignore_errors=True)


def _node(fleet, **overrides) -> tuple[str, dict[str, str]]:
    res = subprocess.run(["bash", str(NODE), "n1"], env={**fleet["env"], **overrides},
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    node, state, detail = res.stdout.rstrip("\n").split("\t")
    assert node == "n1"
    return state, dict(kv.split("=", 1) for kv in detail.split()) if detail != "-" else {}


class TestNodeState:
    def test_a_ready_node_with_a_pinned_running_peer_is_healthy(self, fleet):
        state, d = _node(fleet)
        assert state == "healthy"
        assert d == {"gid": GID, "sudo": "yes", "containers_writable": "yes", "allowlist": "n/a",
                     "unit": "active", "socket": "yes", "daemon": "pinned", "refresh_timer": "active"}

    @pytest.mark.parametrize(
        "overrides, field",
        [
            ({"NODE_GID": "1100"}, "gid"),                # the account's primary group is wrong
            ({"NODE_SUDO": "no"}, "sudo"),                # the deploy needs sudo -n
        ],
    )
    def test_a_node_problem_needs_prep(self, fleet, overrides, field):
        assert _node(fleet, **overrides)[0] == "needs-prep"

    def test_an_unwritable_container_directory_needs_prep(self, fleet):
        # Not chmod: CI runs as root, which writes through any mode, so a 0555 directory still reads
        # as writable there. A directory that does not exist is unwritable for every user.
        state, d = _node(fleet, NVIS_P2P_CONTAINER_DIR=str(fleet["tmp"] / "no-such-dir"))
        assert (state, d["containers_writable"]) == ("needs-prep", "no")

    def test_a_configured_allowlist_must_name_the_service_account(self, fleet):
        state, d = _node(fleet, NVIS_P2P_HEALTHCHECK_ALLOWLIST=str(fleet["allow"]))
        assert (state, d["allowlist"]) == ("healthy", "yes")
        fleet["allow"].write_text("legitUsers = [\n    \"someone-else\",\n]\n")
        state, d = _node(fleet, NVIS_P2P_HEALTHCHECK_ALLOWLIST=str(fleet["allow"]))
        assert (state, d["allowlist"]) == ("needs-prep", "no")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"NODE_UNIT": "activating"},            # crash-looping peer
            {"NODE_TIMER": "inactive"},             # no catalog refresh
            {"NODE_DAEMON_SHA": "f" * 64},          # not the pinned build
        ],
    )
    def test_a_peer_problem_on_a_ready_node_needs_peer(self, fleet, overrides):
        assert _node(fleet, **overrides)[0] == "needs-peer"

    def test_a_missing_socket_needs_peer(self, fleet):
        (fleet["base"] / "run" / "dfdaemon.sock").unlink()
        state, d = _node(fleet)
        assert (state, d["socket"]) == ("needs-peer", "no")

    def test_a_node_that_does_not_answer_is_unreachable(self, fleet):
        assert _node(fleet, NODE_DOWN="n1") == ("unreachable", {})

    @pytest.mark.parametrize("missing", ["NVIS_SERVICE_GID", "NVIS_P2P_EXPECTED_DAEMON_SHA"])
    def test_the_site_specific_values_are_required(self, fleet, missing):
        env = {k: v for k, v in fleet["env"].items() if k != missing}
        res = subprocess.run(["bash", str(NODE), "n1"], env=env, capture_output=True, text=True, check=False)
        assert res.returncode != 0 and missing in res.stderr


class TestFleet:
    def _run(self, fleet, roster_text, **overrides):
        roster = fleet["tmp"] / "roster.nodes"
        roster.write_text(roster_text)
        return subprocess.run(["bash", str(FLEET), str(roster)], env={**fleet["env"], **overrides},
                              capture_output=True, text=True, check=False)

    def test_every_node_once_sorted_with_a_summary(self, fleet):
        res = self._run(fleet, "n3\n# a comment\n\nn1\nn2\n", NODE_DOWN="n2")
        assert res.returncode == 0, res.stderr
        rows = [line.split("\t")[:2] for line in res.stdout.splitlines()]
        assert rows == [["n1", "healthy"], ["n2", "unreachable"], ["n3", "healthy"]]
        assert "CENSUS_SUMMARY healthy=2 needs-peer=0 needs-prep=0 unreachable=1 total=3" in res.stderr

    def test_the_pin_comes_from_artifacts_lock(self, fleet):
        root = fleet["tmp"] / "root"
        root.mkdir()
        (root / "artifacts.lock").write_text(
            f"# comment\n{'0' * 64}  client-amd64-v1.4.9/dfdaemon\n{PIN}  client-test/dfdaemon\n")
        env = {k: v for k, v in fleet["env"].items() if k != "NVIS_P2P_EXPECTED_DAEMON_SHA"}
        fleet["env"] = env
        res = self._run(fleet, "n1\n", NVIS_P2P_ROOT=str(root), NVIS_P2P_PEER_CLIENT="client-test")
        assert res.stdout.split("\t")[1] == "healthy", res.stdout + res.stderr

    def test_a_client_without_a_pin_is_refused(self, fleet):
        root = fleet["tmp"] / "root"
        root.mkdir()
        (root / "artifacts.lock").write_text(f"{PIN}  client-test/dfdaemon\n")
        fleet["env"] = {k: v for k, v in fleet["env"].items() if k != "NVIS_P2P_EXPECTED_DAEMON_SHA"}
        res = self._run(fleet, "n1\n", NVIS_P2P_ROOT=str(root), NVIS_P2P_PEER_CLIENT="client-other")
        assert res.returncode == 65 and "no pin for client-other/dfdaemon" in res.stderr

    def test_the_shipped_lock_pins_the_default_peer_client(self):
        """The default NVIS_P2P_PEER_CLIENT must have a dfdaemon pin in the lock that ships."""
        lock = (P2P / "artifacts.lock").read_text()
        default = next(line.split(":-", 1)[1].rstrip("}\n") for line in FLEET.read_text().splitlines()
                       if line.startswith("client=${NVIS_P2P_PEER_CLIENT:-"))
        assert any(line.split()[1:] == [f"{default}/dfdaemon"] for line in lock.splitlines()
                   if line and not line.startswith("#"))
