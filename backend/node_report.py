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
"""Daily node-validation report: bring-up results -> a schema-validated JSON document.

Deliberately customer-agnostic. The schema this emits originated with the Nscale engagement
(``nvis-ova/nscale``, ``result_schema.json`` @ 50f27a7, vendored at
``backend/schemas/node_report_schema.json``), but nothing here is Nscale-specific: the
system-under-test block is derived from the active catalog platform/cluster, and the test
results from whatever bring-up columns the site actually runs. Point it at another customer
and it produces that customer's report.

What comes from where
---------------------
* **test_results / summary** — measured. One entry per bring-up column, with fleet-wide node
  counts, which is the idiom the schema uses (see its ``CH-19`` example: one test, aggregate
  ``nodes_total``/``nodes_passed``, not one row per node).
* **system_under_test** — derived from the catalog (platform geometry, fabric, cluster racks).
* **firmware_inventory / test_plan_reference / operator** — *assumptions*. Bring-up produces
  none of it, and the schema requires it with non-nullable strings and ``minimum: 1``
  integers, so a report cannot validate without something in those fields. Everything we
  cannot measure is seeded from :data:`ASSUMPTIONS` and marked :data:`TBD` where a real value
  must be filled in. The generated JSON is meant to be edited before it ships — that is why
  the placeholder is a visible token rather than a plausible-looking fake version number.

The schema is strict (``additionalProperties: false`` at every level), so this module must not
invent keys. It validates its own output against the vendored copy before returning, which is
the point of generating server-side rather than in the browser.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "node_report_schema.json"

#: Marker for a required field we cannot measure. Chosen to be obvious in a diff and to fail
#: a human's eye test, so an unedited report is never mistaken for a measured one.
TBD = "TBD"

SCHEMA_VERSION = "1.0"

#: ``test_id`` is pattern-constrained to ``^(GPU|CH|ST|NET|UFM)-[0-9]{2}$``, so bring-up column
#: names cannot be used as ids. Each column gets an id from its category's prefix, numbered in
#: column order; the column name is carried in ``test_name`` and ``execution.command`` so a
#: reader can still map ``NET-01`` back to ``allreduce``.
ID_PREFIX_BY_CATEGORY: dict[str, str] = {
    "GPU Benchmark": "GPU",
    "Health Check": "CH",
    "System Stress": "ST",
    "Network Stress": "NET",
    "UFM Validations": "UFM",
}

#: Placeholders for pattern-constrained hardware identity we cannot measure. All-zero values
#: satisfy the schema's regexes while being impossible to mistake for real hardware — the point
#: is that an unedited report looks unedited.
PLACEHOLDER_MAC = "00:00:00:00:00:00"
PLACEHOLDER_IP = "0.0.0.0"
PLACEHOLDER_BDF = "0000:00:00.0"
PLACEHOLDER_GPU_UUID = "GPU-00000000-0000-0000-0000-000000000000"

#: Bring-up column -> schema ``category`` (the schema's enum is closed, so this is a mapping,
#: not a passthrough). Unlisted columns fall back to :data:`DEFAULT_CATEGORY`.
CATEGORY_BY_JOB: dict[str, str] = {
    "allreduce": "Network Stress",
    "allreduce-forcesharp": "Network Stress",
    "allreduce-nonvlink": "Network Stress",
    "alltoall": "Network Stress",
    "alltoall-nonvlink": "Network Stress",
    "nvbandwidth": "GPU Benchmark",
    "hpl": "GPU Benchmark",
    "stream": "GPU Benchmark",
    "dcgmi-r1": "Health Check",
    "dcgmi-r3": "Health Check",
    "dcgm": "Health Check",
    "mnubergemm": "System Stress",
    "memtester": "System Stress",
    "stressapptest": "System Stress",
    "fio": "System Stress",
}
DEFAULT_CATEGORY = "Health Check"

#: Bring-up status -> schema ``status`` enum. RETRY/RUNNING are genuinely undecided, and the
#: schema has no "in progress", so they map to BLOCKED rather than being counted as failures.
STATUS_BY_VERDICT: dict[str, str] = {
    "DONE": "PASS",
    "FAIL": "FAIL",
    "NOT_RUN": "NOT_RUN",
    "INCOMPLETE": "BLOCKED",
    "RETRY": "BLOCKED",
    "RUNNING": "BLOCKED",
}
DEFAULT_STATUS = "NOT_RUN"

#: Human titles for the columns we know; anything else is titled from its own name.
TEST_NAMES: dict[str, str] = {
    "allreduce": "NCCL All-Reduce (NVLink)",
    "allreduce-forcesharp": "NCCL All-Reduce (SHARP forced)",
    "allreduce-nonvlink": "NCCL All-Reduce (network path, NVLink disabled)",
    "alltoall": "NCCL All-to-All (NVLink)",
    "alltoall-nonvlink": "NCCL All-to-All (network path, NVLink disabled)",
    "nvbandwidth": "nvbandwidth host-to-device / device-to-device",
    "dcgmi-r1": "DCGM Diagnostic (Level 1)",
    "dcgmi-r3": "DCGM Diagnostic (Level 3)",
    "hpl": "HPL",
    "stream": "STREAM",
    "mnubergemm": "MNUBERGEMM soak",
}

#: Fields the schema requires that bring-up cannot measure. Overridable per deployment (see
#: :func:`build_report`'s ``overrides``) so a site can fill in its real values once.
ASSUMPTIONS: dict[str, Any] = {
    "operator": {"name": "NVIS OVA", "email": None, "organization": "NVIDIA"},
    "test_plan_reference": {
        "name": "NVIS Cluster Bring-up Validation Plan",
        "version": "0.1-draft",
        "date": None,
        "document_uri": None,
    },
    # Non-measured hardware identity. Integers must be >= 1 per the schema, so they carry
    # plausible platform defaults rather than zeros; strings carry TBD.
    # A column's rolled-up verdict, published in acceptance_criteria.thresholds so the gate is
    # never implicit. 93 matches the schema's own example. It was 100, which is defensible for a
    # single rack and wrong for a fleet: at 2013 nodes one degraded tray failed an entire test,
    # so a Nscale TX report read FAIL on all ten tests while measuring 94-99% node pass rates.
    # A site with a different contractual gate overrides this rather than editing the module.
    "node_pass_rate_min_pct": 93.0,
    "cpu": {"cores_per_socket": 72, "sockets_per_node": 1},
    "memory": {"system_memory_gb": 960, "memory_type": None},
    "firmware": {
        "host": {"bios_version": TBD, "cpld_version": None, "pcie_retimer_firmware": None},
        "bmc": {"model": TBD, "firmware": TBD, "ipmi_version": None, "redfish_version": None},
        "nic": {
            "model": TBD,
            "firmware": TBD,
            "ofed_version": TBD,
            "link_speed": TBD,
        },
        "gpu": {"vbios_version": TBD, "driver_version": TBD,
                "fabric_manager_version": None, "imex_version": None, "cuda_runtime": None},
        "fabric": {"switch_model": TBD, "switch_firmware": TBD, "nvswitch_firmware": None,
                   "ufm_version": TBD, "ufm_appliance_model": None},
        "software": {"os": TBD, "kernel": TBD, "nccl_version": None, "dcgm_version": None,
                     "hpcx_version": None},
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """RFC3339 with a literal Z, which is what the schema's isoDateTime pattern expects."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prune(obj: Any) -> Any:
    """Drop ``None`` values recursively.

    Optional schema fields are typed as plain strings, so emitting ``null`` for "we do not
    know" would fail validation; omitting the key is the correct representation of absent.
    """
    if isinstance(obj, dict):
        return {k: _prune(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_prune(v) for v in obj]
    return obj


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _slug(text: str) -> str:
    """Reduce a name to the report_id character set."""
    return re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-") or "CLUSTER"


def report_id(cluster_name: str, when: datetime, seq: int = 1) -> str:
    return f"{_slug(cluster_name)}-{when.astimezone(timezone.utc):%Y-%m-%d}-{seq:03d}"


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Enough for a report summary; avoids a numpy dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((pct / 100.0) * len(ordered) + 0.5) - 1))
    return round(ordered[idx], 2)


