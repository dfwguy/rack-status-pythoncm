# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for pdsh -w expansion helpers (comma splitting + Slurm casing)."""

import contextlib
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import nvis_id_templates as nid
import pytest


@contextlib.contextmanager
def naming_forced_to(n):
    """Patch ``_nvis_naming`` on EVERY module that binds it, discovered rather than listed.

    ``from naming import _nvis_naming`` binds the function object at import time, so patching
    ``naming._nvis_naming`` alone leaves every importer still calling the real one. This test used to
    name its importers by hand -- ``naming``, ``app``, ``validation_launch`` -- and that list needed a
    fourth entry every time a route moved to a new module, failing loudly each time with a wrong regex
    rather than a missing attribute. Discovering the binders means an extraction cannot break it again.
    """
    targets = [
        m
        for name, m in list(sys.modules.items())
        if m is not None
        and getattr(m, "__name__", "").split(".")[0] not in ("nvis_id_templates",)
        and callable(getattr(m, "_nvis_naming", None))
    ]
    assert targets, "expected at least naming itself to bind _nvis_naming"
    with contextlib.ExitStack() as stack:
        for m in targets:
            stack.enter_context(mock.patch.object(m, "_nvis_naming", return_value=n))
        yield


def test_split_pdsh_w_top_level_commas():
    # Lives in node_targets.py since the rack-id -> node-target logic moved out of app.py.
    from node_targets import _split_pdsh_w_top_level_commas

    assert _split_pdsh_w_top_level_commas("") == []
    assert _split_pdsh_w_top_level_commas("  ") == []
    assert _split_pdsh_w_top_level_commas("a[1],b[2]") == ["a[1]", "b[2]"]
    assert _split_pdsh_w_top_level_commas("CBR01-DH01-SVR-CN0102[01,02-03]") == [
        "CBR01-DH01-SVR-CN0102[01,02-03]"
    ]
    assert _split_pdsh_w_top_level_commas("rack[01],rack[02]") == ["rack[01]", "rack[02]"]


def test_expand_pdsh_w_slurm_nodenames_skip_env():
    from node_targets import _expand_pdsh_w_slurm_nodenames

    raw = "CBR01-DH01-SVR-CN0102[01],rack[02]"
    with mock.patch.dict(os.environ, {"RACK_PDSH_W_SKIP_SLURM_EXPAND": "1"}, clear=False):
        assert _expand_pdsh_w_slurm_nodenames(raw) == raw


@pytest.mark.parametrize(
    "slurm_out,expected",
    [
        ("CBR01-DH01-SVR-CN010201\n", "CBR01-DH01-SVR-CN010201"),
        ("Host-A\nHost-B\n", "Host-A,Host-B"),
    ],
)
def test_expand_pdsh_w_slurm_nodenames_uses_scontrol(monkeypatch, slurm_out, expected):
    from node_targets import _expand_pdsh_w_slurm_nodenames

    def fake_run(cmd: str, *, timeout: int = 120):
        class P:
            returncode = 0
            stdout = slurm_out

        return P()

    with mock.patch("app.host_exec.run_slurm_on_host", side_effect=fake_run):
        assert _expand_pdsh_w_slurm_nodenames("anything[01]") == expected


def test_expand_pdsh_w_slurm_nodenames_fallback_on_scontrol_fail():
    from node_targets import _expand_pdsh_w_slurm_nodenames

    def fail(_cmd: str, *, timeout: int = 120):
        class P:
            returncode = 1
            stdout = ""

        return P()

    raw = "unknown-nodeset[01,02]"
    with mock.patch("app.host_exec.run_slurm_on_host", side_effect=fail):
        assert _expand_pdsh_w_slurm_nodenames(raw) == raw


def test_merge_ct_individuals_preserves_rack_token_casing_for_pdsh():
    """Tray specs must keep rack literal casing when Slurm/pdsh need it (regex is IGNORECASE; do not lower the token)."""
    from validation_launch import merge_ct_individuals_for_power

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with naming_forced_to(n):
        mixed = merge_ct_individuals_for_power(
            [
                "CBR01-DH01-SVR-CN0102[01]",
                "cbr01-dh01-svr-cn0102[03]",
            ]
        )

        assert mixed == ["CBR01-DH01-SVR-CN0102[01,03]"]


