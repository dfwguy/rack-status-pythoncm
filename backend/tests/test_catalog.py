# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the NVIS catalog: store, models, seed/migrate, and context resolution."""
from __future__ import annotations

import pytest
from catalog import context, models, seed
from catalog.store import (
    TABLE_PLATFORMS,
    TABLE_PROJECTS,
    TABLE_TOOLS,
    TinyDBRepo,
    current_generation,
    set_repo,
)


@pytest.fixture
def repo(tmp_path):
    r = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def seeded(repo):
    seed.migrate(
        repo,
        env={
            "NVIS_RACK_ID": r"p[\d+]-r[\d+]",
            "NVIS_CT_ID": r"p[\d+]-r[\d+]-ct[\d+]",
            "RACK_CLUSTER_PRODUCT_LINE": "gb300",
            "RACK_CLUSTER_CUSTOMER_NAME": "NSCALE",
            "RACK_PLATFORM": "bcm",
            "RACK_MULTIRACK_SITE_PREFIX": "p5",
        },
    )
    return repo


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    def test_crud_roundtrip(self, repo):
        repo.upsert(TABLE_PLATFORMS, {"id": "x", "name": "X"})
        assert repo.get(TABLE_PLATFORMS, "x")["name"] == "X"
        repo.upsert(TABLE_PLATFORMS, {"id": "x", "name": "X2"})
        assert repo.get(TABLE_PLATFORMS, "x")["name"] == "X2"
        assert len(repo.list(TABLE_PLATFORMS)) == 1
        assert repo.delete(TABLE_PLATFORMS, "x") is True
        assert repo.get(TABLE_PLATFORMS, "x") is None

    def test_upsert_requires_id(self, repo):
        with pytest.raises(ValueError):
            repo.upsert(TABLE_PLATFORMS, {"name": "no id"})

    def test_meta_and_generation(self, repo):
        g0 = current_generation()
        repo.set_meta("k", {"a": 1})
        assert repo.get_meta("k") == {"a": 1}
        assert current_generation() > g0

    def test_persistence_across_instances(self, tmp_path):
        p = tmp_path / "c.json"
        r1 = TinyDBRepo(p)
        r1.upsert(TABLE_TOOLS, {"id": "t", "name": "T"})
        r2 = TinyDBRepo(p)
        assert r2.get(TABLE_TOOLS, "t")["name"] == "T"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TestModels:
    def test_platform_validate_and_capability(self):
        p = models.Platform.from_dict(
            {"id": "gb200-nvl72", "name": "GB200", "capabilities": ["nvlink", "mnnvl"]}
        )
        assert p.validate() == []
        assert p.has_capability("NVLINK") is True
        assert p.has_capability("roce") is False

    def test_platform_invalid_id(self):
        assert models.Platform.from_dict({"id": "Bad Id", "name": "x"}).validate()

    def test_tool_compat_modes(self):
        plat = models.Platform.from_dict(
            {"id": "p", "name": "p", "capabilities": ["nvlink", "nvswitch"]}
        )
        assert models.ToolCompatibility(match="all").matches(plat) is True
        assert models.ToolCompatibility(match="none").matches(plat) is False
        assert models.ToolCompatibility(capabilities=["nvswitch"]).matches(plat) is True
        assert models.ToolCompatibility(capabilities=["mnnvl"]).matches(plat) is False
        assert models.ToolCompatibility(platform_ids=["p"]).matches(plat) is True
        assert models.ToolCompatibility(platform_ids=["other"]).matches(plat) is False

    def test_project_cluster_validate(self):
        proj = models.Project.from_dict(
            {
                "id": "proj",
                "name": "Proj",
                "clusters": [{"id": "c", "name": "C", "cluster_manager": "bcm"}],
            }
        )
        assert proj.validate() == []
        assert proj.get_cluster("c").name == "C"

    def test_cluster_bad_manager(self):
        proj = models.Project.from_dict(
            {"id": "p", "name": "P", "clusters": [{"id": "c", "name": "C", "cluster_manager": "xxx"}]}
        )
        assert any("cluster_manager" in e for e in proj.validate())

    def test_rack_mapping_mode_pool_is_accepted(self):
        # The Reno case: no placement anywhere, rendered as a flat pool. No SU list required.
        c = models.Cluster.from_dict({"id": "c", "name": "C", "rack_mapping_mode": "pool"})
        assert "pool" not in " ".join(c.validate())
        assert c.rack_mapping_mode == "pool"

    def test_rack_mapping_mode_pool_rejects_scalable_units(self):
        # pool asserts there is no placement, so an SU grouping is a contradiction.
        c = models.Cluster.from_dict({
            "id": "c", "name": "C", "rack_mapping_mode": "pool",
            "scalable_units": [{"id": "su01", "racks": ["r1"]}],
        })
        assert any("pool" in e and "scalable_units" in e for e in c.validate())

    def test_rack_mapping_mode_unknown_is_rejected(self):
        c = models.Cluster.from_dict({"id": "c", "name": "C", "rack_mapping_mode": "bananas"})
        assert any("rack_mapping_mode" in e for e in c.validate())


