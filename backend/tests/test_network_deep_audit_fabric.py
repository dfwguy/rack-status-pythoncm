# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The network deep audit checks the rails the cluster's fabric has, not the ones another site had.

The audit once walked every RDMA device and demanded 800 Gb/s on four of them -- right for
InfiniBand, wrong for NscaleTX-NC's Spectrum-X RoCE-over-IPv6 rails (200G ``roce_vf_r*`` VFs beside
400G management NICs and DOWN ``roce_r*`` uplink PFs), where it failed every healthy node. Worse, the
resulting record overran the fan-out's line and read back as "structured audit result missing". These
tests run the real script against fake sysfs trees shaped like both sites.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from tests.test_cluster_fabric import LINK_LOCAL, V4, _dev, _nc_node

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
PREFIX = "NVIS_DEEP_AUDIT_JSON="


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _link_up(root: Path) -> None:
    """_dev writes only ``state``; the audit also reads ``phys_state``."""
    for port in root.glob("*/ports/1"):
        up = (port / "state").read_text().startswith("4")
        (port / "phys_state").write_text("5: LinkUp\n" if up else "3: Disabled\n")


def _audit(tmp_path: Path, root: Path, **env: str) -> dict:
    # A private copy without nvlink_preflight.sh: the data-path probe is not under test here.
    tree = tmp_path / "scripts"
    tree.mkdir()
    shutil.copy(SCRIPTS / "nvlink_network_deep_audit.sh", tree)
    shutil.copytree(SCRIPTS / "lib", tree / "lib")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "nvidia-smi", 'case "$1" in -L) for i in 0 1 2 3; do echo "GPU $i: GB300"; done;; '
          '-q) for i in 0 1 2 3; do printf "State : Completed\\nStatus : Success\\n"; done;; esac')
    _stub(bin_dir, "systemctl", "exit 0")
    full = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "NVIS_FABRIC_SYSFS": str(root),
        "NVIS_FABRIC_DEFAULT_ROUTE_IFS": "enP22s22f0np0",
        "NVIS_FABRIC_NVSWITCH_BDFS": "",
        "GPUS_PER_NODE": "4",
        **env,
    }
    r = subprocess.run(["bash", str(tree / "nvlink_network_deep_audit.sh")], capture_output=True, text=True,
                       env=full, timeout=60, check=False)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith(PREFIX)]
    assert len(lines) == 1, r.stdout + r.stderr
    return json.loads(lines[0][len(PREFIX):])


