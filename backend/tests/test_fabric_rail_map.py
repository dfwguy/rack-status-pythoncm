# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which GPU drives each compute rail (lib/fabric.inc.sh: nvis_fabric_rail_map), and ClusterKit's use of it.

On GB300 Spectrum-X a rail reaches full rate only from its own GPU, through the NIC's Data Direct
function in that GPU's PCI domain; from any other GPU the traffic crosses Grace's PCIe link and
stops near 460 Gb/s of 800. nvidia-smi topo says SYS for every pair, so the pairing is read from the
VPD chip id (keyword V3) the rail's PF shares with one Data Direct function. These trees reproduce
NscaleTX-NC as probed on sys1-gso1-p-phy-gpu10-su19, where perftest measured r1=GPU1, r2=GPU0,
r3=GPU3, r4=GPU2 -- the answer a NUMA-order guess gets wrong on every rail.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
FABRIC = SCRIPTS / "lib" / "fabric.inc.sh"
PROFILES = SCRIPTS / "clusterkit-hca-profiles.inc.sh"

#: rail -> (PF BDF, chip id, NUMA node, local CPUs); the Data Direct fn + GPU of each chip below.
RAILS = {
    "roce_vf_r1": ("0000:03:00.0", "d6e96d0de4e8f01180000820e7bf3886", 0, "0-71"),
    "roce_vf_r2": ("0002:03:00.0", "5eb28b0de4e8f01180000820e7bf38a6", 0, "0-71"),
    "roce_vf_r3": ("0010:03:00.0", "da2c265de5e8f01180000820e7bf3d06", 1, "72-143"),
    "roce_vf_r4": ("0012:03:00.0", "ee03455de5e8f01180000820e7bf3d26", 1, "72-143"),
}
#: GPU index (PCI bus order) -> (GPU BDF, Data Direct fn BDF, chip id of the NIC it belongs to)
GPUS = [
    ("0008:06:00.0", "0008:03:00.0", RAILS["roce_vf_r2"][1], 0),
    ("0009:06:00.0", "0009:03:00.0", RAILS["roce_vf_r1"][1], 0),
    ("0018:06:00.0", "0018:03:00.0", RAILS["roce_vf_r4"][1], 1),
    ("0019:06:00.0", "0019:03:00.0", RAILS["roce_vf_r3"][1], 1),
]


def _vpd(serial: str, chip: str) -> bytes:
    """A VPD image laid out as the CX8 reports it: ID string, VPD-R with SN/V3, end tag."""
    ident = b"NVIDIA Dual ConnectX-8 SuperNIC"
    out = bytes([0x82, len(ident) & 0xFF, len(ident) >> 8]) + ident
    fields = b""
    for kw, val in ((b"PN", b"900-9X86E-00CX-ST0"), (b"SN", serial.encode()), (b"V3", chip.encode())):
        fields += kw + bytes([len(val)]) + val
    fields += b"RV" + bytes([1, 0])
    out += bytes([0x90, len(fields) & 0xFF, len(fields) >> 8]) + fields
    return out + bytes([0x78])


def _func(pcifs: Path, bdf: str, *, vendor: str, klass: str, numa: int, cpus: str = "", vpd: bytes | None = None):
    d = pcifs / bdf
    d.mkdir(parents=True, exist_ok=True)
    (d / "vendor").write_text(f"{vendor}\n")
    (d / "class").write_text(f"{klass}\n")
    (d / "numa_node").write_text(f"{numa}\n")
    if cpus:
        (d / "local_cpulist").write_text(f"{cpus}\n")
    if vpd is not None:
        (d / "vpd").write_bytes(vpd)
    return d


