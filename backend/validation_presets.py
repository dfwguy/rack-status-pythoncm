# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Audited validation preset catalogue and request normalization.

The catalogue is intentionally code-owned rather than accepting arbitrary commands,
images, versions, or paths from the browser.  Flask supplies the resolved exact node
list; this module turns the remaining operator choices into a stable launch plan.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from pathlib import PurePosixPath
from typing import Any

DEFAULT_SHARED_CONTAINER_ROOT = "/cluster/OVA/containers/nvftp-8k-known-good/20260824"
DEFAULT_LOCAL_CONTAINER_DIR = "/opt/containers"
CUDA_REQUIRED = "13.1"
NCCL_REQUIRED = "2.29.3"
EXEMPLAR_TAG = "v26.06.01"


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    kind: str
    scopes: tuple[str, ...]
    container_profile: str | None = None
    shared_image: str | None = None
    local_image: str | None = None
    image_size: int | None = None
    image_sha256: str | None = None
    cuda_version: str | None = None
    nccl_version: str | None = None
    max_steps: int | None = None
    max_runtime_minutes: int | None = None
    requires_infiniband: bool = False
    max_nodes: int | None = None
    exact_nodes: int | None = None
    default_rsync: bool | None = None

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row["defaults"] = {
            "storage": "local" if self.local_image else "nfs",
            "rsync": bool(self.local_image) if self.default_rsync is None else self.default_rsync,
            "local_container_dir": str(PurePosixPath(self.local_image).parent) if self.local_image else None,
            "container_profile": self.container_profile,
            "cuda_version": self.cuda_version,
            "nccl_version": self.nccl_version,
            "max_steps": self.max_steps,
            "max_runtime_minutes": self.max_runtime_minutes,
        }
        row["requirements"] = {
            "scopes": list(self.scopes),
            "requires_infiniband": self.requires_infiniband,
            "exact_target_nodes": True,
            "exact_nodes": self.exact_nodes,
            "max_nodes": self.max_nodes,
            "exemplar_tag": EXEMPLAR_TAG if self.id == "exemplar-system-info" else None,
        }
        for key in (
            "scopes", "shared_image", "local_image", "image_size", "image_sha256", "cuda_version", "nccl_version",
            "max_steps", "max_runtime_minutes", "requires_infiniband", "max_nodes", "exact_nodes", "container_profile",
            "default_rsync",
        ):
            row.pop(key, None)
        return row


