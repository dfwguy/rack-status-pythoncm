# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A node's scheduler state in words every view can read, and who said it.

The floor view shows a rack two ways: as the cluster manager sees it (powered, installing,
unreachable) and as the workload manager sees it (idle, running, drained). On Nscale NC the two
disagree for most of the fleet -- powered and healthy to BCM, drained to Slurm -- which is exactly
what an operator needs to see. Slurm's own vocabulary (compact ``%t`` codes, suffix flags, combined
states) is mapped once, in the Slurm backend, onto ``schedulers.base.NODE_STATES``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import demo_data
import native_status as ns
import pytest
import rack_status_engine as engine
from platforms.bcm import BCMProvider
from platforms.maas import MAASProvider
from schedulers import NODE_STATES
from schedulers.slurm import slurm_node_status


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        # Compact sinfo %t codes, as the inventory reads them.
        ("idle", "idle"),
        ("alloc", "running"),
        ("mix", "running"),
        ("comp", "running"),
        ("plnd", "pending"),
        ("resv", "reserved"),
        ("maint", "reserved"),
        ("drng", "draining"),
        ("drain", "drained"),
        ("down", "down"),
        ("fail", "down"),
        ("inval", "down"),
        ("unk", "unknown"),
        ("boot", "unknown"),
        ("pow_dn", "unknown"),
        ("", "unknown"),
        # Long spellings (scontrol / %T) and any case.
        ("IDLE", "idle"),
        ("ALLOCATED", "running"),
        ("DRAINED", "drained"),
        ("PLANNED", "pending"),
        # Suffix flags say how Slurm reached the state, not what it is.
        ("idle*", "idle"),
        ("mix-", "running"),
        ("drain~", "drained"),
        ("down*", "down"),
    ],
)
def test_slurm_states_map_onto_the_shared_vocabulary(raw, status):
    assert slurm_node_status(raw) == status
    assert status in NODE_STATES


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        # A drain flag on a node still holding work is a node draining, not one running.
        ("ALLOCATED+DRAIN", "draining"),
        ("mix+drain", "draining"),
        ("IDLE+DRAIN", "drained"),
        # Down outranks everything: the scheduler cannot use it whatever else is true.
        ("DOWN+DRAIN", "down"),
        # An idle node backfill has planned for a pending job is that job's, not free.
        ("IDLE+PLANNED", "pending"),
        # A node already holding work stays running, whatever is pending behind it.
        ("MIXED+PLANNED", "running"),
        # A completing job is still work on the node.
        ("idle+comp", "running"),
        # A part Slurm reports but this view has no word for does not hide the part it has.
        ("idle+cloud", "idle"),
    ],
)
def test_a_combined_state_takes_its_most_telling_part(raw, status):
    assert slurm_node_status(raw) == status


def test_every_provider_names_itself():
    # The floor view labels its orchestrator mode with this; an unnamed provider would show nothing.
    assert BCMProvider.kind == "bcm"
    assert MAASProvider.kind == "maas"


def test_node_sources_names_the_cluster_manager_and_the_scheduler(monkeypatch):
    monkeypatch.setattr(ns, "_get_platform", lambda: SimpleNamespace(kind="bcm"))
    assert ns.node_sources("slurm") == {"orchestrator": "bcm", "scheduler": "slurm"}
    assert ns.node_sources("") == {"orchestrator": "bcm", "scheduler": ""}


def test_every_node_record_carries_both_views():
    nodes = ns.build_node_inventory({"gpu1": "drain", "gpu2": "alloc", "gpu3": "idle*"}, [])
    assert {n["id"]: (n["scheduler_state"], n["scheduler_status"]) for n in nodes} == {
        "gpu1": ("drain", "drained"),
        "gpu2": ("alloc", "running"),
        "gpu3": ("idle*", "idle"),
    }


def test_the_shell_table_path_names_its_sources_too(monkeypatch):
    # The legacy rack_status.sh table builds its node inventory on a separate path; the floor view
    # reads the same payload from either, so both must say who answered.
    monkeypatch.delenv("RACK_STATUS_DEMO", raising=False)  # conftest turns demo mode on for the suite
    monkeypatch.setenv("RACK_STATUS_USE_SHELL_TABLE", "1")
    monkeypatch.setattr(engine, "_rack_status_script_path", lambda: Path("/opt/rack_status.sh"))
    monkeypatch.setattr(engine, "_script_exists", lambda _p: True)
    monkeypatch.setattr(engine, "_ensure_cmsh_rack_list_env_for_subprocess", lambda _env: None)
    monkeypatch.setattr(engine, "_maybe_build_tmp_status_from_log_cache", lambda _d: None)
    monkeypatch.setattr(engine.host_exec, "run_on_host", lambda *_a, **_k: subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(engine, "read_comments", dict)
    monkeypatch.setattr(engine, "_enrich_node_addresses", lambda _out: None)
    monkeypatch.setattr(engine, "_eget", lambda key, default="": "slurm" if key == "RACK_SCHEDULER" else default)
    monkeypatch.setattr(ns, "collect_sinfo_nodes", lambda: {"gpu1": "drng"})
    monkeypatch.setattr(ns, "_get_platform", lambda: SimpleNamespace(kind="bcm"))

    out = engine.run_rack_status(Path("/logs"))

    assert out["node_sources"] == {"orchestrator": "bcm", "scheduler": "slurm"}
    assert [(n["id"], n["scheduler_status"]) for n in out["nodes"]] == [("gpu1", "draining")]


def test_demo_status_shows_the_same_racks_both_ways():
    # Demo mode is what a laptop preview of the floor view runs on; without nodes it has no
    # scheduler view to show at all.
    out = demo_data.generate_demo_status(Path("/logs"))
    assert out["node_sources"] == {"orchestrator": "bcm", "scheduler": "slurm"}
    by_rack: dict[str, list[dict]] = {}
    for node in out["nodes"]:
        by_rack.setdefault(node["rack"], []).append(node)
    assert set(by_rack) == {row["rack"] for row in out["rows"]}
    for row in out["rows"]:
        nodes = by_rack[row["rack"]]
        # A tray the row calls down is down in both views; every status is the shared vocabulary.
        down = {n["tray"] for n in nodes if n["status"] == "down"}
        assert all(n["scheduler_status"] == "down" for n in nodes if n["tray"] in down)
        assert all(n["scheduler_status"] in NODE_STATES for n in nodes)
        assert ("DN:" in row["ct_down_unk"]) == bool(down)
