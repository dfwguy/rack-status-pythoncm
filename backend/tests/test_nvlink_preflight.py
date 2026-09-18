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
"""The NVLink preflight table must never present an un-checked node as ready.

This gate exists to stop an operator burning a 36-node NCCL run on a rack whose NVSwitch SDN
routing is wedged. That only pays off if the verdict is honest about three cases the naive
parse gets wrong:

  * a node that never answered (pdsh ssh 255) must not be absent from the table,
  * SKIPPED is not PASS — the functional data-path probe self-skips when its venv is missing
    (it IS missing on Nscale TX), and it is the only check that detects the wedged-route hang,
  * pdsh's non-zero exit is the normal "some node failed" case, so the verdict comes from the
    parsed rows and never from the exit code.
"""
from __future__ import annotations

import os

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

from nvlink_preflight import (
    expand_hostlist,
    expand_pdsh_w,
    parse_nvlink_preflight_output,
    status_label,
    summarize,
)

# --- hostlist expansion --------------------------------------------------------------------
# The expected-node list cannot be built by splitting on commas: this site's tray range has one
# inside the brackets, and getting this wrong reported a healthy 18-node rack as "2 did not
# respond" on the first live run.


def test_expand_range_with_an_inner_comma():
    hosts = expand_hostlist("ink50-dh01-h08-[11-18,28-37]")
    assert len(hosts) == 18
    assert hosts[0] == "ink50-dh01-h08-11"
    assert hosts[7] == "ink50-dh01-h08-18"
    assert hosts[8] == "ink50-dh01-h08-28"
    assert hosts[-1] == "ink50-dh01-h08-37"


def test_expand_preserves_zero_padding():
    assert expand_hostlist("p5-r01-ct[01-03]") == ["p5-r01-ct01", "p5-r01-ct02", "p5-r01-ct03"]


def test_expand_plain_hostname_is_itself():
    assert expand_hostlist("ink50-dh01-h08-11") == ["ink50-dh01-h08-11"]


def test_expand_single_value_bracket():
    assert expand_hostlist("p5-r01-ct[07]") == ["p5-r01-ct07"]


def test_expand_pdsh_w_splits_only_top_level_commas():
    w = "ink50-dh01-h02-[11-12],ink50-dh01-h03-[11-12]"
    assert expand_pdsh_w(w) == [
        "ink50-dh01-h02-11",
        "ink50-dh01-h02-12",
        "ink50-dh01-h03-11",
        "ink50-dh01-h03-12",
    ]


def test_expand_pdsh_w_handles_plain_comma_lists():
    assert expand_pdsh_w("n01,n02,n03") == ["n01", "n02", "n03"]


def test_expand_pdsh_w_dedupes_and_keeps_order():
    assert expand_pdsh_w("n02,n01,n02") == ["n02", "n01"]


def test_expand_pdsh_w_of_the_real_h08_target():
    """What validation_target_for_rack produces for a whole rack on this site."""
    hosts = expand_pdsh_w("ink50-dh01-h08-[11-18,28-37]")
    assert len(hosts) == 18
    assert "ink50-dh01-h08-[11-18" not in hosts, "the comma-split bug is back"


# A healthy node, as pdsh prefixes it.
GOOD = """\
n01: ### nvlink_preflight n01 (quick=0) ###
n01: [PASS] gpus_enumerate         4/4
n01: [PASS] nvlink_links           18/18 up @ GPU0
n01: [PASS] nvlink_p2p_peers       GPU0 sees 3 NV peers
n01: [PASS] nvlink_p2p_matrix      all GPU-GPU P2P=OK (topo -p2p n)
n01: [PASS] gpu_fabric_register    State=Completed Status=Success Health=Healthy x4
n01: [PASS] imex_domain            active READY self=UP disconnected_peers=0
n01: [PASS] dmesg_nvlink_link      no Xid149/154 / degraded-link / NVLink-down
n01: [PASS] dmesg_rm_clean         no NV_ERR/GPU-lost runtime faults
n01: [PASS] nvlink_datapath        forced-NVLink rc=0 busbw= 412.3 GB/s
n01: RESULT n01: PASS (NVLS-ready)
"""

# The wedged-NVSwitch signature: static checks pass, the functional probe fails.
WEDGED = """\
n02: ### nvlink_preflight n02 (quick=0) ###
n02: [PASS] gpus_enumerate         4/4
n02: [PASS] gpu_fabric_register    State=Completed Status=Success Health=Healthy x4
n02: [FAIL] nvlink_datapath        forced-NVLink HANG/TIMEOUT(120s) but forced-RoCE rc=0 => NVSwitch SDN routing DEAD (node OK) -> escalate FABRIC
n02: RESULT n02: FAIL (do not use until drained/reset)
"""

