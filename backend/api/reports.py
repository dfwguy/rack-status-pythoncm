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
"""Generated reports: the archive, per-node reports, and the post-run acceptance report.

One dataset, several renderings -- ``core-designs.mdc`` §10. cluster_report.py builds the data;
CSV/JSON/PDF are renderers over it, never separate generators, because parallel generators drift on
the first added column and a report that contradicts itself is worse than a missing one.

The post-run report merges three sources (Slurm accounting, deep audits, hardware inventory) behind a
short-lived cache: it is assembled per rack at the end of a campaign and read repeatedly while the
operator works through the findings.

Node-report jobs use the bounded registry pattern with a TTL and a max, so a fleet-sized sweep cannot
grow the table without limit.
"""

# Notes that stayed in app.py when this module took the code, during the api/ migration. The
# prose describes subjects that live here now; it is reunited with them rather than deleted.
#
# NVLMAPPER output zips under reports/<rack>/ (staged + ./run_nvlmapper.sh from api/fabric_ops.py).
# Parser Excel copies (e.g. rack-parser-export-*.xlsx) and other workbooks under reports/.
# Reserved inventory ``host`` for ``*.xlsx`` files sitting directly under the reports base dir (not in a subfolder).
#
#: Report kind -> the formats it can render, first entry being the default.
#:
#: The Nscale report answers a customer's JSON Schema, so JSON is the only honest format for it —
#: a CSV of that document would not validate against the contract it exists to satisfy. The Rack
#: Status report is ours and carries the same dataset in whichever shape the reader needs.
#
#: Filename extension and MIME type per format.
#
#: Small on purpose: each finished job pins a multi-MB document until it is collected.
#: How long a "running" job stays eligible for idempotent reuse. Comfortably longer than a
#: full sweep (~4 minutes at 2000 nodes) and short enough that a dead worker frees its key.
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
import uuid

import core
import host_exec
import post_run_report
import validation_activities
import workflow_engine
from analytics_cache import _analytics_aggregate_cache_get
from audit.actions import action
from catalog import context as catalog_context
from config_env import _env_int
from core import SLURM_JOB_ID_RE as _SLURM_SCANCEL_JOB_ID_RE
from core import rpath as _rpath
from core import truthy_arg as _truthy_arg
from core import truthy_env as _truthy_env
from flask import Blueprint, Response, jsonify, request
from job_registry import _node_report_jobs, _node_report_jobs_lock
from paths import _default_log_dir
from platforms import get_platform as _get_platform
from slurm_admin import _run_scontrol_node_update
from slurm_query import _all_slurm_nodenames

from ._common import arg_or_body as _arg_or_body
from ._common import operator_from_request as _operator_from_request
from ._common import requested_nodes as _requested_nodes

try:  # optional: the per-node report renderer
    import node_report as _node_report
except ImportError:
    _node_report = None  # type: ignore[assignment]

try:  # optional: bring-up automation tree
    import bringup as _bringup
except ImportError:
    _bringup = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("reports", __name__, url_prefix="")


