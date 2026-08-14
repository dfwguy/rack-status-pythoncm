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
"""Tests for the fleet inventory that fills the report's firmware_inventory block.

The fixture is real output from ``bin/scripts/node_inventory.sh`` on ink50-dh01-d01-18, a GB300
compute tray on Nscale TX, captured 2026-08-11. Using the real thing matters here: every schema
mismatch this module exists to handle (the wide PCI domain, the InfiniBand port GUID) is
invisible against invented x86 test data.
"""
from __future__ import annotations

import node_inventory
import pytest

# Trimmed to two GPUs and two NICs; every value is verbatim from the node.
REAL_GB300 = "\n".join([
    "host.hostname\tink50-dh01-d01-18",
    "host.kernel\t6.17.0-1014-nvidia-64k",
    "host.os\tUbuntu 24.04.4 LTS",
    "host.serial\tBKPZPK4",
    "host.bios_version\t1.0.3",
    "host.chassis_model\tPowerEdge XE9712",
    "host.cpu_model\tNeoverse-V2",
    "host.cpu_sockets\t2",
    "host.cpu_cores_per_socket\t72",
    "host.memory_gb\t2064",
    "gpu.0.index\t0",
    "gpu.0.uuid\tGPU-4835505f-2414-1719-b9e7-25667c77c4a7",
    "gpu.0.serial\t1760426805611",
    "gpu.0.model\tNVIDIA GB300",
    "gpu.0.pcie_bdf\t00000008:06:00.0",
    "gpu.0.vbios_version\t97.10.4A.00.1F",
    "gpu.1.index\t1",
    "gpu.1.uuid\tGPU-6333c480-1cac-6340-b575-a9b6e346fade",
    "gpu.1.serial\t1760426805611",
    "gpu.1.model\tNVIDIA GB300",
    "gpu.1.pcie_bdf\t00000009:06:00.0",
    "gpu.1.vbios_version\t97.10.4A.00.1F",
    "gpu.count\t2",
    "gpu.driver_version\t580.126.20",
    "gpu.cuda_runtime\t13.0",
    "gpu.fabric_manager_version\t580.126.20",
    "gpu.imex_version\t580.126.20",
    "nic.0.device\tmlx5_0",
    "nic.0.port\t1",
    "nic.0.firmware\t40.48.1126",
    "nic.0.rate_gbps\t800",
    "nic.0.state\tACTIVE",
    "nic.0.port_guid\t0x549b240300a27aa4",
    "nic.0.pcie_bdf\t0000:03:00.0",
    "nic.0.netdev\tibo1",
    "nic.1.device\tmlx5_1",
    "nic.1.port\t1",
    "nic.1.firmware\t40.48.1126",
    "nic.1.rate_gbps\t800",
    "nic.1.port_guid\t0x549b240300a27ad4",
    "nic.1.pcie_bdf\t0002:03:00.0",
    "nic.count\t2",
    "nic.model\tMellanox Technologies CX8 Family [ConnectX-8]",
    "nic.ofed_version\tOFED-internal-25.10-1.7.1.413",
    "software.dcgm_version\t4.6.1",
])


@pytest.fixture
def record():
    return node_inventory.parse_record(REAL_GB300)


class TestParse:
    def test_indexed_keys_become_ordered_lists(self, record):
        assert [g["device"] for g in record["nics"]] == ["mlx5_0", "mlx5_1"]
        assert [g["index"] for g in record["gpus"]] == ["0", "1"]

    def test_scalars_land_in_their_section(self, record):
        assert record["host"]["serial"] == "BKPZPK4"
        assert record["gpu"]["driver_version"] == "580.126.20"
        assert record["software"]["dcgm_version"] == "4.6.1"

    def test_blank_and_malformed_lines_are_ignored(self):
        rec = node_inventory.parse_record("host.serial\tX\n\nnot a kv line\nhost.os\t\n")
        assert rec["host"] == {"serial": "X"}

    def test_empty_input_yields_an_empty_record_not_a_crash(self):
        rec = node_inventory.parse_record("")
        assert rec["gpus"] == [] and rec["nics"] == []


