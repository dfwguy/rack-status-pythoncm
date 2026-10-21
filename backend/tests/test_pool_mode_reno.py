# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""End-to-end pool-mode rendering against Akamai/Reno-shaped data.

Reno is the archetype `rack_mapping_mode="pool"` cluster: `cmsh rack; list` returns nothing and
every device's rack attribute is empty, so there is NO placement -- but the node shape (8x RTX 6000)
is fully known. These tests reproduce the two ways a cluster reports that roster + shape:

* its real config, Slurm present: roster and GRES ride one ``sinfo -N`` call; and
* a BCM-only (pythoncm) cluster: roster from the ``cmsh`` device list via the provider, shape from an
  ``nvidia-smi`` sample.

Both must produce the same pool payload the frontend consumes: zero racks, a flat node roster with
``gpu_count`` and ``rack: None`` on every node, and ``rack_mapping_mode == "pool"``.
"""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import bcm_query
import native_status as ns
import nvis_id_templates as ids
import pytest
from platforms.bcm import BCMProvider


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    naming = ids.get_nvis_naming(
        ids.DEFAULT_NVIS_RACK_ID, ids.DEFAULT_NVIS_CT_ID, ids.DEFAULT_NVIS_NVSW_ID
    )
    monkeypatch.setattr(ns, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(ns, "_host_resolve", lambda path: path)
    monkeypatch.setattr(ns, "_env_int", lambda _key, default: default)
    monkeypatch.delenv("SINFO_OPTS", raising=False)
    # A Reno cluster is configured pool; the hostname carries no rack.
    monkeypatch.setattr(ns.config_env, "rack_mapping_mode", lambda: "pool")
    for name in ("run_slurm_on_host", "run_on_host", "which", "resolve"):
        monkeypatch.setattr(ns.host_exec, name, Mock(side_effect=AssertionError(f"unexpected {name}")))


# A faithful slice of Reno's `sinfo -N -h -o '%N %t %G'`: sparse numbering (248,249,...,254 missing
# below, gaps), a couple DOWN nodes, and the login node with no GPU GRES. 8x nvidia per GPU node.
_RENO_SINFO = (
    "gpu-rno-248 idle gpu:nvidia:8(S:0-1)\n"
    "gpu-rno-249 idle gpu:nvidia:8(S:0-1)\n"
    "gpu-rno-254 down gpu:nvidia:8(S:0-1)\n"   # DOWN, but GRES still reports the shape
    "gpu-rno-256 idle gpu:nvidia:8(S:0-1)\n"   # note the gap: 255, 257 absent
    "gpu-rno-301 alloc gpu:nvidia:8(S:0-1)\n"
    "slogin-rno-01 idle (null)\n"              # login node: no GPU shape
)


def test_reno_slurm_pool_payload(monkeypatch):
    """Reno's real path: empty rack list + one sinfo call carrying state and GRES."""
    monkeypatch.setattr(ns, "_cmsh_rack_list", list)  # cmsh rack; list returns nothing
    monkeypatch.setattr(ns.config_env, "_eget", lambda _key, default="": "slurm")
    run = Mock(return_value=subprocess.CompletedProcess(["sinfo"], 0, _RENO_SINFO, ""))
    monkeypatch.setattr(ns.host_exec, "run_slurm_on_host", run)
    placement = Mock(side_effect=AssertionError("Pool must not query BCM placement"))
    monkeypatch.setattr(bcm_query, "nodes", placement)

    state = ns.NativeRackTableState("reno-slurm", Path("/logs"))
    state.refresh_racks_blocking()
    state._worker_sinfo()
    payload = state.node_inventory_snapshot()

    assert state.racks == []  # no placement: nothing for the rack table/floor to draw
    assert payload["rack_mapping_mode"] == "pool"
    run.assert_called_once()  # state + shape from ONE sinfo call
    placement.assert_not_called()

    nodes = {n["id"]: n for n in payload["nodes"]}
    # Every GPU node is ungrouped (rack None) and carries its 8-GPU shape, even the DOWN one.
    assert nodes["gpu-rno-248"] == {
        "id": "gpu-rno-248", "rack": None, "status": "up", "scheduler_state": "idle",
        "scheduler_status": "idle", "ip": "", "gpu_count": 8, "gpu_type": "nvidia",
    }
    assert nodes["gpu-rno-254"]["status"] == "down"
    assert nodes["gpu-rno-254"]["gpu_count"] == 8
    assert all(n["rack"] is None for n in payload["nodes"])
    # The login node has no GPU GRES -> no shape, generic tile.
    assert "gpu_count" not in nodes["slogin-rno-01"]
    # Fleet GPU total the pool view sums: 5 gpu nodes x 8.
    assert sum(n.get("gpu_count", 0) for n in payload["nodes"]) == 40