def _collect_node_inventory(fleet: dict, progress=None, phase=None) -> tuple[dict | None, list[str]]:
    """Sweep the fleet for hardware identity, folding in BCM's rack/BMC view when reachable.

    Returns ``(inventory, deviations)``; ``(None, [])`` when nothing could be collected, so the
    report falls back to placeholders rather than failing.
    """
    try:
        import node_inventory
    except ImportError:
        _logger.warning("node_inventory unavailable", exc_info=True)
        return None, []

    hosts = [str(n.get("node")) for n in (fleet.get("nodes") or []) if n.get("node")]
    if not hosts:
        return None, []

    # SSH first: it reaches every node we already hold an allocation on, and it can report
    # per-node progress as it goes. Slurm refuses the rest ("no running jobs and no WLM
    # allocations"), so those are swept through srun afterwards — one blocking step, hence the
    # phase note rather than a tick. Doing it the other way round would leave the progress bar
    # at zero for the whole run.
    records, errors = node_inventory.collect_fleet(hosts, progress=progress)
    if errors:
        _logger.warning("node inventory: %d/%d node(s) did not answer over SSH",
                        len(errors), len(hosts))
    reached = {(r.get("host") or {}).get("hostname") for r in records}
    missing = [h for h in hosts if h not in reached]
    # Off by default: allocating nodes to inventory them competes with whatever else the site
    # is running, and on a cluster being readied for hand-over that is somebody's validation
    # run. Nodes we cannot reach carry their previous measurement from the cache instead, which
    # is the honest answer for hardware identity that only changes when the hardware does.
    # Set NVIS_INVENTORY_VIA_SRUN=1 for a deliberate full sweep during a quiet window.
    if missing and _truthy_arg(os.environ.get("NVIS_INVENTORY_VIA_SRUN"), default=False):
        if phase:
            phase(f"requesting a Slurm allocation for {len(missing)} unreachable node(s)")
        extra, srun_errors = node_inventory.collect_via_srun(missing)
        _logger.info("node inventory: srun added %d of %d unreachable node(s)",
                     len(extra), len(missing))
        records.extend(extra)
        errors = srun_errors

    captured_at = _node_report._iso(_node_report._now())
    cache = node_inventory.save_cache(records, captured_at=captured_at)
    records, carried = node_inventory.merge_with_cache(records, cache)
    if not records:
        return None, []

    bcm_by_host: dict = {}
    try:
        import bcm_query

        bcm_by_host = bcm_query.index_by_hostname(bcm_query.nodes())
    except Exception:  # BCM identity is an enrichment, not a prerequisite
        _logger.debug("node inventory: BCM lookup unavailable", exc_info=True)

    inventory, deviations = node_inventory.build_inventory(
        records, bcm_by_host=bcm_by_host, carried=carried
    )
    inventory["captured_at"] = captured_at
    measured = len(records) - len(carried)
    if errors:
        deviations.append(
            f"{len(errors)} of {len(hosts)} nodes answered neither the SSH nor the Slurm probe; "
            f"{measured} were measured in this run and {len(carried)} carry earlier data"
        )
    return inventory, deviations


@bp.route("/api/node-report", methods=["GET", "POST"])
@action("report.node:generate")
@action(get="report.node:read")
def api_node_report():
    """Daily node-validation report as schema-validated JSON.

    Generated server-side so it can be checked against the vendored contract
    (``backend/schemas/node_report_schema.json``) before anyone downloads it; the browser just
    saves what it is given. POST carries the node list as a JSON body; ``download`` (body or
    query) sets a filename so the SPA can hand the file straight to the user.
    """
    if _node_report is None:
        return jsonify({"ok": False, "error": "report generator unavailable"}), 503
    try:
        if _truthy_env("RACK_STATUS_DEMO"):
            nodes = _requested_nodes()
            fleet = _bringup.demo_fleet_summary(nodes) if _bringup else {"nodes": [], "columns": []}
        elif _bringup is None or not _bringup.is_enabled():
            return jsonify({"ok": False, "error": "bring-up integration disabled or unavailable"}), 404
        else:
            fleet = _bringup.fleet_summary()
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "bring-up fleet report timed out"}), 504
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500

    # The schema requires at least one test result, and rightly so: a validation report with no
    # tests in it says nothing. Refuse clearly instead of emitting an invalid document.
    if not (fleet.get("columns") and fleet.get("nodes")):
        return jsonify({
            "ok": False,
            "error": "no bring-up results to report on yet",
            "detail": "the bring-up tree reported no active test columns for any node",
        }), 409

    project = cluster = platform = None
    try:
        project, cluster, platform = catalog_context.resolve()
    except Exception as e:  # noqa: BLE001 - a missing catalog must not block the report
        _logger.warning("node-report: catalog context unavailable: %s", e)

    def _as_dict(obj):
        return obj.to_dict() if hasattr(obj, "to_dict") else (obj or {})

    # A measured inventory costs a fleet-wide SSH sweep (~2 min at 2000 nodes), so it is opt-in
    # rather than the cost of every report render. Without it the firmware block falls back to
    # placeholders, which is what this report shipped with before the sweep existed.
    inventory = deviations = None
    if _truthy_arg(_arg_or_body("inventory")):
        inventory, deviations = _collect_node_inventory(fleet)

    report = _node_report.build_report(
        fleet,
        platform=_as_dict(platform),
        cluster=_as_dict(cluster),
        project=_as_dict(project),
        inventory=inventory,
    )
    if deviations:
        # Published per test result: the schema has no document-level place for them, and a
        # reshaped value that is not disclosed is worse than one that was never measured.
        for entry in report.get("test_results") or []:
            entry.setdefault("deviations", []).extend(deviations)
    errors = _node_report.validate(report)
    if errors:
        # Surface it rather than shipping an invalid document to a customer-facing report.
        _logger.error("node-report failed schema validation: %s", errors[:5])
        return jsonify({"ok": False, "error": "report failed schema validation", "detail": errors[:20]}), 500

    payload = request.get_json(silent=True)
    download = payload.get("download") if isinstance(payload, dict) else None
    if download is None:
        download = request.args.get("download")

    body = json.dumps(report, indent=2)
    if _truthy_arg(download):
        name = f"{report['report_metadata']['report_id']}.json"
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
    return Response(body, mimetype="application/json")