class TestPcieBdf:
    def test_gb300_wide_domain_is_narrowed_and_reported(self):
        value, deviation = node_inventory.normalize_pcie_bdf("00000008:06:00.0")
        assert value == "0008:06:00.0"
        assert deviation and "narrowed" in deviation

    def test_an_already_valid_bdf_is_untouched_and_silent(self):
        value, deviation = node_inventory.normalize_pcie_bdf("0000:03:00.0")
        assert value == "0000:03:00.0"
        assert deviation is None

    def test_a_domain_that_cannot_be_narrowed_is_left_to_fail_validation(self):
        # Truncating this would name a different device. Better a loud schema failure.
        value, deviation = node_inventory.normalize_pcie_bdf("00010008:06:00.0")
        assert value == "00010008:06:00.0"
        assert deviation and "cannot be narrowed" in deviation

    def test_a_short_domain_is_padded(self):
        assert node_inventory.normalize_pcie_bdf("8:06:00.0")[0] == "0008:06:00.0"


class TestPortGuid:
    def test_the_eui48_is_recovered_from_a_real_ib_port_guid(self):
        # 54:9b:24 | 03:00 (EUI-64 marker) | a2:7a:a4  ->  54:9B:24:A2:7A:A4
        assert node_inventory.mac_from_port_guid("0x549b240300a27aa4") == "54:9B:24:A2:7A:A4"

    def test_the_fffe_marker_form_is_accepted_too(self):
        assert node_inventory.mac_from_port_guid("0x549b24fffea27aa4") == "54:9B:24:A2:7A:A4"

    @pytest.mark.parametrize("bad", ["", "0x0", "not-a-guid", "0x549b241234a27aa4"])
    def test_anything_not_in_eui64_form_declines_rather_than_inventing_a_mac(self, bad):
        assert node_inventory.mac_from_port_guid(bad) is None


class TestBaseline:
    def test_the_modal_value_wins_so_one_outlier_does_not_redefine_the_fleet(self):
        fleet = [node_inventory.parse_record(REAL_GB300) for _ in range(5)]
        fleet[0]["host"]["bios_version"] = "0.9.9-beta"
        baseline = node_inventory.derive_baseline(fleet)
        assert baseline["host"]["bios_version"] == "1.0.3"

    def test_the_outlier_is_recorded_as_that_node_s_override(self):
        fleet = [node_inventory.parse_record(REAL_GB300) for _ in range(3)]
        fleet[0]["host"]["bios_version"] = "0.9.9-beta"
        inv, _ = node_inventory.build_inventory(fleet)
        overrides = [n["firmware_overrides"] for n in inv["nodes"]]
        assert {"host.bios_version": "0.9.9-beta"} in overrides

    def test_a_uniform_fleet_produces_no_overrides_at_all(self):
        fleet = [node_inventory.parse_record(REAL_GB300) for _ in range(3)]
        inv, _ = node_inventory.build_inventory(fleet)
        assert all(n["firmware_overrides"] == {} for n in inv["nodes"])

    def test_counts_and_link_speed_come_from_the_probe_not_a_guess(self, record):
        baseline = node_inventory.derive_baseline([record])
        assert baseline["gpu"]["count_per_node"] == 2
        assert baseline["nic"]["link_speed"] == "800Gb/s"
        assert baseline["gpu"]["model"] == "NVIDIA GB300"


