# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A cluster's compute fabric: the per-site record, its projection, and the on-node scan.

NscaleTX-NC runs its GB300 Spectrum-X rails as SR-IOV VFs (``roce_vf_r1..4``, 200G) behind OVS
uplink PFs (``roce_r1..4``, no routable GID), addressed over IPv6 only, while its 400G NICs
(``mlx5_12`` on the default route, ``mlx5_13`` unaddressed) are the management plane. The old scan
globbed ``mlx5_*`` and kept the top rate, so it chose exactly the two wrong NICs and never saw the
rails. These tests pin the replacement rule against fake sysfs trees shaped like that node, an IB
cluster (whose GIDs are link-local by design and must not be filtered), and the edges in between.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from catalog import context, models

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
FABRIC = SCRIPTS / "lib" / "fabric.inc.sh"

V6 = "2a03:83e4:410e:1202:0000:0000:0000:0001"
V4 = "0000:0000:0000:0000:0000:ffff:0a0a:20a6"
LINK_LOCAL = "fe80:0000:0000:0000:0a20:e7ff:fe79:2088"


def _cluster(**fabric):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "fabric": fabric})


# --- the record --------------------------------------------------------------------------------


class TestTheRecord:
    def test_defaults_are_blank_and_a_cluster_without_one_still_loads(self):
        f = models.Cluster.from_dict({"id": "c1", "name": "C1"}).fabric
        assert (f.scaleout, f.addr_family, f.compute_hca, f.rail_rate_gbps) == ("", "", "", 0)

    def test_values_are_normalised_and_round_trip(self):
        c = _cluster(scaleout=" RoCE ", addr_family="IPv6", compute_hca=" roce_vf_r ")
        assert c.to_dict()["fabric"] == {
            "scaleout": "roce", "addr_family": "ipv6", "compute_hca": "roce_vf_r", "rail_rate_gbps": 0,
            "net_plugin": "",
        }

    @pytest.mark.parametrize(
        "good", ["roce_vf_r", "roce_vf_r1,roce_vf_r2", "=roce_vf_r1:1,roce_vf_r2:1", "^mlx5_12,mlx5_13", "mlx5_0"]
    )
    def test_nccl_ib_hca_syntax_validates(self, good):
        assert not _cluster(compute_hca=good).validate()

    @pytest.mark.parametrize(
        "bad",
        [
            "roce vf",          # a space splits it on the remote shell
            "roce_vf_r;id",     # command separator
            "$(id)",            # substitution
            "roce_vf_r,",       # dangling separator
            "roce_vf_r:x",      # port must be numeric
        ],
    )
    def test_values_a_shell_cannot_carry_safely_are_refused(self, bad):
        assert any("fabric.compute_hca" in p for p in _cluster(compute_hca=bad).validate())

    def test_unknown_scaleout_and_family_are_refused(self):
        problems = _cluster(scaleout="ethernet", addr_family="v6").validate()
        assert any("fabric.scaleout" in p for p in problems)
        assert any("fabric.addr_family" in p for p in problems)


class TestTheRailRate:
    def test_it_round_trips_and_a_garbage_value_reads_as_not_stated(self):
        assert _cluster(rail_rate_gbps=200).fabric.rail_rate_gbps == 200
        assert _cluster(rail_rate_gbps="fast").fabric.rail_rate_gbps == 0
        assert _cluster(rail_rate_gbps=True).fabric.rail_rate_gbps == 0  # a bool is not a rate

    @pytest.mark.parametrize("bad", [-1, 3201, 200000])
    def test_a_rate_outside_the_sane_range_is_refused(self, bad):
        assert any("fabric.rail_rate_gbps" in p for p in _cluster(rail_rate_gbps=bad).validate())

    @pytest.mark.parametrize("ok", [0, 100, 200, 400, 800, 3200])
    def test_sane_rates_validate(self, ok):
        assert not any("rail_rate_gbps" in p for p in _cluster(rail_rate_gbps=ok).validate())


# --- the projection ----------------------------------------------------------------------------