def _nc_tree(tmp: Path, *, with_vpd: bool = True) -> tuple[Path, Path]:
    """sysfs for one NC node: the rail VFs (with physfn), their PFs, the GPUs and Data Direct fns."""
    pcifs, ibroot = tmp / "pci", tmp / "ib"
    ibroot.mkdir(parents=True)
    for rail, (pf, chip, numa, cpus) in RAILS.items():
        serial = "MT2552J02UZB" if numa == 0 else "MT2552J02UZW"
        _func(pcifs, pf, vendor="0x15b3", klass="0x020000", numa=numa, cpus=cpus,
              vpd=_vpd(serial, chip) if with_vpd else None)
        vf = _func(pcifs, pf[:-1] + "4", vendor="0x15b3", klass="0x020000", numa=numa, cpus=cpus)
        (vf / "physfn").symlink_to(pcifs / pf)
        (ibroot / rail).mkdir()
        (ibroot / rail / "device").symlink_to(vf)
    for gpu, dd, chip, numa in GPUS:
        _func(pcifs, gpu, vendor="0x10de", klass="0x030200", numa=numa)
        serial = "MT2552J02UZB" if numa == 0 else "MT2552J02UZW"
        _func(pcifs, dd, vendor="0x15b3", klass="0x080100", numa=numa, vpd=_vpd(serial, chip) if with_vpd else None)
    return pcifs, ibroot


def _bash(script: str, pcifs: Path, ibroot: Path, **env: str) -> subprocess.CompletedProcess:
    full = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NVIS_FABRIC_PCIFS": str(pcifs),
        "NVIS_FABRIC_SYSFS": str(ibroot),
        "NVIS_FABRIC_VPD_SUDO": "0",  # never reach for sudo in a test
        **env,
    }
    return subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, timeout=60, check=False)


def _map(pcifs, ibroot, *rails, **env) -> tuple[dict[str, tuple[str, str, str, str]], str]:
    r = _bash(f". {FABRIC}; nvis_fabric_rail_map {' '.join(rails)}", pcifs, ibroot, **env)
    assert r.returncode == 0, r.stderr
    rows = {}
    for line in r.stdout.splitlines():
        rail, gpu, numa, core, how = line.split()
        rows[rail] = (gpu, numa, core, how)
    return rows, r.stderr


class TestThePairing:
    def test_vpd_pairs_each_rail_with_the_gpu_nc_measured(self, tmp_path):
        rows, err = _map(*_nc_tree(tmp_path), *RAILS)
        assert {r: v[0] for r, v in rows.items()} == {
            "roce_vf_r1": "1", "roce_vf_r2": "0", "roce_vf_r3": "3", "roce_vf_r4": "2"}
        assert {v[3] for v in rows.values()} == {"vpd"}
        assert err == ""

    def test_each_rail_gets_its_own_numa_node_and_a_distinct_local_core_never_cpu0(self, tmp_path):
        rows, _ = _map(*_nc_tree(tmp_path), *RAILS)
        assert {r: (v[1], v[2]) for r, v in rows.items()} == {
            "roce_vf_r1": ("0", "1"), "roce_vf_r2": ("0", "2"), "roce_vf_r3": ("1", "73"), "roce_vf_r4": ("1", "74")}

    def test_a_stated_pairing_wins(self, tmp_path):
        rows, _ = _map(*_nc_tree(tmp_path), "roce_vf_r1", NVIS_FABRIC_RAIL_GPUS="roce_vf_r1:3,roce_vf_r9:0")
        assert rows["roce_vf_r1"][0] == "3" and rows["roce_vf_r1"][3] == "stated"

    def test_without_vpd_it_guesses_by_numa_and_says_so(self, tmp_path):
        """The guess is the NUMA order -- on NC it is wrong for every rail, which is why it warns."""
        rows, err = _map(*_nc_tree(tmp_path, with_vpd=False), *RAILS)
        assert {r: v[0] for r, v in rows.items()} == {
            "roce_vf_r1": "0", "roce_vf_r2": "1", "roce_vf_r3": "2", "roce_vf_r4": "3"}
        assert {v[3] for v in rows.values()} == {"numa-guess"}
        assert err.count("WARNING: no Data Direct pairing found") == 4

    def test_a_gpu_is_not_handed_to_two_rails_by_the_guess(self, tmp_path):
        rows, _ = _map(*_nc_tree(tmp_path, with_vpd=False), "roce_vf_r1", "roce_vf_r2")
        assert rows["roce_vf_r1"][0] != rows["roce_vf_r2"][0]