class TestBuildInventory:
    def test_measured_values_replace_the_old_placeholders(self, record):
        inv, _ = node_inventory.build_inventory([record])
        node = inv["nodes"][0]
        assert node["serial"] == "BKPZPK4"
        assert node["gpus"][0]["uuid"] == "GPU-4835505f-2414-1719-b9e7-25667c77c4a7"
        assert node["gpus"][0]["vbios_version"] == "97.10.4A.00.1F"
        # The old report shipped these four constants for every node.
        assert node["gpus"][0]["pcie_bdf"] != "0000:00:00.0"
        assert node["nics"][0]["mac"] != "00:00:00:00:00:00"
        assert node["serial"] != "TBD"
        assert inv["shared_baseline"]["software"]["kernel"] == "6.17.0-1014-nvidia-64k"

    def test_both_schema_substitutions_are_published_as_deviations(self, record):
        _, deviations = node_inventory.build_inventory([record])
        assert any("narrowed" in d for d in deviations)
        assert any("port GUID" in d for d in deviations)

    def test_bcm_supplies_the_identity_the_node_cannot_know_about_itself(self, record):
        bcm = {"ink50-dh01-d01-18": {
            "rack_id": "WH1D1", "rack_unit": "18",
            "bmc_ip": "10.40.128.9", "bmc_mac": "2C:9D:90:E6:8E:8E", "bmc_firmware": "1.10.50",
        }}
        inv, _ = node_inventory.build_inventory([record], bcm_by_host=bcm)
        node = inv["nodes"][0]
        assert node["rack_id"] == "WH1D1"
        assert node["rack_unit"] == "18"
        assert node["bmc"] == {"ip": "10.40.128.9", "mac": "2C:9D:90:E6:8E:8E", "firmware": "1.10.50"}

    def test_the_node_s_own_ipmitool_answer_supplies_the_bmc_without_bcm_or_pythoncm(self):
        # Real values from ink50-dh01-d01-18. This is why the report needs neither a per-node
        # cmsh interfaces call nor the root-only cluster CA to fill the bmc block.
        rec = node_inventory.parse_record(REAL_GB300 + "\n".join([
            "", "bmc.ip\t10.40.128.9", "bmc.mac\t94:10:5A:EE:87:ED", "bmc.firmware\t1.30",
        ]))
        inv, _ = node_inventory.build_inventory([rec])
        assert inv["nodes"][0]["bmc"] == {
            "ip": "10.40.128.9", "mac": "94:10:5A:EE:87:ED", "firmware": "1.30",
        }

    def test_the_node_s_own_answer_wins_over_bcm_but_bcm_still_backfills(self):
        rec = node_inventory.parse_record(REAL_GB300 + "\nbmc.ip\t10.40.128.9")
        bcm = {"ink50-dh01-d01-18": {"bmc_ip": "10.9.9.9", "bmc_firmware": "1.30"}}
        node = node_inventory.build_inventory([rec], bcm_by_host=bcm)[0]["nodes"][0]
        assert node["bmc"]["ip"] == "10.40.128.9"     # measured beats configured
        assert node["bmc"]["firmware"] == "1.30"       # but BCM fills what the node did not say

    def test_a_node_with_no_bcm_entry_still_produces_a_schema_valid_entry(self, record):
        inv, _ = node_inventory.build_inventory([record], bcm_by_host={})
        node = inv["nodes"][0]
        assert node["bmc"]["ip"] == "0.0.0.0"
        assert node["rack_id"] == "TBD"

    def test_fabric_values_are_carried_through_when_a_probe_supplied_them(self, record):
        inv, _ = node_inventory.build_inventory(
            [record], fabric={"nvswitch_firmware": "96.10.32", "ufm_version": "6.20.0"}
        )
        assert inv["shared_baseline"]["fabric"]["nvswitch_firmware"] == "96.10.32"
        assert inv["shared_baseline"]["fabric"]["ufm_version"] == "6.20.0"

    def test_unreachable_fabric_degrades_to_tbd_rather_than_blocking_the_report(self, record):
        inv, _ = node_inventory.build_inventory([record])
        assert inv["shared_baseline"]["fabric"]["ufm_version"] == "TBD"

    def test_nodes_are_sorted_so_two_runs_diff_cleanly(self):
        a = node_inventory.parse_record(REAL_GB300.replace("d01-18", "d01-99"))
        b = node_inventory.parse_record(REAL_GB300)
        inv, _ = node_inventory.build_inventory([a, b])
        assert [n["hostname"] for n in inv["nodes"]] == ["ink50-dh01-d01-18", "ink50-dh01-d01-99"]


