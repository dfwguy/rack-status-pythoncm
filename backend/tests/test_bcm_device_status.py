# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A node's BCM state comes from pythoncm's per-device status, not from ``cmsh ds`` text.

Nscale NC, 2026-10-20: ``cmsh ds`` printed ``gpu93-su35 ... [ DOWN ] (... failed to send jobs to
sys1-gso1-p-phy-gpu10-su23)``, the text parser marked the healthy gpu10-su23 down from that
message, and its rack dropped out of multi-rack selection. pythoncm's
``cmstatus.getStatusForDevices`` returns one typed status per device uuid (gpu10-su23: UP with a
separate healthCheckFailed flag), for the whole fleet in one RPC.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import native_status as ns
import nvis_id_templates as ids
import pytest
from platforms.base import _compute_node_records, device_state

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nvis_bcm_query.py"
_spec = importlib.util.spec_from_file_location("nvis_bcm_query_status", SCRIPT)
nbq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nbq)

GPU10 = "sys1-gso1-p-phy-gpu10-su23"
GPU93 = "sys1-gso1-p-phy-gpu93-su35"
MESSAGE = f"An error occurred while provisioning. failed to send jobs to {GPU10}"


def _status(uuid, word, hcf=False, info=""):
    """A pythoncm DeviceStatus: ``status`` is an enum member, the rest plain fields."""
    return SimpleNamespace(ref_device_uuid=uuid, status=SimpleNamespace(name=word),
                           healthCheckFailed=hcf, infoMessage=info, userMessage="")


class _Cluster:
    def __init__(self, devices, statuses=None, fail=None):
        self.devices, self.statuses, self.fail = devices, statuses or [], fail
        self.parallel = SimpleNamespace(device_status=self._device_status)

    def _device_status(self, devices):
        if self.fail:
            raise self.fail
        assert devices == self.devices  # one call for the whole fleet
        return self.statuses

    def get_by_type(self, name):
        return self.devices if name == "PhysicalNode" else []


def _node(hostname, uuid):
    return SimpleNamespace(hostname=hostname, uuid=uuid, rack="", interfaces=[], mac="")


NC_CLUSTER = _Cluster(
    [_node(GPU10, "u10"), _node(GPU93, "u93"), _node("sys1-gso1-p-phy-gpu1-su19", "u1")],
    [_status("u10", "UP", hcf=True), _status("u93", "DOWN", info=MESSAGE), None],
)


class TestHelper:
    def test_a_status_keeps_the_health_flag_and_message_beside_the_state(self):
        assert nbq.shape_status(_status("u", "UP", hcf=True)) == {
            "status": "UP", "health_check_failed": True, "status_message": ""}
        assert nbq.shape_status(None) == {}
        assert nbq.shape_status(SimpleNamespace(status=None)) == {}

    def test_each_node_gets_its_own_status_and_another_nodes_message_stays_with_that_node(self):
        args = SimpleNamespace(query="nodes", type="PhysicalNode")
        out = {r["hostname"]: r for r in nbq._run_query(NC_CLUSTER, args, "/p", 0.0)["data"]}
        assert out[GPU10]["status"] == "UP" and out[GPU10]["health_check_failed"] is True
        assert out[GPU10]["status_message"] == ""
        assert out[GPU93]["status"] == "DOWN" and out[GPU93]["status_message"] == MESSAGE
        # A device BCM reports no status for carries no status fields at all.
        assert "status" not in out["sys1-gso1-p-phy-gpu1-su19"]

    def test_a_failed_status_call_still_returns_the_inventory_and_says_why(self):
        cluster = _Cluster([_node(GPU10, "u10")], fail=OSError("cmstatus unavailable"))
        payload = nbq._run_query(cluster, SimpleNamespace(query="nodes", type="PhysicalNode"), "/p", 0.0)
        assert payload["ok"] is True and payload["data"][0]["hostname"] == GPU10
        assert "status" not in payload["data"][0]
        assert payload["status_error"] == "OSError: cmstatus unavailable"

    def test_the_version_query_reports_no_status_error(self):
        payload = nbq._run_query(SimpleNamespace(version="11"), SimpleNamespace(query="version"), "/p", 0.0)
        assert "status_error" not in payload


@pytest.mark.parametrize(("raw", "want"), [
    ("UP", "up"), ("up", "up"), ("DOWN", "down"), ("UNREACHABLE", "down"),
    ("INSTALLER_FAILED", "inst"), ("INSTALLING", "inst"), ("BOOTING", "inst"),
    ("UNDEFINED", "unknown"), ("", "unknown"), (None, "unknown"), (" Up ", "up"),
])
def test_one_mapping_serves_the_structured_status_and_the_ds_bracket(raw, want):
    assert device_state(raw) == want


