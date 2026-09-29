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
    explicit = init.index('if [ -n "${RACK_SLURM_BIN:-}" ]')
    fallback = init.index("elif ! command -v sbatch")
    assert explicit < fallback
    assert 'export PATH="${RACK_SLURM_BIN}:$PATH"' in init


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


def test_single_node_validation_uses_platform_gpu_geometry():
    launcher = (SCRIPTS / "run_singlerack.sh").read_text(encoding="utf-8")
    batch = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text(encoding="utf-8")

    assert 'n=${#hosts[@]}' in launcher
    assert '(( n > 1 )) && n=$((n / 2 * 2))' in launcher
    assert '--ntasks-per-node="${gpus_per_node}"' in launcher
    assert '--gpus-per-node="${gpus_per_node}"' in launcher
    assert 'GPUS_PER_NODE="${GPUS_PER_NODE:-4}"' in batch
    assert '"${SLURM_NNODES}" -ne 1' in batch
    assert '--num_gpus $[${GPUS_PER_NODE}*${SLURM_NNODES}]' in batch
    assert '--gpus_per_node ${GPUS_PER_NODE}' in batch
    assert 'ns_nic="${NVIS_FAB_NS_IFNAME:-$ns_nic}"' in batch
    assert 'MELLANOX_VISIBLE_DEVICES="${NVIS_NEMO_MLX:-all}"' in batch


def test_nemo_validation_can_stop_after_container_nccl_preflight():
    batch = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text(encoding="utf-8")

    assert '"${NVIS_NEMO_PREFLIGHT_ONLY:-0}" = 1' in batch
    assert 'NCCL_PREFLIGHT_PASS nodes=${SLURM_NNODES}' in batch