class TestSweepConcurrency:
    def test_the_default_stays_under_a_stock_sshd_max_sessions(self):
        """Every hop is a channel on one ControlMaster, so the head's MaxSessions is the ceiling.

        OpenSSH defaults MaxSessions to 10 and Nscale TX does not override it. A default above
        that silently drops nodes with "Session open refused by peer" — a real sweep lost 655
        of 2013 that way — and the loss is invisible in the report unless someone reads the
        deviation line.
        """
        assert node_inventory.DEFAULT_MAX_WORKERS <= 10

    def test_the_worker_count_is_overridable_for_a_head_tuned_higher(self, monkeypatch):
        monkeypatch.setenv("NVIS_INVENTORY_WORKERS", "24")
        seen = {}

        def _fake_fanout(_base, nodes, _cmd, **kw):
            seen.update(kw)
            return []

        import sys
        import types

        monkeypatch.setitem(sys.modules, "transport",
                            types.SimpleNamespace(fanout=_fake_fanout))
        monkeypatch.setitem(sys.modules, "host_exec",
                            types.SimpleNamespace(role_root=lambda _r: "/s",
                                                  connection=lambda _p: object()))
        node_inventory.collect_fleet(["n1"])
        assert seen["max_workers"] == 24


class TestSrunSweep:
    """SSH cannot reach a node we hold no allocation on, so the sweep goes through Slurm."""

    def test_the_command_asks_for_one_task_per_node_and_survives_a_bad_tray(self):
        cmd = node_inventory.build_srun_command(["n1", "n2"], "/s/node_inventory.sh")
        assert "--ntasks-per-node=1" in cmd
        assert "--nodes=2" in cmd
        assert "--nodelist=n1,n2" in cmd
        # One unresponsive tray must not kill the step and lose the other 2000 nodes.
        assert "--no-kill" in cmd
        # Without --label the tasks' output interleaves and no record can be reassembled.
        assert "--label" in cmd

    def test_it_yields_to_a_busy_cluster_instead_of_queueing(self):
        """Inventory must never sit in front of another team's validation run.

        Without --immediate the step queues, which both delays whoever else needs those nodes
        and strands report generation behind a queue it cannot show progress for.
        """
        cmd = node_inventory.build_srun_command(["n1"], "/s/x.sh")
        assert "--immediate=" in cmd
        assert "--nice=" in cmd

    def test_the_command_uses_the_platform_srun_binary(self, monkeypatch):
        class _Platform:
            @staticmethod
            def srun_cmd(args):
                return f"/configured/slurm/srun {args}"

        import platforms

        monkeypatch.setattr(platforms, "get_platform", lambda: _Platform())
        cmd = node_inventory.build_srun_command(["n1"], "/s/x.sh")
        assert cmd.startswith("/configured/slurm/srun ")
        assert not cmd.startswith("srun ")

    def test_a_busy_cluster_stops_the_sweep_rather_than_hammering_the_queue(self, monkeypatch):
        import sys
        import types

        calls = []

        class _Empty:
            stdout = ""
            stderr = "srun: error: Unable to allocate resources: Requested nodes are busy"
            returncode = 1

        def _run(cmd, **kw):
            calls.append(cmd)
            return _Empty()

        monkeypatch.setitem(sys.modules, "host_exec",
                            types.SimpleNamespace(role_root=lambda _r: "/s",
                                                  run_slurm_on_host=_run))
        records, errors = node_inventory.collect_via_srun([f"n{i}" for i in range(500)], chunk=64)
        assert records == []
        # One attempt, not eight: later chunks meet the same queue.
        assert len(calls) == 1
        assert len(errors) == 500
        assert "no Slurm allocation was free" in errors[0]

    def test_the_fleet_is_requested_in_small_chunks_not_one_huge_allocation(self, monkeypatch):
        import sys
        import types

        sizes = []

        def _run(cmd, **kw):
            sizes.append(int(cmd.split("--nodes=")[1].split()[0]))
            idx = len(sizes) - 1
            body = "\n".join(f"{i}: host.hostname\tn{idx * 64 + i}" for i in range(64))
            return types.SimpleNamespace(stdout=body, stderr="", returncode=0)

        monkeypatch.setitem(sys.modules, "host_exec",
                            types.SimpleNamespace(role_root=lambda _r: "/s",
                                                  run_slurm_on_host=_run))
        node_inventory.collect_via_srun([f"n{i}" for i in range(128)], chunk=64)
        assert sizes == [64, 64]

    def test_interleaved_task_output_is_reassembled_per_node(self):
        # srun emits lines as tasks produce them, so two nodes' fields arrive shuffled.
        raw = "\n".join([
            "0: host.hostname\tn1",
            "1: host.hostname\tn2",
            "0: host.serial\tAAA",
            "1: host.serial\tBBB",
            "0: gpu.count\t4",
        ])
        blobs = node_inventory.parse_srun_labeled(raw)
        recs = [node_inventory.parse_record(b) for b in blobs]
        assert [r["host"]["hostname"] for r in recs] == ["n1", "n2"]
        assert recs[0]["host"]["serial"] == "AAA"
        assert recs[1]["host"]["serial"] == "BBB"

    def test_unlabelled_noise_is_ignored_rather_than_parsed_as_a_node(self):
        raw = "srun: job 12345 queued\nsrun: Nodes n1 are ready\n0: host.hostname\tn1"
        assert node_inventory.parse_srun_labeled(raw) == ["host.hostname\tn1"]