def test_provider_records_carry_the_normalized_status():
    rows = _compute_node_records(
        [{"hostname": GPU10, "rack_id": "V19", "status": "UP"},
         {"hostname": GPU93, "rack_id": "V1", "status": "DOWN"},
         {"hostname": "sys1-gso1-p-phy-gpu2-su20", "rack_id": "U2", "status": "INSTALLER_FAILED"},
         {"hostname": "sys1-gso1-p-phy-gpu1-su19", "rack_id": "U20"}],
        provider="BCM", rack_field="rack_id", status_field="status",
    )
    assert [r["status"] for r in rows] == ["up", "down", "inst", "unknown"]


def test_the_bcm_provider_reads_the_status_field(monkeypatch):
    from platforms import bcm

    monkeypatch.setattr(bcm.bcm_query, "nodes", lambda: {"ok": True, "data": [
        {"hostname": GPU10, "rack_id": "V19", "rack_unit": "29", "bmc_ip": "", "status": "UP"}]})
    assert bcm.BCMProvider().compute_node_inventory()[0]["status"] == "up"


@pytest.fixture
def placed(monkeypatch):
    naming = ids.get_nvis_naming(ids.DEFAULT_NVIS_RACK_ID, ids.DEFAULT_NVIS_CT_ID, ids.DEFAULT_NVIS_NVSW_ID)
    monkeypatch.setattr(ns, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(ns, "_hostname_encodes_rack", lambda: False)
    monkeypatch.setattr(ns, "_legacy_ds_configured", lambda: False)


class TestInventory:
    DS = f"{GPU93} ...... [  DOWN  ] ({MESSAGE})\n{GPU10} ...... [ DOWN ]\n"

    def test_the_providers_status_wins_over_ds_text_and_the_scheduler(self, placed):
        records = [{"id": GPU10, "rack": "V19", "status": "up"}, {"id": GPU93, "rack": "V1", "status": "down"}]
        inv = {n["id"]: n for n in ns.build_node_inventory(
            {GPU10: "down*", GPU93: "idle"}, ["V19", "V1"], self.DS, records)}
        assert inv[GPU10]["status"] == "up"
        assert inv[GPU93]["status"] == "down"

    def test_without_a_provider_status_ds_then_the_scheduler_decide(self, placed):
        records = [{"id": GPU10, "rack": "V19", "status": "unknown"}, {"id": GPU93, "rack": "V1", "status": "unknown"}]
        inv = {n["id"]: n for n in ns.build_node_inventory(
            {GPU10: "idle", GPU93: "idle"}, ["V19", "V1"], f"{GPU10} .. [ DOWN ]\n", records)}
        assert inv[GPU10]["status"] == "down"  # from ds
        assert inv[GPU93]["status"] == "up"  # from Slurm idle

    def test_an_undefined_ds_state_does_not_override_the_scheduler(self, placed):
        inv = ns.build_node_inventory({GPU10: "idle"}, [], f"{GPU10} .. [ UNDEFINED ]\n")
        assert inv[0]["status"] == "up"

    def test_the_rack_column_is_built_from_the_inventory(self, placed):
        inventory = [
            {"id": "a", "rack": "V19", "tray": 10, "status": "up"},
            {"id": "b", "rack": "V19", "tray": 11, "status": "down"},
            {"id": "c", "rack": "V19", "tray": 12, "status": "inst"},
            {"id": "d", "rack": "V19", "tray": 9, "status": "up"},
            {"id": "e", "rack": "V19", "tray": 13, "status": "unknown"},
            {"id": "f", "rack": None, "status": "down"},
        ]
        assert ns.ct_column_from_node_inventory(inventory) == {
            "V19": "DN:V19-ct[11];INST:V19-ct[12];UP:V19-ct[9-10]"}

    @pytest.mark.parametrize(("records", "want"), [
        (None, False), ([], False), ([{"status": "unknown"}], False),
        ([{"status": "unknown"}, {"status": "up"}], True),
    ])
    def test_the_provider_reports_status_only_when_a_record_has_one(self, records, want):
        assert ns.provider_reports_status(records) is want


class TestDsWorker:
    def _state(self, records):
        state = ns.NativeRackTableState.__new__(ns.NativeRackTableState)
        state.lock = threading.Lock()
        state.node_records = records
        state.ct_by_rack, state.ds_text, state.ds_done, state.last_errors = {"x": "old"}, "old", False, []
        return state

    def test_cmsh_ds_is_not_run_when_the_provider_reports_status(self, monkeypatch):
        ds = Mock(side_effect=AssertionError("cmsh ds must not run"))
        monkeypatch.setattr(ns, "_cmsh_ds_text", ds)
        state = self._state([{"id": GPU10, "status": "up"}])
        state._worker_ds()
        assert (state.ds_done, state.ds_text, state.ct_by_rack) == (True, "", {})

    def test_cmsh_ds_is_the_fallback_without_provider_status(self, monkeypatch, placed):
        monkeypatch.setattr(ns, "_cmsh_ds_text", Mock(return_value="p5-r01-ct01 .. [ UP ]\n"))
        state = self._state([{"id": "p5-r01-ct01", "status": "unknown"}])
        state._worker_ds()
        assert state.ds_done and state.ds_text.startswith("p5-r01-ct01")
        assert state.ct_by_rack == {"p5-r01": "UP:p5-r01-ct[1]"}