class TestTheClustersComputePlane:
    @pytest.mark.parametrize(
        ("spec", "kept"),
        [
            ("", ["roce_vf_r1", "roce_vf_r2", "roce_vf_r3", "roce_vf_r4"]),
            ("roce_vf_r", ["roce_vf_r1", "roce_vf_r2", "roce_vf_r3", "roce_vf_r4"]),
            ("^roce_vf_r4", ["roce_vf_r1", "roce_vf_r2", "roce_vf_r3"]),
            ("=roce_vf_r1:1,roce_vf_r3:1", ["roce_vf_r1", "roce_vf_r3"]),
            ("mlx5_", []),
        ],
    )
    def test_compute_hca_keeps_the_rails_it_names(self, tmp_path, spec, kept):
        r = _bash(f". {FABRIC}; _nvis_fabric_rails_matching '{spec}' {' '.join(RAILS)}", *_nc_tree(tmp_path))
        assert r.stdout.split() == kept


class TestClusterKitRows:
    """The detect profile turns the map into ClusterKit's RoCE rows: dev,core,numa,gpu."""

    def _apply(self, tmp_path, stub_map: str, **env) -> subprocess.CompletedProcess:
        script = (
            f". {PROFILES}; nvis_fabric_rail_map() {{ {stub_map}; }}; "
            '_ck_apply_hca_profile roce && printf "%s|%s" "$CK_HCA_PROFILE_USED" "$CK_HCA_ROWS"'
        )
        return _bash(script, tmp_path, tmp_path, **env)

    def test_roce_defaults_to_the_detected_rails(self, tmp_path):
        r = self._apply(tmp_path, "printf 'roce_vf_r1 1 0 1 vpd\\nroce_vf_r2 0 0 2 vpd\\n'")
        assert r.returncode == 0, r.stderr
        used, rows = r.stdout.split("|")
        assert used == "detect"
        assert rows.splitlines() == ["roce_vf_r1,1,0,1", "roce_vf_r2,2,0,0"]

    def test_no_rails_falls_back_to_the_previous_preset_with_a_warning(self, tmp_path):
        r = self._apply(tmp_path, "return 1")
        used, rows = r.stdout.split("|")
        assert used == "gb300_roce_spx_quad"
        assert rows.startswith("roce_r0_p0+")
        assert "no RoCE compute rails detected" in r.stderr

    def test_a_named_preset_is_still_honoured(self, tmp_path):
        r = self._apply(tmp_path, "printf 'roce_vf_r1 1 0 1 vpd\\n'", RACK_CLUSTERKIT_HCA_PROFILE="gb300_roce_spx_rail")
        assert r.stdout.split("|")[0] == "gb300_roce_spx_rail"