class TestRoceIpv6Site:
    def test_a_healthy_nc_node_passes_on_its_200g_vf_rails(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE")
        # The DOWN roce_r* PFs and the 400G management NICs are not rails and must not fail it.
        assert out["codes"] == [], out["reason"]
        assert out["metrics"]["fabric"] == "roce"
        assert out["metrics"]["rails"] == [f"roce_vf_r{r}" for r in range(1, 5)]
        assert out["metrics"]["ib_ports"] == 4
        assert out["metrics"]["expected_rail_rate_gbps"] == 200

    def test_a_rail_that_went_down_is_named_not_silently_dropped(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        (root / "roce_vf_r3" / "ports" / "1" / "state").write_text("1: DOWN\n")
        (root / "roce_vf_r3" / "ports" / "1" / "phys_state").write_text("3: Disabled\n")
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE")
        assert out["status"] == "fail"
        assert "ib_port_state" in out["codes"]  # the rail is still counted, and reported as down
        assert "roce_vf_r3" in out["reason"]

    def test_a_rail_below_its_siblings_rate_is_caught_without_a_hardcoded_number(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        (root / "roce_vf_r2" / "ports" / "1" / "rate").write_text("100 Gb/sec (2X NDR)\n")
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE")
        assert "ib_link_rate" in out["codes"]
        assert "roce_vf_r2" in out["reason"]

    def test_a_node_with_no_roce_provisioned_fails_on_the_missing_rails(self, tmp_path):
        root = tmp_path / "ib"
        _dev(root, "mlx5_12", state="ACTIVE", link="Ethernet", rate=400, netdev="enP22s22f0np0", gids=[LINK_LOCAL, V4])
        for r in range(1, 5):
            _dev(root, f"roce_r{r}", state="DOWN", link="Ethernet", rate=200, netdev=f"eth_p1_r{r}", gids=[LINK_LOCAL])
        _link_up(root)
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE")
        assert out["status"] == "fail"
        assert "ib_port_count" in out["codes"]

    def test_an_operator_override_still_wins(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE", RACK_AUDIT_EXPECTED_IB_RATE_GBPS="400")
        assert "ib_link_rate" in out["codes"]


class TestRateExpectationFromTheCatalog:
    """The platform's rate (gb300-nvl72: 800G IB) reached the audit as NVIS_FABRIC_EXPECT_RATE and
    drained healthy NC nodes. A rate from the other protocol is ignored, loudly; the cluster's own wins."""

    def _nc(self, tmp_path):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        return root

    def test_an_ib_platform_rate_is_ignored_on_a_node_that_runs_roce(self, tmp_path):
        # The protocol was inherited from the platform (IB) but the node runs RoCE.
        out = _audit(tmp_path, self._nc(tmp_path), NVIS_FABRIC_NETWORK="IB", NVIS_FABRIC_EXPECT_RATE="800")
        assert out["codes"] == [], out["reason"]
        assert any("rate_expectation" in w and "ignored" in w for w in out["warnings"])
        assert out["metrics"]["expected_rail_rate_gbps"] == 200

    def test_with_the_protocol_unstated_nothing_contradicts_the_expectation(self, tmp_path):
        # Unstated protocol: nothing contradicts the expectation, so it is honoured (the operator's call).
        out = _audit(tmp_path, self._nc(tmp_path), NVIS_FABRIC_EXPECT_RATE="800")
        assert "ib_link_rate" in out["codes"]

    def test_a_rate_the_cluster_states_for_its_own_rails_is_honoured(self, tmp_path):
        out = _audit(tmp_path, self._nc(tmp_path), NVIS_FABRIC_NETWORK="RoCE", NVIS_FABRIC_EXPECT_RATE="200")
        assert out["codes"] == []
        assert out["metrics"]["expected_rail_rate_gbps"] == 200

    def test_a_cluster_stated_rate_above_what_the_rails_train_at_still_fails(self, tmp_path):
        out = _audit(tmp_path, self._nc(tmp_path), NVIS_FABRIC_NETWORK="RoCE", NVIS_FABRIC_EXPECT_RATE="400")
        # Same protocol as the node, so it is a real expectation and 200G rails fall short of it.
        assert "ib_link_rate" in out["codes"]


class TestInfiniBandSite:
    def _ib_node(self, root: Path, rate: int = 800) -> None:
        for i in range(4):
            _dev(root, f"mlx5_{i}", state="ACTIVE", link="InfiniBand", rate=rate,
                 gids=[f"fe80:0000:0000:0000:a088:c203:00{i}5:fa3e"])
        _link_up(root)

    def test_a_healthy_800g_ib_node_passes(self, tmp_path):
        root = tmp_path / "ib"
        self._ib_node(root)
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="IB")
        assert out["codes"] == [], out["reason"]
        assert out["metrics"]["fabric"] == "ib"

    def test_an_ib_rail_at_the_wrong_rate_is_still_a_failure(self, tmp_path):
        # The IB gate is the NDR line rate, as before; only the RoCE rate is derived.
        root = tmp_path / "ib"
        self._ib_node(root, rate=400)
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="IB")
        assert "ib_link_rate" in out["codes"]


class TestTheRecordSurvivesTheFanOut:
    @pytest.mark.parametrize("rails", [4])
    def test_a_node_failing_everywhere_still_emits_one_short_parseable_line(self, tmp_path, rails):
        root = tmp_path / "ib"
        _nc_node(root)
        _link_up(root)
        for r in range(1, rails + 1):
            (root / f"roce_vf_r{r}" / "ports" / "1" / "state").write_text("1: DOWN\n")
            (root / f"roce_vf_r{r}" / "ports" / "1" / "phys_state").write_text("3: Disabled\n")
        out = _audit(tmp_path, root, NVIS_FABRIC_NETWORK="RoCE")
        assert out["status"] == "fail"
        # Codes are reported once each, and the whole record stays far under any per-line cap.
        assert len(out["codes"]) == len(set(out["codes"]))
        assert len(json.dumps(out)) < 1500
