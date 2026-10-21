# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Built-in seed data and one-time migration for the NVIS catalog.

``migrate(repo)`` is idempotent and safe to call on every boot:

* sets/advances ``meta.schema_version``;
* upserts the built-in **platforms** and **tools** *only when a doc with that id does
  not already exist* (so operator edits are never clobbered). Built-ins are tagged
  ``source="seed"``;
* on first run (no projects yet), creates a **default project/cluster from the current
  environment** (``NVIS_*`` / ``RACK_*``) so existing single-cluster deployments keep
  working unchanged, and records it as the active context.

Platform ``tool_param_defaults`` capture the generation-specific knobs that used to be
hard-coded in the shell/sbatch scripts (``--gpu`` flag, ``MNNVL_TOPOLOGY``, the CPU->HCA
affinity mapper, ``CUDA_MAJ``) so tool execution can be made platform-aware.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from . import models
from .store import (
    REPO_ROOT,
    SCHEMA_VERSION,
    TABLE_PLATFORMS,
    TABLE_PROJECTS,
    TABLE_TOOLS,
    Repo,
)

_logger = logging.getLogger(__name__)


def _effective_env(env: dict[str, str] | None) -> dict[str, str]:
    """Merge os.environ with the persisted Settings overrides (overrides win).

    The default project must capture the *effective* config -- a deployment may keep
    its ``NVIS_*`` templates in ``var/cache/job_env_overrides.json`` (Settings UI)
    rather than in real env, so reading only ``os.environ`` would lose them.
    """
    merged = dict(os.environ if env is None else env)
    ov_path = Path(
        os.environ.get("NVIS_JOB_ENV_OVERRIDES_FILE", "")
        or str(REPO_ROOT / "var" / "cache" / "job_env_overrides.json")
    )
    try:
        if ov_path.is_file():
            raw = json.loads(ov_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        merged[k] = v
    except (OSError, json.JSONDecodeError):
        pass
    return merged

# ---------------------------------------------------------------------------
# Platforms (hardware/system SKUs)
# ---------------------------------------------------------------------------


SEED_PLATFORMS: list[dict[str, Any]] = [
    {
        "id": "gb200-nvl72",
        "name": "GB200 NVL72",
        "generation": "Blackwell",
        "gpu_model": "GB200",
        "superchip": "GB200",
        "cpu_vendor": "Grace",
        "cpu_arch": "arm64",
        "system_vendor": "NVIDIA",
        "form_factor": "NVL72",
        "geometry": {
            "gpus_per_node": 4,
            "nodes_per_tray": 2,
            "trays_per_rack": 18,
            "gpus_per_rack": 72,
            "nvlink_domain": 72,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-NDR"},
        "capabilities": ["nvlink", "nvswitch", "imex", "mnnvl", "grace"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "gb200",
            "MNNVL_TOPOLOGY": "gb200_nvl72r1_c2g4_topology",
            "RACK_CLUSTER_PRODUCT_LINE": "gb200",
            "CUDA_MAJ": "12",
        },
        "fabric": {
            "scaleout": "ib",
            "rails_per_gpu": 1,
            "rail_rate_gbps": 400,
            "ns_ifname": "bond0",
            # 8 racks x 72 GPUs = 576 GPUs per SU. Corroborated on Nscale TX 16K, which
            # is 112 racks in 14 SUs. Leaf/spine counts are left unset rather than
            # guessed; fill them from the site's fabric design.
            "racks_per_su": 8,
        },
        "status": "ga",
        "description": "Grace-Blackwell GB200, 72-GPU NVLink domain (NVL72).",
        "source": "seed",
    },
    {
        "id": "gb300-nvl72",
        "name": "GB300 NVL72",
        "generation": "Blackwell-Ultra",
        "gpu_model": "GB300",
        "superchip": "GB300",
        "cpu_vendor": "Grace",
        "cpu_arch": "arm64",
        "system_vendor": "NVIDIA",
        "form_factor": "NVL72",
        "geometry": {
            "gpus_per_node": 4,
            "nodes_per_tray": 2,
            "trays_per_rack": 18,
            "gpus_per_rack": 72,
            "nvlink_domain": 72,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-NDR"},
        "capabilities": ["nvlink", "nvswitch", "imex", "mnnvl", "grace"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "gb300",
            "MNNVL_TOPOLOGY": "gb200_nvl72r1_c2g4_topology",
            "RACK_CLUSTER_PRODUCT_LINE": "gb300",
            "CUDA_MAJ": "12",
        },
        "fabric": {
            "scaleout": "ib",
            "rails_per_gpu": 1,
            "rail_rate_gbps": 800,
            "ns_ifname": "bond0",
            # See gb200-nvl72: 8 racks per SU, 576 GPUs.
            "racks_per_su": 8,
        },
        "status": "ga",
        "description": "Grace-Blackwell Ultra GB300, 72-GPU NVLink domain (NVL72).",
        "source": "seed",
    },
    {
        "id": "b200-hgx-intel",
        "name": "B200 HGX (Intel)",
        "generation": "Blackwell",
        "gpu_model": "B200",
        "superchip": "",
        "cpu_vendor": "Intel",
        "cpu_arch": "x86_64",
        "system_vendor": "Dell",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 8,
            "gpus_per_rack": 64,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-NDR"},
        "capabilities": ["nvlink", "nvswitch"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "b200",
            "RACK_CLUSTER_PRODUCT_LINE": "b200",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 400, "ns_ifname": "bond0"},
        "status": "ga",
        "description": "HGX B200 8-GPU node, Intel x86 host (Dell/other OEM).",
        "source": "seed",
    },
    {
        "id": "b200-hgx-amd",
        "name": "B200 HGX (AMD)",
        "generation": "Blackwell",
        "gpu_model": "B200",
        "superchip": "",
        "cpu_vendor": "AMD",
        "cpu_arch": "x86_64",
        "system_vendor": "SMC",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 8,
            "gpus_per_rack": 64,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "RoCE"},
        "capabilities": ["nvlink", "nvswitch", "roce"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "b200",
            "RACK_CLUSTER_PRODUCT_LINE": "b200",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "roce", "rails_per_gpu": 1, "rail_rate_gbps": 400, "ns_ifname": "bond0"},
        "status": "ga",
        "description": "HGX B200 8-GPU node, AMD x86 host (Supermicro/other OEM), RoCE fabric.",
        "source": "seed",
    },
    {
        "id": "b300-hgx",
        "name": "B300 HGX",
        "generation": "Blackwell-Ultra",
        "gpu_model": "B300",
        "superchip": "",
        "cpu_vendor": "Intel",
        "cpu_arch": "x86_64",
        "system_vendor": "Dell",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 8,
            "gpus_per_rack": 64,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-NDR"},
        "capabilities": ["nvlink", "nvswitch"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "b300",
            "RACK_CLUSTER_PRODUCT_LINE": "b300",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 800, "ns_ifname": "bond0"},
        "status": "ga",
        "description": "HGX B300 8-GPU node, x86 host.",
        "source": "seed",
    },
    {
        "id": "rtx-pro-6000-blackwell",
        "name": "RTX PRO 6000 Blackwell",
        "generation": "Blackwell",
        "gpu_model": "RTX PRO 6000 Blackwell",
        "superchip": "",
        "cpu_vendor": "AMD",
        "cpu_arch": "x86_64",
        "system_vendor": "",
        "form_factor": "PCIe",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 1,
            "gpus_per_rack": 8,
            "nvlink_domain": 0,
        },
        "interconnect": {"scaleup": "PCIe", "scaleout": "Spectrum-X RoCE"},
        "capabilities": ["roce"],
        "tool_param_defaults": {
            "RACK_CLUSTER_PRODUCT_LINE": "rtx6000",
        },
        "fabric": {
            "scaleout": "roce",
            "rails_per_gpu": 1,
            "rail_rate_gbps": 400,
            "ns_ifname": "enp0s5",
        },
        "status": "ga",
        "description": (
            "Eight NVIDIA RTX PRO 6000 Blackwell PCIe GPUs with eight "
            "400 Gb/s Spectrum-X RoCE rails."
        ),
        "source": "seed",
    },
    {
        "id": "dgx-b200",
        "name": "DGX B200",
        "generation": "Blackwell",
        "gpu_model": "B200",
        "superchip": "",
        "cpu_vendor": "Intel",
        "cpu_arch": "x86_64",
        "system_vendor": "DGX",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 8,
            "gpus_per_rack": 64,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-NDR"},
        "capabilities": ["nvlink", "nvswitch"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "b200",
            "RACK_CLUSTER_PRODUCT_LINE": "dgx-b200",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 400, "ns_ifname": "bond0"},
        "status": "ga",
        "description": "NVIDIA DGX B200 8-GPU appliance.",
        "source": "seed",
    },
    {
        "id": "dgx-b300",
        "name": "DGX B300",
        "generation": "Blackwell-Ultra",
        "gpu_model": "B300",
        "superchip": "",
        "cpu_vendor": "Intel",
        "cpu_arch": "x86_64",
        "system_vendor": "DGX",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            # Four 10U appliances per rack by default; a cluster may override placement.
            "trays_per_rack": 4,
            "gpus_per_rack": 32,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink5", "scaleout": "InfiniBand-XDR"},
        "capabilities": ["nvlink", "nvswitch"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "b300",
            "RACK_CLUSTER_PRODUCT_LINE": "dgx-b300",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 800, "ns_ifname": "bond0"},
        "status": "ga",
        "description": "NVIDIA DGX B300 8-GPU Blackwell Ultra appliance, Intel x86 host, 800 Gb/s fabric.",
        "source": "seed",
    },
    {
        "id": "vr200-nvl",
        "name": "VR200 NVL (Vera Rubin)",
        "generation": "Rubin",
        "gpu_model": "VR200",
        "superchip": "VR200",
        "cpu_vendor": "Grace",
        "cpu_arch": "arm64",
        "system_vendor": "NVIDIA",
        "form_factor": "NVL72",
        "geometry": {
            "gpus_per_node": 4,
            "nodes_per_tray": 2,
            "trays_per_rack": 18,
            "gpus_per_rack": 72,
            "nvlink_domain": 72,
        },
        "interconnect": {"scaleup": "NVLink6", "scaleout": "InfiniBand-XDR"},
        "capabilities": ["nvlink", "nvswitch", "imex", "mnnvl", "grace"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "vr200",
            "RACK_CLUSTER_PRODUCT_LINE": "vr200",
            "CUDA_MAJ": "13",
        },
        "fabric": {
            "scaleout": "ib",
            "rails_per_gpu": 1,
            "rail_rate_gbps": 800,
            "ns_ifname": "bond0",
            # VR200 spans more racks per SU than NVL72, but the number is not settled
            # here. Left at 0 ("no SU model") deliberately: an invented value would draw
            # a confidently wrong floor plan, and 0 makes callers degrade to ungrouped
            # racks instead. Set it from the Vera Rubin reference architecture.
        },
        "status": "future",
        "description": "Vera Rubin VR200 NVL (forward-looking; geometry provisional).",
        "source": "seed",
    },
    {
        "id": "r200-hgx",
        "name": "R200 HGX",
        "generation": "Rubin",
        "gpu_model": "R200",
        "superchip": "",
        "cpu_vendor": "Intel",
        "cpu_arch": "x86_64",
        "system_vendor": "Dell",
        "form_factor": "HGX-8",
        "geometry": {
            "gpus_per_node": 8,
            "nodes_per_tray": 1,
            "trays_per_rack": 8,
            "gpus_per_rack": 64,
            "nvlink_domain": 8,
        },
        "interconnect": {"scaleup": "NVLink6", "scaleout": "InfiniBand-XDR"},
        "capabilities": ["nvlink", "nvswitch"],
        "tool_param_defaults": {
            "GPU_MODEL_FLAG": "r200",
            "RACK_CLUSTER_PRODUCT_LINE": "r200",
            "CUDA_MAJ": "13",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 800, "ns_ifname": "bond0"},
        "status": "future",
        "description": "Rubin R200 HGX, x86 host (forward-looking).",
        "source": "seed",
    },
]


# ---------------------------------------------------------------------------
# Tools (the bin/scripts surfaced as documents)
# ---------------------------------------------------------------------------


def _tool(
    tool_id: str,
    name: str,
    category: str,
    script: str,
    env_key: str,
    surfaces: list[str],
    mode: str,
    target: str,
    *,
    capabilities: list[str] | None = None,
    description: str = "",
    ai_context: str = "",
    subtitle: str = "",
    button_label: str = "",
    icon: str = "",
    destructive: bool = False,
    order: int = 100,
    launch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    compatible: dict[str, Any]
    if capabilities:
        compatible = {"capabilities": capabilities}
    else:
        compatible = {"match": "all"}
    return {
        "id": tool_id,
        "name": name,
        "category": category,
        "script": script,
        "env_key": env_key,
        "params": [],
        "compatible": compatible,
        "surfaces": surfaces,
        "execution": {"mode": mode, "target": target},
        "description": description,
        "ai_context": ai_context,
        "status": "ga",
        "source": "seed",
        "subtitle": subtitle,
        "button_label": button_label,
        "icon": icon,
        "destructive": destructive,
        "order": order,
        # A tool with no launch binding is catalogued but has no widget button. Binding is
        # opt-in per tool so seeding this field cannot turn a reference entry into something
        # an operator can fire by accident.
        "launch": launch or {"kind": "none"},
    }


def _preset_tool(
    tool_id: str,
    name: str,
    subtitle: str,
    description: str,
    preset_id: str,
    surfaces: list[str],
    target: str,
    order: int,
    *,
    category: str = "validation",
    capabilities: list[str] | None = None,
    scopes: list[str] | None = None,
    min_nodes: int = 0,
    max_nodes: int = 0,
) -> dict[str, Any]:
    """A vetted recipe from the preset engine, as a catalog document.

    The *recipe* (pins, image digests, node eligibility, plan hash) stays in
    ``validation_presets.py`` where it is code-owned and cannot be weakened from a browser.
    What moves here is everything the UI was hard-coding anyway: that the button exists, what
    it is called, which surface it belongs on, and what it says. That is the split the version
    drift came from -- ``run-validation-preset.sh`` moved to NCCL 2.31.2 while three
    components still read 2.30.7 out of their own source.
    """
    return _tool(
        tool_id,
        name,
        category,
        "run-validation-preset.sh",
        "RACK_VALIDATION_PRESET_SCRIPT",
        surfaces,
        "async",
        target,
        capabilities=capabilities,
        description=description,
        subtitle=subtitle,
        button_label="Plan",
        icon="ClipboardCheck",
        order=order,
        launch={
            "kind": "validation-preset",
            "preset_id": preset_id,
            "scopes": scopes or [target],
            # MNUberGEMM is a soak, not an acceptance result; everything else files as
            # validation. See the note on mnubergemm-stress.
            "body": {"family": "stress"} if preset_id.startswith("mnubergemm") else {},
            "min_nodes": min_nodes,
            "max_nodes": max_nodes,
        },
    )


SEED_TOOLS: list[dict[str, Any]] = [
    _tool(
        "sysinfo", "System Info / Precheck", "hardware", "sysinfo.sh", "RACK_SYSINFO_SCRIPT",
        ["rackDetail.hardware", "clusterDashboard"], "stream", "rack",
        description="pdsh BIOS / OS / GPU / NIC inventory and precheck for a rack.",
        ai_context="sysinfo collects per-node BIOS, firmware, OS, GPU and NIC details via pdsh.",
    ),
    _tool(
        "hw-check", "Hardware Check", "hardware", "hw_check_pdsh.sh", "RACK_HW_CHECK_SCRIPT",
        ["rackDetail.hardware"], "async", "rack",
        description="pdsh hardware sanity check across rack nodes.",
    ),
    _tool(
        "hardware-deep-audit", "Hardware Deep Audit", "hardware",
        "hardware_deep_audit.sh", "RACK_HARDWARE_DEEP_AUDIT_SCRIPT",
        ["rackDetail.hardware", "analytics"], "async", "rack",
        capabilities=["nvlink"],
        description=(
            "Repeatable GB300 node audit for GPU inventory/temperature/ECC/recovery, "
            "kernel Xid/PCIe/EDAC faults, IMEX/slurmd/CMDaemon and RAID readiness."
        ),
        ai_context=(
            "The hardware deep audit emits one structured result per node. Confirmed failures "
            "are recorded in node issue history and immediately drained in Slurm with the "
            "workflow id and exact reason; warnings and unsupported probes are inconclusive."
        ),
    ),
    _tool(
        "nvlink-network-deep-audit", "NVLink / Network Deep Audit", "networking",
        "nvlink_network_deep_audit.sh", "RACK_NVLINK_NETWORK_DEEP_AUDIT_SCRIPT",
        ["rackDetail.networking", "analytics"], "async", "rack",
        capabilities=["nvlink"],
        description=(
            "Repeatable GB300 NVLink fabric, forced GPU data-path, IMEX, IB rail/rate/error "
            "counter and mlx5 bond audit."
        ),
        ai_context=(
            "The network deep audit distinguishes a missing data-path runtime from a pass. "
            "Confirmed NVLink/IB/bond failures are recorded and immediately drained in Slurm."
        ),
    ),
    _tool(
        "fabric-probe", "Fabric Probe (NIC classifier)", "networking",
        "fabric_probe.sh", "RACK_FABRIC_PROBE_SCRIPT", ["infraIbNvlink"], "sync", "node",
        description=(
            "Classifies a compute node's RDMA devices into the compute rails (the plane "
            "NCCL/UCX should use) vs the storage/NS plane, and flags detected-vs-expected "
            "discrepancies against the active platform's fabric profile."
        ),
        ai_context=(
            "fabric_probe.sh sources lib/fabric.inc.sh to pick ACTIVE mlx5 ports on the desired "
            "link layer (InfiniBand or RoCE) at the top line rate, excludes slower BlueField-3 "
            "storage NICs, and emits NCCL_IB_HCA / NCCL_SOCKET_IFNAME / UCX_NET_DEVICES as JSON. "
            "Surfaced by GET /api/fabric-probe and the Infrastructure > InfiniBand/NVLink view."
        ),
    ),
    _tool(
        "validation-singlerack", "Stage 2 — NEMO Training", "validation",
        "run_singlerack.sh", "RACK_VALIDATION_SCRIPT", ["rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        description="NCCL collectives + Nemotron training validation for one rack.",
        ai_context="run_singlerack runs NCCL all-reduce and a Nemotron training step per rack.",
        subtitle="NCCL + LLMB, all trays",
        button_label="Run NEMO",
        icon="Brain",
        order=30,
        launch={
            "kind": "async-job",
            "endpoint": "/api/run-validation-async",
            "dialog": "launch-options",
        },
    ),
    _tool(
        "nccl-fullrack-rounds", "NCCL Benchmark (Stage-1)", "validation",
        "nccl_fullrack_rounds.sh", "RACK_NCCL_FULLRACK_SCRIPT", ["rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        # Seed default only: list_tools derives the live card from RACK_NCCL_FULLRACK_LAUNCHER
        # (nccl_stage1.apply_card), so it always describes the launcher that actually runs.
        subtitle="LLMB NCCL test set (llmb-run)",
        button_label="Run NCCL",
        icon="Activity",
        order=20,
        launch={
            "kind": "async-job",
            "endpoint": "/api/run-nccl-fullrack-async",
            "dialog": "launch-options",
            # Which controls the options dialog offers. Only the direct NCCL paths resolve
            # libnccl via LD_LIBRARY_PATH, so only they can honour a version choice; a
            # container launch would show a selector that does nothing.
            "body": {"show_nccl_version": True, "show_iters": True},
        },
        description=(
            "Stage-1 NCCL acceptance. Default launcher llmb: llmb-run microbenchmark_nccl runs the LLMB "
            "install's whole NCCL test set (stock: 24 tests over five collectives). Launcher mpi: "
            "all_reduce via host mpirun, 5 rounds by default. Chosen by RACK_NCCL_FULLRACK_LAUNCHER."
        ),
        ai_context=(
            "Rack/node Stage-1 NCCL acceptance. With RACK_NCCL_FULLRACK_LAUNCHER=llmb (default) it "
            "runs llmb-run microbenchmark_nccl: the LLMB install's whole test set in one job (stock: "
            "24 tests over all_reduce, all_gather, reduce_scatter, alltoall, sendrecv); rounds and "
            "NCCL version do not apply. With mpi it runs host-mpirun all_reduce, 5 rounds by default. "
            "Either way it drops an NCCL-1Rack-<rack> log into log_dir for the dashboard and parser."
        ),
    ),
    _tool(
        "nemo-xrack", "Multirack NEMO", "validation", "run-nemo-xrack.sh", "RACK_NEMO_XRACK_SCRIPT",
        ["multirack"], "async", "multirack",
        capabilities=["nvlink"],
        description="Cross-rack Nemotron training validation.",
    ),
    # Catalogued, deliberately not bound: the rack Validation tab has only ever shown a
    # *disabled* Llama card pointing at multirack, so giving it a working button here would be
    # a behaviour change smuggled in under a refactor. No binding means no widget, which is
    # its status quo on a fresh install and on an upgrade alike. Bind it in the Tool Registry
    # if a site wants it.
    _tool(
        "llama", "Llama Training", "performance", "run-llama.sh", "RACK_LLAMA_SCRIPT",
        ["rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        description="Llama training workload.",
        order=60,
    ),
    _tool(
        "clusterkit", "ClusterKit Bandwidth/Latency", "performance", "run_clusterkit.sh",
        "RACK_CLUSTERKIT_SCRIPT", ["rackDetail.networking", "rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        description="ClusterKit interconnect bandwidth/latency sweep (CPU->HCA affinity aware).",
        subtitle="IB bandwidth per HCA",
        button_label="Run ClusterKit",
        icon="Network",
        order=80,
        launch={"kind": "async-job", "endpoint": "/api/run-clusterkit-async"},
    ),
    _tool(
        "clusterkit-multirack", "ClusterKit Rack-to-Rack", "performance", "run_clusterkit.sh",
        "RACK_CLUSTERKIT_SCRIPT", ["multirack", "tools"], "async", "multirack",
        description=(
            "ClusterKit across racks: every compute rail measured rack against rack, each rack a scope "
            "(bisection_bw by default)."
        ),
        ai_context=(
            "Multi-rack form of the clusterkit tool, same script and endpoint: POST "
            "/api/run-clusterkit-async with racks:[...] (two or more). Each rack's placed hosts become a "
            "ClusterKit scope via RACK_CLUSTERKIT_SCOPES; the cluster's clusterkit.multirack_tests choose the "
            "test cases. Measures scale-out bandwidth between racks under many concurrent flows, which "
            "pairwise tests and single-rack NCCL cannot."
        ),
        subtitle="Rack-to-rack bandwidth per rail",
        button_label="Run ClusterKit",
        icon="Network",
        order=85,
        launch={"kind": "async-job", "endpoint": "/api/run-clusterkit-async"},
    ),
    _tool(
        "nvloom", "NVLoom Singlerack", "performance", "run_nvloom_singlerack.sh",
        "RACK_NVLOOM_SINGLERACK_SCRIPT", ["rackDetail.networking", "rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        description="NVLoom NVLink bandwidth heatmaps for one rack.",
        subtitle="GPU-to-rack fabric heatmaps",
        button_label="Run nvloom",
        icon="Boxes",
        order=75,
        launch={"kind": "async-job", "endpoint": "/api/run-nvloom-async"},
    ),
    _tool(
        "llmb-nccl", "NCCL Microbenchmark (LLMB)", "performance", "run_llmb_nccl.sh",
        "RACK_LLMB_NCCL_SCRIPT",
        ["rackDetail.networking", "rackDetail.validation", "tools"], "async", "rack",
        capabilities=["nvlink"],
        description=(
            "NCCL microbenchmark via dgxc-benchmarking llmb-run (microbenchmark_nccl, fp8). "
            "Resolves NVIS rack ids to real Slurm nodes and computes GPU scale (nodes*4)."
        ),
        subtitle="llmb-run microbenchmark_nccl, fp8",
        button_label="Run LLMB NCCL",
        icon="Gauge",
        order=85,
        launch={
            "kind": "async-job",
            "endpoint": "/api/run-llmb-nccl-async",
            "dialog": "launch-options",
            "body": {"show_nccl_version": True, "show_iters": True},
        },
        ai_context=(
            "run_llmb_nccl wraps llmb-run submit -w microbenchmark_nccl -d fp8 (MOP §7.3). "
            "Single-rack GB300 NVL72 = scale 72 (18 nodes * 4 GPUs). It resolves the real "
            "Slurm node hostnames from sinfo (rack id != node hostname) before submitting."
        ),
    ),
    _tool(
        "llmb-nccl-multirack", "NCCL Microbenchmark (LLMB, Multi-Rack)", "performance",
        "run_llmb_nccl.sh", "RACK_LLMB_NCCL_SCRIPT", ["multirack", "tools"], "async", "multirack",
        capabilities=["nvlink"],
        description=(
            "Cross-rack NCCL microbenchmark via llmb-run across multiple NVIS racks "
            "(one RACK arg per rack)."
        ),
        ai_context=(
            "Cross-rack form of run_llmb_nccl (MOP §7.3): passes multiple rack ids; GPU scale "
            "= 72 x number of GB300 NVL72 racks. Same wrapper/script as the single-rack tool "
            "(llmb-nccl), differentiated by the multirack surface/target."
        ),
    ),
    _tool(
        "mnubergemm-stress", "MNU Bergemm Stress", "performance", "run_mnubergemm_stress.sh",
        "RACK_MNUBERGEMM_STRESS_SCRIPT", ["rackDetail.validation"], "async", "rack",
        capabilities=["mnnvl"],
        description="Multi-node NVLink GEMM stress (NVL72 MNNVL domains).",
        subtitle="Single-node soak, not the multi-node test",
        button_label="Run Stress",
        icon="Flame",
        order=70,
        # "stress" rather than "validation": the command store files activities by family, and
        # a soak run listed as a validation result is a result nobody asked for.
        launch={
            "kind": "async-job",
            "endpoint": "/api/run-mnubergemm-stress-async",
            "body": {"family": "stress"},
        },
    ),
    _tool(
        "gpu-reset", "GPU Reset", "maintenance", "gpu_reset.sh", "RACK_GPU_RESET_SCRIPT",
        ["rackDetail.services"], "async", "rack",
        description="Reset GPUs on rack nodes.",
    ),
    _tool(
        "nvswitch-audit", "NVSwitch Audit", "networking", "nvswitches_rack_audit.sh",
        "RACK_NVSWITCH_AUDIT_SCRIPT", ["rackDetail.networking"], "stream", "rack",
        capabilities=["nvswitch"],
        description="Audit NVSwitch trays in a rack.",
    ),
    _tool(
        "nvswitch-sdn-reset", "NVSwitch SDN Reset", "maintenance", "nvswitch_sdn_reset.sh",
        "RACK_NVSWITCH_SDN_RESET_SCRIPT", ["rackDetail.networking"], "async", "rack",
        capabilities=["nvswitch"],
        description="Reset NVSwitch SDN configuration.",
    ),
    _tool(
        "nvswitch-system-reset", "NVSwitch System Reset", "maintenance",
        "nvswitch_system_reset.sh", "RACK_NVSWITCH_SYSTEM_RESET_SCRIPT",
        ["rackDetail.networking"], "async", "rack", capabilities=["nvswitch"],
        description="Full NVSwitch system reset.",
    ),
    _tool(
        "imex-reset", "IMEX Reset", "maintenance", "imex_reset.sh", "RACK_IMEX_RESET_SCRIPT",
        ["rackDetail.services"], "async", "rack", capabilities=["imex"],
        description="Reset the IMEX (internode memory exchange) domain.",
    ),
    _tool(
        "imex-check", "IMEX Check", "hardware", "imex_check.sh", "RACK_IMEX_CHECK_SCRIPT",
        ["rackDetail.services"], "async", "rack", capabilities=["imex"],
        description="Check IMEX domain health.",
    ),
    _tool(
        "parser", "Log Parser (NCCL/NEMO charts)", "validation", "parser.sh", "RACK_PARSER_SCRIPT",
        ["parserDialog"], "sync", "rack",
        description="Parse NCCL/Nemotron logs into chartable JSON.",
    ),
    # No surface, for the same reason as `llama` above: the Validation tab already devotes a
    # dedicated panel to preflight, because this is the one action whose *verdict* is the
    # output. A widget would render a second button for it beside a richer one.
    _tool(
        "nvlink-preflight", "NVLink Preflight — run this first", "validation",
        "nvlink_preflight.sh", "RACK_NVLINK_PREFLIGHT_SCRIPT",
        [], "async", "rack",
        capabilities=["nvlink"],
        description=(
            "Per-node NVLink/IMEX/fabric readiness check. A rack that fails here will fail "
            "Stage 1 in a way that looks like a benchmark problem."
        ),
        subtitle="Run before Stage 1",
        button_label="Run Preflight",
        icon="ShieldCheck",
        order=10,
        launch={"kind": "async-job", "endpoint": "/api/nvlink-preflight-async"},
    ),
    # --- vetted recipes, folded in from validation_presets.PRESETS -----------------------
    # These were a second frozen registry: a Python tuple, a mirrored TypeScript union, and a
    # hard-coded button row in each view. The recipe stays code-owned; the catalogue entry is
    # now a document, so adding one is an edit rather than a release.
    _preset_tool(
        # Subtitle is the seed default only; list_tools derives the live card from the code-owned
        # preset version (_preset_version_subtitle), so this never drifts from what the launcher runs.
        "preset-nccl", "NCCL Acceptance Sweep", "NCCL 2.30.7 · CUDA 13.0+",
        "Direct host NCCL acceptance sweep. Fails closed unless the selected runtime really "
        "is CUDA 13.0+ with NCCL 2.30.7.",
        "nccl-2.31.2", ["rackDetail.validation", "multirack"], "rack", 15,
        capabilities=["nvlink"], scopes=["node", "rack", "multirack"],
    ),
    _preset_tool(
        "preset-llama405b", "Llama 3.1 405B FP8", "1024 GPUs · 50 steps · NCCL 2.30.7",
        "The fixed 256-node profile: TP4/PP8/CP1/VP4, MBS1, GBS6144, FP8, NVLS disabled.",
        "llama31-405b-fp8", ["multirack"], "multirack", 40,
        category="performance", capabilities=["nvlink"], scopes=["multirack"],
    ),
    # Two documents for one recipe, because the two surfaces offer it on different terms. The
    # rack tab has always gated it to exactly one node ("no GPU training workload or rack
    # fanout is included"); the multirack view fans it out across the selection. That was an
    # `if nodeCount !== 1` in a component, which is the kind of per-surface rule the catalog
    # can hold now that a binding carries selection bounds.
    _preset_tool(
        "preset-gpt-oss-node", "GPT-OSS CPU Microbenchmark", "Exactly one node",
        "CPU-overhead validation (kernel-launch latency and tokenizer path) on a single "
        "selected node. No GPU training workload and no rack fan-out.",
        "gpt-oss-cpu", ["rackDetail.validation"], "node", 45,
        category="performance", scopes=["node"], min_nodes=1, max_nodes=1,
    ),
    _preset_tool(
        "preset-gpt-oss", "GPT-OSS CPU Microbenchmark", "One job per selected node",
        "CPU-overhead validation (kernel-launch latency and tokenizer path). No training "
        "workload; each node is independently attributable.",
        "gpt-oss-cpu", ["multirack"], "multirack", 45,
        category="performance", scopes=["node", "rack", "multirack"],
    ),
    _preset_tool(
        "preset-exemplar-sysinfo", "DGXC Exemplar system_info", "dgxc-benchmarking v26.06.01",
        "Runs microbenchmark_system_info once per selected node and preserves each Slurm "
        "stdout.",
        "exemplar-system-info", ["rackDetail.hardware", "multirack"], "rack", 50,
        category="hardware", scopes=["node", "rack", "multirack"],
    ),
    _preset_tool(
        "preset-nemo", "NeMo Validation", "NeMo 26.04.01 · 50 steps",
        "NeMo-only rack or multi-rack validation with the staged 26.04.01 ARM image.",
        "nemo-26.04.01", ["rackDetail.validation", "multirack"], "rack", 35,
        capabilities=["nvlink"], scopes=["rack", "multirack"],
    ),
    _preset_tool(
        "preset-node-gate", "Node Gate", "Cleanup + health check between jobs",
        "Run between two jobs on the same nodes: holds them exclusively, cleans up what the last job left "
        "(GPU processes, pyxis containers, NCCL /dev/shm segments, a stopped GPU service) and fails any node "
        "that is not fit for the next one -- GPU memory held, uncorrected ECC, power brake, NVLink fabric not "
        "Completed, or a new hardware-fault Xid. One verdict line per node.",
        "node-gate", ["rackDetail.validation", "multirack"], "rack", 25,
        category="maintenance", scopes=["node", "rack", "multirack"],
    ),
    _preset_tool(
        "preset-mnubergemm", "MNUberGEMM", "26.03-v1.17 · 60-minute ceiling",
        "Node, rack or multi-rack MNUberGEMM using the container actually staged on the "
        "fleet. Multirack fans out one job per rack — a single allocation spanning two racks "
        "joins two NVLink domains and hangs.",
        "mnubergemm-26.03-v1.17", ["rackDetail.validation", "multirack"], "rack", 55,
        capabilities=["mnnvl"], scopes=["node", "rack", "multirack"],
    ),
]


# ---------------------------------------------------------------------------
# Default project from environment (back-compat for single-cluster deploys)
# ---------------------------------------------------------------------------


def _guess_platform_id(env: dict[str, str]) -> str:
    """Best-effort map of RACK_CLUSTER_PRODUCT_LINE to a seeded platform id."""
    pl = (env.get("RACK_CLUSTER_PRODUCT_LINE") or "").strip().lower()
    pl = "-".join(pl.replace("_", "-").split())
    by_product = {p["tool_param_defaults"].get("RACK_CLUSTER_PRODUCT_LINE", ""): p["id"]
                  for p in SEED_PLATFORMS}
    if pl and pl in by_product:
        return by_product[pl]
    # Loose contains match (e.g. "GB200 NVL72" -> gb200-nvl72).
    for token, pid in (("gb300", "gb300-nvl72"), ("gb200", "gb200-nvl72"),
                       ("dgx-b300", "dgx-b300"), ("dgx-b200", "dgx-b200"),
                       ("b300", "b300-hgx"), ("b200", "b200-hgx-intel"),
                       ("vr200", "vr200-nvl"), ("r200", "r200-hgx")):
        if token in pl:
            return pid
    return "gb200-nvl72"


def build_default_project_from_env(env: dict[str, str] | None = None) -> models.Project:
    """Construct the default Project/Cluster from the current ``NVIS_*`` / ``RACK_*`` env."""
    env = _effective_env(env)
    customer = (
        env.get("RACK_CLUSTER_CUSTOMER_NAME")
        or env.get("RACK_CLUSTER_PROJECT")
        or env.get("RACK_WEB_PROJECT_NAME")
        or "Default"
    ).strip() or "Default"
    cluster_name = (
        env.get("RACK_CLUSTER_NAME") or env.get("CLUSTER_NAME") or "Default Cluster"
    ).strip() or "Default Cluster"

    cluster = models.Cluster(
        id="default",
        name=cluster_name,
        platform_id=_guess_platform_id(env),
        cluster_manager=(env.get("RACK_PLATFORM") or "bcm").strip().lower() or "bcm",
        site_prefix=(env.get("RACK_MULTIRACK_SITE_PREFIX") or "").strip(),
        naming=models.ClusterNaming(
            rack_id_template=(env.get("NVIS_RACK_ID") or "").strip(),
            ct_id_template=(env.get("NVIS_CT_ID") or "").strip(),
            nvsw_id_template=(env.get("NVIS_NVSW_ID") or "").strip(),
            ct_joiner=env.get("NVIS_CT_JOINER", "-ct") or "-ct",
            slurm_hostlist=(env.get("NVIS_SLURM_HOSTLIST") or "").strip(),
        ),
        geometry=models.ClusterGeometry(),
        connection=models.ClusterConnection(
            exec_mode=(env.get("RACK_EXEC_MODE") or "").strip(),
            ssh_host=(env.get("RACK_SSH_HOST") or "").strip(),
            ssh_user=(env.get("RACK_SSH_USER") or "").strip(),
            ssh_key=(env.get("RACK_SSH_KEY") or "").strip(),
            remote_base_path=(env.get("RACK_REMOTE_BASE_PATH") or "").strip(),
        ),
    )
    return models.Project(
        id="default",
        name=customer if customer != "Default" else "Default Project",
        customer=customer,
        description="Auto-created from environment on first boot.",
        clusters=[cluster],
        source="seed",
    )


# ---------------------------------------------------------------------------
# Migration entrypoint
# ---------------------------------------------------------------------------


# Fields the widget work added to ``Tool``. A document written before it has none of these
# keys at all, which is what makes the backfill below safe: absent is unambiguous, whereas a
# present-but-default value could be a deliberate operator choice.
_WIDGET_FIELDS = ("subtitle", "button_label", "icon", "destructive", "order", "launch")

# Surfaces to ADD to an existing document, never to remove.
#
# These three tools have always been rendered on the rack Validation tab while their seed
# documents only claimed `rackDetail.networking` -- the placement lived in the component, so
# nothing kept the two honest. Now that the board reads `surfaces`, an already-seeded
# deployment would lose three buttons it has today unless the old documents are told. Keyed
# by tool id and additive, so an operator who curated their own surface list keeps it.
_SURFACE_BACKFILL = {
    "clusterkit": ["rackDetail.validation"],
    "nvloom": ["rackDetail.validation"],
    "llmb-nccl": ["rackDetail.validation"],
}

# These launchers all enter a NeMo/Megatron recipe validated for NVLink platforms. The RTX
# PRO 6000 platform is PCIe-only, and the recipe's own GPU-profile parser rejects ``rtx6000``.
# Requiring the hardware capability makes the catalog refuse the launch before Slurm submission.
_NEMO_TOOL_IDS = (
    "llama",
    "nemo-xrack",
    "preset-llama405b",
    "preset-nemo",
    "validation-singlerack",
)


def _migrate_nemo_compatibility(repo: Repo) -> int:
    """Replace the former match-all NeMo compatibility on pre-v2 catalogs."""
    changed = 0
    match_all = models.ToolCompatibility()
    for tool_id in _NEMO_TOOL_IDS:
        raw = repo.get(TABLE_TOOLS, tool_id)
        if raw is None:
            continue
        if models.ToolCompatibility.from_dict(raw.get("compatible")) != match_all:
            continue
        repo.upsert(
            TABLE_TOOLS,
            models.Tool.from_dict(
                {**raw, "compatible": {"capabilities": ["nvlink"]}}
            ).to_dict(),
        )
        changed += 1
    return changed


def _backfill_tool_widgets(repo: Repo) -> int:
    """Teach pre-widget Tool documents about launch bindings, without clobbering edits.

    ``migrate`` only ever *inserts* seed tools, so operator edits survive a boot. The same
    property means a schema addition never reaches an existing deployment: every already-
    seeded cluster -- which is all of them -- would keep launch-less documents and render an
    empty board, silently losing every button on the Validation tab.

    So this fills in **only keys the stored document does not have**. An old document has no
    ``launch`` key whatsoever, so there is nothing to overwrite; a document saved since has
    one, and is left exactly as the operator left it. That is why the check is
    ``key not in raw`` rather than a comparison against the default -- ``order: 100`` or
    ``launch.kind == "none"`` are legitimate operator choices and must survive.
    """
    changed = 0
    by_id = {t["id"]: t for t in SEED_TOOLS}
    for raw in repo.list(TABLE_TOOLS):
        seed_doc = by_id.get(raw.get("id"))
        if seed_doc is None:
            continue  # an operator's own tool; we have no opinion about it
        patch = {k: seed_doc[k] for k in _WIDGET_FIELDS if k not in raw and k in seed_doc}
        wanted = _SURFACE_BACKFILL.get(raw.get("id"), [])
        have = raw.get("surfaces")
        if wanted and isinstance(have, list):
            missing = [s for s in wanted if s not in have]
            if missing:
                patch["surfaces"] = [*have, *missing]
        if not patch:
            continue
        repo.upsert(TABLE_TOOLS, models.Tool.from_dict({**raw, **patch}).to_dict())
        changed += 1
    return changed


def migrate(repo: Repo, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Idempotently seed/migrate the catalog. Returns a small summary for logging."""
    try:
        previous_schema_version = int(repo.get_meta("schema_version", 0) or 0)
    except (TypeError, ValueError):
        previous_schema_version = 0
    summary: dict[str, Any] = {
        "platforms_added": 0,
        "tools_added": 0,
        "tools_backfilled": 0,
        "nemo_compatibility_migrated": 0,
        "default_project_created": False,
    }

    # Seed platforms/tools that don't yet exist (preserve operator edits).
    for p in SEED_PLATFORMS:
        if repo.get(TABLE_PLATFORMS, p["id"]) is None:
            repo.upsert(TABLE_PLATFORMS, models.Platform.from_dict(p).to_dict())
            summary["platforms_added"] += 1
    for t in SEED_TOOLS:
        if repo.get(TABLE_TOOLS, t["id"]) is None:
            repo.upsert(TABLE_TOOLS, models.Tool.from_dict(t).to_dict())
            summary["tools_added"] += 1
    if previous_schema_version < 2:
        summary["nemo_compatibility_migrated"] = _migrate_nemo_compatibility(repo)
    summary["tools_backfilled"] = _backfill_tool_widgets(repo)

    # On first run, create a default project from the environment and make it active.
    if not repo.list(TABLE_PROJECTS):
        proj = build_default_project_from_env(env)
        repo.upsert(TABLE_PROJECTS, proj.to_dict())
        repo.set_meta(
            "active",
            {"project": proj.id, "cluster": proj.clusters[0].id if proj.clusters else ""},
        )
        summary["default_project_created"] = True

    # Mirror the NVIDIA NPI Knowledge Portal reference catalog when a snapshot is present
    # (env dir, var/cache sync, or vendored offline copy). No-op otherwise -- purely
    # additive, never touches the runnable ``tools`` table.
    try:
        from .npi_import import import_npi_catalog

        summary["npi"] = import_npi_catalog(repo)
    except Exception:
        # WARNING, not debug (FR-4c). Staying defensive here is right — a reference catalog must
        # never stop a boot mid-bring-up — but `_logger.debug` made the failure *invisible*, and
        # that is what let the real defect run for months: `npi_import` imported `tomllib` at
        # module level, which does not exist on the container's Python 3.10, so this branch caught
        # a ModuleNotFoundError on every container start and said nothing at the default level.
        # NPI import was simply dead, with no symptom to notice.
        #
        # The import is fixed (it goes through `toml_compat` now), so this path should be
        # unreachable in practice. That is exactly why it must be loud if it is ever reached again:
        # a swallow nobody can see is indistinguishable from working.
        _logger.warning("migrate: NPI reference catalog import failed", exc_info=True)
        summary["npi"] = {}

    repo.set_meta("schema_version", SCHEMA_VERSION)
    return summary
