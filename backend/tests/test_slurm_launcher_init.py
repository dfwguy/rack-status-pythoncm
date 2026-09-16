# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Shell launchers must initialise the same pinned Slurm client as the backend."""

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"


def test_explicit_slurm_bin_wins_over_an_existing_path_binary():
    init = (SCRIPTS / "slurm-init.inc.sh").read_text(encoding="utf-8")
    explicit = init.index('if [ -n "$_rk_slurm_bin" ]')
    fallback = init.index("elif ! command -v sbatch")
    assert explicit < fallback
    assert 'export PATH="${_rk_slurm_bin}:$PATH"' in init
    assert 'RACK_SLURM_SBATCH' in init


def test_container_module_bypass_is_honored_before_sourcing_modules():
    init = (SCRIPTS / "slurm-init.inc.sh").read_text(encoding="utf-8")
    bypass = init.index('case "${RACK_SLURM_SKIP_MODULE_LOAD:-}"')
    modules = init.index("/cm/local/apps/environment-modules/current/init/bash")
    assert bypass < modules
    assert "if (( ! _rk_skip_modules ))" in init


def test_control_plane_launchers_source_the_shared_slurm_initialiser():
    for name in (
        "run-llama.sh",
        "nccl_campaign.sh",
        "run_singlerack.sh",
        "run_nvloom_singlerack.sh",
        "run_clusterkit.sh",
        "run-nemo-xrack.sh",
        "run_nccl_mpi.sh",
        "run_mnubergemm_stress.sh",
    ):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "slurm-init.inc.sh" in source, f"{name} bypasses the shared Slurm PATH setup"
