# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The Scalable Unit: platform sizing, cluster membership, and the derivation default.

An SU is the group of racks one leaf/spine set serves, so its size is a property of the
platform and not of the site. The behaviour worth pinning is the *boundary between derived
and explicit*: deriving is right for a tidy floor and wrong for a real one, which is why
an explicit map exists and why it must win.
"""
from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from catalog import context, models, seed
from catalog.store import TinyDBRepo, set_repo


def _platform(**fabric) -> models.Platform:
    return models.Platform.from_dict(
        {
            "id": "gb300-nvl72",
            "name": "GB300 NVL72",
            "geometry": {"gpus_per_rack": 72, "trays_per_rack": 18},
            "fabric": {"scaleout": "ib", **fabric},
        }
    )


def _racks(n: int) -> list[str]:
    return [f"ink50-dh01-a{i:02d}" for i in range(1, n + 1)]


class TestPlatformSizing:
    def test_gpus_per_su_is_derived_not_stored(self):
        """Two stored copies of one number is two things to keep in step."""
        p = _platform(racks_per_su=8)
        assert p.gpus_per_su() == 8 * 72
        assert "gpus_per_su" not in p.to_dict()["fabric"]

    def test_no_su_model_reports_zero_rather_than_one(self):
        """`0` has to mean "unmodelled". A platform defaulting to 1 would make every rack
        its own SU, which is a plausible-looking lie."""
        assert _platform().fabric.racks_per_su == 0
        assert _platform().gpus_per_su() == 0

    def test_switch_counts_without_a_rack_count_are_refused(self):
        """Switch counts describe a group; without racks_per_su the group has no size, so
        the record reads like an SU model while being unusable."""
        problems = _platform(leaf_switches_per_su=8).validate()
        assert any("racks_per_su" in p for p in problems), problems

    def test_negative_counts_are_refused(self):
        problems = _platform(racks_per_su=-1).validate()
        assert any("cannot be negative" in p for p in problems), problems

    def test_a_complete_su_model_validates(self):
        p = _platform(racks_per_su=8, leaf_switches_per_su=8, spine_switches_per_su=4)
        assert p.validate() == []


class TestDerivation:
    def test_racks_chunk_into_sus_in_order(self):
        sus = models.derive_scalable_units(_racks(16), 8)
        assert [s.id for s in sus] == ["su01", "su02"]
        assert [s.label for s in sus] == ["SU1", "SU2"]
        assert sus[0].racks == _racks(16)[:8]
        assert sus[1].racks == _racks(16)[8:]

    def test_a_partial_trailing_su_is_kept(self):
        """A half-populated final SU is normal during a bring-up. Dropping the remainder
        would silently hide racks from the floor view."""
        sus = models.derive_scalable_units(_racks(18), 8)
        assert [len(s.racks) for s in sus] == [8, 8, 2]

    def test_no_su_model_derives_nothing(self):
        """Not "one SU containing everything" — that draws a confidently wrong diagram."""
        assert models.derive_scalable_units(_racks(18), 0) == []

    def test_no_racks_derives_nothing(self):
        assert models.derive_scalable_units([], 8) == []

    def test_blank_racks_are_dropped_before_chunking(self):
        """Otherwise a stray empty inventory row shifts every later SU by one."""
        sus = models.derive_scalable_units(["a", "", "  ", "b"], 2)
        assert [s.racks for s in sus] == [["a", "b"]]


class TestClusterMembership:
    def test_a_cluster_with_no_map_derives_one(self):
        c = models.Cluster.from_dict({"id": "c", "name": "C"})
        assert c.scalable_units == []
        assert [len(s.racks) for s in c.scalable_units_for(_racks(18), 8)] == [8, 8, 2]

    def test_an_explicit_map_wins_over_derivation(self):
        """The case derivation cannot express: Nscale TX 16K numbers SU1..SU14 across two
        `dh` rows, which no racks-per-SU chunking of one ordered list reproduces."""
        c = models.Cluster.from_dict(
            {
                "id": "c",
                "name": "C",
                "scalable_units": [
                    {"id": "su01", "label": "SU1", "racks": ["ink50-dh01-a01", "ink50-dh02-a01"]},
                    {"id": "su02", "label": "SU2", "racks": ["ink50-dh01-b01"]},
                ],
            }
        )
        sus = c.scalable_units_for(_racks(18), 8)
        assert [s.id for s in sus] == ["su01", "su02"]
        assert sus[0].racks == ["ink50-dh01-a01", "ink50-dh02-a01"]

    def test_label_falls_back_to_the_id(self):
        c = models.Cluster.from_dict(
            {"id": "c", "name": "C", "scalable_units": [{"id": "su01", "racks": ["r1"]}]}
        )
        assert c.scalable_units[0].label == "su01"

    def test_a_rack_in_two_sus_is_refused(self):
        """Same discipline as a multirack launch: a rack in two groups double-counts on the
        floor view and, if anything ever fans out per SU, sends two jobs to one rack."""
        c = models.Cluster.from_dict(
            {
                "id": "c",
                "name": "C",
                "scalable_units": [
                    {"id": "su01", "racks": ["r1", "r2"]},
                    {"id": "su02", "racks": ["r2"]},
                ],
            }
        )
        problems = c.validate()
        assert any("'r2'" in p and "su01" in p and "su02" in p for p in problems), problems

    def test_duplicate_su_ids_are_refused(self):
        c = models.Cluster.from_dict(
            {
                "id": "c",
                "name": "C",
                "scalable_units": [
                    {"id": "su01", "racks": ["r1"]},
                    {"id": "su01", "racks": ["r2"]},
                ],
            }
        )
        assert any("duplicate scalable unit id" in p for p in c.validate())

    def test_an_su_with_no_racks_is_refused(self):
        c = models.Cluster.from_dict(
            {"id": "c", "name": "C", "scalable_units": [{"id": "su01", "racks": []}]}
        )
        assert any("lists no racks" in p for p in c.validate())

    def test_a_bad_su_id_is_refused(self):
        c = models.Cluster.from_dict(
            {"id": "c", "name": "C", "scalable_units": [{"id": "SU 1!", "racks": ["r1"]}]}
        )
        assert any("invalid scalable unit id" in p for p in c.validate())

    def test_a_valid_map_round_trips_through_dict(self):
        payload = {
            "id": "c",
            "name": "C",
            "scalable_units": [
                {"id": "su01", "label": "SU1", "racks": ["r1", "r2"], "handed_over_at": ""}
            ],
        }
        c = models.Cluster.from_dict(payload)
        assert c.validate() == []
        assert c.to_dict()["scalable_units"] == payload["scalable_units"]


class TestFabricOverlay:
    def test_su_shape_is_published_when_the_platform_models_it(self):
        env = context.fabric_defaults(
            _platform(racks_per_su=8, leaf_switches_per_su=8, spine_switches_per_su=4)
        )
        assert env["NVIS_SU_RACKS"] == "8"
        assert env["NVIS_SU_GPUS"] == "576"
        assert env["NVIS_SU_LEAF_SWITCHES"] == "8"
        assert env["NVIS_SU_SPINE_SWITCHES"] == "4"

    def test_nothing_is_published_without_an_su_model(self):
        """A script must be able to tell "no SU model" from "an SU of one rack", so the
        absent case publishes no key rather than a zero."""
        env = context.fabric_defaults(_platform())
        assert not [k for k in env if k.startswith("NVIS_SU_")]


class TestSeededPlatforms:
    def test_nvl72_platforms_carry_the_su_size(self):
        """8 racks x 72 GPUs = 576. Corroborated by Nscale TX 16K: 112 racks, 14 SUs."""
        by_id = {p["id"]: p for p in seed.SEED_PLATFORMS}
        for pid in ("gb200-nvl72", "gb300-nvl72"):
            assert by_id[pid]["fabric"]["racks_per_su"] == 8, pid
            assert models.Platform.from_dict(by_id[pid]).gpus_per_su() == 576, pid

    def test_every_seeded_platform_validates(self):
        """Catches a seeded fabric that declares switch counts without racks_per_su."""
        for raw in seed.SEED_PLATFORMS:
            assert models.Platform.from_dict(raw).validate() == [], raw["id"]


@pytest.fixture
def client(tmp_path):
    from app import app

    repo = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(repo)
    seed.migrate(repo, env={"RACK_CLUSTER_CUSTOMER_NAME": "Lab"})
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    set_repo(None)


class TestCatalogApi:
    """The SU must survive the HTTP round trip, not just the dataclass."""

    def test_an_su_map_round_trips_through_the_project_api(self, client):
        payload = {
            "id": "p1",
            "name": "P1",
            "clusters": [
                {
                    "id": "c1",
                    "name": "C1",
                    "platform_id": "gb300-nvl72",
                    "scalable_units": [
                        {"id": "su01", "label": "SU1", "racks": ["r1", "r2"]},
                        {"id": "su02", "label": "SU2", "racks": ["r3"]},
                    ],
                }
            ],
        }
        assert client.post("/api/catalog/projects", json=payload).status_code in (200, 201)
        got = client.get("/api/catalog/projects/p1").get_json()
        sus = got["item"]["clusters"][0]["scalable_units"]
        assert [s["id"] for s in sus] == ["su01", "su02"]
        assert sus[0]["racks"] == ["r1", "r2"]

    def test_an_overlapping_su_map_is_refused_by_the_api(self, client):
        """Validation has to run at the edge too, or the bad record is already stored."""
        bad = {
            "id": "p2",
            "name": "P2",
            "clusters": [
                {
                    "id": "c2",
                    "name": "C2",
                    "scalable_units": [
                        {"id": "su01", "racks": ["x"]},
                        {"id": "su02", "racks": ["x"]},
                    ],
                }
            ],
        }
        res = client.post("/api/catalog/projects", json=bad)
        assert res.status_code == 400
        assert "su01" in str(res.get_json())

    def test_the_platform_api_serves_the_su_sizing(self, client):
        fabric = client.get("/api/catalog/platforms/gb300-nvl72").get_json()["item"]["fabric"]
        assert fabric["racks_per_su"] == 8
        assert fabric["leaf_switches_per_su"] == 0  # unknown, not invented
