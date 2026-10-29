# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Exercise workspace preparation against local recipes, without downloading containers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

INSTALLER = Path(__file__).resolve().parents[2] / "bin/scripts/install-dgxc-benchmarking-workspace.sh"


@pytest.mark.parametrize("gpu_type", ["vr200", "gb200", "gb300"])
def test_nvl72_workspace_allows_complete_racks_and_preserves_other_profiles(tmp_path, gpu_type):
    workspace = tmp_path / "workspace"
    repo = workspace / "dgxc-benchmarking"
    (repo / ".git").mkdir(parents=True)
    recipe = repo / "nccl" / "metadata.yaml"
    recipe.parent.mkdir()
    recipe.write_text(
        "run:\n  gpu_configs:\n"
        "    vr200:\n      model_configs:\n        - model: nccl\n          scales: [4, 8]\n"
        "    gb200:\n      model_configs:\n        - model: nccl\n          scales: [4, 8]\n"
        "    gb300:\n      model_configs:\n        - model: nccl\n          scales: [4, 8]\n",
        encoding="utf-8",
    )
    original = recipe.read_text(encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Block real fetch/checkout, global git configuration and privilege escalation.
    for command, status in (("git", 0), ("sudo", 1)):
        stub = fake_bin / command
        stub.write_text(f"#!/bin/sh\nexit {status}\n", encoding="utf-8")
        stub.chmod(0o755)
    env = {
        **{key: value for key, value in os.environ.items() if not key.startswith("NVIS_EXEMPLAR_")},
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "NVIS_EXEMPLAR_MODE": "record",
        "NVIS_EXEMPLAR_GPU_TYPE": gpu_type,
        "NVIS_EXEMPLAR_SCALE_PATCH_WORKLOADS": "nccl",
        "NVIS_EXEMPLAR_REPO_BRANCH": "site-selected-release",
    }
    for _ in range(2):
        proc = subprocess.run(
            ["bash", str(INSTALLER), str(workspace)],
            env=env, capture_output=True, text=True, timeout=20, check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        profiles = yaml.safe_load(recipe.read_text(encoding="utf-8"))["run"]["gpu_configs"]
        scales = profiles[gpu_type]["model_configs"][0]["scales"]
        assert all(72 * racks in scales for racks in range(1, 9))
        assert all(scale in scales for scale in (4, 8, 64, 128, 256, 512))
        for other in profiles.keys() - {gpu_type}:
            assert profiles[other]["model_configs"][0]["scales"] == [4, 8]
        config = yaml.safe_load((repo / "nvis-llmb-config.yaml").read_text(encoding="utf-8"))
        assert config["gpu_type"] == gpu_type
        assert config["slurm"]["gpu_partition_gres"] == 4
    assert Path(f"{recipe}.nvis.bak").read_text(encoding="utf-8") == original
