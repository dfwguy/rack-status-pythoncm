# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Fail-closed provenance manifest for NVFTP-derived production validation wrappers."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

SNAPSHOT: Final = "20260824"
ARCHIVE_RELATIVE_ROOT: Final = "artifacts/working_HLT_NEMO_NCCL_LLAMA/20260824"

BUNDLES: Final[dict[str, tuple[str, str]]] = {
    "llama": ("NscaleTX_Llama405B_working_validation_20260824.tar.gz", "e7c1b4fe20cf81836245db6f65d913a998563e5f5590c372ab6f5ba37027c5dc"),
    "mnubergemm": ("NscaleTX_MNUberGEMM_HLT_working_validation_20260824.tar.gz", "a566fb878a400a1e7b60c87e4c6e83111a2ed55bf3a15a11b59cb1bcf6ac4386"),
    "nccl": ("NscaleTX_NCCL_working_validation_20260824.tar.gz", "1e1f0c32a90b8849e24aa33023591bdf144fc1180f6de4a87366e30b8fa508fc"),
    "nemo": ("NscaleTX_NeMo_NemotronH56B_working_validation_20260824.tar.gz", "14db6f86dec645f0dbe4b6b630072a8b7be806efc4d95d8868ee78426c198585"),
}

# Approved live hashes include only reviewed 16K portability/configuration fixes. The source
# archive and its receipt stay recorded separately above; runtime verification intentionally
# hashes this small invoked file set, never the full extracted 229-file snapshot.
APPROVED_LIVE_SHA256: Final[dict[str, str]] = {
    "slurm-init.inc.sh": "713fc564438a37582002c7510d58bacb0c1b839bab10d759226a105fa52cf716",
    # Reviewed 2026-09-09: presets validate real Slurm hostnames, derive exact
    # MNUberGEMM rack groups for CLI callers and submits its batch/workdir from
    # the compute-visible runtime tree. Multi-rack NeMo keeps MNNVL intra-rack,
    # uses IB inter-rack, and disables only cross-rack NVLS.
    "run-validation-preset.sh": "588a4632677ac7287464b82539f375b76dcd7c49dfdd3ccc97716a40cfc1cafc",
    "run_mnubergemm_stress.sh": "e2e2346b242655d23b950d9baf7a6ad92de7a2d0c1c23812351a103f271ef69e",
    "mnubergemm.sbatch": "c8631ad3d73b026558d99d887a59a6eb222484137db08dc4cb346adac77a9b27",
    "run-exemplar-llama.sh": "4df4ad6cfaf3c195444763fa1d787793f592a8ab9d68446bd768aeb18087dcc0",
    # The job-54553 wrapper plus reviewed MNNVL/NVLS and IMEX fail-closed gates.
    "llama405b_fp8_1024.sbatch": "fa0b8523018d0f5913886fc5635e132799afd1428d9594c26d09925a26972e5c",
    "llama405b_node_preflight.sh": "cea2800d4d73c47013d0e30bf1767a55de596c73e5b604477c003326b9d0be4a",
    # Reviewed 2026-09-12: single-rack NCCL enables NVLS, leaves MNNVL on
    # automatic discovery, performs the allocation-scoped IMEX channel0 repair,
    # and accepts NCCL's correctness result plus the measured rack bandwidth floor.
    "run-llmb-nccl-container.sh": "f061dcaff5f17c3fca3a547412007e56dccf14bc03232a99bf662f7998e6192d",
    "nvftp_nccl_five_collective_20260824.sbatch": "fc61f51a55c0c193f940fcce828a59aeddecc4f203239e7042fd5b086bfc810b",
    "run_llmb_nccl.sh": "2e06d7a582bc42174cf53874918562e685570d2e83fe2dce33e547395b6e34c3",
    "nccl_allreduce.sbatch": "bf349afdc5e629f0b25b586ddbf0c1cef5aee09b1e75ea7e0384bd74067c5b87",
    "run-nemo-xrack.sh": "23c0762ffaa265ebb8eaad53e14a59022b8d927e661c74081602209740524733",
    "nemo-nemotron5h_56b-mbridge-fp8.sh": "a71fd4fc252a98d6b2923612dfed58aaca7b2ab13fc3abbd2804a78a323c739d",
    # Resolve compact Slurm hostlists before the sysinfo ssh fanout so every
    # hardware check is attributed to an exact compute hostname.
    "sysinfo.sh": "715273d6fddbebc4ffc9b73b1dd937df6aa864db4392cead446ab90a3a59b783",
    "validation_container_stage.sbatch": "c260c90580e00c757fa634d4216407df384e20ccd7a24f3609634ef1116f1dc8",
    "validation_imex_channel0_prepare.sbatch": "758afa49fb9449677a8520cb5e55170ddedaaf1ad8b21c2dcd4857465afcb693",
    "run_nccl_mpi.sh": "a982e57bd2f2adba09050fdaeae8f1516c69a42ca4cbe71cfe8dbb7d31cad38c",
}

