# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A ``cmsh ds`` state belongs to the device named before the bracket, not to hosts in the message.

Nscale NC, 2026-10-20: ``gpu93-su35 ... [ DOWN ] (... failed to send jobs to
sys1-gso1-p-phy-gpu10-su23)`` painted the healthy gpu10-su23 (``[ UP ], health check failed``,
Slurm idle) down, which kept it -- and its rack -- out of multi-rack selection.
"""
import native_status as ns
import nvis_id_templates as ids
import pytest

NC_DS = (
    "sys1-gso1-p-phy-gpu10-su23 ...... [   UP   ], health check failed\n"
    "sys1-gso1-p-phy-gpu10-su23-bf3 .. [   UP   ]\n"
    "sys1-gso1-p-phy-gpu93-su35 ...... [  DOWN  ] (An error occurred while provisioning. "
    "failed to send jobs to sys1-gso1-p-phy-gpu10-su23)\n"
)
GPU10 = "sys1-gso1-p-phy-gpu10-su23"
GPU93 = "sys1-gso1-p-phy-gpu93-su35"


@pytest.mark.parametrize(("line", "want"), [
    ("h1 .... [   UP   ]", ("h1 .... ", "up")),
    ("h1 .... [   UP   ], health check failed", ("h1 .... ", "up")),
    ("h1 .... [  DOWN  ] (failed to send jobs to h2)", ("h1 .... ", "down")),
    ("h1 .... [ UNREACHABLE ]", ("h1 .... ", "down")),
    ("h1 .... [ INSTALLER_FAILED ]", ("h1 .... ", "inst")),
    ("[ UP ] gpu01", (" gpu01", "up")),
    ("[ DOWN ] management01", (" management01", "down")),
    ("no status on this line", None),
    ("", None),
])
def test_a_line_yields_the_device_text_before_the_bracket_and_its_state(line, want):
    assert ns._ds_line_device_state(line) == want


@pytest.mark.parametrize("ds", [NC_DS, "\n".join(reversed(NC_DS.splitlines()))])
def test_a_host_named_in_another_devices_message_keeps_its_own_state(monkeypatch, ds):
    monkeypatch.setattr(ns, "_hostname_encodes_rack", lambda: False)
    inv = {n["id"]: n for n in ns.build_node_inventory({GPU10: "idle", GPU93: "down*"}, [], ds)}
    assert inv[GPU10]["status"] == "up"
    assert inv[GPU93]["status"] == "down"


def test_the_rack_column_counts_only_the_device_before_the_bracket(monkeypatch):
    naming = ids.get_nvis_naming(ids.DEFAULT_NVIS_RACK_ID, ids.DEFAULT_NVIS_CT_ID, ids.DEFAULT_NVIS_NVSW_ID)
    monkeypatch.setattr(ns, "_legacy_ds_configured", lambda: False)
    monkeypatch.setattr(ns, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(ns, "_nvis_rack_id_template", lambda: ids.DEFAULT_NVIS_RACK_ID)
    monkeypatch.setattr(ns, "_nvis_ct_id_template", lambda: ids.DEFAULT_NVIS_CT_ID)
    ds = ("p5-r01-ct01 ...... [   UP   ], health check failed\n"
          "p5-r01-ct02 ...... [  DOWN  ] (failed to send jobs to p5-r01-ct01)\n")
    # Before the fix ct01 was also listed under DN, read out of ct02's message.
    assert ns.parse_cmsh_ds_to_ct_column_by_rack(ds) == {"p5-r01": "DN:p5-r01-ct[2];UP:p5-r01-ct[1]"}


def test_the_legacy_token_pattern_also_ignores_the_message(monkeypatch):
    monkeypatch.setattr(ns, "_legacy_ds_configured", lambda: True)
    monkeypatch.setattr(ns, "_native_env_ds", lambda key, default: {
        "RACK_CMSH_DS_NODE_TOKEN_PATTERN": r"(p\d+-r\d+-ct\d+)"}.get(key, default))
    naming = ids.get_nvis_naming(ids.DEFAULT_NVIS_RACK_ID, ids.DEFAULT_NVIS_CT_ID, ids.DEFAULT_NVIS_NVSW_ID)
    monkeypatch.setattr(ns, "_nvis_naming", lambda: naming)
    ds = ("p5-r01-ct01 ...... [   UP   ], health check failed\n"
          "p5-r01-ct02 ...... [  DOWN  ] (failed to send jobs to p5-r01-ct01)\n")
    # The legacy path keeps the tray tokens as written (zero-padded).
    assert ns.parse_cmsh_ds_to_ct_column_by_rack(ds) == {"p5-r01": "DN:p5-r01-ct[02];UP:p5-r01-ct[01]"}