class TestCacheAndCarryForward:
    def test_a_node_missing_this_run_keeps_its_previous_measurement(self, record, tmp_path):
        cache_file = tmp_path / "inv.json"
        node_inventory.save_cache([record], captured_at="2026-08-10T00:00:00Z", path=cache_file)

        # A later sweep reaches a different node; Slurm would not admit us to the first.
        other = node_inventory.parse_record(REAL_GB300.replace("d01-18", "d01-99"))
        merged, carried = node_inventory.merge_with_cache(
            [other], node_inventory.load_cache(cache_file)
        )
        assert sorted((r["host"]["hostname"]) for r in merged) == [
            "ink50-dh01-d01-18", "ink50-dh01-d01-99",
        ]
        assert carried == {"ink50-dh01-d01-18": "2026-08-10T00:00:00Z"}

    def test_a_freshly_measured_node_is_never_overwritten_by_the_cache(self, record, tmp_path):
        cache_file = tmp_path / "inv.json"
        stale = node_inventory.parse_record(REAL_GB300.replace("1.0.3", "0.9.0-old"))
        node_inventory.save_cache([stale], captured_at="2026-08-01T00:00:00Z", path=cache_file)
        merged, carried = node_inventory.merge_with_cache(
            [record], node_inventory.load_cache(cache_file)
        )
        assert len(merged) == 1
        assert merged[0]["host"]["bios_version"] == "1.0.3"
        assert carried == {}

    def test_the_cache_accumulates_rather_than_replacing(self, record, tmp_path):
        cache_file = tmp_path / "inv.json"
        node_inventory.save_cache([record], captured_at="2026-08-10T00:00:00Z", path=cache_file)
        other = node_inventory.parse_record(REAL_GB300.replace("d01-18", "d01-99"))
        merged = node_inventory.save_cache(
            [other], captured_at="2026-08-11T00:00:00Z", path=cache_file
        )
        # Overwriting would discard every node that happened to be busy this sweep.
        assert set(merged) == {"ink50-dh01-d01-18", "ink50-dh01-d01-99"}

    def test_carried_nodes_are_marked_in_the_report_and_summarised(self, record):
        inv, deviations = node_inventory.build_inventory(
            [record], carried={"ink50-dh01-d01-18": "2026-08-10T00:00:00Z"}
        )
        overrides = inv["nodes"][0]["firmware_overrides"]
        assert overrides[node_inventory.CARRY_FORWARD_KEY] == "2026-08-10T00:00:00Z"
        assert any("carry data from an earlier one" in d for d in deviations)

    def test_a_fresh_node_carries_no_marker(self, record):
        inv, _ = node_inventory.build_inventory([record])
        assert node_inventory.CARRY_FORWARD_KEY not in inv["nodes"][0]["firmware_overrides"]

    def test_a_marked_report_still_satisfies_the_schema(self, record):
        jsonschema = pytest.importorskip("jsonschema")
        import json as _json

        import node_report

        schema = _json.loads(node_report.SCHEMA_PATH.read_text(encoding="utf-8"))
        inv, _ = node_inventory.build_inventory(
            [record], carried={"ink50-dh01-d01-18": "2026-08-10T00:00:00Z"}
        )
        inv["captured_at"] = "2026-08-11T00:00:00Z"
        sub = {"$schema": schema["$schema"], "$defs": schema["$defs"],
               **schema["properties"]["firmware_inventory"]}
        jsonschema.validate(instance=inv, schema=sub)

    def test_a_missing_or_corrupt_cache_is_simply_empty(self, tmp_path):
        assert node_inventory.load_cache(tmp_path / "absent.json") == {}
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert node_inventory.load_cache(bad) == {}