APPROVED_SNAPSHOT_SHA256: Final[dict[str, str]] = {
    "working_HLT_NEMO_NCCL_LLAMA/20260824/NCCL/recipes/single-node/nccl_five_collective.sbatch":
        "b923a98e913230f1a0d0d208aa411b72a373147fdfba05fd6e0fe0915a5138d1",
}

PROFILES: Final[dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    # The common preset dispatcher originates in the MNUberGEMM bundle; direct NCCL also
    # invokes NCCL-owned siblings, so both bundle receipts belong to this profile.
    "preset:nccl-2.29.3": (("mnubergemm", "nccl"), (
        "slurm-init.inc.sh",
        "run-validation-preset.sh",
        "run-llmb-nccl-container.sh",
        "run_llmb_nccl.sh",
        "nvftp_nccl_five_collective_20260824.sbatch",
        "validation_container_stage.sbatch",
        "validation_imex_channel0_prepare.sbatch",
    )),
    "preset:llama31-405b-fp8": (("mnubergemm", "llama"), ("run-validation-preset.sh", "llama405b_fp8_1024.sbatch", "llama405b_node_preflight.sh")),
    "preset:nemo-26.04.01": (("mnubergemm", "nemo"), ("run-validation-preset.sh", "nemo-nemotron5h_56b-mbridge-fp8.sh", "sysinfo.sh", "validation_container_stage.sbatch")),
    "preset:mnubergemm-26.03-v1.17": (("mnubergemm",), ("run-validation-preset.sh", "mnubergemm.sbatch", "validation_container_stage.sbatch")),
    "mnubergemm-stress": (("mnubergemm",), (
        "run_mnubergemm_stress.sh",
        "mnubergemm.sbatch",
        "validation_imex_channel0_prepare.sbatch",
    )),
    "llmb-nccl-container": (("nccl",), (
        "slurm-init.inc.sh",
        "run-llmb-nccl-container.sh",
        "run_llmb_nccl.sh",
        "nvftp_nccl_five_collective_20260824.sbatch",
        "validation_container_stage.sbatch",
        "validation_imex_channel0_prepare.sbatch",
    )),
    # The allocation gate is owned by the common MNUberGEMM dispatcher bundle;
    # the NeMo launcher now invokes that reviewed gate before its workload.
    "nemo-xrack": (("mnubergemm", "nemo"), (
        "run-nemo-xrack.sh",
        "nemo-nemotron5h_56b-mbridge-fp8.sh",
        "sysinfo.sh",
        "validation_container_stage.sbatch",
    )),
    "exemplar-llama": (("llama",), ("run-exemplar-llama.sh",)),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(profile: str, scripts_dir: Path, provenance_root: Path | None = None) -> dict[str, object]:
    """Verify the approved live subset for *profile* or raise ``ValueError`` before launch."""
    try:
        bundle_ids, filenames = PROFILES[profile]
    except KeyError:
        raise ValueError(f"unknown validation provenance profile: {profile}") from None
    verified: dict[str, str] = {}
    for filename in filenames:
        expected = APPROVED_LIVE_SHA256[filename]
        path = scripts_dir / filename
        try:
            actual = _sha256(path)
        except OSError as exc:
            raise ValueError(f"validation provenance missing {filename}: {exc}") from None
        if actual != expected:
            raise ValueError(
                f"validation provenance drift for {filename}: got {actual}, approved {expected}"
            )
        verified[filename] = actual
    snapshot_files: dict[str, str] = {}
    if profile in {"llmb-nccl-container", "preset:nccl-2.29.3"}:
        if provenance_root is None:
            raise ValueError("validation provenance root is required for the direct NCCL fallback")
        for relative, expected in APPROVED_SNAPSHOT_SHA256.items():
            path = provenance_root / relative
            try:
                actual = _sha256(path)
            except OSError as exc:
                raise ValueError(f"validation provenance missing {relative}: {exc}") from None
            if actual != expected:
                raise ValueError(
                    f"validation provenance drift for {relative}: got {actual}, approved {expected}"
                )
            snapshot_files[relative] = actual
    return {
        "snapshot": SNAPSHOT,
        "bundles": [
            {
                "path": f"{ARCHIVE_RELATIVE_ROOT}/{BUNDLES[bundle_id][0]}",
                "sha256": BUNDLES[bundle_id][1],
            }
            for bundle_id in bundle_ids
        ],
        "live_files": verified,
        "snapshot_files": snapshot_files,
    }
