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
    """Every ClusterKit row, IB or RoCE, comes from the rail map: dev,core,numa,gpu."""

    def _apply(self, tmp_path, stub_map: str, tech: str = "roce", **env) -> subprocess.CompletedProcess:
        script = (
            f". {PROFILES}; nvis_fabric_rail_map() {{ {stub_map}; }}; "
            f'_ck_apply_hca_profile {tech} && printf "%s|%s" "$CK_HCA_PROFILE_USED" "$CK_HCA_ROWS"'
        )
        return _bash(script, tmp_path, tmp_path, **env)

    @pytest.mark.parametrize("tech", ["roce", "mlx"])
    def test_both_fabrics_default_to_the_detected_rails(self, tmp_path, tech):
        r = self._apply(tmp_path, "printf 'rail_a 1 0 1 vpd\\nrail_b 0 0 2 vpd\\n'", tech)
        assert r.returncode == 0, r.stderr
        used, rows = r.stdout.split("|")
        assert used == "detect"
        assert rows.splitlines() == ["rail_a,1,0,1", "rail_b,2,0,0"]

    @pytest.mark.parametrize("tech", ["roce", "mlx"])
    def test_no_rails_stops_and_names_the_setting_instead_of_guessing(self, tmp_path, tech):
        # The old RoCE fallback was gb300_roce_spx_quad -- roce_r0_p0.., devices NC does not have --
        # and IB never detected at all: it measured mlx5_0..3 whatever the node held.
        r = self._apply(tmp_path, "return 1", tech, NVIS_FABRIC_HCA="roce_vf_r")
        assert r.returncode != 0
        assert r.stdout == ""
        assert f"no {tech} compute rails detected" in r.stderr
        assert "fabric.compute_hca" in r.stderr and "NVIS_FABRIC_HCA='roce_vf_r'" in r.stderr
        assert "RACK_CLUSTERKIT_HCA_PROFILE" in r.stderr
        assert "roce_r0_p0" not in r.stderr and "mlx5_0" not in r.stderr

    @pytest.mark.parametrize(("stated", "node", "adopted"), [
        ("mlx", "roce", "roce"), ("roce", "ib", "mlx"), ("roce", "roce", ""), ("mlx", "ib", ""),
    ])
    def test_detected_rails_carry_the_nodes_own_technology(self, tmp_path, stated, node, adopted):
        # IB stated on a Spectrum-X node would skip the RoCE GID and use IB transports on RoCE rails.
        script = (
            f". {PROFILES}; nvis_fabric_rail_map() {{ printf 'r1 0 0 1 vpd\\n'; }}; "
            f"nvis_fabric_scan() {{ echo NVIS_FAB_LINK={node}; }}; "
            f'_ck_apply_hca_profile {stated} && printf "%s" "${{CK_LINK_TECH_DETECTED:-}}"'
        )
        r = _bash(script, tmp_path, tmp_path, CK_LINK_TECH_DETECTED="stale")
        assert r.returncode == 0, r.stderr
        assert r.stdout == adopted
        assert ("running as" in r.stderr) == bool(adopted)

    def test_the_job_follows_the_adopted_technology(self):
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        apply_at = sbatch.index('_ck_apply_hca_profile "${LINK_TECH}"')
        assert sbatch.index('LINK_TECH="${CK_LINK_TECH_DETECTED:-${LINK_TECH}}"') > apply_at
        assert sbatch.index('export CK_LINK_TECH="${LINK_TECH}"') > apply_at

    # A rail map that echoes the devices it was asked for, GPU = position: the preset supplies only names.
    ECHO_MAP = 'i=0; for d in "$@"; do printf "%s %s %s %s numa-guess\\n" "$d" "$i" "$((i / 2))" "$((i + 1))"; i=$((i + 1)); done'

    @pytest.mark.parametrize(("preset", "devices"), [
        ("gb300_mlx4", ["mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3"]),
        ("b300_mlx8", ["mlx5_0", "mlx5_1", "mlx5_8", "mlx5_9", "mlx5_10", "mlx5_11", "mlx5_12", "mlx5_13"]),
    ])
    def test_an_ib_preset_names_devices_and_the_node_supplies_gpu_core_and_numa(self, tmp_path, preset, devices):
        for dev in devices:
            (tmp_path / dev).mkdir()
        r = self._apply(tmp_path, self.ECHO_MAP, "mlx", RACK_CLUSTERKIT_HCA_PROFILE=preset)
        assert r.returncode == 0, r.stderr
        used, rows = r.stdout.split("|")
        assert used == preset
        assert rows.splitlines() == [f"{d},{i + 1},{i // 2},{i}" for i, d in enumerate(devices)]

    def test_an_ib_preset_device_the_node_lacks_is_an_error(self, tmp_path):
        for dev in ("mlx5_0", "mlx5_1", "mlx5_3"):
            (tmp_path / dev).mkdir()
        r = self._apply(tmp_path, self.ECHO_MAP, "mlx", RACK_CLUSTERKIT_HCA_PROFILE="gb300_mlx4")
        assert r.returncode != 0
        assert "does not have: mlx5_2" in r.stderr

    def test_an_ib_preset_device_with_no_gpu_is_an_error_not_a_shorter_sweep(self, tmp_path):
        for dev in ("mlx5_0", "mlx5_1", "mlx5_2", "mlx5_3"):
            (tmp_path / dev).mkdir()
        stub = 'for d in "$@"; do g=1; [[ $d == mlx5_2 ]] && g=-; printf "%s %s 0 1 vpd\\n" "$d" "$g"; done'
        r = self._apply(tmp_path, stub, "mlx", RACK_CLUSTERKIT_HCA_PROFILE="gb300_mlx4")
        assert r.returncode != 0
        assert "not every device maps to a GPU" in r.stderr

    def test_a_named_roce_preset_is_still_honoured(self, tmp_path):
        r = self._apply(tmp_path, "printf 'roce_vf_r1 1 0 1 vpd\\n'", RACK_CLUSTERKIT_HCA_PROFILE="gb300_roce_spx_rail")
        assert r.stdout.split("|")[0] == "gb300_roce_spx_rail"

    def test_a_preset_for_the_other_fabric_is_refused(self, tmp_path):
        r = self._apply(tmp_path, self.ECHO_MAP, "roce", RACK_CLUSTERKIT_HCA_PROFILE="gb300_mlx4")
        assert r.returncode != 0
        assert "is for mlx, but link technology is roce" in r.stderr


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


