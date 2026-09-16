# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Rack Status standard report: one dataset, three renderings.

The customer-schema report (:mod:`node_report`) answers to Nscale's contract. This one is ours,
and its job is to say what the cluster is and how every node in it performed, in whichever
format the reader needs.

The important property is that CSV, JSON and PDF are **renderings of the same dataset**, built
once by :func:`build_dataset`. Three separate generators would drift the moment one of them
gained a column, and a report that disagrees with itself is worse than a missing one.

Where the data comes from:

* **cluster** — versions of the shared software stack, from ``bin/scripts/cluster_facts.sh`` run
  once on the head, plus the fleet-modal values already derived by :mod:`node_inventory`.
* **nodes** — hardware and firmware identity from the inventory sweep, joined to per-node test
  verdicts and metrics from ``bringup.fleet_summary``. Test columns are whatever the site
  actually ran; nothing here hard-codes a benchmark name, so a new bring-up column appears in
  the report without a change.
* **racks** — rack-level curves and aggregates from the analytics payload, used for the PDF's
  charts. Only the PDF draws them; CSV and JSON carry the same numbers as rows.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

FORMATS = ("csv", "json", "pdf")

#: Fleet-wide facts, in the order a reader wants them: what the machine is, then what software
#: is on it. ``(label, dotted source path)`` — kept as data so the set is visible in one place.
CLUSTER_FIELDS: tuple[tuple[str, str], ...] = (
    ("Cluster", "meta.cluster"),
    ("Customer", "meta.customer"),
    ("Platform", "cluster.platform"),
    ("GPU model", "cluster.gpu_model"),
    ("GPUs per node", "cluster.gpus_per_node"),
    ("Nodes", "summary.nodes_total"),
    ("Racks", "summary.racks_total"),
    ("BCM version", "cluster.bcm_version"),
    ("Slurm version", "cluster.slurm_version"),
    ("OS", "cluster.os"),
    ("Kernel", "cluster.kernel"),
    ("GPU driver", "cluster.driver_version"),
    ("CUDA runtime", "cluster.cuda_runtime"),
    ("CUDA toolkit", "cluster.cuda_toolkit_version"),
    ("vBIOS", "cluster.vbios_version"),
    ("Fabric manager", "cluster.fabric_manager_version"),
    ("IMEX", "cluster.imex_version"),
    ("NCCL", "cluster.nccl_version"),
    ("HPC-X", "cluster.hpcx_version"),
    ("DCGM", "cluster.dcgm_version"),
    ("NIC model", "cluster.nic_model"),
    ("NIC firmware", "cluster.nic_firmware"),
    ("OFED", "cluster.ofed_version"),
    ("Fabric", "cluster.fabric"),
    ("Link speed", "cluster.link_speed"),
    ("BMC firmware", "cluster.bmc_firmware"),
)