_REPORT_KINDS: dict[str, tuple[str, ...]] = {
    "nscale": ("json",),
    "rack-status": ("csv", "json", "pdf"),
}


_FORMAT_MEDIA: dict[str, tuple[str, str]] = {
    "csv": ("csv", "text/csv"),
    "json": ("json", "application/json"),
    "pdf": ("pdf", "application/pdf"),
}


def _cluster_facts() -> dict:
    """Cluster-wide software versions, from ``cluster_facts.sh`` on the head.

    One call, not one per node: BCM, Slurm, NCCL and HPC-X are properties of the shared software
    tree. Failure degrades to an empty dict — the report then shows those rows blank rather than
    refusing to generate, which is the right trade for a field that is nice to have.
    """
    try:
        import cluster_report

        base = (host_exec.role_root("scripts") or "").rstrip("/")
        script = f"{base}/cluster_facts.sh" if base else "cluster_facts.sh"
        log_dir = _default_log_dir()
        proc = host_exec.run_on_host(["bash", script, str(log_dir)], timeout=120)
        return cluster_report.parse_facts(getattr(proc, "stdout", "") or "")
    except Exception:  # a missing fact is not a reason to fail the report
        _logger.debug("cluster facts probe unavailable", exc_info=True)
        return {}


def _analytics_last_cached() -> dict:
    """The most recent analytics aggregate, for the PDF's rack-level charts.

    Read from the on-disk cache rather than recomputed: the aggregate is minutes of log scanning,
    and a report should not silently trigger it. When absent the PDF says so and omits the charts.
    """
    try:
        hit = _analytics_aggregate_cache_get(str(_rpath(_default_log_dir())))
        return hit if isinstance(hit, dict) else {}
    except Exception:  # charts are optional; the tables carry the same numbers
        _logger.debug("analytics cache unavailable for the report", exc_info=True)
        return {}


def _save_generated_report(
    *, kind: str, fmt: str, filename: str, data: bytes | str, meta: dict | None = None
) -> str:
    """Persist a finished report and return its saved id ("" when it could not be stored).

    Called unconditionally when a report completes, not when someone downloads it. The sweep costs
    minutes of fleet-wide SSH, and the operator who started it should be free to leave the page --
    before this, navigating away discarded the whole run.

    A storage failure must not fail the job: the caller still has the document in memory and can
    still download it, so a full disk should cost the operator the archive copy, not the report.
    """
    try:
        import report_store

        entry = report_store.save(kind=kind, fmt=fmt, filename=filename, data=data, meta=meta)
        return str(entry.get("id") or "")
    except Exception:
        _logger.exception("could not save generated report to the report store")
        return ""


@bp.route("/api/reports/generated", methods=["GET"])
@action(get="report:list")
def api_reports_generated():
    """Reports generated by this deployment and kept on disk.

    Survives both leaving the page and a container restart, which is the point: a fleet sweep is
    too expensive to lose to a closed tab.
    """
    try:
        import report_store

        return jsonify({"ok": True, "reports": report_store.list_entries(),
                        "max_kept": report_store.MAX_REPORTS})
    except Exception as e:
        _logger.exception("could not list generated reports")
        return jsonify({"ok": False, "error": str(e), "reports": []}), 500