class TestTheRoutableGid:
    """UCX's automatic GID pick took a link-local RoCE v2 GID on NC and MPI timed out cross-rack."""

    LL = "fe80:0000:0000:0000:0a20:e7ff:fe79:2088"
    V6 = "2a03:83e4:410e:1209:0000:0000:0000:0001"
    V4 = "0000:0000:0000:0000:0000:ffff:0a0a:20a6"

    def _port(self, tmp: Path, gids: list[tuple[str, str]]) -> Path:
        port = tmp / "ib" / "roce_vf_r1" / "ports" / "1"
        (port / "gids").mkdir(parents=True)
        (port / "gid_attrs" / "types").mkdir(parents=True)
        for i, (gid, kind) in enumerate(gids):
            (port / "gids" / str(i)).write_text(gid + "\n")
            (port / "gid_attrs" / "types" / str(i)).write_text(kind + "\n")
        return tmp / "ib"

    def _index(self, ibroot: Path, **env) -> str:
        r = _bash(f". {FABRIC}; nvis_fabric_routable_gid_index roce_vf_r1", ibroot, ibroot, **env)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def test_nc_layout_picks_the_routable_v2_gid_not_the_link_local_ones(self, tmp_path):
        ib = self._port(tmp_path, [(self.LL, "IB/RoCE v1"), (self.LL, "RoCE v2"), (self.LL, "IB/RoCE v1"),
                                   (self.LL, "RoCE v2"), (self.V6, "IB/RoCE v1"), (self.V6, "RoCE v2")])
        assert self._index(ib) == "5"

    def test_the_index_follows_the_node_not_a_constant(self, tmp_path):
        """Some NC nodes carry one link-local pair fewer, so their routable GID is index 3."""
        ib = self._port(tmp_path, [(self.LL, "IB/RoCE v1"), (self.LL, "RoCE v2"), (self.V6, "IB/RoCE v1"),
                                   (self.V6, "RoCE v2")])
        assert self._index(ib) == "3"

    def test_the_clusters_address_family_decides_between_v4_and_v6(self, tmp_path):
        ib = self._port(tmp_path, [(self.LL, "RoCE v2"), (self.V4, "RoCE v2"), (self.V6, "RoCE v2")])
        assert self._index(ib) == "2"  # IPv6 first when the cluster states nothing
        assert self._index(ib, NVIS_FABRIC_ADDR_FAMILY="ipv4") == "1"

    def test_no_routable_gid_prints_nothing(self, tmp_path):
        assert self._index(self._port(tmp_path, [(self.LL, "RoCE v2")])) == ""


class TestTheClusterKitTestList:
    """RACK_CLUSTERKIT_TESTS is a setting: the launch forwards it, the launcher turns it into -d flags."""

    def test_the_setting_reaches_the_clusterkit_job(self, monkeypatch, tmp_path):
        os.environ.setdefault("RACK_STATUS_DEMO", "1")
        from api import validation_runs as vr

        settings = {"RACK_CLUSTERKIT_TESTS": "gpu_gpu_bw gpu_gpu_lat", "RACK_CLUSTERKIT_EXTRA_OPTS": "-G"}
        monkeypatch.setattr(vr, "_eget", lambda key, *a, **k: settings.get(key, ""))
        monkeypatch.setattr(vr, "_resolve_log_dir", lambda _raw: tmp_path)
        monkeypatch.setattr(vr, "_normalize_rack_id", lambda rack: rack)
        monkeypatch.setattr(vr, "validation_target_for_rack", lambda rack, _ov: f"{rack}-ct[01-18]")
        monkeypatch.setattr(vr, "_script_exists", lambda _p: True)
        monkeypatch.setattr(vr, "_nvis_workspace_dir_effective", lambda: "/home/cmods/workspace")
        prep, err = vr._prepare_run_clusterkit_singlerack({"rack": "U19"})
        assert err is None and prep is not None
        assert prep["env_updates"]["RACK_CLUSTERKIT_TESTS"] == "gpu_gpu_bw gpu_gpu_lat"
        assert prep["env_updates"]["RACK_CLUSTERKIT_EXTRA_OPTS"] == "-G"

    def _exe_opt(self, tests: str | None) -> str:
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_exe_opt=""')
        snippet = sbatch[start:sbatch.index("_ck_failed=()", start)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if tests is not None:
            env["RACK_CLUSTERKIT_TESTS"] = tests
        r = subprocess.run(["bash", "-c", snippet + 'printf "%s" "${_ck_exe_opt% }"'],
                           env=env, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout

    def test_unset_keeps_the_previous_bandwidth_and_latency_tests(self):
        assert self._exe_opt(None) == "-d bw -d lat"

    def test_a_stated_list_becomes_one_flag_per_test(self):
        assert self._exe_opt("gpu_gpu_bw gpu_gpu_lat") == "-d gpu_gpu_bw -d gpu_gpu_lat"
