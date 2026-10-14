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
"""`/api/status` carries this cluster's Scalable Unit grouping.

Sent from the backend because an SU map is a **site fact**. The implementation this was
ported from derived it in the browser from a fourteen-entry row-to-SU table hard-coded in
TypeScript: correct for one customer's floor, and silently wrong on every other.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import dashboard
from catalog import models


def _payload(racks):
    return {"rows": [{"rack": rack} for rack in racks]}


def _resolve(monkeypatch, cluster, racks_per_su):
    platform = models.Platform.from_dict({
        "id": "p1", "name": "P1", "fabric": {"racks_per_su": racks_per_su},
    })
    monkeypatch.setattr(
        dashboard, "_nvis_naming", dashboard._nvis_naming, raising=False
    )
    monkeypatch.setattr(
        "catalog.context.resolve", lambda *a, **k: (None, cluster, platform)
    )


class TestDerivedFromThePlatform:
    def test_racks_are_chunked_into_sus(self, monkeypatch):
        cluster = models.Cluster.from_dict({"id": "c", "name": "C"})
        _resolve(monkeypatch, cluster, 2)
        payload = _payload(["r01", "r02", "r03", "r04"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert [u["id"] for u in payload["scalable_units"]] == ["su01", "su02"]
        assert payload["scalable_units"][0]["racks"] == ["r01", "r02"]

    def test_a_partial_final_su_is_kept(self, monkeypatch):
        # A half-populated SU is a real thing on a floor mid-bring-up.
        cluster = models.Cluster.from_dict({"id": "c", "name": "C"})
        _resolve(monkeypatch, cluster, 2)
        payload = _payload(["r01", "r02", "r03"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert payload["scalable_units"][-1]["racks"] == ["r03"]

    def test_a_platform_with_no_su_model_publishes_nothing(self, monkeypatch):
        # Absent must mean "no SU model", never "one SU holding everything" -- the latter
        # would draw a confidently wrong diagram.
        cluster = models.Cluster.from_dict({"id": "c", "name": "C"})
        _resolve(monkeypatch, cluster, 0)
        payload = _payload(["r01", "r02"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert "scalable_units" not in payload


class TestTheClusterRecordOverridesTheDerivation:
    def test_an_explicit_map_is_used_verbatim(self, monkeypatch):
        # Nscale TX numbers SU1-SU14 across two `dh` rows, which no chunking reproduces.
        cluster = models.Cluster.from_dict({
            "id": "c", "name": "C",
            "scalable_units": [
                {"id": "su01", "label": "SU1", "racks": ["r03", "r01"]},
                {"id": "su02", "label": "SU2", "racks": ["r02"]},
            ],
        })
        _resolve(monkeypatch, cluster, 2)
        payload = _payload(["r01", "r02", "r03"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        units = payload["scalable_units"]
        assert [u["label"] for u in units] == ["SU1", "SU2"]
        # Order within the SU is the site's, not re-sorted: it is how they label the floor.
        assert units[0]["racks"] == ["r03", "r01"]

    def test_a_rack_the_snapshot_does_not_have_is_filtered_out(self, monkeypatch):
        # The record can run ahead of the floor, or a rack can be absent from a scan.
        # Naming it anyway would draw a tile with nothing behind it.
        cluster = models.Cluster.from_dict({
            "id": "c", "name": "C",
            "scalable_units": [{"id": "su01", "label": "SU1", "racks": ["r01", "r99"]}],
        })
        _resolve(monkeypatch, cluster, 0)
        payload = _payload(["r01"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert payload["scalable_units"][0]["racks"] == ["r01"]

    def test_an_su_left_with_no_present_racks_is_still_listed_as_empty(self, monkeypatch):
        # It is listed with an empty rack list rather than dropped: the frontend decides
        # whether to draw it, and an SU that exists but is absent from a scan is a fact.
        cluster = models.Cluster.from_dict({
            "id": "c", "name": "C",
            "scalable_units": [{"id": "su09", "label": "SU9", "racks": ["r99"]}],
        })
        _resolve(monkeypatch, cluster, 0)
        payload = _payload(["r01"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert payload["scalable_units"] == [
            {"id": "su09", "label": "SU9", "racks": [], "handed_over_at": ""}
        ]


class TestItNeverBreaksStatus:
    def test_no_active_cluster_is_silent(self, monkeypatch):
        monkeypatch.setattr("catalog.context.resolve", lambda *a, **k: (None, None, None))
        payload = _payload(["r01"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert "scalable_units" not in payload

    def test_a_catalog_failure_is_logged_not_raised(self, monkeypatch):
        # The fleet dashboard must render without an SU map; this is additive.
        def boom(*_a, **_k):
            raise RuntimeError("catalog unavailable")

        monkeypatch.setattr("catalog.context.resolve", boom)
        payload = _payload(["r01"])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert "scalable_units" not in payload

    def test_an_empty_snapshot_is_not_an_error(self, monkeypatch):
        cluster = models.Cluster.from_dict({"id": "c", "name": "C"})
        _resolve(monkeypatch, cluster, 8)
        payload = _payload([])
        dashboard._merge_scalable_units_into_status_payload(payload)
        assert "scalable_units" not in payload


@pytest.mark.parametrize("racks_per_su,expected", [(8, 3), (18, 2), (1, 20)])
def test_su_count_follows_the_platform(monkeypatch, racks_per_su, expected):
    cluster = models.Cluster.from_dict({"id": "c", "name": "C"})
    _resolve(monkeypatch, cluster, racks_per_su)
    payload = _payload([f"r{i:02d}" for i in range(20)])
    dashboard._merge_scalable_units_into_status_payload(payload)
    assert len(payload["scalable_units"]) == expected
