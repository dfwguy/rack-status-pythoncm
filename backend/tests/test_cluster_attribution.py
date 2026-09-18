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
"""Cluster identity must come from config, never from one site's literals.

Both behaviours here were pinned to the Nscale TX deployment: job attempts attributed racks
with a hardcoded ``ink50`` regex, and every lake record was tagged ``NscaleTX``. On any other
cluster that produced silently empty rack/node fields, and provenance filed under the wrong
customer — which is precisely what the lake exists to prevent.
"""
import lake_sink
import nvis_id_templates as nid
import pytest
from api import parser_results as app_mod

NSCALE = (r"ink50-dh01-h[\d+]", r"ink50-dh01-h[\d+]-[\d+]")
BRIGHT = (nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)

_LAKE_CLUSTER_VARS = ("RACK_LAKE_CLUSTER", "RACK_CLUSTER_NAME", "RACK_CLUSTER_CUSTOMER_NAME")


def _use_naming(monkeypatch, rack: str, ct: str) -> None:
    monkeypatch.setenv("NVIS_RACK_ID", rack)
    monkeypatch.setenv("NVIS_CT_ID", ct)
    nid.clear_nvis_naming_cache()


def test_compact_nodelist_is_expanded_before_attribution(monkeypatch):
    """Slurm reports ``<rack>-[01-18]``; the record needs real hostnames."""
    _use_naming(monkeypatch, *NSCALE)
    rack, nodes = app_mod._rack_and_nodes_from_nodelist("ink50-dh01-h01-[01-18]")
    assert rack == "ink50-dh01-h01"
    assert len(nodes) == 18
    assert nodes[0] == "ink50-dh01-h01-01"
    assert nodes[-1] == "ink50-dh01-h01-18"


def test_attribution_follows_configured_naming(monkeypatch):
    """The regression: another site's convention must attribute, not come back empty."""
    _use_naming(monkeypatch, *BRIGHT)
    rack, nodes = app_mod._rack_and_nodes_from_nodelist("p1-r5-ct3,p1-r5-ct4")
    assert nodes == ["p1-r5-ct3", "p1-r5-ct4"]
    assert rack != ""
    assert rack.startswith("p1-r5")


@pytest.mark.parametrize("spec", ["", "   ", "(null)", "None assigned"])
def test_job_without_an_allocation_reports_no_nodes(monkeypatch, spec):
    _use_naming(monkeypatch, *NSCALE)
    assert app_mod._rack_and_nodes_from_nodelist(spec) == ("", [])


def test_unrecognised_convention_still_surfaces_its_nodes(monkeypatch):
    """Better an unattributed rack than a job that appears to have run nowhere."""
    _use_naming(monkeypatch, *NSCALE)
    rack, nodes = app_mod._rack_and_nodes_from_nodelist("odd-host-1,odd-host-2")
    assert rack == ""
    assert nodes == ["odd-host-1", "odd-host-2"]


def test_lake_cluster_prefers_the_explicit_override(monkeypatch):
    monkeypatch.setenv("RACK_LAKE_CLUSTER", "explicit-name")
    monkeypatch.setenv("RACK_CLUSTER_NAME", "catalog-cluster")
    assert lake_sink.cluster() == "explicit-name"


def test_lake_cluster_falls_back_to_the_active_catalog(monkeypatch):
    monkeypatch.delenv("RACK_LAKE_CLUSTER", raising=False)
    monkeypatch.setenv("RACK_CLUSTER_NAME", "catalog-cluster")
    assert lake_sink.cluster() == "catalog-cluster"


def test_lake_cluster_uses_the_customer_when_the_cluster_is_unnamed(monkeypatch):
    monkeypatch.delenv("RACK_LAKE_CLUSTER", raising=False)
    monkeypatch.delenv("RACK_CLUSTER_NAME", raising=False)
    monkeypatch.setenv("RACK_CLUSTER_CUSTOMER_NAME", "ACME-Labs")
    assert lake_sink.cluster() == "ACME-Labs"


def test_lake_cluster_labels_an_unknown_cluster_rather_than_mislabelling_it(monkeypatch):
    for var in _LAKE_CLUSTER_VARS:
        monkeypatch.delenv(var, raising=False)
    tag = lake_sink.cluster()
    assert tag == "unknown-cluster"
    # Upstream validates the cluster tag with the same pattern it uses for run ids.
    assert lake_sink._RUN_ID_RE.match(tag)
