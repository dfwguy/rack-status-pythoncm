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
"""Acceptance campaigns: a pinned roster of exact nodes on the cluster record.

The defect these replace is worth stating, because it is the reason the version is derived
rather than declared. A campaign used to be two environment variables — an id, and a path to
a newline-separated file — and a launch checked only that the caller's id matched. So editing
the file changed which hardware ran while the id stayed put: the check passed, and the audit
trail recorded two different node sets under one name.

A digest of the roster cannot disagree with the roster, which is the same repair as
`identity-auth-traps.mdc` §1 — fix the value so the comparison cannot be wrong, rather than
adding a check around a value that can be.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catalog import models


def _campaign(**kw) -> models.ClusterCampaign:
    kw.setdefault("id", "wave1")
    kw.setdefault("nodes", ["ink50-dh01-h18-01", "ink50-dh01-h18-02"])
    return models.ClusterCampaign(**kw)


class TestTheVersionIsDerivedNotDeclared:
    def test_there_is_no_version_field_to_set(self):
        # A stored version is a value that can disagree with what it describes: edit the
        # roster, forget the bump, and every consumer trusts the previous list. The absence
        # of the field is the design, so this asserts it rather than assuming it.
        assert not hasattr(_campaign(), "version")
        assert "version" not in models.ClusterCampaign.__dataclass_fields__

    def test_changing_the_roster_changes_the_version_by_itself(self):
        before = _campaign().fingerprint()
        after = _campaign(nodes=["ink50-dh01-h18-01", "ink50-dh01-h18-03"]).fingerprint()
        assert before != after

    def test_adding_a_node_changes_it(self):
        grown = _campaign(nodes=[*_campaign().nodes, "ink50-dh01-h18-03"])
        assert grown.fingerprint() != _campaign().fingerprint()

    def test_reordering_or_recasing_does_not(self):
        # Neither the order a site lists nodes in nor the spelling it uses changes which
        # hardware was reviewed, so neither should invalidate a page an operator is reading.
        shuffled = _campaign(nodes=["INK50-DH01-H18-02", "ink50-dh01-h18-01"])
        assert shuffled.fingerprint() == _campaign().fingerprint()

    def test_it_is_short_enough_to_read_aloud(self):
        # It appears in a 409 message an operator has to compare by eye.
        assert len(_campaign().fingerprint()) == 16


class TestWhatACampaignRefuses:
    def test_an_empty_roster_is_invalid(self):
        # It would pin nothing while looking like a control.
        assert "no nodes" in " ".join(_campaign(nodes=[]).validate())

    def test_a_duplicate_node_is_invalid(self):
        # It would run the work twice on one node and inflate the node count the acceptance
        # record reports.
        problems = _campaign(nodes=["n01", "N01"]).validate()
        assert "more than once" in " ".join(problems)

    def test_a_bad_id_is_invalid(self):
        assert "invalid campaign id" in " ".join(_campaign(id="not a slug").validate())

    def test_a_valid_campaign_has_no_problems(self):
        assert _campaign().validate() == []


class TestItLivesOnTheClusterRecord:
    """Which is the point: an env var pointing at one host's file is not portable."""

    def test_a_cluster_round_trips_its_campaigns(self):
        cluster = models.Cluster.from_dict({
            "id": "c1", "name": "C1",
            "campaigns": [
                {"id": "wave1", "nodes": ["n01", "n02"], "nodes_per_rack": 2, "notes": "bring-up"},
            ],
        })
        assert cluster.validate() == []
        assert cluster.campaign("wave1").nodes == ["n01", "n02"]
        assert cluster.campaign("wave1").nodes_per_rack == 2
        # Survives a write/read cycle through the document store's dict form.
        again = models.Cluster.from_dict(cluster.to_dict())
        assert again.campaign("wave1").fingerprint() == cluster.campaign("wave1").fingerprint()

    def test_a_site_may_keep_several_waves(self):
        cluster = models.Cluster.from_dict({
            "id": "c1", "name": "C1",
            "campaigns": [
                {"id": "wave1", "nodes": ["n01"]},
                {"id": "wave2", "nodes": ["n02"]},
            ],
        })
        assert cluster.validate() == []
        assert cluster.campaign("wave2").nodes == ["n02"]

    def test_two_campaigns_cannot_share_an_id(self):
        # It would make "which roster ran" unanswerable, which is the one question a
        # campaign exists to answer.
        cluster = models.Cluster.from_dict({
            "id": "c1", "name": "C1",
            "campaigns": [{"id": "wave1", "nodes": ["n01"]}, {"id": "wave1", "nodes": ["n02"]}],
        })
        assert "duplicate campaign id" in " ".join(cluster.validate())

    def test_a_cluster_with_no_campaigns_is_still_valid(self):
        # Most clusters never run a pinned acceptance wave.
        assert models.Cluster.from_dict({"id": "c1", "name": "C1"}).validate() == []
        assert models.Cluster.from_dict({"id": "c1", "name": "C1"}).campaign("x") is None

    def test_an_unknown_id_is_none_rather_than_a_guess(self):
        cluster = models.Cluster.from_dict({
            "id": "c1", "name": "C1", "campaigns": [{"id": "wave1", "nodes": ["n01"]}],
        })
        assert cluster.campaign("wave9") is None


