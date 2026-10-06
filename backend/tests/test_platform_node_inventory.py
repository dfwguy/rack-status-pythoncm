# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Manager inventory remains usable without Slurm or rack assignments."""
import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest
from platforms.base import PlatformProvider
from platforms.bcm import BCMProvider
from platforms.maas import MAASProvider


@pytest.fixture(autouse=True)
def no_live_transport(monkeypatch):
    monkeypatch.setattr(
        "host_exec.run_on_host", Mock(side_effect=AssertionError("unexpected cluster access")),
    )


def test_provider_default_explicitly_reports_unsupported():
    with pytest.raises(RuntimeError, match="unsupported"):
        PlatformProvider.compute_node_inventory(BCMProvider())


def test_bcm_preserves_assigned_and_unassigned_inventory(monkeypatch):
    query = Mock(return_value={"ok": True, "data": [
        {"hostname": "gpu001", "rack_id": "", "bmc_ip": "10.0.0.1"},
        {"hostname": "compute.example.test", "rack_id": "Row A / Rack 2", "bmc_ip": "10.0.0.2"},
        {"hostname": "gpu001", "rack_id": "", "bmc_ip": "10.0.0.1"},
    ]})
    monkeypatch.setattr("bcm_query.nodes", query)
    assert BCMProvider().compute_node_inventory() == [
        {"id": "gpu001", "rack": None, "ip": "10.0.0.1", "status": "unknown", "scheduler_state": ""},
        {"id": "compute.example.test", "rack": "Row A / Rack 2", "ip": "10.0.0.2",
         "status": "unknown", "scheduler_state": ""},
    ]
    query.assert_called_once_with()


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "pythoncm unavailable"},
    {"ok": True, "data": {}},
    {"ok": True},
    {"ok": True, "data": [{"hostname": "gpu001"}, {"hostname": "node;id"}]},
    {"ok": True, "data": [{"hostname": "-option"}]},
    {"ok": True, "data": [None]},
    None,
])
def test_bcm_failures_do_not_become_empty_success(monkeypatch, result):
    monkeypatch.setattr("bcm_query.nodes", lambda: result)
    with pytest.raises(RuntimeError, match="BCM node inventory"):
        BCMProvider().compute_node_inventory()


def test_bcm_successful_empty_inventory_is_allowed(monkeypatch):
    monkeypatch.setattr("bcm_query.nodes", lambda: {"ok": True, "data": []})
    assert BCMProvider().compute_node_inventory() == []


def test_bcm_preserves_physical_rack_unit(monkeypatch):
    monkeypatch.setattr("bcm_query.nodes", lambda: {"ok": True, "data": [
        {"hostname": "dgx-b300-01", "rack_id": "F41", "rack_unit": " 2 "},
    ]})
    node = BCMProvider().compute_node_inventory()[0]
    assert (node["rack"], node["rack_unit"]) == ("F41", "2")


def test_maas_uses_machine_hostnames_without_fabricating_racks_or_health(monkeypatch):
    machine = {"hostname": "maas-gpu-1", "status": 6, "status_name": "Deployed",
               "ip_addresses": ["10.0.0.9"], "tag_names": ["rack-a"]}
    run = Mock(return_value=CompletedProcess([], 0, json.dumps([machine, machine]), ""))
    monkeypatch.setattr("host_exec.run_on_host", run)
    provider = MAASProvider()
    assert provider.compute_node_inventory() == [
        {"id": "maas-gpu-1", "rack": None, "ip": "", "status": "unknown", "scheduler_state": ""},
    ]
    run.assert_called_once_with(provider.device_status_cmd(), timeout=120)


@pytest.mark.parametrize(("returncode", "stdout", "error"), [
    (1, "[]", "exit 1"),
    (0, "not JSON", "invalid JSON"),
    (0, "{}", "list of devices"),
    (0, '[{"hostname":"gpu 01"}]', "invalid hostname"),
    (0, '[{"system_id":"uuid-only"}]', "invalid hostname"),
])
def test_maas_failures_are_explicit(monkeypatch, returncode, stdout, error):
    monkeypatch.setattr(
        "host_exec.run_on_host", Mock(return_value=CompletedProcess([], returncode, stdout, "")),
    )
    with pytest.raises(RuntimeError, match=error):
        MAASProvider().compute_node_inventory()


def test_maas_successful_empty_inventory_is_allowed(monkeypatch):
    monkeypatch.setattr(
        "host_exec.run_on_host", Mock(return_value=CompletedProcess([], 0, "[]", "")),
    )
    assert MAASProvider().compute_node_inventory() == []
