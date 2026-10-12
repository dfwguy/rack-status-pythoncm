# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A Stage-1 run across racks names every rack it spans, and keeps every host it was given.

On Nscale NC (2026-10-12) a 16+16 U22+U23 selection launched against rack U22 ran on 16 nodes:
the explicit host list was checked against U22's inventory and U23's hosts were dropped as foreign.
The rack check is an authorization boundary (the action is granted per rack), so it stays -- the
launch now names all its racks (``racks``), each is authorized, and a host is kept when it belongs
to any of them.
"""
from __future__ import annotations

import node_targets
import pytest
from api import validation_runs

U22 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(55, 71)]
U23 = [f"sys1-gso1-p-phy-gpu{n}-su19" for n in range(73, 89)]
FOREIGN = "sys1-gso1-p-phy-gpu1-su19"
MEMBERS = {"U22": U22 + ["sys1-gso1-p-phy-gpu71-su19", "sys1-gso1-p-phy-gpu72-su19"], "U23": U23}


def _app_context():
    import app as app_module

    return app_module.app.app_context()


@pytest.fixture(autouse=True)
def _inventory(monkeypatch):
    monkeypatch.setattr(node_targets, "_rack_node_hostnames_from_inventory", lambda r: list(MEMBERS.get(r, [])))
    # NC's rack ids (U22) under this test's default p<N>-r<M> naming template.
    monkeypatch.setattr(validation_runs, "_normalize_rack_id", lambda r: r.strip())
    monkeypatch.delenv("RACK_TARGET_FROM_INVENTORY", raising=False)


def test_a_selection_across_two_named_racks_keeps_both_halves():
    target = node_targets.validation_target_for_racks(["U22", "U23"], ",".join(U22 + U23))
    assert target.split(",") == U22 + U23


def test_a_host_in_no_named_rack_is_still_dropped():
    target = node_targets.validation_target_for_racks(["U22", "U23"], ",".join(U22 + [FOREIGN]))
    assert FOREIGN not in target.split(",") and target.split(",") == U22


def test_naming_one_rack_keeps_the_old_check():
    """The 16-node outcome is what one named rack still means: the other rack is not authorized."""
    target = node_targets.validation_target_for_racks(["U22"], ",".join(U22 + U23))
    assert target.split(",") == U22


def test_a_template_cannot_span_racks_and_no_selection_means_whole_racks():
    with pytest.raises(ValueError, match="template targets one rack"):
        node_targets.validation_target_for_racks(["U22", "U23"], "U22-ct[01-18]")
    assert node_targets.validation_target_for_racks(["U22", "U23"], None).split(",") == MEMBERS["U22"] + U23


def test_the_stage1_prepare_runs_every_selected_host_across_the_named_racks(monkeypatch):
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda raw: "/logs")
    monkeypatch.setattr(validation_runs, "_script_exists", lambda p: True)
    monkeypatch.setattr(validation_runs, "_llmb_env_updates", lambda body, scope: {"SCOPE": scope})
    monkeypatch.setattr(node_targets, "nccl_env_scope_for_target", lambda t: "multirack")
    monkeypatch.setattr(validation_runs, "nccl_env_scope_for_target", lambda t: "multirack")
    with _app_context():
        prep, err = validation_runs._prepare_run_nccl_fullrack(
            {"rack": "U22", "racks": ["U23"], "target": ",".join(U22 + U23)}
        )
    assert err is None
    assert prep["racks"] == ["U22", "U23"]
    assert prep["cmd"][prep["cmd"].index("--nodelist") + 1].split(",") == U22 + U23
    assert prep["env_updates"]["SCOPE"] == "multirack"


def test_a_bad_racks_value_is_a_400(monkeypatch):
    monkeypatch.setattr(validation_runs, "_resolve_log_dir", lambda raw: "/logs")
    with _app_context():
        _prep, err = validation_runs._prepare_run_nccl_fullrack({"rack": "U22", "racks": "U23"})
    assert err is not None and err[1] == 400