# Everything the node ran passed, but the one check that matters was skipped.
SKIPPED = """\
n03: ### nvlink_preflight n03 (quick=0) ###
n03: [PASS] gpus_enumerate         4/4
n03: [PASS] imex_domain            active READY self=UP disconnected_peers=0
n03:   nvlink_datapath             SKIPPED (venv/test missing: /cm/shared/nvtest/nv-real13)
n03: RESULT n03: PASS (NVLS-ready)
"""


# Captured verbatim from `pdsh --quick` on two h08 nodes of Nscale TX (2026-08-06). Note that
# pdsh interleaves nodes, and that quick mode prints NO nvlink_datapath line at all.
REAL_QUICK = """\
ink50-dh01-h08-11: ### nvlink_preflight ink50-dh01-h08-11 (quick=1) ###
ink50-dh01-h08-11: [PASS] gpus_enumerate         4/4
ink50-dh01-h08-12: ### nvlink_preflight ink50-dh01-h08-12 (quick=1) ###
ink50-dh01-h08-11: [PASS] nvlink_links           18/18 up @ GPU0
ink50-dh01-h08-12: [PASS] gpus_enumerate         4/4
ink50-dh01-h08-12: [PASS] nvlink_links           18/18 up @ GPU0
ink50-dh01-h08-11: [PASS] nvlink_p2p_peers       GPU0 sees 3 NV peers
ink50-dh01-h08-12: [PASS] nvlink_p2p_peers       GPU0 sees 3 NV peers
ink50-dh01-h08-11: [PASS] nvlink_p2p_matrix      all GPU-GPU P2P=OK (topo -p2p n)
ink50-dh01-h08-12: [PASS] nvlink_p2p_matrix      all GPU-GPU P2P=OK (topo -p2p n)
ink50-dh01-h08-11: [PASS] gpu_fabric_register    State=Completed Status=Success Health=Healthy x4
ink50-dh01-h08-12: [PASS] gpu_fabric_register    State=Completed Status=Success Health=Healthy x4
ink50-dh01-h08-11: [PASS] imex_domain            active READY self=UP disconnected_peers=0
ink50-dh01-h08-11: [PASS] dmesg_nvlink_link      no Xid149/154 / degraded-link / NVLink-down
ink50-dh01-h08-11: [PASS] dmesg_rm_clean         no NV_ERR/GPU-lost runtime faults
ink50-dh01-h08-12: [PASS] imex_domain            active READY self=UP disconnected_peers=0
ink50-dh01-h08-12: [PASS] dmesg_nvlink_link      no Xid149/154 / degraded-link / NVLink-down
ink50-dh01-h08-12: [PASS] dmesg_rm_clean         no NV_ERR/GPU-lost runtime faults
ink50-dh01-h08-11: [WARN] sharp_hello            only 0/4 GPU rails have SHARP (rails: none; no SHARP on: mlx5_0 mlx5_1 mlx5_2 mlx5_3 mlx5_bond_0 ); SHARP likely not provisioned -> NCCL falls back to NVLS_TREE. SHARP_STRICT=1 to enforce
ink50-dh01-h08-11: RESULT ink50-dh01-h08-11: PASS (NVLS-ready)
ink50-dh01-h08-12: [WARN] sharp_hello            only 0/4 GPU rails have SHARP (rails: none; no SHARP on: mlx5_0 mlx5_1 mlx5_2 mlx5_3 mlx5_bond_0 ); SHARP likely not provisioned -> NCCL falls back to NVLS_TREE. SHARP_STRICT=1 to enforce
ink50-dh01-h08-12: RESULT ink50-dh01-h08-12: PASS (NVLS-ready)
"""


def _row(rows, host):
    return next(r for r in rows if r["host"] == host)


# --- real cluster output ---------------------------------------------------------------------


def test_real_interleaved_pdsh_output_parses_per_node():
    """pdsh runs nodes in parallel, so lines from both are interleaved."""
    hosts = ["ink50-dh01-h08-11", "ink50-dh01-h08-12"]
    rows = parse_nvlink_preflight_output(REAL_QUICK, "", hosts)
    assert [r["host"] for r in rows] == hosts
    for r in rows:
        assert r["passed"] == 8, r
        assert r["warned"] == 1, "SHARP not provisioned is a WARN, not a failure"
        assert r["failed"] == 0


