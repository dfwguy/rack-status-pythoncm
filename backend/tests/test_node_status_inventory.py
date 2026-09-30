# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Actual compute inventory survives absent racks, naming mismatches and query failures."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import native_status as ns
import nvis_id_templates as ids
import pytest
import rack_status_engine as engine


@pytest.fixture(autouse=True)
def isolated_inventory(monkeypatch):
    naming = ids.get_nvis_naming(
        ids.DEFAULT_NVIS_RACK_ID, ids.DEFAULT_NVIS_CT_ID, ids.DEFAULT_NVIS_NVSW_ID
    )
    monkeypatch.setattr(ns, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(ns, "_host_resolve", lambda path: path)
    monkeypatch.setattr(ns, "_RACK_ALIAS_CACHE", None)
    monkeypatch.delenv("RACK_ID_ALIAS_RULES", raising=False)
    monkeypatch.delenv("SINFO_OPTS", raising=False)
    monkeypatch.setattr(ns, "_env_int", lambda _key, default: default)
    monkeypatch.setattr(ns.config_env, "_eget", lambda _key, default="": default)
    monkeypatch.setattr(engine, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(engine, "_env_int", lambda _key, default: default)
    monkeypatch.delenv("RACK_PHYSICAL_RF0_LOOSE_HOSTNAMES", raising=False)
    monkeypatch.delenv("RACK_PHYSICAL_RF0_MAP_DISABLED", raising=False)
    monkeypatch.setattr(ns, "_get_platform", lambda: SimpleNamespace(
        sinfo_cmd=lambda opts: ("sinfo", opts),
        scontrol_hostnames_cmd=lambda spec: ("scontrol-hostnames", spec),
    ))
    for name in ("run_slurm_on_host", "run_on_host", "which", "resolve"):
        monkeypatch.setattr(ns.host_exec, name, Mock(side_effect=AssertionError("unexpected host call")))


def slurm_response(stdout="", returncode=0):
    return subprocess.CompletedProcess(["mock-slurm"], returncode, stdout, "")


def test_rackless_nodes_are_retained_without_invented_geometry(monkeypatch):
    run = Mock(return_value=slurm_response("GPU01 idle\nGPU02 down*\n"))
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", run)
    monkeypatch.setenv("RACK_CLUSTER_GPU_PER_TRAY", "8")
    monkeypatch.setenv("RACK_COMPUTE_NODES_PER_RACK", "72")

    state = ns.NativeRackTableState("rackless", Path("/logs"))
    state._worker_sinfo()
    payload = state.node_inventory_snapshot()

    assert payload["nodes"] == [
        {"id": "GPU01", "rack": None, "status": "up", "scheduler_state": "idle", "ip": ""},
        {"id": "GPU02", "rack": None, "status": "down", "scheduler_state": "down", "ip": ""},
    ]
    assert payload["node_inventory_error"] == ""
    assert state.sinfo_counts == {}
    assert state.racks == []
    run.assert_called_once()
    assert ns.build_node_inventory({}, ["p5-r01"]) == []


def test_duplicate_partition_rows_preserve_spelling_and_all_states(monkeypatch):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response(
        "GPU01 idle\ngpu01 idle\nGpu01 down*\nGPU02 alloc+comp\nGPU02 alloc\n"
    )))

    nodes = ns.collect_sinfo_nodes()

    assert nodes == {"GPU01": "down+idle", "GPU02": "alloc+comp"}
    assert [node["status"] for node in ns.build_node_inventory(nodes, [])] == ["down", "up"]


# A hostname the CT template CAN parse into a rack, so the two modes diverge observably.
_MAP_NODES = {"p1-r5-ct3": "idle"}
_MAP_RACKS = ["p1-r5"]