@bp.route("/api/reports/generated/<report_id>", methods=["GET"])
@action(get="report:read")
def api_reports_generated_download(report_id: str):
    """Download a saved report."""
    try:
        import report_store

        found = report_store.load(report_id)
    except Exception as e:
        _logger.exception("could not read saved report %s", report_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    if found is None:
        return jsonify({"ok": False, "error": "no such report"}), 404
    body, entry = found
    _, mimetype = _FORMAT_MEDIA.get(str(entry.get("format") or ""), ("bin", "application/octet-stream"))
    name = str(entry.get("filename") or "report")
    return Response(body, mimetype=mimetype,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/api/reports/generated/<report_id>", methods=["DELETE"])
@action("report:delete")
def api_reports_generated_delete(report_id: str):
    """Delete a saved report."""
    try:
        import report_store

        removed = report_store.delete(report_id)
    except Exception as e:
        _logger.exception("could not delete saved report %s", report_id)
        return jsonify({"ok": False, "error": str(e)}), 500
    if not removed:
        return jsonify({"ok": False, "error": "no such report"}), 404
    return jsonify({"ok": True})


def _cluster_report_pdf_available() -> bool:
    try:
        import cluster_report
    except ImportError:
        return False
    return cluster_report.pdf_available()


_NODE_REPORT_JOBS_MAX = 4


_NODE_REPORT_JOB_TTL = 30 * 60


@bp.route("/api/node-report/kinds", methods=["GET"])
@action(get="report.node:list")
def api_node_report_kinds():
    """Which reports this deployment can produce, and in which formats.

    Discovered rather than hardcoded in the SPA: PDF depends on reportlab being present in the
    image, so a build without it must not offer a button that returns 503.
    """
    pdf_ok = _cluster_report_pdf_available()
    kinds = []
    for kind, formats in _REPORT_KINDS.items():
        available = [f for f in formats if f != "pdf" or pdf_ok]
        kinds.append({
            "id": kind,
            "label": "Rack Status standard report" if kind == "rack-status"
            else "Nscale TX JSON schema report",
            "description": (
                "Cluster details plus a row per node — the same data as CSV, JSON or an "
                "executive PDF with rack-level charts."
                if kind == "rack-status" else
                "The customer's Consolidated Test Plan schema, validated before download."
            ),
            "formats": available,
            "default_format": available[0] if available else None,
        })
    return jsonify({"ok": True, "kinds": kinds, "pdf_available": pdf_ok})


@bp.route("/api/node-report/start", methods=["POST"])
@action("report.node:generate", variant="async")
def api_node_report_start():
    """Start report generation on a worker; poll ``/api/node-report/status?job_id=…``.

    Idempotent by request shape: asking again while an identical run is in flight returns the
    same ``job_id`` instead of launching a second fleet-wide sweep. Pressing the button twice
    is the normal way to find out whether the first press worked, and at ~2000 nodes the second
    sweep would cost four minutes of SSH against the whole cluster.
    """
    if _node_report is None:
        return jsonify({"ok": False, "error": "report generator unavailable"}), 503
    body = request.get_json(silent=True) or {}
    with_inventory = _truthy_arg(body.get("inventory", True))

    # Both reports are renderings of the same fleet sweep, so they share one job rather than
    # each getting its own launch path — the sweep is the expensive part (~4 min at 2000 nodes)
    # and duplicating it per report type would double the cost of a handover pack.
    report_kind = str(body.get("report") or "nscale").strip().lower()
    if report_kind not in _REPORT_KINDS:
        return jsonify({"ok": False, "error": f"unknown report: {report_kind}",
                        "supported": sorted(_REPORT_KINDS)}), 400
    fmt = str(body.get("format") or _REPORT_KINDS[report_kind][0]).strip().lower()
    if fmt not in _REPORT_KINDS[report_kind]:
        return jsonify({
            "ok": False,
            "error": f"{report_kind} does not support format {fmt!r}",
            "supported": list(_REPORT_KINDS[report_kind]),
        }), 400
    if fmt == "pdf" and not _cluster_report_pdf_available():
        return jsonify({"ok": False, "error": "PDF rendering unavailable (reportlab missing)"}), 503
    key = f"inventory={int(with_inventory)}&report={report_kind}&format={fmt}"

    with _node_report_jobs_lock:
        for existing, row in _node_report_jobs.items():
            # Bounded by age: a worker that died without recording an error would otherwise
            # leave its key pinned as "running" forever, and every later press would join a
            # job that is never going to finish.
            fresh = (time.time() - float(row.get("started") or 0)) < _NODE_REPORT_JOB_TTL
            if row.get("key") == key and row.get("state") == "running" and fresh:
                return jsonify({"ok": True, "job_id": existing, "reused": True})
        if len(_node_report_jobs) >= _NODE_REPORT_JOBS_MAX:
            for stale in sorted(_node_report_jobs,
                                key=lambda k: _node_report_jobs[k].get("started", 0))[:2]:
                _node_report_jobs.pop(stale, None)
        job_id = uuid.uuid4().hex
        _node_report_jobs[job_id] = {
            "state": "running", "phase": "starting", "key": key,
            "report": report_kind, "format": fmt,
            "done": 0, "total": 0, "current": "", "started": time.time(),
        }

    def _set(**fields) -> None:
        with _node_report_jobs_lock:
            row = _node_report_jobs.get(job_id)
            if row is not None:
                row.update(**fields)

    def _progress(done: int, total: int, current: str) -> None:
        _set(done=done, total=total, current=current or "")

    def _worker() -> None:
        try:
            _set(phase="reading bring-up results")
            fleet = _bringup.fleet_summary() if _bringup else {"nodes": [], "columns": []}
            inventory = deviations = None
            if with_inventory:
                _set(phase="sweeping nodes for hardware inventory",
                     total=len(fleet.get("nodes") or []))
                inventory, deviations = _collect_node_inventory(
                    fleet, progress=_progress, phase=lambda p: _set(phase=p)
                )
            _set(phase="assembling the report", current="")

            project = cluster = platform = None
            try:
                project, cluster, platform = catalog_context.resolve()
            except Exception:  # a missing catalog must not block the report
                _logger.debug("node-report: catalog context unavailable", exc_info=True)

            def _as_dict(obj):
                return obj.to_dict() if hasattr(obj, "to_dict") else (obj or {})

            if report_kind == "rack-status":
                import cluster_report

                facts = _cluster_facts()
                bcm_index = {}
                try:
                    import bcm_query

                    bcm_index = bcm_query.index_by_hostname(bcm_query.nodes())
                except Exception:  # BCM identity is an enrichment, not a prerequisite
                    _logger.debug("rack-status report: BCM lookup unavailable", exc_info=True)

                dataset = cluster_report.build_dataset(
                    fleet=fleet, inventory=inventory, facts=facts,
                    analytics=_analytics_last_cached(), bcm=bcm_index,
                    cluster=_as_dict(cluster), project=_as_dict(project),
                    platform=_as_dict(platform),
                )
                _set(phase=f"rendering {fmt.upper()}")
                if fmt == "csv":
                    body_bytes = cluster_report.to_csv(dataset).encode("utf-8")
                elif fmt == "pdf":
                    body_bytes = cluster_report.to_pdf(dataset)
                else:
                    body_bytes = cluster_report.to_json(dataset).encode("utf-8")
                name = f"rack-status-{(dataset['meta'].get('cluster') or 'cluster')}-{dataset['meta']['generated_at'][:10]}"
                saved = _save_generated_report(
                    kind="rack-status", fmt=fmt, filename=name, data=body_bytes,
                    meta={"cluster": dataset["meta"].get("cluster") or "",
                          "nodes": len(dataset.get("nodes") or [])},
                )
                _set(state="done", phase="complete", document=body_bytes, errors=[],
                     nodes=len(dataset.get("nodes") or []), report_id=name,
                     saved_id=saved)
            else:
                report = _node_report.build_report(
                    fleet, platform=_as_dict(platform), cluster=_as_dict(cluster),
                    project=_as_dict(project), inventory=inventory,
                )
                for entry in report.get("test_results") or []:
                    entry.setdefault("deviations", []).extend(deviations or [])

                _set(phase="validating against the schema")
                errors = _node_report.validate(report)
                report_id = report.get("report_metadata", {}).get("report_id", "")
                node_count = len(report.get("firmware_inventory", {}).get("nodes") or [])
                saved = _save_generated_report(
                    kind="nscale", fmt="json", filename=report_id or "nscale-report",
                    data=json.dumps(report, indent=2),
                    meta={"cluster": str((_as_dict(cluster) or {}).get("name") or ""),
                          "nodes": node_count, "schema_errors": len(errors)},
                )
                _set(state="done", phase="complete", report=report, errors=errors[:20],
                     nodes=node_count, report_id=report_id, saved_id=saved)
        except Exception as e:  # a worker crash must reach the operator, not just a log
            _logger.exception("node-report job failed")
            _set(state="error", error=str(e))

    threading.Thread(target=_worker, name=f"node-report-{job_id[:8]}", daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


@bp.route("/api/node-report/status", methods=["GET"])
@action(get="report.node:read")
def api_node_report_status():
    """Progress for a job started by ``/api/node-report/start``.

    The document itself is *not* returned here — at 2000 nodes it is several MB and polling
    would ship it repeatedly. ``/api/node-report/result`` collects it once it is ready.
    """
    job_id = (request.args.get("job_id") or "").strip()
    with _node_report_jobs_lock:
        row = _node_report_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        # Both payload keys are excluded by name: "document" holds rendered bytes, which are not
        # JSON-serialisable at all, so leaking it here fails the poll outright rather than merely
        # making it large.
        out = {k: v for k, v in row.items() if k not in ("report", "document")}
    out["ok"] = True
    out["ready"] = out.get("state") == "done"
    return jsonify(out)


@bp.route("/api/node-report/result", methods=["GET"])
@action(get="report.node:read")
def api_node_report_result():
    """Collect the finished document, optionally as a download."""
    job_id = (request.args.get("job_id") or "").strip()
    with _node_report_jobs_lock:
        row = _node_report_jobs.get(job_id)
        if row is None:
            return jsonify({"ok": False, "error": "unknown or expired job_id"}), 404
        if row.get("state") == "error":
            return jsonify({"ok": False, "error": row.get("error") or "job failed"}), 500
        if row.get("state") != "done":
            return jsonify({"ok": False, "error": "not ready", "phase": row.get("phase")}), 409
        report = row.get("report")
        document = row.get("document")
        fmt = str(row.get("format") or "json")
        stem = str(row.get("report_id") or "report")
        # Freed on collection so an abandoned tab cannot pin a multi-MB document.
        row.pop("report", None)
        row.pop("document", None)
        row["state"] = "collected"

    # A pre-rendered document (Rack Status CSV/JSON/PDF) is served as-is; the customer-schema
    # report is JSON built here so it stays byte-identical to what was schema-validated.
    if document is not None:
        body: bytes | str = document
    else:
        body = json.dumps(report, indent=2)
        fmt = "json"

    ext, mimetype = _FORMAT_MEDIA.get(fmt, ("json", "application/json"))
    if _truthy_arg(request.args.get("download")):
        return Response(body, mimetype=mimetype, headers={
            "Content-Disposition": f'attachment; filename="{stem}.{ext}"',
        })
    return Response(body, mimetype=mimetype)


_post_run_report_lock = threading.Lock()


_post_run_report_cache: tuple[float, dict] = (0.0, {})


def _post_run_accounting(activities: list[dict]) -> list[dict]:
    """One Slurm accounting query for all tracked validation task ids."""
    job_ids: list[str] = []
    seen: set[str] = set()
    for activity in activities:
        for task in activity.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            jid = str(task.get("scheduler_ref") or "").strip().split(".")[0]
            if _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid) and jid not in seen:
                seen.add(jid)
                job_ids.append(jid)
    if not job_ids:
        return []
    # The activity store is capped at 500 entries. Keep the newest 400 Slurm
    # allocations in the live reconciliation; older exact memberships remain in
    # post_run_nodes.json after their first snapshot.
    selected = job_ids[:400]
    cmd = _get_platform().sacct_cmd(
        "-X -n -P -j " + shlex.quote(",".join(selected))
        + " -o JobIDRaw,State,NodeList,Start,End,ExitCode,JobName"
    )
    proc = host_exec.run_slurm_on_host(
        cmd,
        timeout=max(30, _env_int("RACK_POST_RUN_SACCT_TIMEOUT", 120)),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "sacct failed").strip())
    wanted = set(selected)
    rows: list[dict] = []
    found: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        cols = line.split("|")
        if len(cols) < 7:
            continue
        raw_id = cols[0].strip()
        jid = raw_id.split(".")[0]
        if jid not in wanted or jid in found or raw_id != jid:
            continue
        found.add(jid)
        rows.append({
            "job_id": jid,
            "state": cols[1].strip(),
            "nodelist": cols[2].strip(),
            "started_at": cols[3].strip(),
            "ended_at": cols[4].strip(),
            "exit_code": cols[5].strip(),
            "job_name": cols[6].strip(),
        })
    return rows


def _post_run_inventory() -> list[dict]:
    proc = host_exec.run_slurm_on_host(
        _get_platform().sinfo_cmd('-N -h -o "%N|%T|%E"'),
        timeout=max(20, _env_int("RACK_SINFO_NODES_TIMEOUT", 60)),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "sinfo failed").strip())
    by_node: dict[str, dict] = {}
    for line in (proc.stdout or "").splitlines():
        cols = line.split("|", 2)
        if len(cols) < 2 or not cols[0].strip():
            continue
        node = cols[0].strip()
        state = cols[1].strip().lower().rstrip("*")
        reason = cols[2].strip() if len(cols) > 2 else ""
        if reason in {"none", "(null)", "N/A", "n/a"}:
            reason = ""
        by_node[node.casefold()] = {
            "node": node,
            "rack": post_run_report.rack_for_node(node),
            "state": state,
            "reason": reason,
        }
    return list(by_node.values())