def _compute_model(platform: dict[str, Any], assumptions: dict[str, Any]) -> dict[str, Any]:
    """Derive the compute model from the catalog platform, falling back to assumptions."""
    geometry = platform.get("geometry") or {}
    fabric = platform.get("fabric") or {}
    interconnect = platform.get("interconnect") or {}
    gpus_per_node = int(geometry.get("gpus_per_node") or 0) or 1
    rails = int(fabric.get("rails_per_gpu") or 0) * gpus_per_node
    rate = fabric.get("rail_rate_gbps")
    scaleout = (fabric.get("scaleout") or "").upper()
    inter_node = " ".join(str(p) for p in (f"{rate}G" if rate else None, scaleout or None) if p)
    vendor = " ".join(str(p) for p in (platform.get("system_vendor"), platform.get("form_factor")) if p)
    cpu_model = " ".join(
        str(p) for p in (platform.get("cpu_vendor"), f"({platform['cpu_arch']})" if platform.get("cpu_arch") else None) if p
    )
    return {
        "platform": platform.get("name") or platform.get("id") or TBD,
        "chassis_model": vendor or TBD,
        "compute_module": platform.get("generation") or platform.get("gpu_model") or TBD,
        "cpu": {
            "model": cpu_model or TBD,
            "cores_per_socket": assumptions["cpu"]["cores_per_socket"],
            "sockets_per_node": assumptions["cpu"]["sockets_per_node"],
        },
        "gpu": {"model": platform.get("gpu_model") or TBD, "count_per_node": gpus_per_node},
        "memory": {
            "system_memory_gb": assumptions["memory"]["system_memory_gb"],
            "memory_type": assumptions["memory"]["memory_type"],
        },
        "interconnect": {
            "intra_node": interconnect.get("scaleup") or ("NVLink" if geometry.get("nvlink_domain") else TBD),
            "inter_node": inter_node or TBD,
            "ib_devices_per_node": rails or 1,
        },
    }