PRESETS: tuple[Preset, ...] = (
    Preset(
        "nccl-2.29.3",
        "NCCL 2.29.3 — NVFTP 8K–32G / 500 iterations",
        "The proven ARM NVFTP five-collective NCCL recipe: 8K through 32G and 500 iterations. Multi-rack runs disable MNNVL, enable NVLS, and use the four IB rails between racks, matching the proven 8K transport settings. Detailed rank-local NCCL INFO logs are retained.",
        "nccl",
        ("node", "rack", "multirack"),
        container_profile="nemo-26.04.01-nccl2.29.3",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01.sqsh",
        image_size=42245001216,
        image_sha256="e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca",
        cuda_version=CUDA_REQUIRED,
        nccl_version=NCCL_REQUIRED,
        requires_infiniband=True,
        default_rsync=False,
    ),
    Preset(
        "llama31-405b-fp8",
        "Llama 3.1 405B FP8 (1024 GPUs, 50 steps)",
        "The fixed 256-node/1024-GPU profile: TP4/PP8/CP1/VP4, MBS1, GBS6144, FP8, 50 steps, NCCL 2.30.7, and NVLS disabled.",
        "llama405b",
        ("multirack",),
        container_profile="nemo-26.04.01-nccl2.30.7",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh",
        image_size=42477232128,
        image_sha256="0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747",
        nccl_version="2.30.7",
        max_steps=50,
        requires_infiniband=True,
        exact_nodes=256,
    ),
    Preset(
        "gpt-oss-cpu",
        "GPT-OSS CPU microbenchmark",
        "GPT-OSS CPU-overhead validation (kernel-launch latency and tokenizer path), submitted as one independently attributable job per selected node; no training workload.",
        "gpt_oss",
        ("node", "rack", "multirack"),
        container_profile="tensorrt-llm-1.1.0rc5",
        shared_image="/cluster/llmb/images/tensorrt-llm+release+1.1.0rc5.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/tensorrt-llm+release+1.1.0rc5.sqsh",
        image_size=30284881920,
        image_sha256="80de1820829418a3789a4ff5ce0c661f669ffb07c013552ac648822b9c1f9e72",
        max_nodes=256,
    ),
    Preset(
        "exemplar-system-info",
        "DGXC Exemplar system_info",
        "Runs NVIDIA dgxc-benchmarking v26.06.01 microbenchmark_system_info once per selected node and preserves each Slurm stdout.",
        "exemplar_sysinfo",
        ("node", "rack", "multirack"),
        container_profile="nemo-26.04.01",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01-arm.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01-arm.sqsh",
        image_size=42245001216,
        image_sha256="55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc",
        requires_infiniband=True,
    ),
    Preset(
        "nemo-26.04.01",
        "NeMo 26.04.01",
        "NeMo-only rack or multi-rack validation with the staged 26.04.01 ARM image. Multi-rack runs retain MNNVL inside each rack, use four-rail IB between racks, and disable only NVLS multicast while per-rack clique IDs remain inconsistent.",
        "nemo",
        ("rack", "multirack"),
        container_profile="nemo-26.04.01",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01.sqsh",
        image_size=42245001216,
        image_sha256="e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca",
        max_steps=30,
        requires_infiniband=True,
    ),
    Preset(
        "mnubergemm-26.03-v1.17",
        "MNUberGEMM 26.03 v1.17",
        "MNUberGEMM node, rack, or multi-rack validation using the container actually staged on the fleet, with a fail-closed 30-minute burn and bounded startup/teardown envelope.",
        "mnubergemm",
        ("node", "rack", "multirack"),
        container_profile="mnubergemm-26.03-v1.17-arm",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/mnubergemm-26.03-v1.17-arm.sqsh",
        local_image=f"{DEFAULT_LOCAL_CONTAINER_DIR}/mnubergemm-26.03-v1.17-arm.sqsh",
        image_size=13949554688,
        image_sha256="9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6",
        max_runtime_minutes=30,
        requires_infiniband=False,
    ),
)

_BY_ID = {p.id: p for p in PRESETS}
_ALIASES = {
    "nccl": "nccl-2.29.3",
    "nccl-2.31.2": "nccl-2.29.3",
    "llama405b": "llama31-405b-fp8",
    "gpt_oss": "gpt-oss-cpu",
    "gpt-oss": "gpt-oss-cpu",
    "exemplar_sysinfo": "exemplar-system-info",
    "system_info": "exemplar-system-info",
    "nemo": "nemo-26.04.01",
    "nemo-26.04.1": "nemo-26.04.01",
    "mnubergemm": "mnubergemm-26.03-v1.17",
}


_NVFTP_ROOTED_PRESETS = {
    "nccl-2.29.3",
    "llama31-405b-fp8",
    "exemplar-system-info",
    "nemo-26.04.01",
    "mnubergemm-26.03-v1.17",
}


def _absolute_root(value: str, label: str) -> PurePosixPath:
    root = PurePosixPath(str(value).rstrip("/"))
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError(f"{label} must be an absolute normalized path")
    return root


def resolve_image_paths(
    preset: Preset,
    *,
    shared_container_root: str = DEFAULT_SHARED_CONTAINER_ROOT,
    local_container_dir: str = DEFAULT_LOCAL_CONTAINER_DIR,
) -> Preset:
    """Resolve operator-configurable roots while retaining code-owned artifact names/digests."""
    local_root = _absolute_root(local_container_dir, "local_container_dir")
    if preset.id not in _NVFTP_ROOTED_PRESETS:
        if preset.local_image:
            return replace(preset, local_image=str(local_root / PurePosixPath(preset.local_image).name))
        return preset
    shared_root = _absolute_root(shared_container_root, "shared_container_root")
    assert preset.shared_image is not None and preset.local_image is not None
    filename = PurePosixPath(preset.shared_image).name
    return replace(
        preset,
        shared_image=str(shared_root / filename),
        local_image=str(local_root / filename),
    )