class TestFailureReasons:
    def _sweep(self, monkeypatch, result):
        import sys
        import types

        monkeypatch.setitem(sys.modules, "transport",
                            types.SimpleNamespace(fanout=lambda *a, **k: [result]))
        monkeypatch.setitem(sys.modules, "host_exec",
                            types.SimpleNamespace(role_root=lambda _r: "/s",
                                                  connection=lambda _p: object()))
        return node_inventory.collect_fleet(["n1"])

    def test_a_reported_refusal_is_passed_through_verbatim(self, monkeypatch):
        class _R:
            node = "n1"
            stdout = ""
            stderr = "Login not allowed: no running jobs and no WLM allocations"
            returncode = 255

        records, errors = self._sweep(monkeypatch, _R())
        assert records == []
        assert "Login not allowed" in errors[0]

    def test_a_silent_hop_still_names_the_likely_cause(self, monkeypatch):
        """The hop returns rc=255 with both streams empty for an unallocated node.

        A bare "no inventory output" is what made a policy gate look like flaky SSH and sent
        me tuning sshd MaxSessions instead, so the message has to carry the usual explanation.
        """
        class _R:
            node = "n1"
            stdout = ""
            stderr = ""
            returncode = 255

        _, errors = self._sweep(monkeypatch, _R())
        assert "rc=255" in errors[0]
        assert "pam_slurm_adopt" in errors[0]


class TestAgainstTheRealSchema:
    """The point of all of this: the block must satisfy the contract we ship against."""

    def test_the_generated_inventory_validates_against_the_vendored_nscale_schema(self, record):
        jsonschema = pytest.importorskip("jsonschema")
        import json

        import node_report

        schema = json.loads(node_report.SCHEMA_PATH.read_text(encoding="utf-8"))
        inv, _ = node_inventory.build_inventory([record], bcm_by_host={
            "ink50-dh01-d01-18": {"rack_id": "WH1D1", "bmc_ip": "10.40.128.9",
                                  "bmc_mac": "2C:9D:90:E6:8E:8E", "bmc_firmware": "1.10.50"},
        })
        inv["captured_at"] = "2026-08-11T00:00:00Z"

        sub = {"$schema": schema["$schema"], "$defs": schema["$defs"],
               **schema["properties"]["firmware_inventory"]}
        jsonschema.validate(instance=inv, schema=sub)
