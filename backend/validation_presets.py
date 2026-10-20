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

Which container image a preset runs is NOT code-owned: it is the cluster's image catalog entry
whose ``required_by`` names the preset (:func:`launch_image`). The filename, byte size and SHA256
used to live here as literals, and went stale the moment a site re-imported an image -- NC's
catalog was corrected to the real digest while these literals kept the old one, so every NeMo
preset launch failed its stage gate on a healthy image.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

#: Default node-local image cache. Like the shared root below it is a site fact, configured per
#: cluster (``connection.node_container_dir`` -> ``NVIS_NODE_CONTAINER_DIR``); see
#: :func:`node_container_dir`.
LOCAL_CONTAINER_DIR = "/tmp/containers"
CUDA_REQUIRED = "13.0+"
NCCL_REQUIRED = "2.30.7"
EXEMPLAR_TAG = "v26.06.01"

#: Where shared container images live. Nscale mounts them at ``/cluster/containers``; every
#: other site mounts them somewhere else, and this was hard-coded into five image paths on
#: both sides of the launch, so a preset was unrunnable anywhere but Nscale.
DEFAULT_SHARED_CONTAINER_ROOT = "/cluster/containers"

#: LLMB keeps its images in its own store, which moves per site for the same reason.
DEFAULT_LLMB_IMAGE_ROOT = "/cluster/llmb/images"


def shared_container_root() -> str:
    """The site's shared image directory.

    Configurable because it is a **mount point**, which is a site fact. Which file in it a
    preset runs -- and the size and SHA256 that file must have -- is the cluster's image
    catalog (:func:`launch_image`), an authored record behind its own permissions; a launch
    body can choose neither.
    """
    from config_env import _eget

    return (_eget("RACK_VALIDATION_CONTAINER_ROOT", "") or "").strip() or (
        DEFAULT_SHARED_CONTAINER_ROOT
    )


def llmb_image_root() -> str:
    """LLMB's own image store on the launch host (``LLMB_IMAGE_ROOT``), a mount point like the above."""
    from config_env import _eget

    return (_eget("LLMB_IMAGE_ROOT", "") or "").strip() or DEFAULT_LLMB_IMAGE_ROOT


def node_container_dir() -> str:
    """The node-local image cache for the active cluster.

    Fixed per cluster rather than per request: staging writes into it on every selected node,
    so a launch body may restate it but never choose it.
    """
    from config_env import _eget

    return ((_eget("NVIS_NODE_CONTAINER_DIR", "") or "").strip() or LOCAL_CONTAINER_DIR).rstrip("/")


def node_images_p2p_managed() -> bool:
    """Does NVIS-P2P own this cluster's node image cache?

    Then images reach nodes only through P2P campaigns: a launch verifies the image is present and
    never rsyncs it from the head -- that fan-out duplicates the distribution P2P already did,
    writes into a directory P2P manages, and puts a 40+ GiB NFS burst on the head.
    """
    from config_env import _eget

    return (_eget("NVIS_NODE_IMAGES_P2P", "") or "").strip() == "1"


def resolve_local_image(path: str | None) -> str | None:
    """Re-root a preset's node-local image at this cluster's node cache, keeping the basename."""
    if not path:
        return path
    return f"{node_container_dir()}/{path.rsplit('/', 1)[-1]}"


def check_local_container_dir(supplied: object) -> str:
    """The configured node cache, after rejecting a request that names a different one."""
    configured = node_container_dir()
    local_dir = str(supplied or configured).rstrip("/")
    if local_dir != configured:
        raise ValueError(f"local_container_dir is fixed to this cluster's node cache, {configured}")
    return configured


#: Where a launcher finds the image it stages: the cluster's shared container mount, or LLMB's own
#: store. Code owns only this choice of mount; the file and its digest are the catalog's.
IMAGE_STORES = ("shared", "llmb")


def image_store_root(store: str) -> str:
    """The launch host's directory for *store*, resolved per call like the roots it names."""
    if store == "llmb":
        return llmb_image_root()
    if store == "shared":
        return shared_container_root()
    raise ValueError(f"unknown image store {store!r}; expected one of {', '.join(IMAGE_STORES)}")


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LaunchImageError(ValueError):
    """The cluster's catalog does not (unambiguously, with a pin) name the image a launch needs."""


