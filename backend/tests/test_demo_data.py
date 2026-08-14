# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for demo_data module."""
import demo_data


class TestGenerateDemoStatus:
    def test_returns_valid_structure(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        assert result["ok"] is True
        assert result["returncode"] == 0
        assert "rows" in result
        assert "summary" in result
        assert "comments" in result
        assert "physical_rf0" in result
        assert "native_workers" in result
        assert "footnotes" in result

    def test_generates_expected_rack_count(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        assert len(result["rows"]) == 18

    def test_rows_have_required_fields(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        for row in result["rows"]:
            assert "rack" in row
            assert "nemo" in row
            assert "last_job" in row
            assert "sinfo" in row
            assert "ct_down_unk" in row
            assert "nemo_hint" in row

    def test_done_rows_have_empty_hint(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        for row in result["rows"]:
            if row["nemo"] == "done":
                assert row.get("nemo_hint", "") == ""

    def test_rack_ids_follow_pattern(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        for row in result["rows"]:
            assert row["rack"].startswith("p5-r")

    def test_summary_adds_up(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        s = result["summary"]
        total = s["done"] + s["fail"] + s["ongoing"] + s["pending"]
        assert total == len(result["rows"])

    def test_nemo_states_are_valid(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        valid_prefixes = ("done", "fail", "ongoing", "pending")
        for row in result["rows"]:
            assert any(row["nemo"].startswith(p) for p in valid_prefixes)

    def test_physical_rf0_ips_generated(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path)
        rf0 = result["physical_rf0"]
        assert len(rf0) > 0
        assert "p5-r01-ct01" in rf0

    def test_deterministic_within_bucket(self, tmp_path):
        r1 = demo_data.generate_demo_status(tmp_path)
        r2 = demo_data.generate_demo_status(tmp_path)
        assert r1["rows"] == r2["rows"]

    def test_include_raw(self, tmp_path):
        result = demo_data.generate_demo_status(tmp_path, include_raw=True)
        assert "stdout" in result