def _post_run_audits() -> list[dict]:
    """Latest structured hardware/network result rows from durable workflows."""
    out: list[dict] = []
    for summary in workflow_engine.list_runs():
        if summary.get("workflow_id") not in core.DEEP_AUDIT_WORKFLOWS:
            continue
        run = workflow_engine.get_run_state(str(summary.get("run_id") or "")) or summary
        if run.get("invalidated_at"):
            continue
        rack = str((run.get("params") or {}).get("rack") or "")
        for audit in (run.get("audits") or {}).values():
            if not isinstance(audit, dict):
                continue
            kind = str(audit.get("kind") or "")
            for node_result in audit.get("nodes") or []:
                if not isinstance(node_result, dict):
                    continue
                out.append({
                    **node_result,
                    "kind": kind,
                    "workflow_id": str(run.get("workflow_id") or ""),
                    "run_state": str(run.get("state") or ""),
                    "rack": rack or post_run_report.rack_for_node(
                        str(node_result.get("node") or "")
                    ),
                    "run_id": str(run.get("run_id") or ""),
                    "started_at": str(run.get("started_at") or ""),
                    "updated_at": str(run.get("updated_at") or ""),
                })
    return out


def _build_post_run_report(*, rack: str = "", node: str = "", force: bool = False) -> dict:
    global _post_run_report_cache
    now = time.monotonic()
    ttl = max(5, _env_int("RACK_POST_RUN_REPORT_TTL_SEC", 30))
    cached_at, cached = _post_run_report_cache
    if cached and not force and not rack and not node and (now - cached_at) < ttl:
        return cached
    with _post_run_report_lock:
        cached_at, cached = _post_run_report_cache
        if cached and not force and not rack and not node and (now - cached_at) < ttl:
            return cached
        activities = validation_activities.list_activities(limit=500)
        accounting_error = ""
        try:
            accounting = _post_run_accounting(activities)
            fresh_events = post_run_report.events_from_accounting(activities, accounting)
            merge = post_run_report.merge_events(fresh_events)
        except Exception as exc:  # noqa: BLE001 - preserve durable history on Slurm failure
            accounting_error = str(exc)
            _logger.warning("post-run accounting refresh failed: %s", exc)
            merge = {"merged": 0, "error": accounting_error}
        inventory_error = ""
        try:
            inventory = _post_run_inventory()
        except Exception as exc:  # noqa: BLE001 - report cached events when inventory is unavailable
            inventory_error = str(exc)
            inventory = []
            _logger.warning("post-run inventory refresh failed: %s", exc)
        import node_issues

        issue_store = node_issues.load()
        # Only Rack Status' structured findings are used. Google punchcard cells
        # are intentionally excluded from this first-class report.
        findings = [
            row for row in issue_store.get("issues", []) if isinstance(row, dict)
        ]
        payload = post_run_report.build_report(
            events=post_run_report.load_events(),
            inventory=inventory,
            findings=findings,
            audit_results=_post_run_audits(),
            rack=rack,
            node=node,
        )
        payload["refresh"] = {
            "activities": len(activities),
            "merge": merge,
            "accounting_error": accounting_error,
            "inventory_error": inventory_error,
        }
        if not rack and not node:
            _post_run_report_cache = (time.monotonic(), payload)
        return payload