class TestTheProjection:
    def test_stated_values_reach_the_launcher_env(self):
        env = context.high_overlay(None, _cluster(scaleout="roce", addr_family="ipv6", compute_hca="roce_vf_r"))
        assert env["NVIS_FABRIC_NETWORK"] == "RoCE"
        assert env["NVIS_FABRIC_ADDR_FAMILY"] == "ipv6"
        assert env["NVIS_FABRIC_HCA"] == "roce_vf_r"

    def test_blank_means_detect_so_nothing_is_projected(self):
        env = context.high_overlay(None, _cluster())
        assert not {"NVIS_FABRIC_ADDR_FAMILY", "NVIS_FABRIC_HCA"} & env.keys()
        assert "NVIS_FABRIC_NETWORK" not in env

    def test_the_cluster_overrides_the_platform_default(self):
        platform = models.Platform.from_dict({"id": "p", "name": "P", "fabric": {"scaleout": "ib"}})
        assert context.fabric_defaults(platform)["NVIS_FABRIC_NETWORK"] == "IB"  # low overlay
        high = context.high_overlay(None, _cluster(scaleout="roce"), platform)
        # Overlay order is low < process env < settings < high, so the cluster's value wins.
        assert high["NVIS_FABRIC_NETWORK"] == "RoCE"

    def test_both_protocols_project_by_their_names(self):
        for scaleout, network in (("ib", "IB"), ("roce", "RoCE")):
            assert context.high_overlay(None, _cluster(scaleout=scaleout))["NVIS_FABRIC_NETWORK"] == network
            platform = models.Platform.from_dict({"id": "p", "name": "P", "fabric": {"scaleout": scaleout}})
            assert context.fabric_defaults(platform)["NVIS_FABRIC_NETWORK"] == network

    def test_a_platform_without_a_protocol_projects_none(self):
        platform = models.Platform.from_dict({"id": "p", "name": "P", "fabric": {"scaleout": ""}})
        assert "NVIS_FABRIC_NETWORK" not in context.fabric_defaults(platform)

    def test_the_cluster_states_its_own_rail_rate(self):
        env = context.high_overlay(None, _cluster(scaleout="roce", rail_rate_gbps=200))
        assert env["NVIS_FABRIC_EXPECT_RATE"] == "200"
        assert "NVIS_FABRIC_EXPECT_RATE" not in context.high_overlay(None, _cluster())

    def _gb300(self, scaleout="ib"):
        return models.Platform.from_dict({
            "id": "gb300", "name": "GB300", "geometry": {"gpus_per_node": 4},
            "fabric": {"scaleout": scaleout, "rails_per_gpu": 1, "rail_rate_gbps": 800},
        })

    def test_a_platforms_rate_does_not_follow_it_onto_a_cluster_on_the_other_protocol(self):
        # gb300-nvl72 is an 800G IB shape; NscaleTX-NC is a GB300 on Spectrum-X RoCE with 200G VFs.
        # Projecting the platform's 800 flagged every healthy NC rail and drained the node.
        env = context.fabric_defaults(self._gb300("ib"), _cluster(scaleout="roce"))
        assert "NVIS_FABRIC_EXPECT_RATE" not in env
        assert "NVIS_FABRIC_EXPECT_RAILS" not in env

    @pytest.mark.parametrize("stated", ["", "ib"])
    def test_a_platforms_rate_still_applies_where_the_cluster_runs_its_protocol(self, stated):
        env = context.fabric_defaults(self._gb300("ib"), _cluster(scaleout=stated))
        assert env["NVIS_FABRIC_EXPECT_RATE"] == "800"
        assert env["NVIS_FABRIC_EXPECT_RAILS"] == "4"

    def test_without_a_cluster_the_platform_applies_as_before(self):
        assert context.fabric_defaults(self._gb300("ib"))["NVIS_FABRIC_EXPECT_RATE"] == "800"

    def test_low_overlay_hands_the_cluster_to_the_platform_rule(self):
        low = context.low_overlay(self._gb300("ib"), _cluster(scaleout="roce"))
        assert "NVIS_FABRIC_EXPECT_RATE" not in low

    def test_the_names_forward_over_ssh(self):
        from transport.factory import FORWARD_ENV_PREFIXES

        for key in ("NVIS_FABRIC_NETWORK", "NVIS_FABRIC_ADDR_FAMILY", "NVIS_FABRIC_HCA", "NVIS_FABRIC_EXPECT_RATE"):
            assert key.startswith(FORWARD_ENV_PREFIXES), key