def test_merge_ct_individuals_preserves_exact_hostname():
    from validation_launch import merge_ct_individuals_for_power

    assert merge_ct_individuals_for_power(["compute-001"]) == ["compute-001"]


def test_validation_target_without_rack_uses_exact_hostname():
    from node_targets import validation_target_for_rack

    with mock.patch(
        "node_targets._all_slurm_nodenames",
        return_value=["compute-001", "compute-002"],
    ):
        assert validation_target_for_rack("", "compute-001") == "compute-001"


@pytest.mark.parametrize("placed", [None, ["DGX-B300-01"]])
def test_translated_rack_validation_uses_actual_scheduler_hostnames(monkeypatch, placed):
    """Members come from the placed inventory; before one exists (``None``, a cold worker), from the
    manager's placement joined to Slurm's roster. Either way, the scheduler's spelling of the host."""
    import config_env
    import naming
    import native_status
    import node_targets
    import slurm_query

    monkeypatch.setattr(config_env, "rack_mapping_mode", lambda: "translate")
    monkeypatch.setattr(native_status, "placed_rack_members", lambda _rack: placed)
    monkeypatch.setattr(naming, "_nvis_naming", lambda: nid.get_nvis_naming("F[41-42]", "dgx-b300-[01-04]", ""))
    monkeypatch.setattr(slurm_query, "_all_slurm_nodenames", lambda: ["DGX-B300-01", "dgx-b300-02"])
    monkeypatch.setattr(slurm_query, "collect_slurm_placement_records", lambda: [
        {"id": "dgx-b300-01", "rack": "F41"},
        {"id": "dgx-b300-02", "rack": "F42"},
        {"id": "dgx-b300-03", "rack": "F41"},  # absent from Slurm
    ])
    monkeypatch.setenv("RACK_TARGET_FROM_INVENTORY", "1")
    assert node_targets.validation_target_for_rack("F41", None) == "DGX-B300-01"


@pytest.mark.parametrize("mode", ["translate", "pool"])
def test_nondirect_rack_validation_never_falls_back_to_invented_hostnames(monkeypatch, mode):
    import config_env
    import node_targets

    monkeypatch.setattr(config_env, "rack_mapping_mode", lambda: mode)
    monkeypatch.setattr(node_targets, "_rack_node_hostnames_from_inventory", lambda _rack: [])
    with pytest.raises(ValueError, match="select explicit node hostnames"):
        node_targets.validation_target_for_rack("F41", None)


def test_query_and_target_primitives_import_without_the_status_orchestrator():
    backend = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.modules['native_status'] = None; import slurm_query; import node_targets"],
        cwd=backend, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("manager", "scheduler"), [
    (manager, scheduler) for manager in ("bcm", "maas")
    for scheduler in ("slurm", "pythoncm", "local", "runai", "slinky", "maas")
    if (manager, scheduler) != ("bcm", "slurm")
])
def test_other_cluster_types_take_rack_members_from_the_placed_inventory(monkeypatch, manager, scheduler):
    """Translate means the hostname names no rack, for every manager and scheduler.

    Only BCM with Slurm fetches placement beside the Slurm roster; every other combination's
    provider already placed the inventory. Either way a rack's members are the placed inventory's,
    never a hostname-prefix match (Nscale NC, BCM with pythoncm: rack Q5 has no ``Q5-`` host).
    """
    import config_env
    import native_status
    import slurm_query

    values = {"RACK_PLATFORM": manager, "RACK_SCHEDULER": scheduler,
              "NVIS_RACK_MAPPING_MODE": "translate"}
    monkeypatch.setattr(config_env, "_eget", lambda key, default=None: values.get(key, default))
    provider = mock.Mock()
    provider.compute_node_inventory.side_effect = AssertionError("unexpected placement query")
    monkeypatch.setattr(slurm_query, "_get_platform", lambda: provider)
    monkeypatch.setattr(slurm_query, "_all_slurm_nodenames", lambda: ["F41-node-01", "gpu7"])
    assert slurm_query.collect_slurm_placement_records() is None
    monkeypatch.setattr(native_status, "placed_rack_members", lambda rack: ["gpu7"] if rack == "F41" else [])
    assert slurm_query._rack_node_hostnames_from_inventory("F41") == ["gpu7"]
    monkeypatch.setattr(native_status, "placed_rack_members", lambda _rack: None)
    assert slurm_query._rack_node_hostnames_from_inventory("F41") == []
    provider.compute_node_inventory.assert_not_called()


