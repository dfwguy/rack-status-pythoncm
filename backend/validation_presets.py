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
from dataclasses import asdict, dataclass
from typing import Any

LOCAL_CONTAINER_DIR = "/tmp/containers"
CUDA_REQUIRED = "13.3+"
NCCL_REQUIRED = "2.31.2"
EXEMPLAR_TAG = "v26.06.01"

#: Where shared container images live. Nscale mounts them at ``/cluster/containers``; every
#: other site mounts them somewhere else, and this was hard-coded into five image paths on
#: both sides of the launch, so a preset was unrunnable anywhere but Nscale.
DEFAULT_SHARED_CONTAINER_ROOT = "/cluster/containers"

#: LLMB keeps its images in its own store, which moves per site for the same reason.
DEFAULT_LLMB_IMAGE_ROOT = "/cluster/llmb/images"


def shared_container_root() -> str:
    """The site's shared image directory.

    Configurable because it is a **mount point**, which is a site fact. The image *filename
    and digest* stay code-owned in the presets below, and that division is the security
    boundary: a site may point at its own mirror of a reviewed image, and cannot substitute
    a different image, because the size and SHA256 gates still have to pass.
    """
    from config_env import _eget

    return (_eget("RACK_VALIDATION_CONTAINER_ROOT", "") or "").strip() or (
        DEFAULT_SHARED_CONTAINER_ROOT
    )


def resolve_shared_image(path: str | None) -> str | None:
    """Re-root a preset's shared image at this site's container directory.

    Only the directory moves; the basename is the reviewed artifact and is preserved
    exactly. Resolved per call rather than baked at import, so a cluster overlay switching
    sites takes effect without a restart.
    """
    if not path:
        return path
    root = shared_container_root().rstrip("/")
    if root == DEFAULT_SHARED_CONTAINER_ROOT:
        return path
    return f"{root}/{path.rsplit('/', 1)[-1]}"


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

    def public(self) -> dict[str, Any]:
        row = asdict(self)
        row["defaults"] = {
            "storage": "local" if self.local_image else "nfs",
            "rsync": bool(self.local_image),
            "local_container_dir": LOCAL_CONTAINER_DIR,
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
        ):
            row.pop(key, None)
        return row


PRESETS: tuple[Preset, ...] = (
    Preset(
        "nccl-2.31.2",
        "NCCL 2.31.2 / CUDA 13.3+",
        "Direct host NCCL acceptance sweep; fails closed unless the selected runtime is CUDA 13.3+ and NCCL 2.31.2.",
        "nccl",
        ("node", "rack", "multirack"),
        cuda_version=CUDA_REQUIRED,
        nccl_version=NCCL_REQUIRED,
        requires_infiniband=True,
    ),
    Preset(
        "llama31-405b-fp8",
        "Llama 3.1 405B FP8 (1024 GPUs, 50 steps)",
        "The fixed 256-node/1024-GPU profile: TP4/PP8/CP1/VP4, MBS1, GBS6144, FP8, 50 steps, NCCL 2.30.7, and NVLS disabled.",
        "llama405b",
        ("multirack",),
        container_profile="nemo-26.04.01-nccl2.30.7",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh",
        local_image=f"{LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh",
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
        shared_image=f"{DEFAULT_LLMB_IMAGE_ROOT}/tensorrt-llm+release+1.1.0rc5.sqsh",
        local_image=f"{LOCAL_CONTAINER_DIR}/tensorrt-llm+release+1.1.0rc5.sqsh",
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
        local_image=f"{LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01-arm.sqsh",
        image_size=42245001216,
        image_sha256="55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc",
        requires_infiniband=True,
    ),
    Preset(
        "nemo-26.04.01",
        "NeMo 26.04.01",
        "NeMo-only rack or multi-rack validation with the staged 26.04.01 ARM image.",
        "nemo",
        ("rack", "multirack"),
        container_profile="nemo-26.04.01",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/nvidia+nemo+26.04.01-arm.sqsh",
        local_image=f"{LOCAL_CONTAINER_DIR}/nvidia+nemo+26.04.01-arm.sqsh",
        image_size=42245001216,
        image_sha256="55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc",
        max_steps=50,
        requires_infiniband=True,
    ),
    Preset(
        "mnubergemm-26.03-v1.17",
        "MNUberGEMM 26.03 v1.17",
        "MNUberGEMM node, rack, or multi-rack validation using the container actually staged on the fleet, with a fail-closed 60-minute allocation ceiling.",
        "mnubergemm",
        ("node", "rack", "multirack"),
        container_profile="mnubergemm-26.03-v1.17-arm",
        shared_image=f"{DEFAULT_SHARED_CONTAINER_ROOT}/mnubergemm-26.03-v1.17-arm.sqsh",
        local_image=f"{LOCAL_CONTAINER_DIR}/mnubergemm-26.03-v1.17-arm.sqsh",
        image_size=13949554688,
        image_sha256="9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6",
        max_runtime_minutes=60,
        requires_infiniband=False,
    ),
)