class TestRackMappingMode:
    """Direct parses the rack from the hostname; translate refuses to, taking placement only
    from the provider record. The divergence is the whole point of the flag: on Nscale NC the
    hostname carries no rack, so parsing it would invent a wrong one from a mis-set CT template.
    """

    def test_direct_parses_the_rack_out_of_the_hostname(self, monkeypatch):
        monkeypatch.setattr(ns, "_rack_mapping_mode", lambda: "direct")
        inv = ns.build_node_inventory(_MAP_NODES, _MAP_RACKS, records=[])
        assert inv[0]["rack"] == "p1-r5"

    def test_translate_ignores_the_hostname_when_provider_is_silent(self, monkeypatch):
        """No provider placement -> ungrouped, rather than a hostname-guessed rack."""
        monkeypatch.setattr(ns, "_rack_mapping_mode", lambda: "translate")
        inv = ns.build_node_inventory(_MAP_NODES, _MAP_RACKS, records=[])
        assert inv[0]["rack"] is None

    def test_translate_places_from_the_provider_rack_attribute(self, monkeypatch):
        """The NC path: rack/slot come from the cluster manager's device record, not the name."""
        monkeypatch.setattr(ns, "_rack_mapping_mode", lambda: "translate")
        records = [{"id": "p1-r5-ct3", "rack": "p1-r5", "ip": ""}]
        inv = ns.build_node_inventory(_MAP_NODES, _MAP_RACKS, records=records)
        assert inv[0]["rack"] == "p1-r5"


class TestProviderRackCounts:
    """A BCM/pythoncm cluster has no sinfo, so the rack tiles' node totals must come from the
    placed roster. Without this every provider rack showed 0/0 even with 18 nodes enumerated.
    """

    def test_counts_are_grouped_by_rack_and_state(self):
        inv = (
            [{"id": f"n{i}", "rack": "U19", "status": "inst"} for i in range(16)]
            + [{"id": "nd", "rack": "U19", "status": "down"}]
            + [{"id": "nu", "rack": "U19", "status": "up"}]
        )
        counts = ns.counts_from_node_inventory(inv)
        assert counts == {("U19", "inst"): 16, ("U19", "down"): 1, ("U19", "up"): 1}
        assert ns.rack_total_sinfo_nodes("U19", counts) == 18  # the total the tile was missing

    def test_unplaced_nodes_do_not_count(self):
        inv = [{"id": "app", "rack": None, "status": "up"}, {"id": "n", "rack": "R2", "status": "down"}]
        assert ns.counts_from_node_inventory(inv) == {("R2", "down"): 1}


def test_expanded_hostnames_are_actual_roster_and_expanded_only_once(monkeypatch):
    run = Mock(side_effect=[
        slurm_response("GPU[01-02] idle\nGPU01 idle\n"),
        slurm_response("GPU01\nGPU02\n"),
    ])
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", run)

    assert ns.collect_sinfo_nodes() == {"GPU01": "idle", "GPU02": "idle"}
    assert run.call_count == 2
    assert run.call_args_list[1].args[0] == ("scontrol-hostnames", "GPU[01-02]")


@pytest.mark.parametrize("response", [
    slurm_response("GPU01 idle\n", 1),
    OSError("controller unreachable"),
    subprocess.TimeoutExpired("sinfo", 1),
])
def test_failed_slurm_query_is_not_confirmed_empty(monkeypatch, response):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(side_effect=[response]))
    with pytest.raises(RuntimeError, match="inventory"):
        ns.collect_sinfo_nodes()


@pytest.mark.parametrize("expansion", [
    slurm_response("GPU01\n", 1),
    slurm_response(""),
    slurm_response("GPU01\nnot a hostname\n"),
    slurm_response("GPU[01-02]\n"),
    OSError("controller unreachable"),
    subprocess.TimeoutExpired("scontrol", 1),
])
def test_failed_or_malformed_expansion_never_publishes_partial_inventory(monkeypatch, expansion):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(side_effect=[
        slurm_response("GPU00 idle\nGPU[01-02] idle\n"), expansion,
    ]))
    with pytest.raises(RuntimeError):
        ns.collect_sinfo_nodes()


def test_successful_empty_slurm_response_is_confirmed_empty(monkeypatch):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response()))
    assert ns.collect_sinfo_nodes() == {}


@pytest.mark.parametrize("text", ["GPU01\n", "GPU01 *\n", "GPU00 idle\nGPU01\n"])
def test_malformed_nonempty_slurm_response_is_not_confirmed_empty_or_partial(monkeypatch, text):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response(text)))
    with pytest.raises(RuntimeError):
        ns.collect_sinfo_nodes()


