# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for the NPI Knowledge Portal catalog importer (catalog.npi_import)."""
from __future__ import annotations

import pytest
from catalog import models, npi_import
from catalog.store import TABLE_NPI_TOOLS, TinyDBRepo, set_repo

# A minimal but faithful upstream record (mirrors reports/catalog/tools/*.toml shape).
SAMPLE_TOML = """\
id = "sample-bench"
name = "Sample Bench"
aliases = ["sample"]
summary = "A sample performance microbenchmark used for tests."
portfolio_role = "test"
capability = "benchmark-performance"
evidence_depth = "document-verified"
maturity = "active"
deployment = "unknown"
execution_surfaces = ["slurm"]
scopes = ["Both (Single + Multi)"]
phases = ["phase-3"]
layers = ["layer-5"]
phase_note = "3"
npi_model_status = "mapped"
repositories = [{ url = "https://github.com/NVIDIA/sample" }]
tests = [{ name = "sweep", runs = "runs a sweep", parameters = "-b 8 -e 16G", primitives = ["nccl-collectives"], coverage_status = "attributed", check_id = "sweep" }]
review = { status = "review-needed", confidence = "medium" }
component_kind = "executable-payload"
"""

FRAMEWORK_TOML = """\
id = "sample-framework"
name = "Sample Framework"
summary = "A framework, not a runnable payload."
portfolio_role = "test-framework"
capability = "screen-test-execution"
maturity = "emerging"
"""

TELEMETRY_TOML = """\
id = "sample-telemetry"
name = "Sample Telemetry"
summary = "A telemetry plane."
portfolio_role = "telemetry-analytics"
capability = "telemetry-analytics"
maturity = "legacy"
"""


@pytest.fixture
def repo(tmp_path):
    r = TinyDBRepo(tmp_path / "catalog.json")
    set_repo(r)
    yield r
    set_repo(None)


@pytest.fixture
def catalog_dir(tmp_path):
    d = tmp_path / "npi" / "tools"
    d.mkdir(parents=True)
    (d / "sample-bench.toml").write_text(SAMPLE_TOML, encoding="utf-8")
    (d / "sample-framework.toml").write_text(FRAMEWORK_TOML, encoding="utf-8")
    (d / "sample-telemetry.toml").write_text(TELEMETRY_TOML, encoding="utf-8")
    return d


class TestLoadAndMap:
    def test_load_records_stamps_source_ref(self, catalog_dir):
        records = npi_import.load_records(catalog_dir)
        assert {r["id"] for r in records} == {
            "sample-bench",
            "sample-framework",
            "sample-telemetry",
        }
        bench = next(r for r in records if r["id"] == "sample-bench")
        assert bench["source_ref"].endswith("sample-bench.toml")
        assert bench["source"] == "npi-catalog"

    def test_model_roundtrip_and_derived(self):
        tool = models.NpiTool.from_dict(
            {
                "id": "sample-bench",
                "name": "Sample Bench",
                "capability": "benchmark-performance",
                "maturity": "active",
                "portfolio_role": "test",
                "scopes": ["Both (Single + Multi)"],
            }
        )
        assert tool.validate() == []
        assert tool.category() == "performance"
        assert tool.status() == "ga"
        assert tool.is_runnable_kind() is True
        stub = tool.to_tool_stub()
        assert stub["id"] == "npi-sample-bench"
        assert stub["category"] == "performance"
        assert stub["execution"]["target"] == "multirack"  # scope mentions multi
        assert stub["script"] == ""  # left blank until an operator promotes it

    def test_status_and_category_maps(self):
        fw = models.NpiTool.from_dict({"id": "f", "name": "F", "capability": "screen-test-execution", "maturity": "emerging", "portfolio_role": "test-framework"})
        assert fw.category() == "validation"
        assert fw.status() == "preview"
        assert fw.is_runnable_kind() is True
        tel = models.NpiTool.from_dict({"id": "t", "name": "T", "capability": "telemetry-analytics", "maturity": "legacy", "portfolio_role": "telemetry-analytics"})
        assert tel.category() == "maintenance"
        assert tel.status() == "deprecated"
        assert tel.is_runnable_kind() is False


class TestImport:
    def test_import_populates_and_is_idempotent(self, repo, catalog_dir):
        s1 = npi_import.import_npi_catalog(repo, catalog_dir)
        assert s1["added"] == 3
        assert s1["found"] == 3
        assert s1["updated"] == 0
        assert len(repo.list(TABLE_NPI_TOOLS)) == 3

        s2 = npi_import.import_npi_catalog(repo, catalog_dir)
        assert s2["added"] == 0
        assert s2["updated"] == 0
        assert s2["found"] == 3

    def test_import_updates_changed_records(self, repo, catalog_dir):
        npi_import.import_npi_catalog(repo, catalog_dir)
        (catalog_dir / "sample-bench.toml").write_text(
            SAMPLE_TOML.replace("A sample performance", "An edited"), encoding="utf-8"
        )
        s = npi_import.import_npi_catalog(repo, catalog_dir)
        assert s["updated"] == 1
        row = repo.get(TABLE_NPI_TOOLS, "sample-bench")
        assert row["summary"].startswith("An edited")

    def test_import_prunes_removed_records(self, repo, catalog_dir):
        npi_import.import_npi_catalog(repo, catalog_dir)
        (catalog_dir / "sample-telemetry.toml").unlink()
        s = npi_import.import_npi_catalog(repo, catalog_dir, prune=True)
        assert s["pruned"] == 1
        assert repo.get(TABLE_NPI_TOOLS, "sample-telemetry") is None
        assert len(repo.list(TABLE_NPI_TOOLS)) == 2

    def test_import_no_prune_keeps_removed(self, repo, catalog_dir):
        npi_import.import_npi_catalog(repo, catalog_dir)
        (catalog_dir / "sample-telemetry.toml").unlink()
        s = npi_import.import_npi_catalog(repo, catalog_dir, prune=False)
        assert s["pruned"] == 0
        assert repo.get(TABLE_NPI_TOOLS, "sample-telemetry") is not None

    def test_missing_source_is_noop(self, repo, tmp_path):
        s = npi_import.import_npi_catalog(repo, tmp_path / "does-not-exist")
        assert s["source_dir"] == ""
        assert s["found"] == 0
        assert len(repo.list(TABLE_NPI_TOOLS)) == 0

    def test_vendored_snapshot_imports(self, repo):
        # With no explicit dir, the vendored offline snapshot under catalog/npi_seed is used.
        s = npi_import.import_npi_catalog(repo)
        assert s["found"] >= 6
        ids = {r["id"] for r in repo.list(TABLE_NPI_TOOLS)}
        assert {"nccl-tests", "nvbandwidth", "nvloom", "dcgm"}.issubset(ids)