def catalogue(
    *,
    shared_container_root: str = DEFAULT_SHARED_CONTAINER_ROOT,
    local_container_dir: str = DEFAULT_LOCAL_CONTAINER_DIR,
) -> list[dict[str, Any]]:
    return [
        resolve_image_paths(
            preset,
            shared_container_root=shared_container_root,
            local_container_dir=local_container_dir,
        ).public()
        for preset in PRESETS
    ]


def resolve_preset(value: object) -> Preset:
    token = str(value or "").strip().lower()
    token = _ALIASES.get(token, token)
    try:
        return _BY_ID[token]
    except KeyError:
        raise ValueError(f"unknown validation preset: {value!r}") from None


def _version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){1,3}", value):
        raise ValueError(f"invalid version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def normalize_options(
    body: dict[str, Any],
    preset: Preset,
    *,
    local_container_dir: str = DEFAULT_LOCAL_CONTAINER_DIR,
) -> dict[str, Any]:
    scope = str(body.get("scope") or "rack").strip().lower()
    if scope == "nodes":
        scope = "node"
    if scope not in preset.scopes:
        raise ValueError(f"{preset.id} supports scope(s) {', '.join(preset.scopes)}, not {scope!r}")

    storage = str(body.get("storage") or ("local" if preset.local_image else "nfs")).strip().lower()
    if storage not in ("nfs", "local"):
        raise ValueError("storage must be nfs or local")
    default_rsync = bool(preset.local_image) if preset.default_rsync is None else preset.default_rsync
    rsync = body.get("rsync_to_local", body.get("rsync", storage == "local" and default_rsync)) is True
    if rsync and storage != "local":
        raise ValueError("rsync requires storage=local")
    if preset.id == "nccl-2.29.3" and (storage != "local" or rsync):
        raise ValueError("nccl-2.29.3 requires the verified node-local image with rsync disabled")
    configured_local_dir = str(_absolute_root(local_container_dir, "local_container_dir"))
    local_dir = str(body.get("local_container_dir") or configured_local_dir).rstrip("/")
    if local_dir != configured_local_dir:
        raise ValueError(f"local_container_dir is fixed to {configured_local_dir}")

    supplied_steps = body.get("max_steps")
    if supplied_steps is not None and preset.max_steps is not None:
        try:
            different = int(supplied_steps) != preset.max_steps or isinstance(supplied_steps, bool)
        except (TypeError, ValueError):
            different = True
        if different:
            raise ValueError(f"{preset.id} is fixed at max_steps={preset.max_steps}")

    supplied_runtime = body.get("max_runtime_minutes")
    if supplied_runtime is not None and preset.max_runtime_minutes is not None:
        try:
            different = int(supplied_runtime) != preset.max_runtime_minutes or isinstance(supplied_runtime, bool)
        except (TypeError, ValueError):
            different = True
        if different:
            raise ValueError(
                f"{preset.id} is capped at max_runtime_minutes={preset.max_runtime_minutes}"
            )

    cuda = str(body.get("cuda_version") or preset.cuda_version or "").strip()
    nccl = str(body.get("nccl_version") or preset.nccl_version or "").strip()
    if preset.id == "nccl-2.29.3":
        if cuda != CUDA_REQUIRED:
            raise ValueError(f"nccl-2.29.3 requires CUDA exactly {CUDA_REQUIRED}")
        if nccl != NCCL_REQUIRED:
            raise ValueError(f"nccl-2.29.3 requires NCCL exactly {NCCL_REQUIRED}")
    elif preset.nccl_version and nccl != preset.nccl_version:
        raise ValueError(f"{preset.id} is pinned to NCCL {preset.nccl_version}")

    profile = str(body.get("container_profile") or preset.container_profile or "").strip()
    if preset.container_profile and profile != preset.container_profile:
        raise ValueError(f"{preset.id} is pinned to container_profile={preset.container_profile}")

    return {
        "scope": scope,
        "storage": storage,
        "rsync_to_local": rsync,
        "local_container_dir": configured_local_dir,
        "container_profile": preset.container_profile,
        "cuda_version": cuda or None,
        "nccl_version": nccl or None,
        "max_steps": preset.max_steps,
        "max_runtime_minutes": preset.max_runtime_minutes,
        "requires_infiniband": preset.requires_infiniband,
        "shared_image": preset.shared_image,
        "local_image": preset.local_image,
    }