_BY_ID = {p.id: p for p in PRESETS}
_ALIASES = {
    "nccl": "nccl-2.31.2",
    "llama405b": "llama31-405b-fp8",
    "gpt_oss": "gpt-oss-cpu",
    "gpt-oss": "gpt-oss-cpu",
    "exemplar_sysinfo": "exemplar-system-info",
    "system_info": "exemplar-system-info",
    "nemo": "nemo-26.04.01",
    "nemo-26.04.1": "nemo-26.04.01",
    "mnubergemm": "mnubergemm-26.03-v1.17",
}


def catalogue() -> list[dict[str, Any]]:
    return [preset.public() for preset in PRESETS]


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


def normalize_options(body: dict[str, Any], preset: Preset) -> dict[str, Any]:
    scope = str(body.get("scope") or "rack").strip().lower()
    if scope == "nodes":
        scope = "node"
    if scope not in preset.scopes:
        raise ValueError(f"{preset.id} supports scope(s) {', '.join(preset.scopes)}, not {scope!r}")

    storage = str(body.get("storage") or ("local" if preset.local_image else "nfs")).strip().lower()
    if storage not in ("nfs", "local"):
        raise ValueError("storage must be nfs or local")
    rsync = body.get("rsync_to_local", body.get("rsync", storage == "local" and bool(preset.local_image))) is True
    if rsync and storage != "local":
        raise ValueError("rsync requires storage=local")
    local_dir = str(body.get("local_container_dir") or LOCAL_CONTAINER_DIR).rstrip("/")
    if local_dir != LOCAL_CONTAINER_DIR:
        raise ValueError(f"local_container_dir is fixed to {LOCAL_CONTAINER_DIR}")

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
    if preset.id == "nccl-2.31.2":
        if cuda.endswith("+"):
            requested_cuda = cuda[:-1]
        else:
            requested_cuda = cuda
        if _version_tuple(requested_cuda) < (13, 3):
            raise ValueError("nccl-2.31.2 requires CUDA 13.3 or newer")
        if nccl != NCCL_REQUIRED:
            raise ValueError(f"nccl-2.31.2 requires NCCL exactly {NCCL_REQUIRED}")
        cuda = CUDA_REQUIRED
    elif preset.nccl_version and nccl != preset.nccl_version:
        raise ValueError(f"{preset.id} is pinned to NCCL {preset.nccl_version}")

    profile = str(body.get("container_profile") or preset.container_profile or "").strip()
    if preset.container_profile and profile != preset.container_profile:
        raise ValueError(f"{preset.id} is pinned to container_profile={preset.container_profile}")

    return {
        "scope": scope,
        "storage": storage,
        "rsync_to_local": rsync,
        "local_container_dir": LOCAL_CONTAINER_DIR,
        "container_profile": preset.container_profile,
        "cuda_version": cuda or None,
        "nccl_version": nccl or None,
        "max_steps": preset.max_steps,
        "max_runtime_minutes": preset.max_runtime_minutes,
        "requires_infiniband": preset.requires_infiniband,
        "shared_image": preset.shared_image,
        "local_image": preset.local_image,
    }
