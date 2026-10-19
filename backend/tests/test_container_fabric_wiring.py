# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A containerized NCCL job is wired to the fabric the cluster has, not left on TCP.

On Nscale NC (2026-10-12) a 32-node U22+U23 LLMB sweep (Slurm 189003) peaked at 7 GB/s: the ranks
run in the NeMo image, the node's enroot hook exposes the RDMA devices only when
MELLANOX_VISIBLE_DEVICES is set, the LLMB launcher never set it, so NCCL found no device
("NET/IB : Using [RO]") and ran on NET/Socket over the bootstrap netdev. A multi-rack NeMo run the
same morning (188619) named the Spectrum-X plugin by a HOST path the container cannot see. And the
launch, named for rack U22 with hosts from U22 and U23, got the single-rack NCCL list.

``lib/fabric.inc.sh nvis_fabric_container_env`` now turns the fabric the scan found (or the cluster
declares) into the container's wiring -- RDMA devices on IB or RoCE, the cluster's NCCL net plugin
as a path inside the image (``fabric.net_plugin``) -- and run_llmb_nccl.sh forces it, with MPI's
own transport for that fabric, into the job's --export; the NCCL list is chosen by the racks the
hosts are placed in.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from catalog import context, models
from node_targets import nccl_env_scope_for_target

from tests.test_cluster_fabric import _nc_node

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
FABRIC = SCRIPTS / "lib" / "fabric.inc.sh"
LLMB_ENV = SCRIPTS / "lib" / "llmb_env.inc.sh"
LAUNCHERS = [SCRIPTS / "run_llmb_nccl.sh", SCRIPTS / "run_llmb_nemo.sh"]
STATED = SCRIPTS / "lib" / "stated_env.inc.sh"
SPCX = "/opt/hpcx/nccl_spectrum-x_plugin/lib/libnccl-net-spcx.so"


def _bash(body: str, root: Path | None = None, **env: str) -> dict[str, str]:
    """Source the fabric library (and llmb_env), run *body*, return the KEY=VALUE lines it prints."""
    full = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NVIS_FABRIC_DEFAULT_ROUTE_IFS": "enP22s22f0np0",
        "NVIS_FABRIC_NVSWITCH_BDFS": "",
        "NVIS_FABRIC_NS_IFNAME": "enP22s22f0np0",
        "GPUS_PER_NODE": "4",
        **({"NVIS_FABRIC_SYSFS": str(root)} if root else {}),
        **env,
    }
    script = f'source "{FABRIC}"\nsource "{STATED}"\nsource "{LLMB_ENV}"\n{body}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


_SHOW = ('echo "MVD=${MELLANOX_VISIBLE_DEVICES:-}"; echo "PLUGIN=${NCCL_NET_PLUGIN:-}"; '
         'echo "EXPORTS=${NVIS_FABRIC_CONTAINER_EXPORTS:-}"')


# ---------------------------------------------------------------- the library


def test_an_nc_node_gets_its_rdma_devices_in_the_container(tmp_path):
    _nc_node(tmp_path)
    out = _bash(f'eval "$(nvis_fabric_scan)"; nvis_fabric_container_env 2>/dev/null; {_SHOW}', tmp_path)
    assert out["MVD"] == "all"
    assert out["PLUGIN"] == "", "no plugin is chosen for a cluster that names none"
    assert out["EXPORTS"] == "MELLANOX_VISIBLE_DEVICES"


def test_the_clusters_plugin_is_selected_by_its_path_inside_the_image(tmp_path):
    _nc_node(tmp_path)
    out = _bash(f'eval "$(nvis_fabric_scan)"; nvis_fabric_container_env 2>/dev/null; {_SHOW}', tmp_path,
                NVIS_FABRIC_NET_PLUGIN=SPCX)
    assert out["PLUGIN"] == SPCX
    assert out["EXPORTS"] == "MELLANOX_VISIBLE_DEVICES NCCL_NET_PLUGIN"


def test_a_value_the_caller_set_wins(tmp_path):
    _nc_node(tmp_path)
    out = _bash(f'eval "$(nvis_fabric_scan)"; nvis_fabric_container_env 2>/dev/null; {_SHOW}', tmp_path,
                NVIS_FABRIC_NET_PLUGIN=SPCX, NCCL_NET_PLUGIN="none", MELLANOX_VISIBLE_DEVICES="0,1")
    assert (out["PLUGIN"], out["MVD"]) == ("none", "0,1")


@pytest.mark.parametrize(("declared", "devices"), [("IB", "all"), ("RoCE", "all"), ("auto", "")])
def test_without_a_scan_the_declared_protocol_decides(declared, devices):
    out = _bash(f"nvis_fabric_container_env 2>/dev/null; {_SHOW}", NVIS_FABRIC_NETWORK=declared)
    assert out["MVD"] == devices


# ---------------------------------------------------------------- the launchers' wiring