@pytest.mark.parametrize(("manager", "mode", "template"), [
    ("maas", "direct", True), ("maas", "translate", False), ("bcm", "translate", False),
])
def test_only_direct_falls_back_to_the_template_target(monkeypatch, manager, mode, template):
    """A template builds ``<rack>-ctNN`` names, which exist only where the hostname encodes the rack."""
    import config_env
    import node_targets

    values = {"RACK_PLATFORM": manager, "RACK_SCHEDULER": "slurm"}
    monkeypatch.setattr(config_env, "_eget", lambda key, default=None: values.get(key, default))
    monkeypatch.setattr(config_env, "rack_mapping_mode", lambda: mode)
    monkeypatch.setattr(node_targets, "_rack_node_hostnames_from_inventory", lambda _rack: [])
    naming = mock.Mock()
    naming.build_validation_target.return_value = "existing-template-target"
    monkeypatch.setattr(node_targets, "_nvis_naming", lambda: naming)
    if template:
        assert node_targets.validation_target_for_rack("F41", None) == "existing-template-target"
    else:
        with pytest.raises(ValueError, match="select explicit node hostnames"):
            node_targets.validation_target_for_rack("F41", None)


def test_grafana_embed_host_regex_prefers_rack_prefix():
    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with naming_forced_to(n):
        r, mode = _grafana_embed_host_regex_and_mode(
            "cbr01-dh01-svr-cn0102",
            ["CBR01-DH01-SVR-CN010211", "CBR01-DH01-SVR-CN010212"],
        )
    assert mode == "rack_prefix"
    assert r == "CBR01-DH01-SVR-CN0102.*"


def test_grafana_embed_rack_prefix_from_slurm_hosts_if_rack_param_invalid():
    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with naming_forced_to(n):
        r, mode = _grafana_embed_host_regex_and_mode(
            "unparsed-rack-label",
            ["CBR01-DH01-SVR-CN010211"],
        )
    assert mode == "rack_prefix"
    assert r == "CBR01-DH01-SVR-CN0102.*"


def test_grafana_rack_embed_regex_fullmatches_full_hostname():
    import re

    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with naming_forced_to(n):
        r, _ = _grafana_embed_host_regex_and_mode("cbr01-dh01-svr-cn0102", [])
    assert re.fullmatch(r, "CBR01-DH01-SVR-CN010211")


def test_grafana_embed_rack_prefix_lower_env():
    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with (
        naming_forced_to(n),
        mock.patch.dict(os.environ, {"RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER": "1"}),
    ):
        r, mode = _grafana_embed_host_regex_and_mode("cbr01-dh01-svr-cn0102", [])
    assert mode == "rack_prefix"
    assert r == "cbr01-dh01-svr-cn0102.*"


def test_grafana_embed_rack_prefix_bright():
    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    with naming_forced_to(n):
        r, mode = _grafana_embed_host_regex_and_mode("p5-r03", ["p5-r03-ct07"])
    assert mode == "rack_prefix"
    assert r == "p5-r03.*"


def test_grafana_embed_host_regex_per_host_env():
    from grafana_embed import _grafana_embed_host_regex_and_mode

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with (
        naming_forced_to(n),
        mock.patch.dict(os.environ, {"RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST": "1"}),
    ):
        r, mode = _grafana_embed_host_regex_and_mode(
            "cbr01-dh01-svr-cn0102",
            ["CBR01-DH01-SVR-CN010211"],
        )
    assert mode == "hosts"
    assert "cn010211" in r.lower()
    assert len(r) > 80


def test_grafana_bright_hostname_promql_regex_includes_verbatim_when_mixed_case():
    from grafana_embed import _grafana_bright_hostname_promql_regex

    r = _grafana_bright_hostname_promql_regex(["CBR01-DH01-SVR-CN010211"])
    assert r"cbr01\-dh01\-svr\-cn010211" in r
    assert r"CBR01\-DH01\-SVR\-CN010211" in r


def test_merge_ct_individuals_bright_tray_is_last_capture_group():
    from validation_launch import merge_ct_individuals_for_power

    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    with naming_forced_to(n):
        out = merge_ct_individuals_for_power(["p5-r03-ct[1]", "p5-r03-ct[18]"])
        assert out == ["p5-r03-ct[1,18]"]