def test_rack_membership_requires_known_rack_and_exact_prefix_boundary():
    nodes = {host: "idle" for host in (
        "p5-r01-ct01", "p5-r02-ct01", "rack1-node", "rack10-node", "rack100-node",
        "rack1-deep-node", "rack1other", "GPU01",
    )}
    rows = ns.build_node_inventory(nodes, ["p5-r01", "rack1", "rack10", "rack1-deep"])
    assert {row["id"]: row["rack"] for row in rows} == {
        "p5-r01-ct01": "p5-r01", "p5-r02-ct01": None,
        "rack1-node": "rack1", "rack10-node": "rack10", "rack100-node": None,
        "rack1-deep-node": "rack1-deep", "rack1other": None, "GPU01": None,
    }


def test_alias_mapping_applies_to_nodes_and_legacy_counts(monkeypatch):
    monkeypatch.setenv("RACK_ID_ALIAS_RULES", r"^p5-r01$=>physical-rack-a")
    nodes = {"p5-r01-ct01": "idle", "p5-r01-ct02": "alloc", "GPU01": "idle"}

    rows = ns.build_node_inventory(nodes, ["physical-rack-a"])

    assert {row["id"]: row["rack"] for row in rows} == {
        "p5-r01-ct01": "physical-rack-a", "p5-r01-ct02": "physical-rack-a", "GPU01": None,
    }
    assert ns.sinfo_counts_for_nodes(nodes) == {
        ("physical-rack-a", "idle"): 1, ("physical-rack-a", "alloc"): 1,
    }
    assert all(row["rack"] is None for row in ns.build_node_inventory(nodes, []))


def test_device_status_enriches_only_exact_discovered_hosts():
    ds_text = (
        "[ UP ] gpu01\nGPU02 [ UNREACHABLE ]\n[ INSTALLING ] GPU03\n"
        "GPU010 [ DOWN ]\n[ DOWN ] management01\nGPU04-extra [ DOWN ]\n"
    )
    nodes = {"GPU01": "down", "GPU02": "idle", "GPU03": "idle", "GPU04": "idle"}

    rows = ns.build_node_inventory(nodes, [], ds_text)

    assert {row["id"]: row["status"] for row in rows} == {
        "GPU01": "up", "GPU02": "down", "GPU03": "inst", "GPU04": "up",
    }
    assert all(row["rack"] is None for row in rows)


def test_worker_preserves_last_good_on_failure_then_clears_on_confirmed_empty(monkeypatch):
    run = Mock(side_effect=[
        slurm_response("p5-r01-ct01 idle\nGPU01 down\n"),
        slurm_response("", 1),
        slurm_response(),
    ])
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", run)
    state = ns.NativeRackTableState("last-good", Path("/logs"))
    assert state.node_inventory_snapshot()["nodes"] is None

    state._worker_sinfo()
    previous = state.node_inventory_snapshot()["nodes"]
    previous_counts = dict(state.sinfo_counts)
    state._worker_sinfo()
    failed = state.node_inventory_snapshot()
    assert failed["nodes"] == previous
    assert state.sinfo_counts == previous_counts
    assert "last successful" in failed["node_inventory_error"]
    assert "exit 1" in failed["node_inventory_error"]

    state._worker_sinfo()
    assert state.node_inventory_snapshot() == {"nodes": [], "node_inventory_error": ""}
    assert state.sinfo_counts == {}


def test_first_failed_worker_keeps_inventory_unknown(monkeypatch):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response("", 1)))
    state = ns.NativeRackTableState("unavailable", Path("/logs"))
    state._worker_sinfo()
    payload = state.node_inventory_snapshot()
    assert payload["nodes"] is None
    assert "unavailable" in payload["node_inventory_error"]
    assert state.sinfo_done is True


@pytest.mark.parametrize("changed_key", [
    "NVIS_PROJECT_ID", "NVIS_CLUSTER_ID", "RACK_SSH_HOST", "RACK_EXEC_MODE", "RACK_SCHEDULER",
])
def test_same_log_directory_cannot_reuse_other_cluster_state(monkeypatch, changed_key):
    config = {"NVIS_PROJECT_ID": "project-a", "NVIS_CLUSTER_ID": "cluster-a",
              "RACK_SSH_HOST": "head-a", "RACK_EXEC_MODE": "ssh"}
    monkeypatch.setattr(ns.config_env, "_eget", lambda key, default="": config.get(key, default))
    monkeypatch.setattr(ns, "_states", {})
    first_key = ns.native_state_key(Path("/logs"))
    first = ns._get_state(first_key, Path("/logs"))
    first.node_states = {"GPU01": "idle"}
    config[changed_key] = "other"
    second_key = ns.native_state_key(Path("/logs"))
    second = ns._get_state(second_key, Path("/logs"))
    assert second_key != first_key
    assert second.node_inventory_snapshot()["nodes"] is None
    assert first.node_inventory_snapshot()["nodes"][0]["id"] == "GPU01"