def _shared_baseline(platform: dict[str, Any], assumptions: dict[str, Any]) -> dict[str, Any]:
    """Firmware baseline: platform-derived where possible, TBD where it must be filled in."""
    fw = assumptions["firmware"]
    geometry = platform.get("geometry") or {}
    fabric = platform.get("fabric") or {}
    gpus_per_node = int(geometry.get("gpus_per_node") or 0) or 1
    rails = int(fabric.get("rails_per_gpu") or 0) * gpus_per_node
    gpu = dict(fw["gpu"])
    gpu.update({"model": platform.get("gpu_model") or TBD, "count_per_node": gpus_per_node})
    nic = dict(fw["nic"])
    nic["ports_per_node"] = rails or 1
    return {
        "host": dict(fw["host"]),
        "bmc": dict(fw["bmc"]),
        "nic": nic,
        "gpu": gpu,
        "fabric": dict(fw["fabric"]),
        "software": dict(fw["software"]),
    }


def _job_measurements(cells: list[dict[str, Any]], label: str | None) -> dict[str, Any]:
    """Aggregate one column across the fleet into schema ``measurements``.

    ``measurements`` is a free-form object in the schema, so the keys mirror the vocabulary its
    own examples use (``nodes_total``/``pass_rate_pct``) and name the metric with its unit.

    ``per_node`` carries every node's own verdict. The aggregate alone says "18 failed" without
    saying which 18, which is the first thing a reader needs and the reason this column existed;
    the schema names ``per_node[]`` in its own description of ``measurements``.
    """
    total = len(cells)
    passed = sum(1 for c in cells if c["status"] == "PASS")
    failed = sum(1 for c in cells if c["status"] == "FAIL")
    out: dict[str, Any] = {
        "nodes_total": total,
        "nodes_passed": passed,
        "nodes_failed": failed,
        "nodes_not_run": sum(1 for c in cells if c["status"] == "NOT_RUN"),
        "nodes_blocked": sum(1 for c in cells if c["status"] == "BLOCKED"),
        "pass_rate_pct": round(100.0 * passed / total, 1) if total else 0.0,
    }
    values = [v for v in (c["metric"] for c in cells) if v is not None]
    if values and label:
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        out.update(
            {
                f"{key}_avg": round(sum(values) / len(values), 2),
                f"{key}_min": round(min(values), 2),
                f"{key}_max": round(max(values), 2),
                f"{key}_p50": _percentile(values, 50),
                "nodes_measured": len(values),
            }
        )

    per_node = []
    for cell in sorted(cells, key=lambda c: str(c.get("node") or "")):
        entry: dict[str, Any] = {"node": cell.get("node", ""), "status": cell["status"]}
        if cell.get("metric") is not None:
            entry["metric"] = cell["metric"]
        # A reason is only meaningful on a node that did not pass, and only when bring-up
        # recorded one; carrying an empty key on 2000 passing nodes is pure weight.
        if cell["status"] != "PASS" and cell.get("reason"):
            entry["reason"] = cell["reason"]
        per_node.append(entry)
    out["per_node"] = per_node
    return out


