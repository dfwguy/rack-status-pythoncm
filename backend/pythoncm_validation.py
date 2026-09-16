# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Map validation recipes to PythonCM support or an explicit Slurm fallback."""
from __future__ import annotations

import scheduler_runtime
from schedulers import JobSpec


class UnsupportedValidation(RuntimeError):
    """The installed recipe has no semantically equivalent PythonCM form."""


_BLOCKED = {
    "validation": "NeMo/Nemotron uses a Slurm/Pyxis batch recipe; direct enroot + distributed launch is not validated",
    "nemo-xrack": "cross-rack NeMo uses a Slurm/Pyxis batch recipe; direct distributed launch is not validated",
    "nccl-fullrack": "the current direct NCCL recipe launches MPI through SSH; a ProgramRunner-only multi-node recipe is not validated",
    "llmb-nccl": "the replacement NCCL recipe launches MPI through SSH; a ProgramRunner-only multi-node recipe is not validated",
    "nccl-2.31.2": "the NCCL 2.31.2 recipe launches MPI through SSH; a ProgramRunner-only multi-node recipe is not validated",
    "nccl-2.29.3": "the proven containerized NCCL 2.29.3 recipe requires Slurm/Pyxis and PMI2",
    "clusterkit": "the installed ClusterKit multi-host recipe has not been proven free of SSH/workload-manager fan-out",
    "nvloom": "the installed NVLoom recipe invokes sysinfo SSH/pdsh fan-out and MPI through SSH",
    "mnubergemm": "the current PythonCM recipe runs dcgmi mndiag and does not implement the pinned-container MNUberGEMM preset semantics",
    "llama": "the installed LLMB Llama recipe calls llmb-run submit, which submits to Slurm",
    "llama-exemplar": "the Exemplar Llama recipe calls llmb-run submit, which submits to Slurm",
    "gpt-oss-cpu": "the GPT-OSS recipe uses srun/Pyxis; its direct enroot form is not validated",
    "exemplar-system-info": "the DGXC system-info recipe is packaged as a Slurm batch launch",
    "sysinfo": "the installed sysinfo recipe performs its own pdsh/SSH fan-out and has no node-local PythonCM form",
}


def unsupported_reason(kind: str) -> str | None:
    return _BLOCKED.get(str(kind or "").strip().lower())


def require_supported(kind: str) -> None:
    """Require a PythonCM-native recipe; callers may instead select Slurm fallback."""
    reason = unsupported_reason(kind)
    if reason:
        raise UnsupportedValidation(reason)


def resolve_nodes(*, target: str = "", explicit: list[str] | None = None) -> list[str]:
    """Resolve an exact target only against current PythonCM PhysicalNode entities."""
    known = {row["node"].casefold(): row["node"] for row in scheduler_runtime.nodes()}
    requested = [str(node).strip() for node in (explicit or []) if str(node).strip()]
    if not requested and target.strip():
        requested = scheduler_runtime.expand_nodelist(target)
    if not requested:
        raise ValueError("an exact PythonCM node target is required")
    missing = [node for node in requested if node.casefold() not in known]
    if missing:
        raise ValueError("node(s) not in PythonCM inventory: " + ", ".join(missing[:20]))
    out: list[str] = []
    seen: set[str] = set()
    for node in requested:
        canonical = known[node.casefold()]
        if canonical.casefold() not in seen:
            seen.add(canonical.casefold())
            out.append(canonical)
    return out


def build_spec(
    kind: str,
    *,
    nodes: list[str],
    rack: str,
    log_dir: str,
    scripts_dir: str,
    timeout: int,
    env: dict[str, str] | None = None,
) -> JobSpec:
    """Build an exact PythonCM recipe when one is available."""
    normalized = str(kind or "").strip().lower()
    require_supported(normalized)
    raise UnsupportedValidation(
        f"validation {normalized!r} has no reviewed PythonCM/ProgramRunner-only runner"
    )