def test_naming_change_and_log_root_get_distinct_native_state_keys(monkeypatch):
    monkeypatch.setattr(ns.config_env, "_eget", lambda _key, default="": default)
    monkeypatch.setattr(ns, "_nvis_rack_id_template", lambda: "rack-a")
    monkeypatch.setattr(ns, "_nvis_ct_id_template", lambda: "rack-a-node")
    original = ns.native_state_key(Path("/logs"))
    assert original != ns.native_state_key(Path("/other-logs"))
    monkeypatch.setattr(ns, "_nvis_ct_id_template", lambda: "rack-a-compute")
    assert original != ns.native_state_key(Path("/logs"))


def use_nonslurm_provider(monkeypatch, provider):
    monkeypatch.setattr(ns.config_env, "_eget", lambda key, default="": (
        "pythoncm" if key == "RACK_SCHEDULER" else default
    ))
    monkeypatch.setattr(ns, "_get_platform", lambda: provider)


def test_nonslurm_worker_uses_provider_inventory_and_keeps_unassigned_node_ip(monkeypatch):
    records = [
        {"id": "RTX6000-01", "rack": None, "ip": "10.0.0.1", "scheduler_state": ""},
        {"id": "RTX6000-02", "rack": "p5-r01", "ip": "10.0.0.2", "scheduler_state": ""},
        {"id": "RTX6000-03", "rack": "unlisted-rack", "ip": "", "scheduler_state": ""},
    ]
    provider = SimpleNamespace(compute_node_inventory=Mock(return_value=records))
    use_nonslurm_provider(monkeypatch, provider)
    state = ns.NativeRackTableState("provider", Path("/logs"))
    state.racks = ["p5-r01"]
    state.ds_text = "[ UP ] RTX6000-01\nRTX6000-02 [ DOWN ]\n"

    state._worker_sinfo()

    assert state.node_inventory_snapshot() == {
        "nodes": [
            {"id": "RTX6000-01", "rack": None, "ip": "10.0.0.1", "scheduler_state": "", "status": "up"},
            {"id": "RTX6000-02", "rack": "p5-r01", "ip": "10.0.0.2", "scheduler_state": "", "status": "down"},
            {"id": "RTX6000-03", "rack": None, "ip": "", "scheduler_state": "", "status": "unknown"},
        ],
        "node_inventory_error": "",
    }
    provider.compute_node_inventory.assert_called_once_with()
    ns.host_exec.run_slurm_on_host.assert_not_called()


def test_provider_failure_distinguishes_unavailable_stale_and_confirmed_empty(monkeypatch):
    records = [{"id": "GPU01", "rack": None, "ip": "10.0.0.1", "scheduler_state": ""}]
    provider = SimpleNamespace(compute_node_inventory=Mock(side_effect=[
        RuntimeError("BCM unavailable"), records, RuntimeError("BCM unavailable"), [],
    ]))
    use_nonslurm_provider(monkeypatch, provider)
    state = ns.NativeRackTableState("provider-errors", Path("/logs"))

    state._worker_sinfo()
    assert state.node_inventory_snapshot()["nodes"] is None
    assert "unavailable" in state.node_inventory_snapshot()["node_inventory_error"]
    state._worker_sinfo()
    last_good = state.node_inventory_snapshot()["nodes"]
    state._worker_sinfo()
    assert state.node_inventory_snapshot()["nodes"] == last_good
    assert "last successful" in state.node_inventory_snapshot()["node_inventory_error"]
    state._worker_sinfo()
    assert state.node_inventory_snapshot() == {"nodes": [], "node_inventory_error": ""}
    ns.host_exec.run_slurm_on_host.assert_not_called()


def test_rf0_parser_accepts_known_arbitrary_hostname_without_naming_match():
    text = "RTX6000-01\nrf0\n10.0.0.1/24\n"
    assert engine._parse_physical_rf0_stdout(text) == {}
    assert engine._parse_physical_rf0_stdout(text, ["rtx6000-01"]) == {"rtx6000-01": "10.0.0.1"}