def build_report(
    fleet: dict[str, Any],
    *,
    platform: dict[str, Any] | None = None,
    cluster: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
    inventory: dict[str, Any] | None = None,
    now: datetime | None = None,
    seq: int = 1,
) -> dict[str, Any]:
    """Build the report document from a :func:`bringup.fleet_summary` payload.

    ``overrides`` is merged over :data:`ASSUMPTIONS`, so a deployment can supply its real
    operator, test plan and firmware baseline without touching this module.

    ``inventory`` is a measured :func:`node_inventory.build_inventory` block. Without it the
    firmware section falls back to the placeholders this module shipped with, which is why an
    unedited report used to carry all-zero MACs and ``TBD`` firmware for every node.
    """
    when = now or _now()
    assumptions = _deep_merge(ASSUMPTIONS, overrides or {})
    platform = platform or {}
    cluster = cluster or {}
    project = project or {}

    nodes = list(fleet.get("nodes") or [])
    columns = list(fleet.get("columns") or [])
    labels = dict(fleet.get("metric_labels") or {})

    # Per column, the fleet's verdicts. Built once so summary and test_results agree.
    per_column: dict[str, list[dict[str, Any]]] = {c: [] for c in columns}
    for node in nodes:
        for job in node.get("jobs") or []:
            name = job.get("name")
            if name not in per_column:
                continue
            per_column[name].append(
                {
                    "node": node.get("node", ""),
                    "status": STATUS_BY_VERDICT.get(str(job.get("status") or "").upper(), DEFAULT_STATUS),
                    "metric": _num(job.get("metric")),
                    # Captured here rather than looked up later: per_node needs it for every
                    # cell, and re-scanning the fleet per column is quadratic at 2000 nodes.
                    "reason": str(job.get("reason") or "").strip(),
                }
            )

    cluster_name = cluster.get("name") or project.get("customer") or "cluster"
    rack_ids = sorted({r for n in nodes if (r := _rack_of(n.get("node", ""), cluster))})

    test_results: list[dict[str, Any]] = []
    by_category: dict[str, dict[str, int]] = {}
    seq_by_prefix: dict[str, int] = {}
    for col in columns:
        cells = per_column.get(col) or []
        category = CATEGORY_BY_JOB.get(col, DEFAULT_CATEGORY)
        prefix = ID_PREFIX_BY_CATEGORY.get(category, "CH")
        seq_by_prefix[prefix] = seq_by_prefix.get(prefix, 0) + 1
        test_id = f"{prefix}-{min(99, seq_by_prefix[prefix]):02d}"
        label = labels.get(col)
        measurements = _job_measurements(cells, label)
        passed = measurements["nodes_passed"]
        blocked = measurements["nodes_blocked"]
        judged = passed + measurements["nodes_failed"]
        threshold = float(assumptions["node_pass_rate_min_pct"])
        # Roll the fleet up against the published threshold. Nothing judged is NOT_RUN (or
        # BLOCKED if attempts are mid-flight) rather than a vacuous PASS.
        if not judged:
            status = "BLOCKED" if blocked else "NOT_RUN"
        else:
            status = "PASS" if (100.0 * passed / judged) >= threshold else "FAIL"
        bucket = by_category.setdefault(
            category, {"total": 0, "passed": 0, "failed": 0, "waived": 0, "not_run": 0}
        )
        bucket["total"] += 1
        bucket["passed" if status == "PASS" else "failed" if status == "FAIL" else "not_run"] += 1

        reasons = sorted({c for cell in cells if (c := _reason_of(cell, nodes, col))})
        test_results.append(
            {
                "test_id": test_id,
                "test_name": TEST_NAMES.get(col, col.replace("-", " ").replace("_", " ").title()),
                "category": category,
                "owner": "NVIS",
                "scope": {"type": "SU", "id": _slug(cluster_name), "node_count": max(1, len(cells))},
                "execution": {
                    "started_at": _iso(when),
                    "completed_at": _iso(when),
                    # Bring-up records verdicts, not per-column wall time. 0 is the honest
                    # value for "not measured"; the schema requires the field.
                    "duration_seconds": 0,
                    "operator": assumptions["operator"]["name"],
                    "tool": {"name": "nvis bring-up automation", "version": TBD},
                    "command": f"status-check.sh -t {col}",
                    "host_count": max(1, len(cells)),
                },
                "acceptance_criteria": {
                    "text": (
                        f"Per node, judged by the site's bring-up gate for {col} (a node passes "
                        "when its result clears the configured floor). The column passes when at "
                        f"least {threshold:g}% of judged nodes pass."
                    ),
                    "thresholds": {"node_pass_rate_min_pct": threshold},
                },
                "measurements": measurements,
                "status": status,
                "microsoft_acceptance_test": False,
                "microsoft_criteria_satisfied": [],
                "notes": "; ".join(reasons[:5]) or None,
            }
        )

    summary = {
        "total_tests": len(test_results),
        "passed": sum(1 for t in test_results if t["status"] == "PASS"),
        "failed": sum(1 for t in test_results if t["status"] == "FAIL"),
        "waived": sum(1 for t in test_results if t["status"] == "WAIVED"),
        "not_run": sum(1 for t in test_results if t["status"] in ("NOT_RUN", "BLOCKED")),
        "by_category": by_category,
        # No Microsoft criteria mapping exists for these benchmarks yet; 0/0 states that
        # honestly instead of implying coverage we have not established.
        "msft_acceptance": {"criteria_total": 0, "criteria_satisfied": 0},
    }

    report = {
        "report_metadata": {
            "schema_version": SCHEMA_VERSION,
            "report_id": report_id(cluster_name, when, seq),
            "generated_at": _iso(when),
            "test_plan_reference": assumptions["test_plan_reference"],
            "operator": assumptions["operator"],
            "execution_window": {"started_at": _iso(when), "completed_at": _iso(when)},
        },
        "system_under_test": {
            "scope": {
                "type": "SU",
                "id": _slug(cluster_name),
                "rack_ids": rack_ids or None,
                "node_count": max(1, len(nodes)),
            },
            "compute_model": _compute_model(platform, assumptions),
        },
        "firmware_inventory": _firmware_inventory(
            inventory, nodes, platform, cluster, assumptions, when
        ),
        "summary": summary,
        "test_results": test_results,
    }
    return _prune(report)


