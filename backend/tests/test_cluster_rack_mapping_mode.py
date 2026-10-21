# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""How a node maps to the floor is a property of the cluster, not of the hostname parser.

Most clusters embed the rack id in the hostname (`bos01-a05-11-...`), so the naming templates
parse row/rack/slot straight out of it -- `direct`. Nscale NC does not: its hostnames are
`sys1-gso1-p-phy-gpu1-su19`, the rack (`U19`) lives only as a BCM device attribute, and the
SU<->rack correspondence is arbitrary site cabling. That cluster is `translate`: rack/slot come
from the provider and the SU grouping from the cluster's own `scalable_units` list. This field
carries that choice on the cluster record so one instance serves both kinds.
"""
from __future__ import annotations

import pytest
from catalog import context, models


def _cluster(**kw):
    base = {"id": "c1", "name": "C1"}
    base.update(kw)
    return models.Cluster.from_dict(base)


def _su(su_id, *racks):
    return {"id": su_id, "label": su_id.upper(), "racks": list(racks)}


class TestTheField:
    def test_the_default_is_direct(self):
        """Every existing cluster keeps hostname-parsed placement without touching its record."""
        assert _cluster().rack_mapping_mode == "direct"

    def test_it_is_case_folded(self):
        assert _cluster(rack_mapping_mode="TRANSLATE").rack_mapping_mode == "translate"

    def test_a_blank_value_falls_back_to_direct(self):
        assert _cluster(rack_mapping_mode="").rack_mapping_mode == "direct"

    def test_a_known_mode_validates(self):
        assert _cluster(rack_mapping_mode="direct").validate() == []
        assert _cluster(
            rack_mapping_mode="translate", scalable_units=[_su("su19", "U19", "U20")]
        ).validate() == []

    def test_an_unknown_mode_is_refused(self):
        problems = _cluster(rack_mapping_mode="bcm").validate()
        assert any("unknown rack_mapping_mode" in p for p in problems), problems

    def test_it_round_trips(self):
        assert _cluster(rack_mapping_mode="translate").to_dict()["rack_mapping_mode"] == "translate"


class TestTranslateSuGrouping:
    def test_translate_without_scalable_units_is_accepted(self):
        """BCM physical placement does not imply a fabric scalable unit."""
        assert _cluster(rack_mapping_mode="translate").validate() == []

    @pytest.mark.parametrize(("manager", "scheduler"), [
        (manager, scheduler) for manager in models.CLUSTER_MANAGERS for scheduler in models.SCHEDULERS
        if (manager, scheduler) != ("bcm", "slurm")
    ])
    def test_other_cluster_types_still_require_sus(self, manager, scheduler):
        problems = _cluster(
            cluster_manager=manager, scheduler=scheduler, rack_mapping_mode="translate",
        ).validate()
        assert any("requires a scalable_units" in p for p in problems)

    def test_translate_with_scalable_units_is_accepted(self):
        c = _cluster(
            rack_mapping_mode="translate",
            naming={"rack_id_template": r"[A-Z][\d+]"},
            scalable_units=[_su("su19", "U19", "U20"), _su("su26", "R2", "R3")],
        )
        assert c.validate() == []

    def test_direct_mode_does_not_require_scalable_units(self):
        assert _cluster(rack_mapping_mode="direct").validate() == []

    def test_a_rack_in_two_sus_is_still_refused_in_translate(self):
        """The clean-partition rule still applies: one rack cannot belong to two SUs."""
        problems = _cluster(
            rack_mapping_mode="translate",
            scalable_units=[_su("su19", "U19"), _su("su26", "U19")],
        ).validate()
        assert any("U19" in p and "scalable unit" in p for p in problems), problems


class TestItReachesTheOverlay:
    def test_the_overlay_publishes_the_mode_native_status_reads(self):
        """native_status reads NVIS_RACK_MAPPING_MODE; the cluster record is what sets it, so
        the choice travels with the active cluster rather than being process-global."""
        env = context.high_overlay(None, _cluster(rack_mapping_mode="translate"), None)
        assert env["NVIS_RACK_MAPPING_MODE"] == "translate"

    def test_direct_is_published_too(self):
        env = context.high_overlay(None, _cluster(rack_mapping_mode="direct"), None)
        assert env["NVIS_RACK_MAPPING_MODE"] == "direct"
