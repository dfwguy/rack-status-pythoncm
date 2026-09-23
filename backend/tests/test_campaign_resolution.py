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
"""Resolving a campaign: the cluster record first, the legacy manifest second.

One resolver with a stated order, not two callers each choosing a source. That is the
mistake `paths` and `workflow_engine` made with script directories — two resolvers agreed
until one learned something, and then the API and a workflow step ran different copies of
the same script.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import naming as naming_mod
import nvis_id_templates as nid
import validation_launch
from catalog import models

#: Real Nscale TX shapes, so the naming parse under test is the one production uses.
_NODES = ["ink50-dh01-h18-01", "ink50-dh01-h18-02"]
_RACK_TMPL = "ink[01-99]-dh[01-99]-[a-z][01-99]"
_CT_TMPL = "ink[01-99]-dh[01-99]-[a-z][01-99]-[01-99]"


def _cluster(campaigns):
    return models.Cluster.from_dict({
        "id": "nscale-tx", "name": "Nscale TX", "campaigns": campaigns,
    })


class Harness:
    """Holds the cluster and the settings the resolver reads."""

    def __init__(self):
        self.cluster = _cluster([])
        self.env: dict[str, str] = {}

    def resolve(self):
        return validation_launch._multirack_campaign_manifest()


@pytest.fixture
def harness(monkeypatch):
    """Inject a cluster, a working naming convention, and settings.

    Settings go through a patched `_eget` rather than `monkeypatch.setenv`. `_eget`
    consults the catalog active context, which re-materializes `os.environ` from the
    baseline captured when `app` was imported — so a variable set afterwards is silently
    reverted mid-call, and the test would assert the default while appearing to configure
    something. Same reason `test_multirack_nemo_target` patches accessors.
    """
    h = Harness()
    monkeypatch.setattr(
        validation_launch.catalog_context, "resolve", lambda *a, **k: (None, h.cluster, None)
    )
    naming = nid.get_nvis_naming(_RACK_TMPL, _CT_TMPL)
    monkeypatch.setattr(validation_launch, "_nvis_naming", lambda: naming)
    # `_normalize_rack_id` resolves its own naming object from `naming`, so patching only
    # the accessor imported into `validation_launch` leaves rack normalisation on the
    # default `p[\d+]-r[\d+]` template and every real hostname is rejected.
    monkeypatch.setattr(naming_mod, "_nvis_naming", lambda: naming)
    monkeypatch.setattr(validation_launch, "_eget", lambda n, d="": h.env.get(n, d))
    monkeypatch.setattr(
        validation_launch, "_eget_int", lambda n, d=0: int(h.env.get(n, d) or d)
    )
    return h


class TestTheClusterRecordWins:
    def test_a_single_campaign_needs_no_naming(self, harness):
        # Having to name the only option would be ceremony.
        harness.cluster = _cluster([{"id": "wave1", "nodes": _NODES}])
        snapshot = harness.resolve()
        assert snapshot["active"] is True
        assert snapshot["id"] == "wave1"
        assert snapshot["source"] == "catalog"
        assert snapshot["rack_nodes"] == {"ink50-dh01-h18": _NODES}

    def test_an_env_id_selects_between_waves(self, harness):
        # Switching wave is a setting, not a code change.
        harness.cluster = _cluster([
            {"id": "wave1", "nodes": _NODES},
            {"id": "wave2", "nodes": ["ink50-dh01-h19-01"]},
        ])
        harness.env["RACK_MULTIRACK_CAMPAIGN_ID"] = "wave2"
        snapshot = harness.resolve()
        assert snapshot["id"] == "wave2"
        assert snapshot["rack_nodes"] == {"ink50-dh01-h19": ["ink50-dh01-h19-01"]}

    def test_several_waves_without_a_named_one_is_not_a_guess(self, harness):
        # Picking the first of several would silently validate the wrong hardware.
        harness.cluster = _cluster([
            {"id": "wave1", "nodes": _NODES},
            {"id": "wave2", "nodes": ["ink50-dh01-h19-01"]},
        ])
        assert harness.resolve()["active"] is False

    def test_it_carries_a_version_derived_from_the_roster(self, harness):
        harness.cluster = _cluster([{"id": "wave1", "nodes": _NODES}])
        expected = models.ClusterCampaign(id="wave1", nodes=_NODES).fingerprint()
        assert harness.resolve()["version"] == expected

    def test_a_malformed_roster_is_refused_rather_than_run(self, harness):
        harness.cluster = _cluster([{"id": "wave1", "nodes": ["n01", "N01"]}])
        with pytest.raises(RuntimeError, match="more than once"):
            harness.resolve()

    def test_nodes_per_rack_is_enforced_when_stated(self, harness):
        # A bring-up that reviewed 18 trays wants a 2-node rack to be a refusal.
        harness.cluster = _cluster([{"id": "wave1", "nodes": _NODES, "nodes_per_rack": 18}])
        with pytest.raises(RuntimeError, match="exactly 18 nodes per rack"):
            harness.resolve()

    def test_zero_nodes_per_rack_does_not_check(self, harness):
        # A partly populated rack is a real thing during bring-up.
        harness.cluster = _cluster([{"id": "wave1", "nodes": _NODES, "nodes_per_rack": 0}])
        assert harness.resolve()["active"] is True

    def test_a_non_compute_hostname_is_refused(self, harness):
        harness.cluster = _cluster([{"id": "wave1", "nodes": ["not-a-node"]}])
        with pytest.raises(RuntimeError, match="invalid compute node"):
            harness.resolve()


class TestTheLegacyManifestStillWorks:
    """Nscale TX runs on it today, so it keeps answering — second, and it says so."""

    @staticmethod
    def _manifest(harness, tmp_path, body: str) -> Path:
        path = tmp_path / "roster.txt"
        path.write_text(body, encoding="utf-8")
        harness.env["RACK_MULTIRACK_CAMPAIGN_ID"] = "exemplar-16"
        harness.env["RACK_MULTIRACK_CAMPAIGN_MANIFEST"] = f"local:{path}"
        harness.env["RACK_MULTIRACK_CAMPAIGN_NODES_PER_RACK"] = "2"
        return path

    def test_it_is_used_when_the_cluster_keeps_no_campaigns(self, harness, tmp_path):
        self._manifest(harness, tmp_path, "\n".join(_NODES) + "\n")
        snapshot = harness.resolve()
        assert snapshot["active"] is True
        assert snapshot["source"] == "manifest"
        assert snapshot["id"] == "exemplar-16"

    def test_the_manifest_also_gets_a_content_version(self, harness, tmp_path):
        # This is where the version earns its keep: the id is a setting and the roster is a
        # separate file, so an edit used to change the nodes while the id stayed put.
        path = self._manifest(harness, tmp_path, "\n".join(_NODES) + "\n")
        before = harness.resolve()

        path.write_text("ink50-dh01-h18-01\nink50-dh01-h18-03\n", encoding="utf-8")
        after = harness.resolve()

        assert before["id"] == after["id"] == "exemplar-16"
        # Same id, different hardware — which the old id-only check could not see at all.
        assert before["version"] != after["version"]

    def test_comments_and_blank_lines_are_ignored(self, harness, tmp_path):
        self._manifest(harness, tmp_path, f"# wave 1\n\n{_NODES[0]}\n  {_NODES[1]}  \n")
        assert harness.resolve()["rack_nodes"] == {"ink50-dh01-h18": _NODES}

    def test_the_cluster_record_takes_precedence_over_a_manifest(self, harness, tmp_path):
        # Both configured: the record wins, because it is the portable one.
        self._manifest(harness, tmp_path, "\n".join(_NODES) + "\n")
        harness.cluster = _cluster([{"id": "exemplar-16", "nodes": ["ink50-dh01-h19-01"]}])
        snapshot = harness.resolve()
        assert snapshot["source"] == "catalog"
        assert snapshot["rack_nodes"] == {"ink50-dh01-h19": ["ink50-dh01-h19-01"]}

    def test_neither_source_means_inactive_not_an_error(self, harness):
        # Most clusters never pin a roster, and that is not a fault.
        assert harness.resolve()["active"] is False

    def test_an_unreadable_catalog_falls_through_rather_than_failing(
        self, harness, tmp_path, monkeypatch
    ):
        # The resolver must not turn a catalog problem into "no campaign exists anywhere".
        def boom(*_a, **_k):
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr(validation_launch.catalog_context, "resolve", boom)
        self._manifest(harness, tmp_path, "\n".join(_NODES) + "\n")
        assert harness.resolve()["source"] == "manifest"