def _launch(root: Path, **env: str) -> dict[str, str]:
    body = ('nvis_stated_env_capture\neval "$(nvis_fabric_scan)"\nnvis_llmb_container_wiring test\n'
            'echo "FORCE=${NVIS_FORCE_EXPORT_VARS:-}"; echo "UCX_TLS=${UCX_TLS:-}"; '
            'echo "UCX_NET_DEVICES=${UCX_NET_DEVICES:-}"; ' + _SHOW)
    return _bash(body, root, **env)


def test_the_launcher_forces_the_wiring_and_moves_mpi_off_the_rdma_devices(tmp_path):
    _nc_node(tmp_path)
    out = _launch(tmp_path, NVIS_FABRIC_NET_PLUGIN=SPCX)
    force = out["FORCE"].split()
    assert {"MELLANOX_VISIBLE_DEVICES", "NCCL_NET_PLUGIN", "UCX_TLS", "UCX_NET_DEVICES"} <= set(force)
    # RoCE in routed per-node subnets: MPI bootstraps over TCP on the routable netdev, not a VF.
    assert (out["UCX_TLS"], out["UCX_NET_DEVICES"]) == ("tcp", "enP22s22f0np0")


def test_ucx_the_launch_stated_is_left_alone(tmp_path):
    _nc_node(tmp_path)
    out = _launch(tmp_path, UCX_TLS="rc", NVIS_EXTRA_ENV_KEYS="UCX_TLS")
    assert out["UCX_TLS"] == "rc"
    assert "MELLANOX_VISIBLE_DEVICES" in out["FORCE"].split()


def test_a_host_ucx_pin_the_launch_did_not_state_is_dropped(tmp_path):
    _nc_node(tmp_path)
    out = _launch(tmp_path, UCX_TLS="^tcp")
    assert out["UCX_TLS"] == "tcp"


def test_keys_already_forced_are_not_listed_twice(tmp_path):
    _nc_node(tmp_path)
    out = _launch(tmp_path, NVIS_FORCE_EXPORT_VARS="MELLANOX_VISIBLE_DEVICES OMPI_MCA_pml")
    assert out["FORCE"].split().count("MELLANOX_VISIBLE_DEVICES") == 1


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.stem)
def test_both_llmb_launchers_wire_the_container_after_capturing_what_was_stated(launcher):
    """The NeMo half ran its ranks with no RDMA device in the container until it shared this wiring."""
    text = launcher.read_text(encoding="utf-8")
    capture = text.index("\nnvis_stated_env_capture\n")
    probe = text.index("nvis_fabric_export_from_node")
    wiring = text.index(f"\nnvis_llmb_container_wiring {launcher.stem}\n")
    assert capture < probe < wiring
    assert re.search(rf"{launcher.stem}: container: MELLANOX_VISIBLE_DEVICES=", text)


# ---------------------------------------------------------------- the cluster setting


class TestTheNetPluginSetting:
    def _fabric(self, value):
        return models.ClusterFabric.from_dict({"net_plugin": value})

    @pytest.mark.parametrize("ok", ["", "none", SPCX, "/opt/hpcx/nccl_rdma_sharp_plugin/lib/libnccl-net.so.0"])
    def test_blank_none_and_image_paths_validate(self, ok):
        assert self._fabric(ok).validate() == []

    @pytest.mark.parametrize("bad", ["libnccl-net.so", "/a b.so", "/x/y.so,/z.so", "/x/y.txt", "/x/$(id).so"])
    def test_a_value_the_job_environment_cannot_carry_is_refused(self, bad):
        assert any("fabric.net_plugin" in p for p in self._fabric(bad).validate())

    def test_it_round_trips_and_projects(self):
        cl = models.Cluster.from_dict({"id": "c1", "name": "C1", "fabric": {"net_plugin": SPCX}})
        assert cl.to_dict()["fabric"]["net_plugin"] == SPCX
        assert context.high_overlay(None, cl, None)["NVIS_FABRIC_NET_PLUGIN"] == SPCX

    def test_blank_projects_nothing(self):
        cl = models.Cluster.from_dict({"id": "c1", "name": "C1"})
        assert "NVIS_FABRIC_NET_PLUGIN" not in context.high_overlay(None, cl, None)


# ---------------------------------------------------------------- which NCCL list a launch gets


U22 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(55, 71)]
U23 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(73, 89)]


def test_hosts_from_two_placed_racks_are_multirack(monkeypatch):
    import native_status

    placed = {**dict.fromkeys(U22, "U22"), **dict.fromkeys(U23, "U23")}
    monkeypatch.setattr(native_status, "placed_racks_of", lambda hosts: {h: placed[h] for h in hosts})
    assert nccl_env_scope_for_target(",".join(U22 + U23)) == "multirack"
    assert nccl_env_scope_for_target(",".join(U22)) == "rack"


def test_unplaced_hosts_keep_the_single_rack_answer(monkeypatch):
    import native_status

    monkeypatch.setattr(native_status, "placed_racks_of", lambda hosts: None)
    assert nccl_env_scope_for_target(",".join(U22 + U23)) == "rack"