# --- the scan ----------------------------------------------------------------------------------


def _dev(root: Path, name: str, *, state: str, link: str, rate: int, netdev: str = "", gids=(), bdf: str = ""):
    """One RDMA device. With *bdf*, laid out as sysfs does: a link to <pci>/<bdf>/infiniband/<name>."""
    base = root / name
    if bdf:
        root.mkdir(parents=True, exist_ok=True)
        base = root.parent / "pci" / bdf / "infiniband" / name
        base.mkdir(parents=True)
        (root / name).symlink_to(base)
    port = base / "ports" / "1"
    (port / "gids").mkdir(parents=True)
    (port / "state").write_text(f"{'4: ACTIVE' if state == 'ACTIVE' else '1: DOWN'}\n")
    (port / "link_layer").write_text(f"{link}\n")
    (port / "rate").write_text(f"{rate} Gb/sec (4X NDR)\n")
    for i, gid in enumerate(gids):
        (port / "gids" / str(i)).write_text(f"{gid}\n")
    if netdev:
        (base / "device" / "net" / netdev).mkdir(parents=True)


def _nc_node(root: Path) -> None:
    """The NscaleTX-NC GB300 layout, as probed on sys1-gso1-p-phy-gpu3-su19."""
    _dev(root, "mlx5_12", state="ACTIVE", link="Ethernet", rate=400, netdev="enP22s22f0np0", gids=[LINK_LOCAL, V4])
    _dev(root, "mlx5_13", state="ACTIVE", link="Ethernet", rate=400, netdev="enP22s22f1np1", gids=[LINK_LOCAL])
    for r in range(1, 5):
        _dev(root, f"roce_r{r}", state="DOWN", link="Ethernet", rate=200, netdev=f"eth_p1_r{r}", gids=[LINK_LOCAL])
        _dev(root, f"roce_vf_r{r}", state="ACTIVE", link="Ethernet", rate=200, netdev=f"eth_r{r}_vf0",
             gids=[LINK_LOCAL, V6.replace("1202", f"{r}202"), V6.replace("1202", f"{r}202")])


def _run(root: Path, body: str, **env: str | None) -> dict[str, str]:
    full = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NVIS_FABRIC_SYSFS": str(root),
        "NVIS_FABRIC_DEFAULT_ROUTE_IFS": "enP22s22f0np0",
        "NVIS_FABRIC_NVSWITCH_BDFS": "",
        "NVIS_FABRIC_NS_IFNAME": "enP22s22f0np0",
        "GPUS_PER_NODE": "4",
        **env,
    }
    full = {k: v for k, v in full.items() if v is not None}  # None = leave the variable unset
    script = f'source "{FABRIC}"\n{body}\n'
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=full, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _scan(root: Path, **env: str | None) -> dict[str, str]:
    return _run(root, 'eval "$(nvis_fabric_scan)"; set | grep "^NVIS_FAB_"', **env)


def _exported(root: Path, **env: str | None) -> dict[str, str]:
    return _run(
        root,
        "nvis_fabric_export 2>/dev/null; "
        'echo "NCCL_IB_HCA=${NCCL_IB_HCA:-}"; echo "NCCL_IB_ADDR_FAMILY=${NCCL_IB_ADDR_FAMILY:-}"; '
        'echo "NCCL_SOCKET_IFNAME=${NCCL_SOCKET_IFNAME:-}"',
        **env,
    )


