# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for platform providers."""
import os
from unittest import mock

import pytest
from platforms import get_platform
from platforms.bcm import BCMProvider
from platforms.maas import MAASProvider


class TestPlatformRegistry:
    def test_default_is_bcm(self):
        with mock.patch.dict(os.environ, {"RACK_PLATFORM": "bcm"}, clear=False):
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
        assert cmd[0] == "/cm/local/apps/python312/bin/python3"
        assert cmd[cmd.index("--query") + 1] == "racks"
        assert cmd[cmd.index("--format") + 1] == "cmsh"

    def test_device_status_cmd(self):
        cmd = self.plat.device_status_cmd()
        assert cmd[cmd.index("--query") + 1] == "status"
        assert cmd[cmd.index("--type") + 1] == "PhysicalNode"

    def test_physical_rf0_cmd(self):
        cmd = self.plat.physical_rf0_cmd()
        assert cmd[cmd.index("--query") + 1] == "rf0"

    def test_device_power_cmd(self):
        cmd = self.plat.device_power_cmd("device; power -n p1-r01-ct01 off")
        assert cmd[cmd.index("--query") + 1] == "power"
        assert cmd[cmd.index("--target") + 1] == "p1-r01-ct01"
        assert cmd[cmd.index("--action") + 1] == "off"

    def test_device_power_rejects_arbitrary_cmsh(self):
        with pytest.raises(ValueError, match="unsupported BCM power expression"):
            self.plat.device_power_cmd("device; foreach (*) (shutdown)")

    def test_node_drain_and_undrain_are_pythoncm_commands(self):
        drain = self.plat.node_drain_cmd("node[01-02]", "maintenance window")
        undrain = self.plat.node_undrain_cmd("node[01-02]")
        assert drain[drain.index("--query") + 1] == "drain"
        assert drain[drain.index("--reason") + 1] == "maintenance window"
        assert undrain[undrain.index("--query") + 1] == "undrain"

    def test_legacy_scontrol_node_update_is_translated_without_slurm(self):
        cmd = self.plat.scontrol_cmd(
            "update nodename='node[01-02]' state=drain reason='planned maintenance'"
        )
        assert "nvis_bcm_query.py" in cmd
        assert "--query drain" in cmd
        assert "planned maintenance" in cmd

    def test_unrelated_scontrol_verb_retains_explicit_legacy_fallback(self):
        with mock.patch.dict(os.environ, {"RACK_SLURM_SKIP_MODULE_LOAD": "1"}, clear=False):
            cmd = self.plat.scontrol_cmd("show hostnames node[01-02]")
        assert cmd == "scontrol show hostnames node[01-02]"

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

    @pytest.mark.parametrize(
        ("name", "method"),
        [
            ("SBATCH", "sbatch_cmd"),
            ("SRUN", "srun_cmd"),
            ("SINFO", "sinfo_cmd"),
            ("SQUEUE", "squeue_cmd"),
            ("SACCT", "sacct_cmd"),
            ("SCONTROL", "scontrol_cmd"),
            ("SCANCEL", "scancel_cmd"),
        ],
    )
    def test_every_slurm_cli_honours_its_configured_binary(self, name, method):
        binary = f"/configured/slurm/{name.lower()}"
        with mock.patch.dict(
            os.environ,
            {"RACK_SLURM_SKIP_MODULE_LOAD": "1", f"RACK_SLURM_{name}": binary},
            clear=False,
        ):
            cmd = getattr(BCMProvider(), method)("--version")
        assert cmd == f"{binary} --version"
