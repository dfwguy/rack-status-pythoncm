# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Regression contracts for the shipped deep-audit workflow definitions."""

from pathlib import Path

import pytest
import workflow_models
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / "var" / "workflows"


@pytest.mark.parametrize(
    "filename",
    ["hardware-deep-audit.yaml", "nvlink-network-deep-audit.yaml"],
)
def test_deep_audit_resolves_multi_rack_selection_before_fanout(filename):
    """A comma-separated rack selection must not be treated as one rack id."""
    workflow = yaml.safe_load((WORKFLOWS_DIR / filename).read_text(encoding="utf-8"))

    assert workflow_models.validate_workflow(workflow) == []
    resolve, audit = workflow["steps"][:2]
    assert resolve["resolve"] == {"racks": "${rack}", "into": "nodelist"}
    assert audit["fanout"]["nodes"] == "${nodelist}"
    assert audit["fanout"]["gpus_per_node"] == 4
    assert audit["issue_policy"]["drain_confirmed"] is True