class TestSizeIsNotAProblemForTheDocumentStore:
    def test_a_two_thousand_node_roster_stays_small(self):
        # The join test puts this in the document store, and the thing to check is that a
        # whole-file rewrite stays cheap: `catalog.json` is rewritten on every write.
        import json

        big = _campaign(nodes=[f"ink50-dh{d:02d}-h{r:02d}-{n:02d}"
                               for d in (1, 2) for r in range(1, 15) for n in range(1, 19)])
        assert len(big.nodes) > 400
        assert big.validate() == []
        assert len(json.dumps(models.Cluster(id="c", name="c", campaigns=[big]).to_dict())) < 200_000


@pytest.mark.parametrize("nodes_per_rack", [-1, -16])
def test_a_negative_nodes_per_rack_is_invalid(nodes_per_rack):
    assert "cannot be negative" in " ".join(_campaign(nodes_per_rack=nodes_per_rack).validate())


class TestSharedImagesAreRerootedPerSite:
    """A mount point is a site fact; which image is in it is the cluster's image catalog.

    `/cluster/containers` was hard-coded into five image paths on both sides of the launch,
    so a preset was unrunnable anywhere but Nscale. The catalog names the file; the site's
    root says where it lives.
    """

    def test_the_default_root_holds_the_catalog_file(self, monkeypatch, preset_image_catalog):
        import validation_presets

        monkeypatch.setattr(validation_presets, "shared_container_root", lambda: "/cluster/containers")
        image = validation_presets.launch_image("nemo-26.04.01")
        assert image.source == "/cluster/containers/nvidia+nemo+26.04.01-arm.sqsh"

    def test_a_site_root_moves_only_the_directory(self, monkeypatch, preset_image_catalog):
        import validation_presets

        monkeypatch.setattr(validation_presets, "shared_container_root", lambda: "/raid/images/")
        image = validation_presets.launch_image("nemo-26.04.01")
        # A trailing slash on the root does not double up.
        assert image.source == "/raid/images/nvidia+nemo+26.04.01-arm.sqsh"

    def test_llmb_presets_read_their_own_store(self, monkeypatch, preset_image_catalog):
        import validation_presets

        monkeypatch.setattr(validation_presets, "llmb_image_root", lambda: "/site/llmb/images")
        image = validation_presets.launch_image("gpt-oss-cpu", "llmb")
        assert image.source == "/site/llmb/images/tensorrt-llm+release+1.1.0rc5.sqsh"

    def test_no_preset_still_hard_codes_a_site_path(self):
        # The regression this closes: five absolute `/cluster/...` paths that only existed on
        # one customer's filesystem. The declared defaults are allowed; literals are not.
        import validation_presets

        source = Path(validation_presets.__file__).read_text(encoding="utf-8")
        literals = [
            line for line in source.splitlines()
            if '"/cluster/' in line and "DEFAULT_" not in line
        ]
        assert literals == []