@dataclass(frozen=True)
class LaunchImage:
    """The container image a launch stages, as the cluster's image catalog states it."""

    consumer: str
    filename: str
    bytes: int
    sha256: str
    source: str  # the file in the shared store, as the launch host sees it
    local: str  # the same basename in this cluster's node cache

    def artifact(self, storage: str) -> dict[str, Any]:
        """The reviewed-plan view: what is staged, from where, and the gates it must pass."""
        return {"path": self.local if storage == "local" else self.source, "source": self.source,
                "filename": self.filename, "size": self.bytes, "sha256": self.sha256,
                "required_by": self.consumer}

    def env(self) -> dict[str, str]:
        """The launcher's input (``lib/node_images.inc.sh:nvis_launch_image``)."""
        return {"NVIS_LAUNCH_IMAGE": self.filename, "NVIS_LAUNCH_IMAGE_BYTES": str(self.bytes),
                "NVIS_LAUNCH_IMAGE_SHA256": self.sha256}


def launch_image(consumer: str, store: str = "shared") -> LaunchImage:
    """The image *consumer* (a preset or tool id) runs, from the active cluster's image catalog.

    The catalog entry whose ``required_by`` lists *consumer* is the answer -- the same rule
    ``p2p_catalog.image_for_tool`` applies to the legacy NeMo launchers, so one edit in the Catalog
    Editor moves every launch path. An entry must carry its byte size and SHA256: those are the
    stage gates, and an unpinned image is refused rather than staged unchecked. Raises
    :class:`LaunchImageError` with the fix in the message.
    """
    import p2p_catalog
    from catalog import context as catalog_context

    _project, cluster, _platform = catalog_context.resolve()
    where = "Artifact Distribution -> Catalog"
    if cluster is None:
        raise LaunchImageError(f"{consumer}: no active cluster, so no image catalog to resolve its image from")
    try:
        image = p2p_catalog.image_for_tool(cluster.id, consumer)
    except ValueError as exc:
        raise LaunchImageError(f"image catalog: {exc}") from None
    if image is None:
        raise LaunchImageError(
            f"no image in this cluster's catalog is required by {consumer!r}: add {consumer} to the "
            f"Required by of its image in {where}"
        )
    if image.bytes <= 0 or not _SHA256_RE.match(image.sha256):
        raise LaunchImageError(
            f"{image.filename} (required by {consumer}) is not pinned in the image catalog: set its "
            f"bytes and sha256 in {where}"
        )
    root = image_store_root(store).rstrip("/")
    return LaunchImage(consumer=consumer, filename=image.filename, bytes=image.bytes, sha256=image.sha256,
                       source=f"{root}/{image.filename}", local=str(resolve_local_image(image.filename)))


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    kind: str
    scopes: tuple[str, ...]
    container_profile: str | None = None
    #: The store this preset stages its image from (one of :data:`IMAGE_STORES`), or ``None`` for
    #: a preset with no container. Which image is the catalog's: its ``required_by`` names this id.
    image_store: str | None = None
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
            "storage": "local" if self.image_store else "nfs",
            "rsync": bool(self.image_store) and not node_images_p2p_managed(),
            "local_container_dir": node_container_dir(),
            "p2p_managed": node_images_p2p_managed(),
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
        # The image as this cluster's catalog states it, or why it cannot -- so the picker says
        # "add nemo-26.04.01 to Required by" before the operator prepares a launch, not after.
        row["image"] = None
        if self.image_store:
            try:
                row["image"] = launch_image(self.id, self.image_store).artifact(row["defaults"]["storage"])
            except LaunchImageError as exc:
                row["image"] = {"error": str(exc)}
        for key in (
            "scopes", "image_store", "cuda_version", "nccl_version",
            "max_steps", "max_runtime_minutes", "requires_infiniband", "max_nodes", "exact_nodes", "container_profile",
        ):
            row.pop(key, None)
        return row