# The literal "mock a BCM response": the `cmsh` device list the provider reshapes into records.
# Mirrors Reno's `device list` -- PhysicalNode rows, empty rack position, a DOWN node.
_RENO_BCM_DEVICES = [
    {"hostname": "gpu-rno-248", "rack_id": "", "bmc_ip": "10.0.2.248"},
    {"hostname": "gpu-rno-249", "rack_id": "", "bmc_ip": "10.0.2.249"},
    {"hostname": "gpu-rno-254", "rack_id": "", "bmc_ip": "10.0.2.254"},
    {"hostname": "slogin-rno-01", "rack_id": "", "bmc_ip": "10.0.0.11"},
]

# `nvidia-smi -L` from one sampled node, pdsh-prefixed with the host (8x RTX 6000).
_RENO_SMI = "".join(
    f"gpu-rno-248: GPU {i}: NVIDIA RTX 6000 Ada Generation (UUID: GPU-{i:02d})\n" for i in range(8)
)


def test_reno_bcm_only_pool_payload(monkeypatch):
    """BCM-only (pythoncm) path: roster from the cmsh device list, shape from an nvidia-smi sample."""
    monkeypatch.setattr(ns, "_cmsh_rack_list", list)
    monkeypatch.setattr(ns.config_env, "_eget", lambda _key, default="": "pythoncm")
    monkeypatch.setattr(ns, "_get_platform", BCMProvider)
    # The provider reshapes this exactly as it would a live `cmsh` fleet walk.
    monkeypatch.setattr(bcm_query, "nodes", Mock(return_value={"ok": True, "data": _RENO_BCM_DEVICES}))
    smi = Mock(return_value=subprocess.CompletedProcess(["pdsh"], 0, _RENO_SMI, ""))
    monkeypatch.setattr(ns.host_exec, "run_on_host", smi)

    state = ns.NativeRackTableState("reno-bcm", Path("/logs"))
    state.refresh_racks_blocking()
    state._worker_sinfo()
    payload = state.node_inventory_snapshot()

    assert state.racks == []
    assert payload["rack_mapping_mode"] == "pool"
    # Homogeneous fleet -> one nvidia-smi probe covers every node (sampled, not fanned out to all).
    smi.assert_called_once()

    nodes = {n["id"]: n for n in payload["nodes"]}
    assert nodes["gpu-rno-248"]["rack"] is None
    assert nodes["gpu-rno-248"]["gpu_count"] == 8
    assert nodes["gpu-rno-248"]["gpu_type"] == "NVIDIA RTX 6000 Ada Generation"
    # The sampled shape is applied across the whole category, including the DOWN node.
    assert nodes["gpu-rno-254"]["gpu_count"] == 8
    assert all(n["rack"] is None for n in payload["nodes"])


def test_reno_pool_does_not_invent_racks_from_sinfo_states(monkeypatch):
    """Guard: pool must never project nodes onto a rack table, even when sinfo counts exist."""
    monkeypatch.setattr(ns, "_cmsh_rack_list", list)
    monkeypatch.setattr(ns.config_env, "_eget", lambda _key, default="": "slurm")
    monkeypatch.setattr(
        ns.host_exec, "run_slurm_on_host",
        Mock(return_value=subprocess.CompletedProcess(["sinfo"], 0, _RENO_SINFO, "")),
    )

    state = ns.NativeRackTableState("reno-guard", Path("/logs"))
    state.refresh_racks_blocking()
    state._worker_sinfo()
    rows, summary, _footnotes, _legend = state.build_table_rows()

    assert rows == []  # no rack rows: the pool view renders nodes instead
    assert summary["done"] == 0 and summary["pending"] == 0
