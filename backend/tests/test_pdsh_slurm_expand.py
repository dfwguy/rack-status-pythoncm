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
import sys
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
    from app import merge_ct_individuals_for_power

    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    with naming_forced_to(n):
        mixed = merge_ct_individuals_for_power(
            [
                "CBR01-DH01-SVR-CN0102[01]",
                "cbr01-dh01-svr-cn0102[03]",
            ]
        )
        assert mixed == ["CBR01-DH01-SVR-CN0102[01,03]"]


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
    with naming_forced_to(n):
        with mock.patch.dict(os.environ, {"RACK_WEB_GRAFANA_EMBED_RACK_REGEX_LOWER": "1"}):
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
    with naming_forced_to(n):
        with mock.patch.dict(os.environ, {"RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST": "1"}):
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
    from app import merge_ct_individuals_for_power

    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    with naming_forced_to(n):
        out = merge_ct_individuals_for_power(["p5-r03-ct[1]", "p5-r03-ct[18]"])
        assert out == ["p5-r03-ct[1,18]"]
