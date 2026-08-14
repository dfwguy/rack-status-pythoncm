# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Unit tests for the declarative connection topology.

The topology is what lets one build run anywhere: endpoints say how to reach a
place (chaining through ``via`` for N hops), roles say what a call site wants
(``logs``, ``scripts``, ``bcm``, or any site-defined name) without encoding which
host that is. These tests assert the resolution rules — chain nesting, role
binding, fallbacks and the failure modes — rather than any single deployment's
shape.
"""
import base64
import re

import pytest
from transport.topology import (
    SSHDefaults,
    TopologyError,
    build_executors,
    build_topology,
    describe_topology,
    role_roots,
)

LOCAL = {"id": "here", "type": "local"}
BASTION = {"id": "bastion", "type": "ssh", "host": "10.0.0.5", "user": "jump"}
HEAD = {"id": "head", "type": "ssh", "host": "10.1.1.1", "user": "cmods", "via": "bastion"}
NODE = {"id": "node", "type": "ssh", "host": "dgx-01", "via": "head"}

CONTROL_ROLE = {"id": "control", "endpoint": "head"}


def _argv_str(conn, cmd="hostname") -> str:
    return " ".join(conn.wrap_argv(cmd))


def _decode_legs(argv: str) -> list[str]:
    """Peel the base64 payload of each hop, returning one string per leg."""
    legs = [argv]
    cur = argv
    while True:
        m = re.search(r"printf %s ([A-Za-z0-9+/=]{8,})", cur)
        if not m:
            return legs
        cur = base64.b64decode(m.group(1)).decode()
        legs.append(cur)


class TestEndpointResolution:
    def test_local_endpoint_runs_locally(self):
        ex = build_executors([LOCAL])["here"]
        assert ex.kind == "local"

    def test_ssh_endpoint_without_via_is_a_leaf(self):
        ex = build_executors([BASTION])["bastion"]
        assert ex.kind == "ssh"
        assert "jump@10.0.0.5" in " ".join(ex.wrap_argv("true"))

    def test_via_chain_nests_each_leg_through_its_predecessor(self):
        conn = build_topology([BASTION, HEAD, NODE], [CONTROL_ROLE, {"id": "n", "endpoint": "node"}])
        legs = _decode_legs(_argv_str(conn.select("n")))
        # The outermost leg is the only one the app dials directly; the head and the
        # node legs ride inside it, each executed by the hop before it.
        assert "jump@10.0.0.5" in legs[0]
        assert "cmods@10.1.1.1" in legs[0]
        assert any("dgx-01" in leg for leg in legs[1:])
        assert legs[-1].strip().endswith("hostname")

    def test_endpoints_are_built_once_and_shared(self):
        built = build_executors([BASTION, HEAD, NODE])
        # `node` hops through the very same head executor object, so the chain
        # reuses one multiplexed ssh master instead of dialling twice.
        assert built["node"].base is built["head"]

    def test_unknown_via_is_rejected(self):
        with pytest.raises(TopologyError, match="unknown endpoint"):
            build_executors([{"id": "a", "type": "ssh", "host": "h", "via": "ghost"}])

    def test_via_cycle_is_rejected(self):
        with pytest.raises(TopologyError, match="cycle"):
            build_executors(
                [
                    {"id": "a", "type": "ssh", "host": "h1", "via": "b"},
                    {"id": "b", "type": "ssh", "host": "h2", "via": "a"},
                ]
            )

    def test_ssh_endpoint_needs_a_host(self):
        with pytest.raises(TopologyError, match="need a host"):
            build_executors([{"id": "a", "type": "ssh"}])

    def test_local_endpoint_cannot_hop(self):
        with pytest.raises(TopologyError, match="cannot have a via hop"):
            build_executors([BASTION, {"id": "a", "type": "local", "via": "bastion"}])

    def test_defaults_fill_unset_ssh_fields(self, tmp_path):
        # ssh only passes -i for a key that exists, so the fixture writes real files.
        key = tmp_path / "default_key"
        key.write_text("x")
        d = SSHDefaults(user="fallback-user", key=str(key), port="2222")
        argv = " ".join(
            build_executors([{"id": "a", "type": "ssh", "host": "h"}], defaults=d)["a"].wrap_argv("true")
        )
        assert "fallback-user@h" in argv
        assert f"-i {key}" in argv
        assert "-p 2222" in argv

    def test_explicit_endpoint_fields_beat_defaults(self, tmp_path):
        fallback = tmp_path / "default_key"
        fallback.write_text("x")
        real = tmp_path / "real_key"
        real.write_text("x")
        d = SSHDefaults(user="fallback-user", key=str(fallback))
        argv = " ".join(
            build_executors(
                [{"id": "a", "type": "ssh", "host": "h", "user": "real", "key": str(real)}],
                defaults=d,
            )["a"].wrap_argv("true")
        )
        assert "real@h" in argv
        assert f"-i {real}" in argv
        assert str(fallback) not in argv


class TestRoleBinding:
    def test_roles_can_point_at_different_endpoints(self):
        conn = build_topology(
            [LOCAL, BASTION, HEAD],
            [CONTROL_ROLE, {"id": "scripts", "endpoint": "here"}],
        )
        assert conn.select("control").kind == "ssh"
        assert conn.select("scripts").kind == "local"

    def test_role_without_an_endpoint_follows_control(self):
        conn = build_topology([BASTION, HEAD], [CONTROL_ROLE, {"id": "logs"}])
        assert _argv_str(conn.select("logs")) == _argv_str(conn.select("control"))

    def test_arbitrary_role_names_are_allowed(self):
        conn = build_topology(
            [LOCAL, BASTION, HEAD],
            [CONTROL_ROLE, {"id": "site-invented-thing", "endpoint": "here"}],
        )
        assert conn.select("site-invented-thing").kind == "local"

    def test_unknown_role_falls_back_to_control(self):
        conn = build_topology([BASTION, HEAD], [CONTROL_ROLE])
        assert _argv_str(conn.select("never-defined")) == _argv_str(conn.select("control"))

    def test_legacy_script_profile_stays_resolvable(self):
        # Workflow YAML and older call sites still say profile="script".
        conn = build_topology([BASTION, HEAD], [CONTROL_ROLE])
        assert "script" in conn.targets

    def test_legacy_script_profile_does_not_alias_the_scripts_role(self):
        # "script" is the privileged trust domain; "scripts" is merely where the
        # script tree lives. Aliasing them would route root operations by filename.
        conn = build_topology(
            [LOCAL, BASTION, HEAD],
            [CONTROL_ROLE, {"id": "scripts", "endpoint": "here"}],
        )
        assert conn.select("script").kind == "ssh"
        assert _argv_str(conn.select("script")) == _argv_str(conn.select("control"))

    def test_explicit_script_role_wins_over_the_fallback(self):
        conn = build_topology(
            [LOCAL, BASTION, HEAD, {"id": "slogin", "type": "ssh", "host": "slogin", "user": "root"}],
            [CONTROL_ROLE, {"id": "script", "endpoint": "slogin"}],
        )
        assert "root@slogin" in _argv_str(conn.select("script"))

    def test_role_sudo_escalates(self):
        conn = build_topology(
            [BASTION, HEAD], [CONTROL_ROLE, {"id": "priv", "endpoint": "head", "sudo": True}]
        )
        # head is itself a hop, so the escalated command travels base64-encoded.
        assert any("sudo" in leg for leg in _decode_legs(_argv_str(conn.select("priv"))))
        assert not any("sudo" in leg for leg in _decode_legs(_argv_str(conn.select("control"))))

    def test_endpoint_sudo_applies_to_the_hop_payload(self):
        conn = build_topology(
            [BASTION, HEAD, {"id": "root-node", "type": "ssh", "host": "n1", "via": "head", "sudo": True}],
            [CONTROL_ROLE, {"id": "n", "endpoint": "root-node"}],
        )
        assert any("sudo" in leg for leg in _decode_legs(_argv_str(conn.select("n"))))

    def test_missing_control_role_is_rejected(self):
        with pytest.raises(TopologyError, match="no 'control' role"):
            build_topology([BASTION, HEAD], [{"id": "logs", "endpoint": "head"}])

    def test_no_roles_is_rejected(self):
        with pytest.raises(TopologyError, match="no roles"):
            build_topology([BASTION, HEAD], [])

    def test_role_pointing_at_unknown_endpoint_is_rejected(self):
        with pytest.raises(TopologyError, match="unknown endpoint"):
            build_topology([BASTION, HEAD], [CONTROL_ROLE, {"id": "x", "endpoint": "ghost"}])


class TestWriteBytes:
    """The write half of the filesystem API, which artifacts (exports) rely on."""

    def test_local_write_creates_parents_and_content(self, tmp_path):
        ex = build_executors([LOCAL])["here"]
        dest = tmp_path / "nested" / "deeper" / "report.bin"
        assert ex.write_bytes(dest, b"\x00\x01binary\xff") is None
        assert dest.read_bytes() == b"\x00\x01binary\xff"

    def test_local_write_leaves_no_temp_file(self, tmp_path):
        ex = build_executors([LOCAL])["here"]
        dest = tmp_path / "report.bin"
        ex.write_bytes(dest, b"x")
        assert [p.name for p in tmp_path.iterdir()] == ["report.bin"]

    def test_local_write_reports_error_instead_of_raising(self, tmp_path):
        ex = build_executors([LOCAL])["here"]
        blocker = tmp_path / "afile"
        blocker.write_text("not a directory")
        err = ex.write_bytes(blocker / "child.bin", b"x")
        assert err and "Error" in err

    def test_remote_write_ships_base64_and_renames(self):
        ex = build_executors([BASTION])["bastion"]
        sent: list[str] = []

        def fake_run(argv, **kwargs):
            sent.append(argv[-1] if isinstance(argv, list) else argv)
            return __import__("subprocess").CompletedProcess(argv, 0, "", "")

        ex.run = fake_run  # type: ignore[method-assign]
        assert ex.write_bytes("/tmp/out/report.xlsx", b"\x50\x4b\x03\x04") is None
        script = sent[0]
        assert base64.b64encode(b"\x50\x4b\x03\x04").decode() in script
        assert "mkdir -p /tmp/out" in script
        assert "base64 -d" in script
        assert "mv -f" in script  # atomic rename, never a partial artifact


class TestRoleRootsAndDescription:
    def test_role_roots_collects_declared_roots_only(self):
        roles = [
            CONTROL_ROLE,
            {"id": "logs", "endpoint": "head", "root": "/home/cmods/logs"},
            {"id": "scripts", "endpoint": "head"},
        ]
        assert role_roots(roles) == {"logs": "/home/cmods/logs"}

    def test_describe_reports_the_hop_path_per_role(self):
        desc = describe_topology(
            [LOCAL, BASTION, HEAD, NODE],
            [CONTROL_ROLE, {"id": "bcm", "endpoint": "node"}, {"id": "s", "endpoint": "here"}],
        )
        assert desc["control"]["path"] == ["app", "bastion", "head"]
        assert desc["bcm"]["path"] == ["app", "bastion", "head", "node"]
        assert desc["bcm"]["hops"] == 3
        # A local endpoint is where the app already is, so it costs no hop.
        assert desc["s"]["hops"] == 0
