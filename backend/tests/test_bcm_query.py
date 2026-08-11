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
import subprocess
import sys
from pathlib import Path

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


class TestScriptAlwaysAnswersJson:
    """The caller parses stdout; a traceback there would be an outage, not a fallback."""

    def test_a_missing_pythoncm_prints_json_not_a_traceback(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--query", "nodes",
             "--module-path", str(tmp_path / "nowhere")],
            capture_output=True, text=True, timeout=60,
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
        if secret.stat().st_mode & 0o400:  # running as root: the check cannot fire
            pytest.skip("unreadable-file semantics do not apply to root")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--query", "nodes", "--module-path", str(tmp_path),
             "--ca", str(secret)],
            capture_output=True, text=True, timeout=60,
        )
        body = json.loads(proc.stdout)
        assert body["ok"] is False
        assert body["error"] == "credentials unavailable"
        assert "cacert.pem" in body["detail"]
        assert "admin" in body["hint"]


class TestWrapper:
    def test_per_cluster_module_path_and_interpreter_reach_the_command_line(self, monkeypatch):
        monkeypatch.setenv("NVIS_BCM_PYTHON", "/cm/local/apps/python312/bin/python3")
        monkeypatch.setenv("NVIS_PYTHONCM_PATH", "/shared/nvis/pythoncm-11.0")
        cmd = bcm_query.build_command("nodes")
        assert cmd[0] == "/cm/local/apps/python312/bin/python3"
        assert "--module-path" in cmd
        assert cmd[cmd.index("--module-path") + 1] == "/shared/nvis/pythoncm-11.0"

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