def test_quick_mode_passes_but_reports_the_skipped_probe():
    """Quick mode omits the data-path check; that is recorded, but it does not block the pass.

    Policy: only a FAILED check makes a node not-ready. The skip still has to be visible, so the
    missing coverage is reported rather than silently absent.
    """
    hosts = ["ink50-dh01-h08-11", "ink50-dh01-h08-12"]
    rows = parse_nvlink_preflight_output(REAL_QUICK, "", hosts)
    for r in rows:
        assert r["status"] == "pass"
        dp = next(c for c in r["checks"] if c["name"] == "nvlink_datapath")
        assert dp["state"] == "skipped"
        assert "quick mode" in dp["detail"]
    s = summarize(rows)
    assert s["ready"] is True
    assert s["nodes_with_skips"] == 2, "the missing coverage must still be reported"
    assert s["nodes_with_warnings"] == 2, "SHARP unprovisioned is a warning, not a blocker"


def test_full_mode_with_the_probe_is_ready():
    """The same nodes, full mode with the probe actually running, do certify as ready."""
    rows = parse_nvlink_preflight_output(GOOD, "", ["n01"])
    assert _row(rows, "n01")["status"] == "pass"
    assert summarize(rows)["ready"] is True


# --- per-node verdicts ---------------------------------------------------------------------


def test_healthy_node_is_ready():
    rows = parse_nvlink_preflight_output(GOOD, "", ["n01"])
    r = _row(rows, "n01")
    assert r["status"] == "pass"
    assert r["passed"] == 9
    assert r["failed"] == 0
    assert r["started"] is True
    assert r["quick"] is False
    assert status_label(r["status"]) == "ready"


def test_failed_node_reports_the_failing_check():
    rows = parse_nvlink_preflight_output(WEDGED, "", ["n02"])
    r = _row(rows, "n02")
    assert r["status"] == "fail"
    assert r["failed"] == 1
    assert "nvlink_datapath" in r["first_failure"]
    assert "NVSwitch SDN routing DEAD" in r["first_failure"]


def test_a_skipped_check_is_reported_but_still_passes():
    """A self-skipped probe (missing venv) is recorded and does not withhold the pass."""
    rows = parse_nvlink_preflight_output(SKIPPED, "", ["n03"])
    r = _row(rows, "n03")
    assert r["skipped"] == 1
    assert r["status"] == "pass"
    assert status_label(r["status"]) == "ready"
    dp = next(c for c in r["checks"] if c["name"] == "nvlink_datapath")
    assert dp["state"] == "skipped"
    assert "venv/test missing" in dp["detail"]


def test_unreachable_node_still_gets_a_row():
    """pdsh reports ssh failures on stderr; the node must not vanish from the table."""
    stderr = "n04: ssh exited with exit code 255"
    rows = parse_nvlink_preflight_output(GOOD, stderr, ["n01", "n04"])
    assert [r["host"] for r in rows] == ["n01", "n04"]
    r = _row(rows, "n04")
    assert r["status"] == "error"
    assert "255" in r["transport_error"]
    assert status_label(r["status"]) == "unreachable"


def test_expected_node_with_no_output_at_all_is_no_response():
    rows = parse_nvlink_preflight_output(GOOD, "", ["n01", "n99"])
    assert _row(rows, "n99")["status"] == "no_response"


def test_node_that_started_but_never_reported_is_incomplete():
    """Killed by the pdsh timeout mid-run: started, no RESULT line."""
    partial = "n05: ### nvlink_preflight n05 (quick=0) ###\nn05: [PASS] gpus_enumerate 4/4\n"
    r = _row(parse_nvlink_preflight_output(partial, "", ["n05"]), "n05")
    assert r["status"] == "incomplete"
    assert r["started"] is True


def test_warn_alone_does_not_stop_a_node_being_ready():
    """SHARP not provisioned is a WARN — NCCL falls back to NVLS_TREE, so it is not a blocker.

    Uses a FULL run whose data-path probe ran, to isolate the WARN from quick-mode semantics.
    """
    warned = (
        "n06: ### nvlink_preflight n06 (quick=0) ###\n"
        "n06: [PASS] gpus_enumerate 4/4\n"
        "n06: [WARN] sharp_hello only 2/4 GPU rails have SHARP\n"
        "n06: [PASS] nvlink_datapath forced-NVLink rc=0 busbw= 401.1 GB/s\n"
        "n06: RESULT n06: PASS (NVLS-ready)\n"
    )
    r = _row(parse_nvlink_preflight_output(warned, "", ["n06"]), "n06")
    assert r["status"] == "pass"
    assert r["warned"] == 1
    assert r["quick"] is False


