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
            "CORE_TO_HCA_SCRIPT": "core_to_hca_dgxgb200.sh",
            "CUDA_MAJ": "12",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 400, "ns_ifname": "bond0"},
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
            "CORE_TO_HCA_SCRIPT": "core_to_hca_dgxgb300.sh",
            "CUDA_MAJ": "12",
            "RACK_VALIDATION_CONTAINER_ROOT": "/cluster/OVA/containers/nvftp-8k-known-good/20260824",
            "RACK_VALIDATION_NODE_CONTAINER_DIR": "/opt/containers",
            "RACK_VALIDATION_PROVENANCE_ROOT": "/cluster/OVA/scripts/nscaletx-16k-validation",
            "RACK_STATUS_LOG_DIR": "/cluster/OVA/logs/nscaletx-16k-validation",
            # Existing browser sessions may still submit the former dashboard
            # log root. Keep it read-compatible while all new validation output
            # is standardized under the OVA root above.
            "RACK_STATUS_ALLOWED_PREFIXES": "/cluster/OVA/logs,/app/var/logs",
        },
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 800, "ns_ifname": "bond0"},
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
            "NVIS_RDMA_LINK_TECH": "roce",
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
        "fabric": {"scaleout": "ib", "rails_per_gpu": 1, "rail_rate_gbps": 800, "ns_ifname": "bond0"},
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
    }


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
        "validation-singlerack", "Singlerack Validation (NCCL + NEMO)", "validation",
        "run_singlerack.sh", "RACK_VALIDATION_SCRIPT", ["rackDetail.validation"], "async", "rack",
        description="NCCL collectives + Nemotron training validation for one rack.",
        ai_context="run_singlerack runs NCCL all-reduce and a Nemotron training step per rack.",
    ),
    _tool(
        "nccl-fullrack-rounds", "NCCL Benchmark (Stage-1)", "validation",
        "nccl_fullrack_rounds.sh", "RACK_NCCL_FULLRACK_SCRIPT", ["rackDetail.validation"], "async", "rack",
        capabilities=["nvlink"],
        description="Standalone Stage-1 NCCL acceptance: N rounds of all_reduce (1K..16G) via the working portable launcher.",
        ai_context=(
            "nccl_fullrack_rounds runs the Stage-1 NCCL acceptance test (default 5 rounds of "
            "all_reduce_perf, 1K..16G) on one rack using the bond0-pinned/portable launcher, and "
            "drops an NCCL-1Rack-<rack> log into log_dir so the dashboard + parser pick it up. "
            "Use this to validate NCCL alone, independent of the full NEMO training run."
        ),
    ),
    _tool(
        "nemo-xrack", "Multirack NEMO", "validation", "run-nemo-xrack.sh", "RACK_NEMO_XRACK_SCRIPT",
        ["multirack"], "async", "multirack",
        description="Cross-rack Nemotron training validation.",
    ),
    _tool(
        "llama", "Llama Training", "performance", "run-llama.sh", "RACK_LLAMA_SCRIPT",
        ["rackDetail.validation"], "async", "rack",
        description="Llama training workload.",
    ),
    _tool(
        "clusterkit", "ClusterKit Bandwidth/Latency", "performance", "run_clusterkit.sh",
        "RACK_CLUSTERKIT_SCRIPT", ["rackDetail.networking"], "async", "rack",
        capabilities=["nvlink"],
        description="ClusterKit interconnect bandwidth/latency sweep (CPU->HCA affinity aware).",
    ),
    _tool(
        "nvloom", "NVLoom Singlerack", "performance", "run_nvloom_singlerack.sh",
        "RACK_NVLOOM_SINGLERACK_SCRIPT", ["rackDetail.networking"], "async", "rack",
        capabilities=["nvlink"],
        description="NVLoom NVLink bandwidth heatmaps for one rack.",
    ),
    _tool(
        "llmb-nccl", "NCCL Microbenchmark (LLMB)", "performance", "run_llmb_nccl.sh",
        "RACK_LLMB_NCCL_SCRIPT", ["rackDetail.networking", "tools"], "async", "rack",
        capabilities=["nvlink"],
        description=(
            "NCCL microbenchmark via dgxc-benchmarking llmb-run (microbenchmark_nccl, fp8). "
            "Resolves NVIS rack ids to real Slurm nodes and computes GPU scale (nodes*4)."
        ),
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
]


# ---------------------------------------------------------------------------
# Default project from environment (back-compat for single-cluster deploys)
# ---------------------------------------------------------------------------


def _guess_platform_id(env: dict[str, str]) -> str:
    """Best-effort map of RACK_CLUSTER_PRODUCT_LINE to a seeded platform id."""
    pl = (env.get("RACK_CLUSTER_PRODUCT_LINE") or "").strip().lower()
    by_product = {p["tool_param_defaults"].get("RACK_CLUSTER_PRODUCT_LINE", ""): p["id"]
                  for p in SEED_PLATFORMS}
    if pl and pl in by_product:
        return by_product[pl]
    # Loose contains match (e.g. "GB200 NVL72" -> gb200-nvl72).
    for token, pid in (("gb300", "gb300-nvl72"), ("gb200", "gb200-nvl72"),
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
    cluster_id = (env.get("RACK_CLUSTER_ID") or "default").strip() or "default"
    project_id = (
        env.get("RACK_PROJECT_ID")
        or ("nscale" if customer.casefold() == "nscale" else "default")
    ).strip() or "default"

    cluster = models.Cluster(
        id=cluster_id,
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
        id=project_id,
        name=customer if customer != "Default" else "Default Project",
        customer=customer,
        description="Auto-created from environment on first boot.",
        clusters=[cluster],
        source="seed",
    )


# ---------------------------------------------------------------------------
# Migration entrypoint
# ---------------------------------------------------------------------------


def migrate(repo: Repo, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Idempotently seed/migrate the catalog. Returns a small summary for logging."""
    summary: dict[str, Any] = {
        "platforms_added": 0,
        "tools_added": 0,
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
    except Exception:  # pragma: no cover - defensive: import must never break boot
        _logger.debug("migrate: ignoring error", exc_info=True)
        summary["npi"] = {}

    repo.set_meta("schema_version", SCHEMA_VERSION)
    return summary