def test_rf0_host_missing_address_cannot_steal_following_known_host_address():
    text = "GPU01\nrf0\nGPU02\nrf0\n10.0.0.2\n"
    assert engine._parse_physical_rf0_stdout(text, ["GPU01", "GPU02"]) == {"gpu02": "10.0.0.2"}


def test_rf0_host_missing_address_cannot_steal_management_server_address():
    assert engine._parse_physical_rf0_stdout(
        "GPU01\nhead01\n10.0.0.99\n", ["GPU01"]
    ) == {}


@pytest.mark.parametrize("noise", ["Welcome to the cluster", "Welcome operator", "GPU01 new_state"])
def test_slurm_noise_cannot_become_a_compute_node(monkeypatch, noise):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response(
        noise + "\nGPU02 idle\n"
    )))
    with pytest.raises(RuntimeError):
        ns.collect_sinfo_nodes()


@pytest.mark.parametrize("state", ["alloc+", "idle~", "idle!", "down%", "mix@", "idle^", "idle-", "block"])
def test_slurm_compact_state_flags_are_accepted(monkeypatch, state):
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response(
        f"GPU01 {state}\n"
    )))
    assert list(ns.collect_sinfo_nodes()) == ["GPU01"]


@pytest.mark.parametrize("state", [
    "?", "ALLOC", "ALLOC+", "BLOCK", "BOOT", "BOOT*", "BOOT^", "CANC_R", "CLOUD",
    "COMP", "DOWN", "DRAIN", "DRNG", "EXT", "FAIL", "FAILG", "FUTR", "IDLE", "INVAL",
    "MAINT", "MIX", "PLND", "POW_DN", "POW_UP", "POWERED_DN", "POWERING_UP",
    "POWRING_DN", "RESM", "RESV", "UNK",
])
def test_all_slurm_compact_states_preserve_the_fleet_inventory(monkeypatch, state):
    # Values returned by SchedMD's node_state_string_compact, including states
    # omitted by the sinfo manual's summary. One rebooting or invalid node must
    # not turn an otherwise successful whole-fleet inventory into unavailable.
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", Mock(return_value=slurm_response(
        f"GPU00 idle\nGPU01 {state}\n"
    )))
    assert set(ns.collect_sinfo_nodes()) == {"GPU00", "GPU01"}


def test_invalid_registration_is_not_shown_as_healthy():
    records = ns.build_node_inventory({"GPU01": "inval", "GPU02": "inval+idle"}, [])
    assert all(record["status"] == "down" for record in records)


def test_rf0_cache_is_scoped_to_cluster_and_known_roster(monkeypatch):
    config = {"NVIS_CLUSTER_ID": "cluster-a"}
    monkeypatch.setattr(ns.config_env, "_eget", lambda key, default="": config.get(key, default))
    monkeypatch.setattr(engine, "_physical_rf0_loaded", False)
    monkeypatch.setattr(engine, "_physical_rf0_map", {})
    monkeypatch.setattr(engine, "_physical_rf0_context", None)
    monkeypatch.setattr(engine, "_get_platform", lambda: SimpleNamespace(
        physical_rf0_cmd=lambda: ["mock-cmsh", "physical-rf0"],
    ))
    monkeypatch.setattr(engine.host_exec, "which", Mock(return_value="/mock/cmsh"))
    run = Mock(side_effect=[
        slurm_response("GPU01\n10.0.0.1\n"),
        slurm_response("GPU01\n10.0.0.1\nGPU02\n10.0.0.2\n"),
        slurm_response("GPU01\n10.1.0.1\nGPU02\n10.1.0.2\n"),
    ])
    monkeypatch.setattr(engine.host_exec, "run_on_host", run)

    first = engine.get_physical_rf0_map(["GPU01"])
    assert first == {"gpu01": "10.0.0.1"}
    first["gpu01"] = "mutated-response"
    assert engine.get_physical_rf0_map(["gpu01"]) == {"gpu01": "10.0.0.1"}
    assert run.call_count == 1
    assert engine.get_physical_rf0_map(["GPU01", "GPU02"]) == {
        "gpu01": "10.0.0.1", "gpu02": "10.0.0.2",
    }
    assert run.call_count == 2
    config["NVIS_CLUSTER_ID"] = "cluster-b"
    assert engine.get_physical_rf0_map(["GPU01", "GPU02"]) == {
        "gpu01": "10.1.0.1", "gpu02": "10.1.0.2",
    }
    assert run.call_count == 3