# ---------------------------------------------------------------------------
# Seed / migrate
# ---------------------------------------------------------------------------


class TestSeed:
    @pytest.mark.parametrize("product_line", ["dgx-b300", "DGX B300", "dgx_b300", "NVIDIA DGX B300"])
    def test_dgx_b300_context(self, repo, product_line):
        seed.migrate(repo, env={"RACK_CLUSTER_PRODUCT_LINE": product_line})
        _project, cluster, platform = context.resolve()

        assert cluster.platform_id == platform.id == "dgx-b300"
        assert platform.validate() == []
        assert platform.gpu_model == "B300"
        assert platform.cpu_vendor == "Intel"
        assert platform.cpu_arch == "x86_64"
        assert platform.system_vendor == "DGX"
        assert platform.geometry.gpus_per_node == 8
        assert platform.geometry.nvlink_domain == 8
        low = context.low_overlay()
        assert low["GPU_MODEL_FLAG"] == "b300"
        assert low["GPUS_PER_NODE"] == "8"
        assert low["RACK_CLUSTER_GPU_PER_TRAY"] == "8"
        assert low["RACK_VALIDATION_CT_RANGE"] == "01-04"
        assert low["NVIS_FABRIC_EXPECT_RAILS"] == "8"
        assert low["NVIS_FABRIC_EXPECT_RATE"] == "800"
        assert low["NVIS_FABRIC_NETWORK"] == "IB"
        assert context.high_overlay()["RACK_CLUSTER_PRODUCT_LINE"] == "dgx-b300"
        ids = {t["id"] for t in context.tools_for_cluster()}
        assert "sysinfo" in ids
        assert "nvlink-preflight" in ids
        assert "imex-reset" not in ids
        assert "mnubergemm-stress" not in ids

    @pytest.mark.parametrize("product_line, expected", [
        ("B300", "b300-hgx"),
        ("GB300 NVL72", "gb300-nvl72"),
        ("DGX B200", "dgx-b200"),
    ])
    def test_b300_mapping_preserves_other_products(self, product_line, expected):
        project = seed.build_default_project_from_env({"RACK_CLUSTER_PRODUCT_LINE": product_line})
        assert project.clusters[0].platform_id == expected

    def test_existing_catalog_gains_dgx_b300_without_changing_active_cluster(self, seeded):
        seeded.delete(TABLE_PLATFORMS, "dgx-b300")
        result = seed.migrate(seeded, env={"RACK_CLUSTER_PRODUCT_LINE": "dgx-b300"})

        assert result["platforms_added"] == 1
        assert seeded.get(TABLE_PLATFORMS, "dgx-b300") is not None
        assert context.resolve()[2].id == "gb300-nvl72"
        assert seed.migrate(seeded, env={})["platforms_added"] == 0

    def test_rtx_pro_6000_blackwell_platform(self):
        platform = models.Platform.from_dict(
            next(p for p in seed.SEED_PLATFORMS if p["id"] == "rtx-pro-6000-blackwell")
        )

        assert platform.validate() == []
        assert platform.geometry.gpus_per_node == 8
        assert platform.geometry.nvlink_domain == 0
        assert not platform.has_capability("nvlink")
        assert context.fabric_defaults(platform) == {
            "NVIS_FABRIC_NETWORK": "RoCE",
            "NVIS_FABRIC_EXPECT_RAILS": "8",
            "NVIS_FABRIC_EXPECT_RATE": "400",
        }
        assert seed._guess_platform_id({"RACK_CLUSTER_PRODUCT_LINE": "rtx6000"}) == platform.id

    def test_seed_counts_and_idempotent(self, repo):
        s1 = seed.migrate(repo, env={})
        assert s1["platforms_added"] == len(seed.SEED_PLATFORMS)
        assert s1["tools_added"] == len(seed.SEED_TOOLS)
        assert s1["default_project_created"] is True
        s2 = seed.migrate(repo, env={})
        assert s2["platforms_added"] == 0
        assert s2["tools_added"] == 0
        assert s2["default_project_created"] is False

    def test_seed_preserves_edits(self, repo):
        seed.migrate(repo, env={})
        p = repo.get(TABLE_PLATFORMS, "gb200-nvl72")
        p["description"] = "EDITED"
        repo.upsert(TABLE_PLATFORMS, p)
        seed.migrate(repo, env={})
        assert repo.get(TABLE_PLATFORMS, "gb200-nvl72")["description"] == "EDITED"

    def test_default_project_from_env(self, repo):
        seed.migrate(
            repo,
            env={
                "RACK_CLUSTER_PRODUCT_LINE": "gb300",
                "RACK_CLUSTER_CUSTOMER_NAME": "ACME",
                "RACK_PLATFORM": "maas",
                "NVIS_RACK_ID": r"p[\d+]-r[\d+]",
            },
        )
        proj = models.Project.from_dict(repo.list(TABLE_PROJECTS)[0])
        assert proj.customer == "ACME"
        c = proj.clusters[0]
        assert c.platform_id == "gb300-nvl72"
        assert c.cluster_manager == "maas"
        assert c.naming.rack_id_template == r"p[\d+]-r[\d+]"