def _firmware_inventory(
    inventory: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    platform: dict[str, Any],
    cluster: dict[str, Any],
    assumptions: dict[str, Any],
    when: datetime,
) -> dict[str, Any]:
    """Measured inventory when a sweep supplied one, placeholders otherwise.

    A measured block is merged *over* the assumed baseline rather than replacing it, so a field
    the probe could not reach (the fabric switch firmware, which lives in UFM and not on any
    node) keeps whatever the deployment configured instead of regressing to ``TBD``.
    """
    baseline = _shared_baseline(platform, assumptions)
    if not inventory or not inventory.get("nodes"):
        return {
            "captured_at": _iso(when),
            "shared_baseline": baseline,
            "nodes": _placeholder_nodes(nodes, platform, cluster),
        }

    measured = inventory.get("shared_baseline") or {}
    merged = _deep_merge(baseline, {
        section: {k: v for k, v in fields.items() if v not in (None, "", TBD)}
        for section, fields in measured.items()
    })
    return {
        "captured_at": inventory.get("captured_at") or _iso(when),
        "shared_baseline": merged,
        "nodes": inventory["nodes"],
    }


def _placeholder_nodes(
    nodes: list[dict[str, Any]], platform: dict[str, Any], cluster: dict[str, Any]
) -> list[dict[str, Any]]:
    """One entry for ``firmware_inventory.nodes``, which the schema requires to be non-empty.

    The schema's own model is ``shared_baseline`` for what every node has in common and
    ``nodes`` for per-node detail and overrides, so a single representative entry is a fair
    reading while no per-node firmware probe exists. Its hostname and rack are real (traceable);
    every hardware identifier is an all-zero placeholder because we have not measured it, and a
    fabricated-looking MAC or GPU UUID in a customer report would be worse than an obvious one.
    """
    hostname = next((str(n.get("node")) for n in nodes if n.get("node")), TBD)
    return [
        {
            "hostname": hostname,
            "serial": TBD,
            "rack_id": _rack_of(hostname, cluster) or TBD,
            "bmc": {"ip": PLACEHOLDER_IP, "mac": PLACEHOLDER_MAC, "firmware": TBD},
            "nics": [
                {
                    "device": TBD,
                    "port": 1,
                    "pcie_bdf": PLACEHOLDER_BDF,
                    "mac": PLACEHOLDER_MAC,
                    "firmware": TBD,
                }
            ],
            "gpus": [
                {
                    "index": 0,
                    "uuid": PLACEHOLDER_GPU_UUID,
                    "serial": TBD,
                    "model": platform.get("gpu_model") or TBD,
                    "pcie_bdf": PLACEHOLDER_BDF,
                    "vbios_version": TBD,
                }
            ],
            "firmware_overrides": {},
        }
    ]