class TestTheNcNode:
    def test_the_rails_are_the_routable_vfs_not_the_400g_management_nics(self, tmp_path):
        _nc_node(tmp_path)
        s = _scan(tmp_path)
        assert s["NVIS_FAB_LINK"] == "roce"
        assert s["NVIS_FAB_IB_HCA"].replace("\\", "") == "roce_vf_r1,roce_vf_r2,roce_vf_r3,roce_vf_r4"
        assert s["NVIS_FAB_RAILS"] == "4"
        assert s["NVIS_FAB_RATE"] == "200"

    def test_the_address_family_is_read_off_the_gids(self, tmp_path):
        _nc_node(tmp_path)
        assert _scan(tmp_path)["NVIS_FAB_ADDR_FAMILY"] == "ipv6"

    def test_nccl_is_pinned_by_a_prefix_every_node_shares(self, tmp_path):
        _nc_node(tmp_path)
        e = _exported(tmp_path)
        assert e["NCCL_IB_HCA"] == "roce_vf_r"
        assert e["NCCL_IB_ADDR_FAMILY"] == "AF_INET6"
        assert e["NCCL_SOCKET_IFNAME"] == "enP22s22f0np0"

    def test_the_cluster_record_overrides_detection(self, tmp_path):
        _nc_node(tmp_path)
        e = _exported(tmp_path, NVIS_FABRIC_HCA="roce_vf_r1,roce_vf_r2", NVIS_FABRIC_ADDR_FAMILY="ipv4")
        assert e["NCCL_IB_HCA"] == "roce_vf_r1,roce_vf_r2"
        assert e["NCCL_IB_ADDR_FAMILY"] == "AF_INET"

    def test_an_operators_own_nccl_ib_hca_is_never_overwritten(self, tmp_path):
        _nc_node(tmp_path)
        assert _exported(tmp_path, NCCL_IB_HCA="=roce_vf_r2")["NCCL_IB_HCA"] == "=roce_vf_r2"