# ---------------------------------------------------------------------------
# Context resolution / overlays
# ---------------------------------------------------------------------------


class TestContext:
    def test_active_and_resolve(self, seeded):
        active = context.get_active()
        assert active["project"] == "default"
        project, _cluster, platform = context.resolve()
        assert project.id == "default"
        assert platform.id == "gb300-nvl72"

    def test_high_overlay(self, seeded):
        high = context.high_overlay()
        assert high["NVIS_RACK_ID"] == r"p[\d+]-r[\d+]"
        assert high["RACK_PLATFORM"] == "bcm"
        assert high["RACK_CLUSTER_CUSTOMER_NAME"] == "NSCALE"
        assert high["RACK_CLUSTER_PRODUCT_LINE"] == "gb300"

    def test_low_overlay_geometry(self, seeded):
        low = context.low_overlay()
        # GB300 NVL72: 72 GPUs / 18 trays = 4 per tray; CT range 01-18.
        assert low["RACK_VALIDATION_CT_RANGE"] == "01-18"
        assert low["RACK_CLUSTER_GPU_PER_TRAY"] == "4"
        assert low["RACK_EXPORT_NODES_PER_RACK"] == "18"
        assert low["GPU_MODEL_FLAG"] == "gb300"

    def test_cluster_layout_does_not_change_rtx_node_geometry(self):
        platform = models.Platform.from_dict(
            next(p for p in seed.SEED_PLATFORMS if p["id"] == "rtx-pro-6000-blackwell")
        )
        cluster = models.Cluster(
            id="rtx",
            name="RTX",
            geometry=models.ClusterGeometry(trays_per_rack=4),
        )

        low = context.geometry_defaults(platform, cluster)

        assert low["RACK_VALIDATION_CT_RANGE"] == "01-04"
        assert low["RACK_CLUSTER_GPU_PER_TRAY"] == "8"
        assert low["GPUS_PER_NODE"] == "8"

    def test_switch_changes_geometry_and_compat(self, seeded):
        repo = seeded
        repo.upsert(
            TABLE_PROJECTS,
            models.Project(
                id="acme",
                name="ACME",
                customer="ACME",
                clusters=[
                    models.Cluster(id="c1", name="HGX", platform_id="b200-hgx-amd", cluster_manager="maas")
                ],
            ).to_dict(),
        )
        context.set_active("acme", "c1")
        low = context.low_overlay()
        assert low["RACK_VALIDATION_CT_RANGE"] == "01-08"
        assert low["RACK_CLUSTER_GPU_PER_TRAY"] == "8"
        ids = {t["id"] for t in context.tools_for_cluster()}
        assert "mnubergemm-stress" not in ids  # needs mnnvl
        assert "imex-reset" not in ids  # needs imex
        assert "sysinfo" in ids  # match all

    def test_tools_surface_filter(self, seeded):
        ids = {t["id"] for t in context.tools_for_cluster(surface="rackDetail.hardware")}
        assert "sysinfo" in ids
        assert "validation-singlerack" not in ids

    def test_rtx_platform_filters_nemo_tools(self, seeded):
        seeded.upsert(
            TABLE_PROJECTS,
            models.Project(
                id="rtx-project",
                name="RTX Project",
                clusters=[
                    models.Cluster(
                        id="rtx-cluster",
                        name="RTX Cluster",
                        platform_id="rtx-pro-6000-blackwell",
                    )
                ],
            ).to_dict(),
        )
        context.set_active("rtx-project", "rtx-cluster")

        ids = {tool["id"] for tool in context.tools_for_cluster()}

        assert {
            "llama",
            "nemo-xrack",
            "preset-llama405b",
            "preset-nemo",
            "validation-singlerack",
        }.isdisjoint(ids)
        assert context.tool_for_cluster("preset-nemo") is None
        assert "sysinfo" in ids

    def test_ai_context_block_is_authoritative(self, seeded):
        block = context.build_ai_context_block()
        assert "Customer: NSCALE" in block
        assert "GB300" in block
        assert "NVLink domain 72" in block
        # switching platform changes the facts in the block
        repo = seeded
        repo.upsert(
            TABLE_PROJECTS,
            models.Project(
                id="acme", name="ACME", customer="ACME",
                clusters=[models.Cluster(id="c1", name="HGX", platform_id="b200-hgx-amd")],
            ).to_dict(),
        )
        context.set_active("acme", "c1")
        block2 = context.build_ai_context_block()
        assert "B200" in block2
        assert "Customer: ACME" in block2

    def test_overlay_cache_invalidates_on_change(self, seeded):
        h1, _ = context.overlays_for_active()
        assert h1["RACK_PLATFORM"] == "bcm"
        # mutate active cluster manager and confirm cache refreshes
        proj = seeded.get(TABLE_PROJECTS, "default")
        proj["clusters"][0]["cluster_manager"] = "maas"
        seeded.upsert(TABLE_PROJECTS, proj)
        h2, _ = context.overlays_for_active()
        assert h2["RACK_PLATFORM"] == "maas"