PRESETS: tuple[Preset, ...] = (
    Preset(
        "nccl-2.31.2",
        "NCCL 2.30.7 / CUDA 13.0+",
        "Direct host NCCL acceptance sweep; fails closed unless the selected runtime is CUDA 13.0+ and NCCL 2.30.7. (Preset id is historical; it now validates the host-built 2.30.7 workspace toolchain.)",
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
        image_store="shared",
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
        image_store="llmb",
        max_nodes=256,
    ),
    Preset(
        "exemplar-system-info",
        "DGXC Exemplar system_info",
        "Runs NVIDIA dgxc-benchmarking v26.06.01 microbenchmark_system_info once per selected node and preserves each Slurm stdout.",
        "exemplar_sysinfo",
        ("node", "rack", "multirack"),
        container_profile="nemo-26.04.01",
        image_store="shared",
        requires_infiniband=True,
    ),
    Preset(
        "nemo-26.04.01",
        "NeMo 26.04.01",
        "NeMo-only rack or multi-rack validation with the staged 26.04.01 ARM image.",
        "nemo",
        ("rack", "multirack"),
        container_profile="nemo-26.04.01",
        image_store="shared",
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
        image_store="shared",
        max_runtime_minutes=60,
        requires_infiniband=False,
    ),
    Preset(
        "node-gate",
        "Node gate",
        "Cleanup and health check between jobs: one exclusive Slurm job over the exact selected nodes kills "
        "leftover GPU processes, removes leftover pyxis containers and stale /dev/shm segments, restarts a GPU "
        "service that is down, then fails any node with GPU memory still held, uncorrected ECC, a power brake, "
        "an incomplete or split NVLink fabric, or a new hardware-fault Xid since its last gate.",
        "node_gate",
        ("node", "rack", "multirack"),
        max_runtime_minutes=20,
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
    "node_gate": "node-gate",
    "gate": "node-gate",
}


def catalogue() -> list[dict[str, Any]]:
    return [preset.public() for preset in PRESETS]


#: The Required by name of the containerized LLMB NCCL 24-test workflow's image.
LLMB_NCCL_CONTAINER = "llmb-nccl-container"

#: Launches outside this catalogue that also run the catalog image naming them. The NeMo pair is
#: ``api.validation_runs._TOOL_IMAGE_ENV``; a test keeps the two lists from drifting apart.
OTHER_IMAGE_CONSUMERS: tuple[tuple[str, str], ...] = (
    ("nemo-xrack", "Multirack NEMO"),
    ("validation-singlerack", "NeMo single-rack validation"),
    (LLMB_NCCL_CONTAINER, "LLMB NCCL container (24 tests)"),
)


def image_consumers() -> list[dict[str, str]]:
    """Every name a catalog image's Required by can carry, for the Catalog Editor to offer.

    A typo there is silent until launch -- the launch finds no image -- so the editor picks from
    these rather than taking free text.
    """
    rows = [{"id": p.id, "label": p.label} for p in PRESETS if p.image_store]
    return rows + [{"id": cid, "label": label} for cid, label in OTHER_IMAGE_CONSUMERS]


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

    storage = str(body.get("storage") or ("local" if preset.image_store else "nfs")).strip().lower()
    if storage not in ("nfs", "local"):
        raise ValueError("storage must be nfs or local")
    p2p_managed = node_images_p2p_managed()
    rsync_default = storage == "local" and bool(preset.image_store) and not p2p_managed
    rsync = body.get("rsync_to_local", body.get("rsync", rsync_default)) is True
    if rsync and p2p_managed:
        raise ValueError(
            "rsync staging is off on this cluster: NVIS-P2P distributes its images. "
            "Run a P2P campaign for the image instead, then launch with node-local storage."
        )
    if rsync and storage != "local":
        raise ValueError("rsync requires storage=local")
    local_dir = check_local_container_dir(body.get("local_container_dir"))

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
        if _version_tuple(requested_cuda) < (13, 0):
            raise ValueError("nccl-2.31.2 requires CUDA 13.0 or newer")
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
        "local_container_dir": local_dir,
        "container_profile": preset.container_profile,
        "cuda_version": cuda or None,
        "nccl_version": nccl or None,
        "max_steps": preset.max_steps,
        "max_runtime_minutes": preset.max_runtime_minutes,
        "requires_infiniband": preset.requires_infiniband,
    }
