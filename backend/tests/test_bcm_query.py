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
"""Tests for the out-of-process pythoncm query path.

Every failure asserted here is one that actually happened while bringing this up against the
Nscale TX head on 2026-08-11, in this order: pythoncm missing from the interpreter's path, the
distro python finding pythoncm but lacking its passlib dependency, and the cluster CA existing
but being root-only so the handshake died on a bare PermissionError. The value of the design is
that each of those becomes an answerable JSON message instead of a traceback, so the caller can
fall back to cmsh — which is exactly what these tests pin.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from uuid import uuid4

import bcm_query
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nvis_bcm_query.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("nvis_bcm_query", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nbq = _load_script()


class _Iface:
    def __init__(self, name, ip="", mac="", kind=None):
        self.name, self.ip, self.mac = name, ip, mac
        if kind:
            self.__class__ = type(kind, (_Iface,), {})


class _BmcNetworkInterface(_Iface):
    pass


class _RackPosition:
    """Mirrors BCM 11.0's RackPosition entity: a U number and a rack *uuid*.

    Its str() is a JSON dump of the whole object, which is precisely the trap shape_device has
    to avoid, so this deliberately reproduces that.
    """

    def __init__(self, position, rack):
        self.position, self.rack = position, rack

    def __str__(self):
        return json.dumps({"uuid": "x", "baseType": "RackPosition",
                           "rack": self.rack, "position": self.position}, indent=4)


class _PhysicalNode:
    def __init__(self, hostname, rack="", interfaces=(), mac="", **extra):
        self.hostname, self.rack, self.mac = hostname, rack, mac
        self.interfaces = list(interfaces)
        for k, v in extra.items():
            setattr(self, k, v)


class TestShapeDevice:
    def test_the_bmc_interface_supplies_the_address_cmsh_cannot_give_fleet_wide(self):
        node = _PhysicalNode(
            "ink50-dh01-d01-18", rack="WH1D1:18", mac="2C:9D:90:E6:8E:8E",
            interfaces=[
                _Iface("bond0", ip="10.40.0.9"),
                _BmcNetworkInterface("rf0", ip="10.40.128.9", mac="2C:9D:90:E6:8E:8F"),
            ],
        )
        out = nbq.shape_device(node)
        assert out["bmc_ip"] == "10.40.128.9"
        assert out["bmc_mac"] == "2C:9D:90:E6:8E:8F"
        # The provisioning address must not be mistaken for the BMC; that is the exact bug
        # `cmsh device list -f ip` walks into.
        assert out["bmc_ip"] != "10.40.0.9"

    def test_rack_placement_comes_from_the_rackposition_entity_via_the_uuid_map(self):
        # The real BCM 11.0 shape: rackPosition is an entity holding the U and a rack *uuid*,
        # not a name. Verified against ink50-dh01-d01-18 -> WH1D1:18.
        pos = _RackPosition(position=18, rack="c83be58a-c098-4f44-8c47-845c8bd6c1bd")
        out = nbq.shape_device(
            _PhysicalNode("ink50-dh01-d01-18", rackPosition=pos),
            {"c83be58a-c098-4f44-8c47-845c8bd6c1bd": "WH1D1"},
        )
        assert (out["rack_id"], out["rack_unit"]) == ("WH1D1", "18")

    def test_an_entity_valued_attribute_never_leaks_its_json_dump_into_the_report(self):
        # str() on a pythoncm entity yields a multi-line JSON blob. Shipping that as a rack id
        # is how this was caught: rack_id came out as '{\n    "uuid"'.
        out = nbq.shape_device(_PhysicalNode("n1", rack=_RackPosition(position=18, rack="u1")))
        assert "{" not in out["rack_id"] and "uuid" not in out["rack_id"]

    def test_the_fused_cmsh_string_still_works_for_releases_that_return_one(self):
        out = nbq.shape_device(_PhysicalNode("n1", rack="WH1D1:18"))
        assert (out["rack_id"], out["rack_unit"]) == ("WH1D1", "18")

    def test_an_unresolvable_rack_uuid_yields_empty_rather_than_a_uuid_in_the_report(self):
        pos = _RackPosition(position=3, rack="unknown-uuid")
        out = nbq.shape_device(_PhysicalNode("n1", rackPosition=pos), {})
        assert out["rack_id"] == ""
        assert out["rack_unit"] == "3"

    def test_bmc_detection_falls_back_to_the_interface_name_when_the_class_is_generic(self):
        out = nbq.shape_device(_PhysicalNode("n1", interfaces=[_Iface("ipmi0", ip="10.1.1.1")]))
        assert out["bmc_ip"] == "10.1.1.1"

    def test_a_node_with_no_bmc_interface_yields_empty_not_a_wrong_address(self):
        out = nbq.shape_device(_PhysicalNode("n1", interfaces=[_Iface("bond0", ip="10.40.0.9")]))
        assert out["bmc_ip"] == ""

    def test_attribute_spelling_differences_across_bcm_releases_are_tolerated(self):
        # Older releases spell it rackposition; the report must not go blank on those.
        out = nbq.shape_device(_PhysicalNode("n1", rackposition=4))
        assert out["rack_unit"] == "4"

    def test_missing_attributes_never_raise(self):
        class Bare:
            pass

        out = nbq.shape_device(Bare())
        assert out["hostname"] == "" and out["interfaces"] == []

    def test_uuid_values_are_serialized_without_entity_json(self):
        value = uuid4()
        assert nbq._scalar(value) == str(value)


class TestAuthentication:
    class Settings:
        cookie = None

        def __init__(self, accepted=True):
            self.accepted = accepted
            self.login = None

        def get_cookie(self, username, password):
            self.login = (username, password)
            if self.accepted:
                self.cookie = "session=opaque"
            return self.accepted

    @staticmethod
    def args(**values):
        defaults = {"username": "", "password_file": "", "cookie_file": ""}
        defaults.update(values)
        return Namespace(**defaults)

    def test_cookie_file_is_loaded_without_putting_secret_in_argv(self, tmp_path):
        cookie_file = tmp_path / "cookie"
        cookie_file.write_text("session=secret-value")
        settings = self.Settings()
        mode = nbq._configure_auth(settings, self.args(cookie_file=str(cookie_file)))
        assert mode == "cookie"
        assert settings.cookie == "session=secret-value"

    def test_password_file_authenticates_and_rejection_is_explicit(self, tmp_path):
        password_file = tmp_path / "password"
        password_file.write_text("not-on-command-line")
        settings = self.Settings(accepted=False)
        with pytest.raises(PermissionError, match="rejected"):
            nbq._configure_auth(
                settings,
                self.args(username="operator", password_file=str(password_file)),
            )
        assert settings.login == ("operator", "not-on-command-line")

    def test_partial_login_configuration_fails_closed(self, monkeypatch):
        monkeypatch.delenv("NVIS_BCM_PASSWORD", raising=False)
        with pytest.raises(PermissionError, match="both BCM username and password"):
            nbq._configure_auth(self.Settings(), self.args(username="operator"))


class TestModuleResolution:
    def test_a_bcm_style_install_root_is_expanded_to_its_site_packages(self, tmp_path, monkeypatch):
        pkg = tmp_path / "pythoncm-11" / "lib" / "python3.12" / "site-packages" / "pythoncm"
        pkg.mkdir(parents=True)
        monkeypatch.setattr(sys, "path", list(sys.path))
        used = nbq.resolve_module_path([str(tmp_path / "pythoncm-11")])
        assert used and used.endswith("site-packages")
        assert used in sys.path

    def test_a_direct_parent_directory_also_resolves(self, tmp_path, monkeypatch):
        (tmp_path / "pythoncm").mkdir()
        monkeypatch.setattr(sys, "path", list(sys.path))
        assert nbq.resolve_module_path([str(tmp_path)]) == str(tmp_path)

    def test_nothing_found_declines_instead_of_guessing(self, tmp_path):
        assert nbq.resolve_module_path([str(tmp_path / "absent")]) is None

    def test_the_newest_per_cluster_copy_wins_when_several_are_present(self, tmp_path, monkeypatch):
        for version in ("pythoncm-10", "pythoncm-11"):
            (tmp_path / version / "pythoncm").mkdir(parents=True)
        monkeypatch.setattr(sys, "path", list(sys.path))
        assert nbq.resolve_module_path([str(tmp_path / "pythoncm-*")]).endswith("pythoncm-11")

    def test_default_ca_uses_the_readable_bundle_next_to_pythoncm(self, monkeypatch):
        paths = [
            "/cm/local/apps/cmd/pythoncm/lib/python3.9/site-packages/pythoncm/etc/cacert.pem",
            "/cm/local/apps/cmd/pythoncm/lib/python3.12/site-packages/pythoncm/etc/cacert.pem",
        ]
        monkeypatch.setattr(nbq.glob, "glob", lambda _pattern: paths)
        assert "python3.12/site-packages/pythoncm/etc/cacert.pem" in nbq.default_ca_path()

    def test_numeric_version_sort_does_not_choose_pythoncm_9_over_11(self, tmp_path, monkeypatch):
        for version in ("pythoncm-9", "pythoncm-11"):
            (tmp_path / version / "pythoncm").mkdir(parents=True)
        monkeypatch.setattr(sys, "path", list(sys.path))
        assert nbq.resolve_module_path([str(tmp_path / "pythoncm-*")]).endswith("pythoncm-11")


class _EnumValue:
    def __init__(self, name):
        self.name = name


class _Node:
    def __init__(self, hostname):
        self.hostname = hostname
        self.uuid = uuid4()

    def drain(self):
        return None


class _NodeGroup:
    def __init__(self, name, nodes):
        self.name = name
        self.nodes = nodes


class _Rack:
    def __init__(self, name):
        self.name = name
        self.uuid = uuid4()
        self.serialNumber = ""


def test_topology_shapes_racks_and_naturally_sorted_node_groups():
    node_a = _Node("ink50-dh02-a01-11")
    node_b = _Node("ink50-dh01-a01-11")
    groups = [
        _NodeGroup("SU12", [node_b.uuid]),
        _NodeGroup("SU2", []),
        _NodeGroup("SU1", [node_a]),
    ]
    racks = [_Rack("WH1A1"), _Rack("EH1A1")]

    class Cluster:
        def get_by_type(self, kind):
            return {"Rack": racks, "NodeGroup": groups}.get(kind, [])

        def get_by_uuid(self, value):
            return node_b if value == node_b.uuid else None

    assert nbq._shape_topology(Cluster()) == {
        "racks": [
            {"name": "EH1A1", "uuid": str(racks[1].uuid), "serial": ""},
            {"name": "WH1A1", "uuid": str(racks[0].uuid), "serial": ""},
        ],
            "node_groups": [
            {"name": "SU1", "nodes": ["ink50-dh02-a01-11"]},
            {"name": "SU2", "nodes": []},
                {"name": "SU12", "nodes": ["ink50-dh01-a01-11"]},
            ],
            "nodes": [],
        }


class _Status:
    def __init__(self, node, state):
        self.device = node
        self.ref_device_uuid = node.uuid
        self.status = _EnumValue(state)
        self.reportedStatus = self.status
        self.closed = self.muted = self.burning = False

    @property
    def final_status(self):
        return self.status


class _DrainResult:
    def __init__(self, nodes, states, reasons=None, success=True):
        self.ref_node_uuids = [node.uuid for node in nodes]
        self.result = [_EnumValue(state) for state in states]
        self.reason = reasons or [""] * len(nodes)
        self.success = success


class TestSchedulerNodeState:
    def test_state_uses_device_and_drain_without_workload_manager(self):
        allocated, drained, down, unknown = [_Node(name) for name in ("n1", "n2", "n3", "n4")]
        nodes = [allocated, drained, down, unknown]

        class Parallel:
            @staticmethod
            def device_status(_devices):
                return [_Status(allocated, "UP"), _Status(drained, "UP"),
                        _Status(down, "DOWN"), _Status(unknown, "UP")]

            @staticmethod
            def drain_status(_devices):
                return [_DrainResult(
                    nodes,
                    ["UNDRAINED", "DRAINED", "UNDRAINED", "UNKNOWN"],
                    ["", "maintenance", "", "controller uncertain"],
                )]

        class Job:
            def __init__(self):
                self.jobID = "42"
                self.status = _EnumValue("RUNNING")
                self.nodes = [allocated.uuid]

        class Workload:
            @staticmethod
            def get_jobs(_wlms):
                return True, [Job()]

        class Cluster:
            parallel = Parallel()
            workload = Workload()

            def get_by_type(self, kind):
                return nodes if kind == "PhysicalNode" else []

            def get_by_uuid(self, value):
                return next((node for node in nodes if node.uuid == value), None)

        rows = {row["hostname"]: row for row in nbq._scheduler_nodes(Cluster())}
        # Allocation ownership is overlaid from Rack Status' ProgramRunner
        # registry by scheduler_runtime.  This helper must never query BCM's
        # WlmCluster/cluster.workload surface because Nscale backs it with Slurm.
        assert rows["n1"]["scheduler_state"] == "idle"
        assert rows["n2"]["scheduler_state"] == "drain"
        assert rows["n2"]["reason"] == "maintenance"
        assert rows["n3"]["scheduler_state"] == "down"
        assert rows["n4"]["scheduler_state"] == "unknown"

    def test_missing_drain_result_preserves_physical_up_without_claiming_idle(self):
        node = _Node("n1")

        class Cluster:
            parallel = type("Parallel", (), {
                "device_status": lambda _self, _nodes: [_Status(node, "UP")],
                "drain_status": lambda _self, _nodes: [],
            })()

            def get_by_type(self, kind):
                return [node] if kind == "PhysicalNode" else []

            def get_by_uuid(self, value):
                return node if value == node.uuid else None

        row = nbq._scheduler_nodes(Cluster())[0]
        assert row["device_status"] == "UP"
        assert row["drain_state"] == "UNKNOWN"
        assert row["scheduler_state"] == "up"

    def test_workload_surface_is_never_read(self):
        node = _Node("n1")

        class Cluster:
            def __init__(self):
                self.parallel = type("Parallel", (), {
                    "device_status": lambda _self, _nodes: [_Status(node, "UP")],
                    "drain_status": lambda _self, _nodes: [_DrainResult([node], ["UNDRAINED"])],
                })()
                self.workload = type("Workload", (), {
                    "get_jobs": lambda _self, _wlms: (_ for _ in ()).throw(
                        AssertionError("cluster.workload must not be read")
                    ),
                })()

            def get_by_type(self, kind):
                return [node] if kind == "PhysicalNode" else []

            def get_by_uuid(self, value):
                return node if value == node.uuid else None

        rows = nbq._scheduler_nodes(Cluster())
        assert rows[0]["scheduler_state"] == "idle"


class TestCompatibilityOutput:
    def test_status_text_keeps_the_existing_ds_parser_shape(self, capsys):
        nbq._write_cmsh({"data": [{"hostname": "node01", "status": "UP"}]}, "status")
        assert capsys.readouterr().out == "node01 [ UP ]\n"

    def test_rf0_text_keeps_hostname_then_ip_shape(self, capsys):
        nbq._write_cmsh({"data": [{"hostname": "node01", "ip": "10.0.0.1"}]}, "rf0")
        assert capsys.readouterr().out == "node01\n10.0.0.1\n"


class TestScriptAlwaysAnswersJson:
    """The caller parses stdout; a traceback there would be an outage, not a fallback."""

    def test_a_missing_pythoncm_prints_json_not_a_traceback(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--query", "nodes",
             "--module-path", str(tmp_path / "nowhere")],
            capture_output=True, text=True, timeout=60, check=False,
        )
        body = json.loads(proc.stdout)
        assert body["ok"] is False
        assert "pythoncm" in body["error"]
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr

    def test_an_unreadable_ca_is_reported_as_credentials_not_as_a_crash(self, tmp_path):
        # The real Nscale TX case: /cm/local/apps/cmd/etc/cacert.pem exists but is root-only, so
        # an existence check passes and the failure surfaces deep in the TLS handshake instead.
        (tmp_path / "pythoncm").mkdir()
        secret = tmp_path / "cacert.pem"
        secret.write_text("x")
        secret.chmod(0o000)
        # Ask whether *this* user can read it, not what the mode bits say: CI runs as root,
        # which bypasses the permission entirely and makes the case untestable there.
        if os.access(secret, os.R_OK):
            pytest.skip("running as a user that bypasses file permissions (root)")
        # Give it readable cert/key so the CA is the only thing that can be reported. Otherwise
        # the default ~/.cm paths are missing in CI and get named instead.
        cert, key = tmp_path / "cert.pem", tmp_path / "cert.key"
        cert.write_text("x")
        key.write_text("x")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--query", "nodes", "--module-path", str(tmp_path),
             "--cert", str(cert), "--key", str(key), "--ca", str(secret)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        body = json.loads(proc.stdout)
        assert body["ok"] is False
        assert body["error"] == "credentials unavailable"
        assert "cacert.pem" in body["detail"]
        assert "admin" in body["hint"]

    def test_api_hash_mismatch_is_reported_as_version_skew(self, tmp_path):
        package = tmp_path / "pythoncm"
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "settings.py").write_text(
            "class Settings:\n"
            "    def __init__(self, **kwargs): self.cookie = None\n"
        )
        (package / "cluster.py").write_text(
            "class Cluster:\n"
            "    def __init__(self, settings): raise ValueError('Invalid API hash')\n"
        )
        cert, key, ca = (tmp_path / name for name in ("cert.pem", "cert.key", "ca.pem"))
        for path in (cert, key, ca):
            path.write_text("test")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--query", "version",
             "--module-path", str(tmp_path), "--cert", str(cert),
             "--key", str(key), "--ca", str(ca)],
            capture_output=True, text=True, timeout=60, check=False,
        )
        body = json.loads(proc.stdout)
        assert body["ok"] is False
        assert body["error"] == "pythoncm/cmdaemon version mismatch"
        assert "matching install" in body["hint"]
        assert "Traceback" not in proc.stderr


class TestWrapper:
    def test_per_cluster_module_path_and_interpreter_reach_the_command_line(self, monkeypatch):
        monkeypatch.setenv("NVIS_BCM_PYTHON", "/cm/local/apps/python312/bin/python3")
        monkeypatch.setenv("NVIS_PYTHONCM_PATH", "/shared/nvis/pythoncm-11.0")
        cmd = bcm_query.build_command("nodes")
        assert cmd[0] == "/cm/local/apps/python312/bin/python3"
        assert "--module-path" in cmd
        assert cmd[cmd.index("--module-path") + 1] == "/shared/nvis/pythoncm-11.0"

    def test_raw_password_and_cookie_are_never_put_on_the_command_line(self, monkeypatch):
        monkeypatch.setenv("NVIS_BCM_PASSWORD", "password-secret")
        monkeypatch.setenv("NVIS_BCM_COOKIE", "cookie-secret")
        monkeypatch.setenv("NVIS_BCM_PASSWORD_FILE", "/run/secrets/bcm-password")
        monkeypatch.setenv("NVIS_BCM_COOKIE_FILE", "/run/secrets/bcm-cookie")
        cmd = bcm_query.build_command("capabilities")
        joined = " ".join(cmd)
        assert "password-secret" not in joined and "cookie-secret" not in joined
        assert "/run/secrets/bcm-password" in cmd and "/run/secrets/bcm-cookie" in cmd

    def test_configured_broker_owns_the_query_without_spawning_a_helper(self, monkeypatch):
        monkeypatch.setenv("NVIS_PYTHONCM_SOCKET", "/run/nvis-pythoncm/query.sock")
        monkeypatch.setattr(
            bcm_query,
            "_query_broker",
            lambda path, argv, timeout: {
                "ok": True,
                "transport": "persistent-pythoncm",
                "socket": path,
                "argv": argv,
            },
        )
        monkeypatch.setattr(
            bcm_query.host_exec,
            "run_on_host",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("one-shot helper must not run")
            ),
        )

        result = bcm_query.query("node-states", extra_args=["--target", "n1"])

        assert result["ok"] is True
        assert result["transport"] == "persistent-pythoncm"
        assert result["argv"][-3:] == ["node-states", "--target", "n1"]

    def test_unavailable_broker_keeps_the_one_shot_availability_fallback(self, monkeypatch):
        monkeypatch.setenv("NVIS_PYTHONCM_SOCKET", "/run/nvis-pythoncm/query.sock")
        monkeypatch.setattr(
            bcm_query,
            "_query_broker",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("not ready")),
        )

        class Proc:
            stdout = '{"ok": true, "transport": "one-shot"}\n'
            stderr = ""
            returncode = 0

        monkeypatch.setattr(bcm_query.host_exec, "run_on_host", lambda *args, **kwargs: Proc())
        assert bcm_query.query("nodes")["transport"] == "one-shot"

    def test_accepted_broker_failure_never_replays_a_possible_mutation(self, monkeypatch):
        monkeypatch.setenv("NVIS_PYTHONCM_SOCKET", "/run/nvis-pythoncm/query.sock")
        monkeypatch.setattr(
            bcm_query,
            "_query_broker",
            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad response")),
        )
        monkeypatch.setattr(
            bcm_query.host_exec,
            "run_on_host",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("an accepted request must never be replayed")
            ),
        )
        result = bcm_query.query("power", extra_args=["--target", "n1", "--action", "reset"])
        assert result["ok"] is False
        assert result["error"] == "persistent PythonCM broker failed"

    def test_unset_knobs_are_simply_absent_so_the_script_keeps_its_defaults(self, monkeypatch):
        for var in ("NVIS_PYTHONCM_PATH", "NVIS_BCM_HOST", "NVIS_BCM_CA", "NVIS_BCM_PYTHON"):
            monkeypatch.delenv(var, raising=False)
        cmd = bcm_query.build_command("nodes")
        assert "--module-path" not in cmd and "--ca" not in cmd
        assert cmd[0] == bcm_query.DEFAULT_PYTHON

    def test_a_login_banner_before_the_json_does_not_break_parsing(self, monkeypatch):
        class Proc:
            stdout = 'Welcome to the cluster\nLast login: today\n{"ok": true, "data": []}\n'
            stderr = ""
            returncode = 0

        monkeypatch.setattr(host_exec_stub := bcm_query.host_exec, "run_on_host",
                            lambda *a, **k: Proc())
        assert host_exec_stub  # silence the unused-name lint; the patch is the point
        assert bcm_query.query("nodes")["ok"] is True

    def test_a_transport_failure_becomes_a_fallback_signal_not_an_exception(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("ssh: connect to host ... port 22: Connection refused")

        monkeypatch.setattr(bcm_query.host_exec, "run_on_host", boom)
        result = bcm_query.query("nodes")
        assert result["ok"] is False and result["error"] == "transport failed"

    def test_empty_stdout_reports_the_command_so_the_failure_is_reproducible(self, monkeypatch):
        class Proc:
            stdout = ""
            stderr = "bash: /cm/.../nvis_bcm_query.py: No such file or directory"
            returncode = 127

        monkeypatch.setattr(bcm_query.host_exec, "run_on_host", lambda *a, **k: Proc())
        result = bcm_query.query("nodes")
        assert result["ok"] is False
        assert "No such file" in result["detail"]
        assert "nvis_bcm_query.py" in result["command"]

    def test_available_explains_itself_when_the_path_is_not_usable(self, monkeypatch):
        monkeypatch.setattr(bcm_query, "query", lambda *a, **k: {
            "ok": False, "error": "credentials unavailable", "hint": "ask the BCM admin"})
        ok, why = bcm_query.available()
        assert ok is False
        assert "credentials unavailable" in why and "admin" in why

    def test_index_by_hostname_is_empty_on_failure_so_the_report_degrades(self):
        assert bcm_query.index_by_hostname({"ok": False, "error": "nope"}) == {}

    def test_index_by_hostname_keys_a_successful_walk(self):
        result = {"ok": True, "data": [{"hostname": "n1", "bmc_ip": "10.0.0.1"}, {"hostname": ""}]}
        index = bcm_query.index_by_hostname(result)
        assert list(index) == ["n1"]
        assert index["n1"]["bmc_ip"] == "10.0.0.1"