def _rack_of(node: str, cluster: dict[str, Any]) -> str:
    """Best-effort rack id for a node name, for ``scope.rack_ids``."""
    if not node:
        return ""
    try:
        import nvis_id_templates

        hit = nvis_id_templates.get_nvis_naming().sinfo_rack_from_host(node)
        if hit:
            return hit[0]
    except Exception as e:  # noqa: BLE001 - naming is advisory; fall back to the suffix split
        _log.debug("rack attribution for %s fell back: %s", node, e)
    # Fall back to everything up to the last dash-separated field.
    parts = node.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else ""


def _reason_of(cell: dict[str, Any], nodes: list[dict[str, Any]], col: str) -> str:
    """The failure reason recorded for this column, if any (deduplicated by the caller)."""
    if cell["status"] != "FAIL":
        return ""
    for node in nodes:
        if node.get("node") != cell["node"]:
            continue
        for job in node.get("jobs") or []:
            if job.get("name") == col:
                return str(job.get("reason") or "").strip()
    return ""


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(report: dict[str, Any]) -> list[str]:
    """Validate against the vendored schema. Returns human-readable errors, [] when valid.

    ``jsonschema`` is soft-imported: a deployment missing it should still be able to produce a
    report (the caller decides whether to serve an unvalidated one), rather than 500.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - exercised only without the dependency
        _log.warning("jsonschema unavailable: report served without schema validation")
        return []
    validator = jsonschema.Draft202012Validator(load_schema())
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
        for e in sorted(validator.iter_errors(report), key=lambda e: list(e.absolute_path))
    ]