class TestInfiniBandIsUnchanged:
    def _ib_node(self, root: Path) -> None:
        # IB GIDs sit on the default fe80::/64 subnet prefix; they must not be read as unroutable.
        for i in range(4):
            _dev(root, f"mlx5_{i}", state="ACTIVE", link="InfiniBand", rate=400,
                 gids=[f"fe80:0000:0000:0000:a088:c203:00{i}5:fa3e"])
        _dev(root, "mlx5_4", state="ACTIVE", link="Ethernet", rate=200, netdev="enp1s0", gids=[V4])

    def test_ib_rails_are_found_by_rate_and_left_to_per_rank_auto_detect(self, tmp_path):
        self._ib_node(tmp_path)
        s = _scan(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "ib"
        assert s["NVIS_FAB_IB_HCA"].replace("\\", "") == "mlx5_0,mlx5_1,mlx5_2,mlx5_3"
        assert s["NVIS_FAB_HCA_PIN"] in ("", "''")
        assert s["NVIS_FAB_ADDR_FAMILY"] in ("", "''")
        e = _exported(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert e["NCCL_IB_HCA"] == ""  # job 65648: IB keeps per-rank auto-detect
        assert e["NCCL_IB_ADDR_FAMILY"] == ""


def _scan_warned(root: Path, **env: str | None) -> dict[str, str]:
    """The scan, plus whatever it warned on stderr, as WARNING."""
    return _run(
        root,
        'w="$(nvis_fabric_scan 2>&1 >/dev/null)"; eval "$(nvis_fabric_scan 2>/dev/null)"; '
        'set | grep "^NVIS_FAB_"; echo "WARNING=${w//$\'\\n\'/ }"',
        **env,
    )


class TestTheStatedTechnology:
    """A stated scale-out is an expectation the node confirms, not a substitute for the node.

    NC inherits gb300-nvl72's ``scaleout = ib``, so every launch ran with fabric network IB on
    nodes with no InfiniBand port: no rails, no pin, no address family -- NCCL opened all 26 devices
    and hit "GID index -1" (multirack job 138174). The same code must keep IB sites exactly as they were.
    """

    def _ib_only(self, root: Path) -> None:
        for i in range(4):
            _dev(root, f"mlx5_{i}", state="ACTIVE", link="InfiniBand", rate=400,
                 gids=[f"fe80:0000:0000:0000:a088:c203:00{i}5:fa3e"])

    def test_a_roce_node_stated_as_ib_is_wired_as_the_roce_node_it_is(self, tmp_path):
        _nc_node(tmp_path)
        s = _scan_warned(tmp_path, NVIS_FABRIC_NETWORK="IB")
        assert s["NVIS_FAB_LINK"] == "roce"
        assert s["NVIS_FAB_IB_HCA"].replace("\\", "") == "roce_vf_r1,roce_vf_r2,roce_vf_r3,roce_vf_r4"
        assert s["NVIS_FAB_ADDR_FAMILY"] == "ipv6"
        assert "fabric network ib" in s["WARNING"] and "fabric.scaleout" in s["WARNING"]
        e = _exported(tmp_path, NVIS_FABRIC_NETWORK="IB")
        assert e["NCCL_IB_HCA"] == "roce_vf_r"
        assert e["NCCL_IB_ADDR_FAMILY"] == "AF_INET6"

    def test_an_ib_node_stated_as_ib_is_unchanged_and_silent(self, tmp_path):
        self._ib_only(tmp_path)
        s = _scan_warned(tmp_path, NVIS_FABRIC_NETWORK="IB", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "ib"
        assert s["NVIS_FAB_IB_HCA"].replace("\\", "") == "mlx5_0,mlx5_1,mlx5_2,mlx5_3"
        assert s["WARNING"] == ""
        e = _exported(tmp_path, NVIS_FABRIC_NETWORK="IB", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert e["NCCL_IB_HCA"] == "" and e["NCCL_IB_ADDR_FAMILY"] == ""

    def test_an_ib_node_stated_as_roce_is_wired_as_ib(self, tmp_path):
        self._ib_only(tmp_path)
        s = _scan_warned(tmp_path, NVIS_FABRIC_NETWORK="RoCE", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "ib"
        assert s["NVIS_FAB_RAILS"] == "4"
        assert "fabric network roce" in s["WARNING"]

    def test_a_stated_technology_the_node_has_is_honoured_over_auto(self, tmp_path):
        # IB rails and a routable RoCE NIC side by side: auto would take IB; a stated roce stands.
        self._ib_only(tmp_path)
        _dev(tmp_path, "mlx5_4", state="ACTIVE", link="Ethernet", rate=200, netdev="enp1s0", gids=[V4])
        assert _scan_warned(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")["NVIS_FAB_LINK"] == "ib"
        s = _scan_warned(tmp_path, NVIS_FABRIC_NETWORK="RoCE", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "roce" and s["WARNING"] == ""

    def test_a_node_with_neither_keeps_the_statement_and_finds_nothing(self, tmp_path):
        _dev(tmp_path, "mlx5_0", state="DOWN", link="InfiniBand", rate=400)
        s = _scan_warned(tmp_path, NVIS_FABRIC_NETWORK="IB")
        assert s["NVIS_FAB_LINK"] == "ib" and s["NVIS_FAB_RAILS"] == "0" and s["WARNING"] == ""


class TestTheFabricNetworkVariable:
    """NVIS_FABRIC_NETWORK=IB|RoCE is how every script learns the cluster's protocol."""

    def _both(self, root: Path) -> None:
        # IB rails and a routable RoCE NIC side by side, so the declared protocol decides.
        for i in range(4):
            _dev(root, f"mlx5_{i}", state="ACTIVE", link="InfiniBand", rate=400,
                 gids=[f"fe80:0000:0000:0000:a088:c203:00{i}5:fa3e"])
        _dev(root, "mlx5_4", state="ACTIVE", link="Ethernet", rate=200, netdev="enp1s0", gids=[V4])

    @pytest.mark.parametrize(("value", "link"), [
        ("IB", "ib"), ("InfiniBand", "ib"), ("ib", "ib"), ("mlx", "ib"),
        ("RoCE", "roce"), ("roce", "roce"), ("spx", "roce"), ("spectrumx", "roce"),
    ])
    def test_every_spelling_of_the_two_protocols_is_read(self, tmp_path, value, link):
        self._both(tmp_path)
        assert _scan(tmp_path, NVIS_FABRIC_NETWORK=value, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")["NVIS_FAB_LINK"] == link

    def test_it_wins_over_the_deprecated_name(self, tmp_path):
        self._both(tmp_path)
        s = _scan(tmp_path, NVIS_FABRIC_NETWORK="RoCE", NVIS_RDMA_LINK_TECH="ib", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "roce"

    def test_the_deprecated_name_still_works_alone(self, tmp_path):
        self._both(tmp_path)
        assert _scan(tmp_path, NVIS_RDMA_LINK_TECH="roce", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")["NVIS_FAB_LINK"] == "roce"

    def test_a_remote_scan_carries_it_to_the_node(self, tmp_path):
        # The node sees only what nvis_fabric_scan_node ships: a fake "ssh" that drops the caller's
        # copy of the variable proves the protocol arrives in the shipped script.
        self._both(tmp_path / "sys")
        fake_ssh = tmp_path / "fake-ssh"
        fake_ssh.write_text("#!/bin/bash\nexec env -u NVIS_FABRIC_NETWORK bash -s\n")
        fake_ssh.chmod(0o755)
        s = _run(tmp_path / "sys", f'nvis_fabric_scan_node node1 "{fake_ssh}"',
                 NVIS_FABRIC_NETWORK="RoCE", NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_LINK"] == "roce"

    def test_the_rdma_helpers_resolve_it_the_same_way(self, tmp_path):
        out = _run(tmp_path, f'source "{SCRIPTS / "nvis-rdma-common.inc.sh"}"; '
                             'echo "T=$(nvis_rdma_normalize_link_tech_raw "$(nvis_fabric_network)")"',
                   NVIS_FABRIC_NETWORK="RoCE", NVIS_RDMA_LINK_TECH="ib")
        assert out["T"] == "roce"


class TestARenamedSetting:
    def test_a_saved_legacy_key_keeps_its_value_under_the_new_name(self, tmp_path):
        import json

        import config_env

        f = tmp_path / "job_env_overrides.json"
        f.write_text(json.dumps({"NVIS_RDMA_LINK_TECH": "roce"}))
        assert config_env._read_job_env_file_raw(f)["NVIS_FABRIC_NETWORK"] == "roce"
        f.write_text(json.dumps({"NVIS_RDMA_LINK_TECH": "roce", "NVIS_FABRIC_NETWORK": "IB"}))
        assert config_env._read_job_env_file_raw(f)["NVIS_FABRIC_NETWORK"] == "IB"


class TestCrossRackMpiAndNvls:
    """What a job spanning racks needs beyond NCCL's rails: MPI's own transport, and NVLS.

    Nscale NC NeMo multirack job 165605 (2026-10-11) died in MPI_Allgather: the batch script pinned
    MPI's UCX to the RoCE rails with RC, UCX picked a link-local GID (dgid=fe80::...) that cannot leave
    a node's routed subnet, and every endpoint timed out. Its racks also all report the unset fabric
    CliqueId 32766, which hangs NVLS in ncclCommInitRank across racks.
    """

    def _transport(self, root: Path, **env: str | None) -> dict[str, str]:
        return _run(root, 'eval "$(nvis_fabric_scan)"; nvis_fabric_mpi_xrack_transport 2>/dev/null; '
                          'echo "UCX_TLS=${UCX_TLS:-}"; echo "UCX_NET_DEVICES=${UCX_NET_DEVICES:-}"', **env)

    def test_over_roce_mpi_takes_tcp_on_the_routable_bootstrap_netdev(self, tmp_path):
        _nc_node(tmp_path)
        t = self._transport(tmp_path, NVIS_FABRIC_NETWORK="RoCE")
        assert t == {"UCX_TLS": "tcp", "UCX_NET_DEVICES": "enP22s22f0np0"}

    def test_over_infiniband_mpi_keeps_rc_on_the_rails(self, tmp_path):
        for i in range(4):
            _dev(tmp_path, f"mlx5_{i}", state="ACTIVE", link="InfiniBand", rate=400,
                 gids=[f"fe80:0000:0000:0000:a088:c203:00{i}5:fa3e"])
        t = self._transport(tmp_path, NVIS_FABRIC_NETWORK="IB", NVIS_FABRIC_DEFAULT_ROUTE_IFS="",
                            UCX_NET_DEVICES="mlx5_0:1,mlx5_1:1")
        assert t == {"UCX_TLS": "rc", "UCX_NET_DEVICES": "mlx5_0:1,mlx5_1:1"}

    def _nvls(self, tmp_path: Path, clique: str | None) -> tuple[str, str]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        if clique is not None:
            smi = bin_dir / "nvidia-smi"
            smi.write_text("#!/bin/bash\n"
                           "echo '    Fabric'\n"
                           f"echo '        CliqueId                          : {clique}'\n"
                           "echo '        ClusterUUID                       : 65792be7-e2bc-42f5-a98d-f62b94f367c0'\n")
            smi.chmod(0o755)
        script = f'source "{FABRIC}"\nnvis_fabric_xrack_nvls\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=False,
                           env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip(), r.stderr

    def test_the_unset_clique_turns_nvls_off_across_racks_and_says_why(self, tmp_path):
        out, err = self._nvls(tmp_path, "32766")
        assert out == "0"
        assert "CliqueId is unset (32766)" in err

    def test_a_real_clique_keeps_nvls(self, tmp_path):
        assert self._nvls(tmp_path, "4097")[0] == "1"

    def test_without_nvidia_smi_nvls_stays_on(self, tmp_path):
        assert self._nvls(tmp_path, None)[0] == "1"

    def test_the_nemo_batch_script_decides_nvls_before_every_use_and_lets_the_launch_win(self):
        lines = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text().splitlines()
        decided = next(n for n, line in enumerate(lines) if line.startswith("_nvls_default=1"))
        uses = [n for n, line in enumerate(lines) if line.startswith("export NCCL_NVLS_ENABLE=")]
        assert uses and all(n > decided for n in uses)
        assert all(lines[n] == "export NCCL_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-${_nvls_default}}" for n in uses), (
            "every NVLS export must default to the decided value, the launch's own setting first")
        assert any("nvis_fabric_mpi_xrack_transport" in line for line in lines)


class TestTheEdges:
    def test_no_pin_when_nothing_else_is_active(self, tmp_path):
        for r in (1, 2):
            _dev(tmp_path, f"roce_vf_r{r}", state="ACTIVE", link="Ethernet", rate=200, netdev=f"eth{r}", gids=[V4])
        s = _scan(tmp_path)
        assert s["NVIS_FAB_HCA_PIN"] in ("", "''")
        assert s["NVIS_FAB_ADDR_FAMILY"] == "ipv4"

    def test_a_prefix_that_would_also_match_a_management_nic_falls_back_to_the_exact_list(self, tmp_path):
        _dev(tmp_path, "mlx5_1", state="ACTIVE", link="Ethernet", rate=400, netdev="mgmt0", gids=[V4])
        _dev(tmp_path, "mlx5_2", state="ACTIVE", link="Ethernet", rate=200, netdev="r2", gids=[V6])
        _dev(tmp_path, "mlx5_3", state="ACTIVE", link="Ethernet", rate=200, netdev="r3", gids=[V6])
        e = _exported(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="mgmt0")
        assert e["NCCL_IB_HCA"] == "mlx5_2,mlx5_3"

    def test_the_nvswitch_attached_hca_is_never_a_rail(self, tmp_path):
        root = tmp_path / "ib"
        _dev(root, "mlx5_8", state="ACTIVE", link="Ethernet", rate=400, netdev="nvsw0", gids=[V6], bdf="0008:01:00.0")
        _dev(root, "roce_vf_r1", state="ACTIVE", link="Ethernet", rate=200, netdev="eth1", gids=[V6], bdf="0000:03:00.4")
        s = _scan(root, NVIS_FABRIC_DEFAULT_ROUTE_IFS="", NVIS_FABRIC_NVSWITCH_BDFS="0008:01:00")
        assert s["NVIS_FAB_IB_HCA"] == "roce_vf_r1"

    def test_bonds_are_never_rails(self, tmp_path):
        _dev(tmp_path, "mlx5_bond_0", state="ACTIVE", link="Ethernet", rate=400, netdev="bond0", gids=[V6])
        _dev(tmp_path, "roce_vf_r1", state="ACTIVE", link="Ethernet", rate=200, netdev="eth1", gids=[V6])
        s = _scan(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert s["NVIS_FAB_IB_HCA"] == "roce_vf_r1"
        assert s["NVIS_FAB_HCA_PIN"] == "roce_vf_r1"  # the active bond would otherwise be opened

    def test_no_routable_rail_still_wires_the_bootstrap_plane(self, tmp_path):
        for r in (1, 2):
            _dev(tmp_path, f"roce_r{r}", state="ACTIVE", link="Ethernet", rate=200, netdev=f"p{r}", gids=[LINK_LOCAL])
        e = _exported(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert e["NCCL_IB_HCA"] == ""
        assert e["NCCL_SOCKET_IFNAME"] == "enP22s22f0np0"


class TestInsideAContainer:
    """The NeMo launchers scan inside pyxis, where `ip` is usually absent; /proc/net still answers."""

    def _procnet(self, tmp_path: Path) -> Path:
        net = tmp_path / "procnet"
        net.mkdir()
        (net / "route").write_text(
            "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
            "enP22s22f0np0\t00000000\t01180A0A\t0003\t0\t0\t0\t00000000\n"
            "enP22s22f0np0\t00180A0A\t00000000\t0001\t0\t0\t0\t00FEFFFF\n"
        )
        (net / "ipv6_route").write_text(
            "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
            "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo\n"
        )
        return net

    def test_the_management_nic_is_still_excluded_without_ip(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        s = _scan(root, NVIS_FABRIC_DEFAULT_ROUTE_IFS=None, NVIS_FABRIC_PROCNET=str(self._procnet(tmp_path)))
        assert "mlx5_12" not in s["NVIS_FAB_IB_HCA"]
        assert s["NVIS_FAB_HCA_PIN"] == "roce_vf_r"


class TestThePinnedExport:
    def _pinned(self, root: Path, **env: str | None) -> tuple[int, dict[str, str]]:
        out = _run(
            root,
            "nvis_fabric_export_pinned 2>/dev/null; rc=$?; echo \"RC=$rc\"; "
            'echo "NCCL_IB_HCA=${NCCL_IB_HCA:-}"; echo "UCX_NET_DEVICES=${UCX_NET_DEVICES:-}"; '
            'echo "NCCL_IB_ADDR_FAMILY=${NCCL_IB_ADDR_FAMILY:-}"',
            **env,
        )
        return int(out["RC"]), out

    def test_it_pins_this_nodes_exact_rails(self, tmp_path):
        _nc_node(tmp_path)
        rc, e = self._pinned(tmp_path)
        assert rc == 0
        assert e["NCCL_IB_HCA"] == "=roce_vf_r1,roce_vf_r2,roce_vf_r3,roce_vf_r4"
        assert e["UCX_NET_DEVICES"] == "roce_vf_r1:1,roce_vf_r2:1,roce_vf_r3:1,roce_vf_r4:1"
        assert e["NCCL_IB_ADDR_FAMILY"] == "AF_INET6"

    def test_a_callers_own_values_and_the_cluster_record_win(self, tmp_path):
        _nc_node(tmp_path)
        assert self._pinned(tmp_path, NCCL_IB_HCA="mine")[1]["NCCL_IB_HCA"] == "mine"
        assert self._pinned(tmp_path, NVIS_FABRIC_HCA="roce_vf_r1")[1]["NCCL_IB_HCA"] == "roce_vf_r1"

    def test_no_rail_pins_nothing_and_says_so(self, tmp_path):
        _dev(tmp_path, "roce_r1", state="ACTIVE", link="Ethernet", rate=200, netdev="p1", gids=[LINK_LOCAL])
        rc, e = self._pinned(tmp_path, NVIS_FABRIC_DEFAULT_ROUTE_IFS="")
        assert rc == 1
        assert e["NCCL_IB_HCA"] == "" and e["UCX_NET_DEVICES"] == ""