@bp.route("/api/post-run-node-report", methods=["GET"])
@action(get="report.node:read")
def api_post_run_node_report():
    """Participation, attributed failure trends, audits and current Slurm state."""
    rack = (request.args.get("rack") or "").strip()
    node = (request.args.get("node") or "").strip()
    if rack and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", rack):
        return jsonify({"ok": False, "error": "invalid rack"}), 400
    if node and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", node):
        return jsonify({"ok": False, "error": "invalid node"}), 400
    try:
        payload = _build_post_run_report(
            rack=rack,
            node=node,
            force=_truthy_arg(request.args.get("refresh")),
        )
    except Exception as exc:
        _logger.exception("post-run node report failed")
        return jsonify({"ok": False, "error": str(exc)}), 503
    return jsonify({"ok": True, **payload})


@bp.route("/api/post-run-node-report/mark", methods=["POST"])
@action("report.node:mark")
def api_post_run_node_mark():
    """Record an operator finding and optionally drain the exact node."""
    import node_issues

    body = request.get_json(silent=True) or {}
    node = str(body.get("node") or "").strip()
    reason = str(body.get("reason") or "").strip()
    category = str(body.get("category") or "validation").strip().lower()
    scheduler_ref = str(
        body.get("job_id") or body.get("scheduler_ref") or ""
    ).strip()
    drain = body.get("drain") is True
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{1,127}", node):
        return jsonify({"ok": False, "error": "a valid exact node is required"}), 400
    if node.casefold() not in {name.casefold() for name in _all_slurm_nodenames()}:
        return jsonify({"ok": False, "error": "node is not in current Slurm inventory"}), 409
    if category not in {
        "hardware", "network", "gpu", "thermal", "storage", "control_plane",
        "validation", "maintenance",
    }:
        return jsonify({"ok": False, "error": "unsupported category"}), 400
    if not reason or len(reason) > 1000:
        return jsonify({
            "ok": False,
            "error": "reason is required (maximum 1000 characters)",
        }), 400
    rack = post_run_report.rack_for_node(node)
    slurm_reason = ""
    drain_result = "local annotation only; scheduler state unchanged"
    drained = False
    if drain:
        raw_reason = (
            f"Drained for {category} issue - for Job {scheduler_ref or 'operator-mark'} "
            f"due to {reason}"
        )
        slurm_reason = re.sub(
            r"[^A-Za-z0-9 _.,:/()\-]", " ", raw_reason
        )[:256].strip()
        result = _run_scontrol_node_update(
            node, ["state=drain", f"reason={slurm_reason}"]
        )
        drained = bool(result.get("ok"))
        drain_result = str(
            result.get("stdout")
            or result.get("stderr")
            or f"returncode={result.get('returncode')}"
        )[:2000]
        if not drained:
            return jsonify({
                "ok": False,
                "error": "Slurm drain failed",
                "slurm": result,
            }), 502
    operator = _operator_from_request() or "rack-status-operator"
    entry = node_issues.record_issue(
        node=node,
        rack=rack,
        category=category,
        code="operator_mark",
        reason=reason,
        severity="failure",
        source="post-run-node-report",
        validation_run_id=str(body.get("validation_run_id") or ""),
        scheduler_ref=scheduler_ref,
        evidence=str(body.get("evidence") or "")[:8000],
        drained=drained,
        slurm_reason=slurm_reason,
        drain_result=drain_result,
        operator=operator,
    )
    global _post_run_report_cache
    _post_run_report_cache = (0.0, {})
    return jsonify({
        "ok": True,
        "finding": entry,
        "drained": drained,
        "slurm_reason": slurm_reason,
    })