#: Per-node columns that always exist, before the site's test columns are appended.
NODE_BASE_COLUMNS: tuple[str, ...] = (
    "node", "rack", "rack_unit", "serial", "ip", "bmc_ip", "gpu_count", "gpu_ids",
    "vbios_version", "driver_version", "cuda_runtime", "nic_firmware", "ofed_version",
    "link_layer", "bios_version", "bmc_firmware", "data_captured_at", "overall",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dig(obj: Any, path: str) -> Any:
    """Walk a dotted path, returning "" rather than raising on a missing branch."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(part)
    return "" if cur is None else cur


def parse_facts(text: str) -> dict[str, Any]:
    """``key<TAB>value`` from ``cluster_facts.sh`` into a nested dict."""
    out: dict[str, Any] = {}
    for line in (text or "").splitlines():
        key, _, value = line.partition("\t")
        key, value = key.strip(), value.strip()
        if not key or not value:
            continue
        section, _, field = key.partition(".")
        if field:
            out.setdefault(section, {})[field] = value
        else:
            out[key] = value
    return out


def _fabric_summary(inventory: dict[str, Any]) -> str:
    """"InfiniBand" / "RoCE (Ethernet)" / both, read from the ports rather than guessed.

    The distinction matters for anyone reading the report: it determines which transport NCCL
    used and therefore what the bandwidth numbers mean.
    """
    layers = set()
    for node in inventory.get("nodes") or []:
        for nic in node.get("nics") or []:
            layer = str(nic.get("link_layer") or "").strip()
            if layer:
                layers.add(layer)
    if not layers:
        return ""
    names = {"InfiniBand": "InfiniBand", "Ethernet": "RoCE (Ethernet)"}
    return ", ".join(sorted(names.get(x, x) for x in layers))


def _primary_ip(bcm_entry: dict[str, Any] | None) -> str:
    """The node's own network address, as opposed to its controller's.

    The report wants both: ``bmc_ip`` reaches the BMC out of band, ``ip`` is the host itself.
    Only BCM knows the second — the customer schema has no field for it, so it never reaches
    the inventory entry, and the column sat empty until this read it from the device record.
    """
    for iface in ((bcm_entry or {}).get("interfaces") or []):
        ip = str(iface.get("ip") or "")
        kind = str(iface.get("kind") or "").lower()
        if ip and ip != "0.0.0.0" and "bmc" not in kind:
            return ip
    return ""


def build_dataset(
    *,
    fleet: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    facts: dict[str, Any] | None = None,
    analytics: dict[str, Any] | None = None,
    bcm: dict[str, dict[str, Any]] | None = None,
    cluster: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
    platform: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The single source every rendering reads from."""
    fleet = fleet or {}
    inventory = inventory or {}
    facts = facts or {}
    analytics = analytics or {}
    bcm = bcm or {}
    baseline = (inventory.get("shared_baseline") or {})

    columns = [str(c) for c in (fleet.get("columns") or [])]
    labels = dict(fleet.get("metric_labels") or {})

    inv_by_host = {str(n.get("hostname")): n for n in (inventory.get("nodes") or [])}
    carry_key_prefix = "* carried forward"

    rows: list[dict[str, Any]] = []
    racks: set[str] = set()
    for node in fleet.get("nodes") or []:
        name = str(node.get("node") or "")
        if not name:
            continue
        inv = inv_by_host.get(name) or {}
        gpus = inv.get("gpus") or []
        nics = inv.get("nics") or []
        overrides = inv.get("firmware_overrides") or {}
        carried = next((v for k, v in overrides.items() if k.startswith(carry_key_prefix)), "")

        statuses = []
        row: dict[str, Any] = {
            "node": name,
            "rack": inv.get("rack_id") or "",
            "rack_unit": inv.get("rack_unit") or "",
            "serial": inv.get("serial") or "",
            "ip": node.get("ip") or _primary_ip(bcm.get(name)),
            "bmc_ip": (inv.get("bmc") or {}).get("ip") or "",
            "gpu_count": len(gpus),
            # Every GPU's UUID, so a failing accelerator is traceable to a part.
            "gpu_ids": " ".join(str(g.get("uuid") or "") for g in gpus).strip(),
            "vbios_version": (gpus[0].get("vbios_version") if gpus else "") or "",
            "driver_version": overrides.get("gpu.driver_version") or baseline.get("gpu", {}).get("driver_version", ""),
            "cuda_runtime": overrides.get("gpu.cuda_runtime") or baseline.get("gpu", {}).get("cuda_runtime", ""),
            "nic_firmware": (nics[0].get("firmware") if nics else "") or "",
            "ofed_version": overrides.get("nic.ofed_version") or baseline.get("nic", {}).get("ofed_version", ""),
            "link_layer": (nics[0].get("link_layer") if nics else "") or "",
            "bios_version": overrides.get("host.bios_version") or baseline.get("host", {}).get("bios_version", ""),
            "bmc_firmware": (inv.get("bmc") or {}).get("firmware") or "",
            # Blank means measured in this run; a timestamp means it was carried forward.
            "data_captured_at": carried,
        }
        if row["rack"]:
            racks.add(str(row["rack"]))

        for job in node.get("jobs") or []:
            col = str(job.get("name") or "")
            if col not in columns:
                continue
            status = str(job.get("status") or "").upper()
            statuses.append(status)
            row[f"{col}_status"] = status
            row[f"{col}_metric"] = job.get("metric") if job.get("metric") not in (None, "") else ""
        # One verdict per node: any failure fails the node; nothing run is not a pass.
        row["overall"] = (
            "FAIL" if "FAIL" in statuses
            else "PASS" if statuses and all(s == "DONE" for s in statuses)
            else "INCOMPLETE" if statuses else "NOT_RUN"
        )
        rows.append(row)

    rows.sort(key=lambda r: str(r["node"]))
    node_columns = list(NODE_BASE_COLUMNS)
    for col in columns:
        node_columns += [f"{col}_status", f"{col}_metric"]

    passed = sum(1 for r in rows if r["overall"] == "PASS")
    failed = sum(1 for r in rows if r["overall"] == "FAIL")

    return {
        "meta": {
            "report": "Rack Status standard report",
            "generated_at": _now_iso(),
            "cluster": (cluster or {}).get("name") or "",
            "customer": (project or {}).get("customer") or "",
            "head": _dig(facts, "head.hostname"),
            "inventory_captured_at": inventory.get("captured_at") or "",
            "excluded_non_compute": fleet.get("excluded_non_compute") or {
                "count": 0,
                "nodes": [],
            },
        },
        "cluster": {
            "platform": (platform or {}).get("name") or (platform or {}).get("id") or "",
            "gpu_model": baseline.get("gpu", {}).get("model", ""),
            "gpus_per_node": baseline.get("gpu", {}).get("count_per_node", ""),
            "bcm_version": _dig(facts, "bcm.version"),
            "slurm_version": _dig(facts, "slurm.version"),
            "os": baseline.get("software", {}).get("os", "") or _dig(facts, "head.os"),
            "kernel": baseline.get("software", {}).get("kernel", ""),
            "driver_version": baseline.get("gpu", {}).get("driver_version", ""),
            "cuda_runtime": baseline.get("gpu", {}).get("cuda_runtime", ""),
            "cuda_toolkit_version": _dig(facts, "cuda.toolkit_version"),
            "vbios_version": baseline.get("gpu", {}).get("vbios_version", ""),
            "fabric_manager_version": baseline.get("gpu", {}).get("fabric_manager_version", ""),
            "imex_version": baseline.get("gpu", {}).get("imex_version", ""),
            "nccl_version": _dig(facts, "nccl.version"),
            "hpcx_version": _dig(facts, "hpcx.version"),
            "dcgm_version": baseline.get("software", {}).get("dcgm_version", ""),
            "nic_model": baseline.get("nic", {}).get("model", ""),
            "nic_firmware": baseline.get("nic", {}).get("firmware", ""),
            "ofed_version": baseline.get("nic", {}).get("ofed_version", ""),
            "fabric": _fabric_summary(inventory),
            "link_speed": baseline.get("nic", {}).get("link_speed", ""),
            "bmc_firmware": baseline.get("bmc", {}).get("firmware", ""),
        },
        "summary": {
            "nodes_total": len(rows),
            "racks_total": len(racks),
            "nodes_passed": passed,
            "nodes_failed": failed,
            "nodes_incomplete": len(rows) - passed - failed,
            "tests": columns,
        },
        "node_columns": node_columns,
        "metric_labels": labels,
        "nodes": rows,
        "racks": _rack_section(analytics),
    }


def _rack_section(analytics: dict[str, Any]) -> dict[str, Any]:
    """Rack-level numbers, kept in the shape the PDF's charts need.

    Carried in every format, not just the PDF: a CSV reader should be able to see the same
    per-rack bandwidth and TFLOP/s figures the charts are drawn from.
    """
    single = (analytics or {}).get("single_rack") or {}
    multi = (analytics or {}).get("multirack") or {}
    return {
        "nccl_collectives": single.get("nccl_collectives") or [],
        "nccl_all_reduce_series": single.get("nccl_ar_series") or {},
        "nemotron": single.get("nemotron") or [],
        "nemotron_scaling": multi.get("nemotron_scaling") or [],
    }


def to_pdf(dataset: dict[str, Any]) -> bytes:
    """Executive view of the same dataset. Delegated so the reportlab import stays optional."""
    import cluster_report_pdf

    return cluster_report_pdf.to_pdf(dataset)


def pdf_available() -> bool:
    """Whether this deployment can render PDF, so a caller can offer only what it can produce."""
    try:
        import cluster_report_pdf
    except ImportError:
        return False
    return cluster_report_pdf.available()


def to_json(dataset: dict[str, Any]) -> str:
    """JSON is the dataset itself — nested key/value, nothing reshaped."""
    return json.dumps(dataset, indent=2, default=str)


def to_csv(dataset: dict[str, Any]) -> str:
    """One node per row.

    The cluster-wide facts are the same for every row, so repeating them 2000 times would be
    noise; they lead the file as ``#`` comment lines instead, which spreadsheets show as text
    and ``pandas.read_csv(comment='#')`` skips. Rack-level results follow the node table as a
    second labelled block, because they are a different shape and padding them into the node
    columns would invent relationships that do not exist.
    """
    buf = io.StringIO()
    buf.write(f"# {dataset['meta']['report']}\n")
    buf.write(f"# generated_at,{dataset['meta']['generated_at']}\n")
    for label, path in CLUSTER_FIELDS:
        value = _dig(dataset, path)
        if value != "":
            buf.write(f"# {label},{value}\n")
    excluded = dataset.get("meta", {}).get("excluded_non_compute") or {}
    buf.write(f"# Excluded non-compute nodes,{excluded.get('count', 0)}\n")
    if excluded.get("nodes"):
        buf.write(f"# Excluded non-compute hostnames,{' '.join(excluded['nodes'])}\n")
    summary = dataset.get("summary") or {}
    buf.write(f"# Nodes passed,{summary.get('nodes_passed', 0)}\n")
    buf.write(f"# Nodes failed,{summary.get('nodes_failed', 0)}\n")
    buf.write("#\n")

    columns = dataset.get("node_columns") or list(NODE_BASE_COLUMNS)
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in dataset.get("nodes") or []:
        writer.writerow({c: row.get(c, "") for c in columns})

    collectives = (dataset.get("racks") or {}).get("nccl_collectives") or []
    if collectives:
        buf.write("\n# Rack-level NCCL collectives (GB/s)\n")
        keys = ["rack", "all_reduce_gbps", "all_gather_gbps", "alltoall_gbps",
                "reduce_scatter_gbps", "sendrecv_gbps"]
        rack_writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore",
                                     lineterminator="\n")
        rack_writer.writeheader()
        for row in collectives:
            rack_writer.writerow({k: row.get(k, "") for k in keys})

    nemotron = (dataset.get("racks") or {}).get("nemotron") or []
    if nemotron:
        buf.write("\n# Rack-level NeMo training\n")
        keys = ["rack", "tflops_per_gpu", "median_step_time", "step_time_cv_pct"]
        nemo_writer = csv.DictWriter(buf, fieldnames=keys, extrasaction="ignore",
                                     lineterminator="\n")
        nemo_writer.writeheader()
        for row in nemotron:
            nemo_writer.writerow({k: row.get(k, "") for k in keys})

    return buf.getvalue()
