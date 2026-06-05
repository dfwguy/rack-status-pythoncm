"""Tests for platform providers."""
import os
from unittest import mock

import pytest

from platforms import get_platform
from platforms.base import PlatformProvider
from platforms.bcm import BCMProvider
from platforms.maas import MAASProvider


class TestPlatformRegistry:
    def test_default_is_bcm(self):
        with mock.patch.dict(os.environ, {"RACK_PLATFORM": "bcm"}, clear=False):
            from platforms import _instance
            import platforms
            platforms._instance = None
            plat = get_platform()
            assert isinstance(plat, BCMProvider)

    def test_maas_provider(self):
        with mock.patch.dict(os.environ, {"RACK_PLATFORM": "maas"}, clear=False):
            import platforms
            platforms._instance = None
            plat = get_platform()
            assert isinstance(plat, MAASProvider)


class TestBCMProvider:
    def setup_method(self):
        self.plat = BCMProvider()

    def test_list_racks_cmd(self):
        cmd = self.plat.list_racks_cmd()
        assert cmd[0] == "cmsh"
        assert "rack" in cmd[-1]
        assert "list" in cmd[-1]

    def test_device_status_cmd(self):
        cmd = self.plat.device_status_cmd()
        assert cmd[0] == "cmsh"
        assert "ds" in cmd[-1]

    def test_physical_rf0_cmd(self):
        cmd = self.plat.physical_rf0_cmd()
        assert "cmsh" == cmd[0]
        assert "foreach" in cmd[-1]

    def test_device_power_cmd(self):
        cmd = self.plat.device_power_cmd("device; power -n p1-r01-ct01 off")
        assert cmd[0] == "cmsh"

    def test_services_restart_remote_cmd(self):
        cmd = self.plat.services_restart_remote_cmd()
        assert "munge" in cmd
        assert "slurmd" in cmd

    def test_pdsh_cmd(self):
        cmd = self.plat.pdsh_cmd("p1-r01-ct[01-02]", "uptime")
        assert cmd[0] == "pdsh"
        assert "-w" in cmd
        assert "uptime" in cmd

    def test_pdsh_cmd_with_fanout(self):
        cmd = self.plat.pdsh_cmd("hosts", "cmd", fanout=32)
        assert "-f" in cmd
        assert "32" in cmd


class TestMAASProvider:
    def setup_method(self):
        self.plat = MAASProvider()

    def test_list_racks_cmd(self):
        cmd = self.plat.list_racks_cmd()
        assert "maas" == cmd[0]
        assert "tags" in cmd

    def test_device_status_cmd(self):
        cmd = self.plat.device_status_cmd()
        assert "maas" == cmd[0]
        assert "machines" in cmd


class TestSlurmPrefix:
    def test_with_module_load(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RACK_SLURM_SKIP_MODULE_LOAD", None)
            plat = BCMProvider()
            cmd = plat.slurm_prefix("sinfo -N")
            assert "module load" in cmd
            assert "sinfo -N" in cmd

    def test_skip_module_load(self):
        with mock.patch.dict(os.environ, {"RACK_SLURM_SKIP_MODULE_LOAD": "1"}, clear=False):
            plat = BCMProvider()
            cmd = plat.slurm_prefix("sinfo -N")
            assert "module load" not in cmd
            assert "sinfo -N" == cmd

    def test_sinfo_cmd(self):
        with mock.patch.dict(os.environ, {"RACK_SLURM_SKIP_MODULE_LOAD": "1"}, clear=False):
            plat = BCMProvider()
            cmd = plat.sinfo_cmd("-R --long")
            assert "sinfo" in cmd
            assert "-R --long" in cmd

    def test_scontrol_hostnames_cmd(self):
        with mock.patch.dict(os.environ, {"RACK_SLURM_SKIP_MODULE_LOAD": "1"}, clear=False):
            plat = BCMProvider()
            cmd = plat.scontrol_hostnames_cmd("node[01-04]")
            assert "scontrol" in cmd
            assert "hostnames" in cmd