class TestClusterKitGpuMode:
    """GPU-Direct mode: the CUDA runtime on the path, memory-only binding, and no silent skips.

    ClusterKit dlopens libcudart for its GPU tests and, when it cannot, skips them and still exits 0.
    On GB300 it also ran no GPU tests when pinned to a CPU of the second socket. Both looked like a
    successful run with no results, which is what the launcher now refuses.
    """

    def _libdir(self, tmp_path, **env) -> str:
        r = subprocess.run(
            ["bash", "-c", f"CUDA_MAJ=13; . {SCRIPTS / 'workspace-cuda-major-common.sh'}; nvis_cuda_runtime_libdir"],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "WORKSPACE_DIR": str(tmp_path), **env},
            capture_output=True, text=True, timeout=30, check=False)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def _cuda(self, root: Path, name: str, lib: str) -> Path:
        d = root / name / "targets" / "sbsa-linux" / "lib"
        d.mkdir(parents=True)
        (d / lib).write_text("")
        return d

    def test_the_workspace_runtime_for_this_major_is_found(self, tmp_path):
        self._cuda(tmp_path, "cuda", "libcudart.so.12")
        want = self._cuda(tmp_path, "cuda13", "libcudart.so.13")
        assert self._libdir(tmp_path) == str(want)

    def test_a_toolkit_of_another_major_is_not_taken(self, tmp_path):
        self._cuda(tmp_path, "cuda12", "libcudart.so.12")
        assert self._libdir(tmp_path) == "" or "12" not in self._libdir(tmp_path)

    def test_a_site_override_wins(self, tmp_path):
        self._cuda(tmp_path, "cuda13", "libcudart.so.13")
        assert self._libdir(tmp_path, NVIS_CUDA_LIB="/opt/site/cuda/lib") == "/opt/site/cuda/lib"

    def _mapper(self, tmp_path, gpu_tests: str, **extra: str) -> str:
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "numactl").write_text('#!/bin/bash\nif [[ "$*" == *"/bin/true"* ]]; then exit 0; fi\necho "numactl $*"\n')
        (stub / "numactl").chmod(0o755)
        env = {
            "PATH": f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "CLUSTERKIT_HCA": "roce_vf_r3", "CORE": "73", "NUMA": "1", "GPU": "3",
            "UCX_IB_GID_INDEX": "5", "CK_GPU_TESTS": gpu_tests, **extra,
        }
        r = subprocess.run(["bash", str(SCRIPTS / "core_to_hca_rail_map.sh"), "clusterkit"],
                           env=env, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout.strip()

    def test_gpu_tests_bind_memory_but_do_not_pin_the_cpu(self, tmp_path):
        assert self._mapper(tmp_path, "1") == "numactl -m 1 -- clusterkit"

    def test_host_tests_keep_the_core_pin(self, tmp_path):
        assert self._mapper(tmp_path, "0") == "numactl -C 73 -m 1 -- clusterkit"

    def test_on_infiniband_the_same_mapper_binds_the_rail_and_skips_the_roce_gid(self, tmp_path):
        # IB addresses peers by LID: there is no routable GID to look up, and the old IB mappers
        # (core_to_hca_dgxgb200/300.sh) bound a fixed mlx5_N and CPU range per local rank instead.
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "numactl").write_text('#!/bin/bash\nif [[ "$*" == *"/bin/true"* ]]; then exit 0; fi\n'
                                      'echo "numactl $* UCX=$UCX_NET_DEVICES GPU=$CUDA_VISIBLE_DEVICES GID=${UCX_IB_GID_INDEX:-}"\n')
        (stub / "numactl").chmod(0o755)
        env = {"PATH": f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}", "CLUSTERKIT_HCA": "mlx5_2",
               "CORE": "73", "NUMA": "1", "GPU": "2", "CK_GPU_TESTS": "0", "CK_LINK_TECH": "mlx"}
        r = subprocess.run(["bash", str(SCRIPTS / "core_to_hca_rail_map.sh"), "clusterkit"],
                           env=env, capture_output=True, text=True, timeout=30, check=False)
        assert r.stdout.strip() == "numactl -C 73 -m 1 -- clusterkit UCX=mlx5_2:1 GPU=2 GID="
        assert "GID" not in r.stderr

    def test_the_job_uses_one_rail_map_mapper_on_both_fabrics(self):
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        assert 'CORE_TO_HCA="${NVIS_BIN_SCRIPTS}/core_to_hca_rail_map.sh"' in sbatch
        assert "-x CK_LINK_TECH" in sbatch
        assert "PRODUCT_LINE" not in sbatch and "core_to_hca_dgxgb" not in sbatch
        assert not (SCRIPTS / "core_to_hca_dgxgb200.sh").exists()
        assert not (SCRIPTS / "core_to_hca_dgxgb300.sh").exists()

    def _gpu_flag(self, tests: str | None, extra: str | None = None) -> str:
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        start = sbatch.index('_ck_exe_opt=""')
        snippet = sbatch[start:sbatch.index("_ck_failed=()", start)]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        if tests is not None:
            env["RACK_CLUSTERKIT_TESTS"] = tests
        if extra is not None:
            env["RACK_CLUSTERKIT_EXTRA_OPTS"] = extra
        r = subprocess.run(["bash", "-c", snippet + 'printf "%s" "$CK_GPU_TESTS"'],
                           env=env, capture_output=True, text=True, timeout=30, check=False)
        return r.stdout

    @pytest.mark.parametrize(
        ("tests", "extra", "flag"),
        [(None, None, "0"), ("bw lat", None, "0"), ("gpu_gpu_bw gpu_gpu_lat", "-G", "1"), (None, "-g -G", "1")],
    )
    def test_gpu_mode_follows_the_requested_tests(self, tests, extra, flag):
        assert self._gpu_flag(tests, extra) == flag

    def test_a_gpu_run_without_gpu_results_fails_the_rail(self):
        sbatch = (SCRIPTS / "clusterkit.sbatch").read_text()
        assert 'compgen -G "${CK_OUTPUT_SUBDIR}/GPU_*.json"' in sbatch
        assert "-x CK_GPU_TESTS" in sbatch


def test_no_script_binds_a_fixed_mlx5_device_per_rank():
    """A device name and CPU range per local rank describes one machine; the rail map describes any."""
    import re

    offenders = [
        f"{p.name}:{n}"
        for p in sorted(SCRIPTS.glob("*.sh")) + sorted(SCRIPTS.glob("*.sbatch"))
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"export\s+UCX_NET_DEVICES=mlx5_\d", line) and not line.lstrip().startswith("#")
    ]
    assert offenders == []