def test_quick_flag_is_read_from_the_header():
    quick = "n07: ### nvlink_preflight n07 (quick=1) ###\nn07: RESULT n07: PASS (NVLS-ready)\n"
    assert _row(parse_nvlink_preflight_output(quick, "", ["n07"]), "n07")["quick"] is True


def test_checks_keep_their_order_and_detail():
    r = _row(parse_nvlink_preflight_output(WEDGED, "", ["n02"]), "n02")
    assert [c["name"] for c in r["checks"]] == [
        "gpus_enumerate",
        "gpu_fabric_register",
        "nvlink_datapath",
    ]
    assert r["checks"][2]["state"] == "fail"


# --- rack roll-up -------------------------------------------------------------------------


def test_rack_is_ready_only_when_every_node_passes():
    rows = parse_nvlink_preflight_output(GOOD + WEDGED, "", ["n01", "n02"])
    s = summarize(rows)
    assert s == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "incomplete": 0,
        "no_response": 0,
        "nodes_with_skips": 0,
        "nodes_with_warnings": 0,
        "ready": False,
        "worst": "fail",
    }


def test_all_pass_is_ready():
    s = summarize(parse_nvlink_preflight_output(GOOD, "", ["n01"]))
    assert s["ready"] is True
    assert s["worst"] == "pass"


def test_a_skipped_check_does_not_block_ready():
    """Per policy: skips are reported, failures block."""
    rows = parse_nvlink_preflight_output(GOOD + SKIPPED, "", ["n01", "n03"])
    s = summarize(rows)
    assert s["ready"] is True
    assert s["nodes_with_skips"] == 1


def test_a_warning_does_not_block_ready():
    warned = (
        "n08: ### nvlink_preflight n08 (quick=0) ###\n"
        "n08: [PASS] gpus_enumerate 4/4\n"
        "n08: [WARN] sharp_hello only 0/4 GPU rails have SHARP\n"
        "n08: [PASS] nvlink_datapath forced-NVLink rc=0\n"
        "n08: RESULT n08: PASS (NVLS-ready)\n"
    )
    s = summarize(parse_nvlink_preflight_output(warned, "", ["n08"]))
    assert s["ready"] is True
    assert s["nodes_with_warnings"] == 1
    assert s["nodes_with_skips"] == 0


def test_one_failing_node_still_blocks_ready_even_with_others_warning():
    rows = parse_nvlink_preflight_output(REAL_QUICK + WEDGED, "", ["ink50-dh01-h08-11", "n02"])
    s = summarize(rows)
    assert s["ready"] is False
    assert s["failed"] == 1


def test_missing_node_blocks_ready():
    s = summarize(parse_nvlink_preflight_output(GOOD, "", ["n01", "n07"]))
    assert s["ready"] is False
    assert s["no_response"] == 1


def test_empty_output_is_not_ready():
    s = summarize(parse_nvlink_preflight_output("", "", ["n01", "n02"]))
    assert s["ready"] is False
    assert s["no_response"] == 2


def test_no_expected_hosts_still_parses_what_it_sees():
    rows = parse_nvlink_preflight_output(GOOD + WEDGED, "")
    assert {r["host"] for r in rows} == {"n01", "n02"}


def test_real_nscale_hostnames_parse():
    """Host tokens contain dashes and digits; the pdsh prefix split must not eat them.

    The RESULT line repeats the hostname after the pdsh prefix, so a naive split on ':' would
    mis-key the row.
    """
    out = (
        "ink50-dh01-h08-11: ### nvlink_preflight ink50-dh01-h08-11 (quick=0) ###\n"
        "ink50-dh01-h08-11: [PASS] gpus_enumerate 4/4\n"
        "ink50-dh01-h08-11: [PASS] nvlink_datapath forced-NVLink rc=0\n"
        "ink50-dh01-h08-11: RESULT ink50-dh01-h08-11: PASS (NVLS-ready)\n"
    )
    rows = parse_nvlink_preflight_output(out, "", ["ink50-dh01-h08-11"])
    assert len(rows) == 1
    assert rows[0]["host"] == "ink50-dh01-h08-11"
    assert rows[0]["status"] == "pass"
