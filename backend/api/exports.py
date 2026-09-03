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
"""Exports: the same results dataset as CSV, as a styled Excel workbook, and as a zip of raw logs.

A renderer, not a second generator (core-designs.mdc citizen 10). The rows come from the parser and the
rack-status engine; nothing here computes a measurement of its own, and it must stay that way -- two
generators drift on the first added column, and an export that contradicts the dashboard is worse than
no export.

What is genuinely export-specific is the RESHAPING, and that is what the ``_export_*`` helpers are:

* Coalescing. A rack can have several runs, and a spreadsheet row is one rack, so
  ``_export_nccl_coalesce_max`` takes the best bandwidth and ``_export_llm_coalesce_latest_job`` takes the
  most recent training job. Those are different rules on purpose: for a bandwidth sweep the peak is the
  result, while for training the newest run supersedes the older.
* ``_export_float_cell`` writes a number as a number. A metric shipped as text sorts lexically in Excel,
  which is how "912.4" ends up between "1" and "2" in a customer's acceptance pack.
* ``_workbook_style_and_write_table`` is where the deviation gets PUBLISHED rather than hidden -- when a
  sheet cannot hold what the dataset says, the renderer reshapes and labels it.

``list_log_txt_paths_for_export`` and ``_multirack_txt_paths_under_failed`` decide what goes in the zip,
including the failed-run directory: a bundle that silently omits the failures is the opposite of useful
when the zip exists to be attached to a support case.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import host_exec
from audit.actions import action
from config_env import _eget, _eget_int, _env_int, _subprocess_env
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from flask import Blueprint, Response, jsonify, redirect, request, url_for
from naming import (
    _is_multirack_log_basename,
    _nemo_nccl_in_log_basename,
    _normalize_rack_id,
    _rack_id_from_nemo_1rack_filename,
)
from node_targets import _validation_ct_inner
from paths import (
    _bug_report_local_base_path,
    _parser_path,
    _resolve_log_dir,
    _script_exists,
)
from rack_status_engine import get_cluster_snapshot
from results_ingest import (
    _nemo_log_file_looks_successful,
    _parser_export_glob,
    _parser_extra_args,
)

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("exports", __name__, url_prefix="")


def _multirack_txt_paths_under_failed(log_dir: Path, pat: str) -> list[Path]:
    """Multirack NEMO/NCCL *.txt under log_dir/failed/ (same glob as root export).

    Multirack jobs often write logs only under ``failed/``; include them in Excel/zip export so
    parser results match the Multirack tab.
    """
    failed = _rpath(log_dir) / "failed"
    out: list[Path] = []
    # One listing through the connection: the directory lives on the execution
    # host, and stat_dir already returns regular files only (absent dir -> {}).
    for path_s in host_exec.stat_dir(failed, patterns=(pat,)):
        p = PurePosixPath(path_s)
        if _nemo_nccl_in_log_basename(p.name) and _is_multirack_log_basename(p.name):
            out.append(p)  # type: ignore[arg-type]
    return out


def list_log_txt_paths_for_export(
    log_dir: Path, *, include_multirack_under_failed: bool = True
) -> list[Path]:
    """Matching *.txt under log_dir root, optionally plus multirack logs under ``failed/``.

    NEMO one-rack logs (*1Rack-pNN-rMM-*.txt): at most one file per rack — the newest by mtime
    among logs that look successful (same heuristics as parser.sh: no Slurm hard-fail grep, LLM
    markers present). Only racks whose **nemo** cell from ``rack_status.sh`` is ``done`` (not
    ongoing / fail / pending) are included unless ``RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER`` is set.
    Racks with no successful log contribute no 1Rack file. Other matching files in the root are
    included unchanged (including multirack logs in the root). When ``include_multirack_under_failed``
    is true, multirack logs under ``failed/`` are merged in (deduped by resolved path); Excel export
    passes false so nothing under ``failed/`` is included.
    """
    pat = _parser_export_glob()
    root_files: list[Path] = []
    # One directory listing through the connection. Listing locally used to yield
    # ZERO files against a remote log dir, which is why the Logs page reported
    # "1 of 1" while 13 real logs sat in the directory.
    root = _rpath(log_dir)
    root_stats = host_exec.stat_dir(root, patterns=(pat,))
    for path_s in root_stats:
        name = PurePosixPath(path_s).name
        if name and _nemo_nccl_in_log_basename(name):
            root_files.append(PurePosixPath(path_s))  # type: ignore[arg-type]

    done_racks: frozenset[str] | None = None
    if not _truthy_env("RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER"):
        done_racks = _nemo_done_rack_ids_from_rack_status(log_dir)

    rack_buckets: dict[str, list[Path]] = {}
    others: list[Path] = []
    for p in root_files:
        rk = _rack_id_from_nemo_1rack_filename(p.name)
        if rk is None:
            others.append(p)
        else:
            rack_buckets.setdefault(rk, []).append(p)

    selected_rack: list[Path] = []
    for _rk, paths in rack_buckets.items():
        if done_racks is not None and _rk not in done_racks:
            continue
        successful = [p for p in paths if _nemo_log_file_looks_successful(p)]
        if not successful:
            continue
        # mtimes come from the listing above; the files are on the execution host,
        # so p.stat() would be a local call against a path that is not local.
        best = max(successful, key=lambda x: root_stats[str(x)].mtime)
        selected_rack.append(best)

    tail_failed = (
        _multirack_txt_paths_under_failed(log_dir, pat) if include_multirack_under_failed else []
    )
    merged: dict[str, Path] = {}
    for p in others + selected_rack + tail_failed:
        # Already absolute and normalised by _rpath/stat_dir, so dedupe on the
        # string; .resolve() is a local-filesystem call and these are exec-host paths.
        merged[str(p)] = p
    out = sorted(merged.values(), key=lambda p: p.name.lower())
    return out


def _nemo_done_rack_ids_from_rack_status(log_dir: Path) -> frozenset[str] | None:
    """Racks whose **nemo** cell is ``done`` in the current status table (native or shell).

    Returns ``None`` if status could not be loaded (caller treats as: do not apply this filter).
    Returns an empty frozenset if the table loaded but no rack is ``done``.
    """
    try:
        snapshot = get_cluster_snapshot(log_dir)
        if snapshot.payload is None:
            return None
        data = snapshot.payload
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    except Exception:
        _logger.debug("_nemo_done_rack_ids_from_rack_status: ignoring error", exc_info=True)
        return None
    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) == 0:
        return None
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rack_raw = (row.get("rack") or "").strip()
        nemo_raw = (row.get("nemo") or "").strip()
        if not rack_raw:
            continue
        first = nemo_raw.split()[0].lower() if nemo_raw else ""
        if first != "done":
            continue
        try:
            rk = _normalize_rack_id(rack_raw)
        except ValueError:
            continue
        out.add(rk)
    return frozenset(out)


_EXPORT_RACK_RE = re.compile(r"^(p\d+-r)(\d+)$", re.IGNORECASE)


def _export_float_cell(v: object) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _export_nodes_per_rack() -> int:
    n = _eget_int("RACK_EXPORT_NODES_PER_RACK", 18)
    return max(1, min(n, 65535))


def _export_parse_rack(rack: str) -> tuple[str, int] | None:
    m = _EXPORT_RACK_RE.match((rack or "").strip())
    if not m:
        return None
    pref = m.group(1).lower()
    try:
        num = int(m.group(2), 10)
    except ValueError:
        return None
    return pref, num


def _export_nccl_coalesce_max(nccl: list[dict], field: str) -> dict[str, float]:
    """One peak value per rack (max across duplicate job logs)."""
    out: dict[str, float] = {}
    for row in nccl:
        if not isinstance(row, dict):
            continue
        rk = str(row.get("rack") or "").strip()
        if not rk:
            continue
        v = _export_float_cell(row.get(field))
        if v is None:
            continue
        out[rk] = max(out.get(rk, float("-inf")), v)
    return {k: v for k, v in out.items() if v > float("-inf")}


def _export_llm_coalesce_latest_job(llm: list[dict]) -> dict[str, dict]:
    """Keep the row per rack with the highest numeric job_id."""
    best: dict[str, tuple[int, dict]] = {}
    for row in llm:
        if not isinstance(row, dict):
            continue
        rk = str(row.get("rack") or "").strip()
        if not rk:
            continue
        jid_raw = str(row.get("job_id") or "").strip()
        try:
            jk = int(jid_raw, 10)
        except ValueError:
            jk = -1
        prev = best.get(rk)
        if prev is None or jk >= prev[0]:
            best[rk] = (jk, row)
    return {k: v[1] for k, v in best.items()}


def _export_title_racks_flat(rack_ids: object) -> str:
    """Title listing racks that appear in the export (no synthetic multi-node spans)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for rid in rack_ids:
        s = str(rid).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        ordered.append(s)

    def sort_key(rid: str) -> tuple[str, int]:
        pr = _export_parse_rack(rid)
        if pr:
            return (pr[0].lower(), pr[1])
        return (rid.lower(), 0)

    ordered.sort(key=sort_key)
    if not ordered:
        return "Racks"
    return "Racks " + ", ".join(ordered)


def _export_formatted_metric_rows(values_by_rack: dict[str, float]) -> list[tuple[str, int, float]]:
    """One row per rack (single-rack logs only)."""
    parsed: list[tuple[str, int, str, float]] = []
    for rid, val in values_by_rack.items():
        pr = _export_parse_rack(rid)
        if pr:
            parsed.append((pr[0].lower(), pr[1], rid, val))
    parsed.sort(key=lambda x: (x[0], x[1]))
    if not parsed:
        return []
    npr = _export_nodes_per_rack()
    return [(rid, npr, val) for _a, _b, rid, val in parsed]


def _export_formatted_llm_rows(by_rack: dict[str, dict]) -> list[tuple[str, int, float, float, float]]:
    """One row per rack: Rack, Nodes, step_s, tflops, mfu (numeric for MFU cell formatting)."""
    tmp: dict[str, tuple[float, float, float]] = {}
    for rid, row in by_rack.items():
        st = _export_float_cell(row.get("step_s"))
        tf = _export_float_cell(row.get("tflops_per_gpu"))
        mf = _export_float_cell(row.get("mfu_percent"))
        if st is None and tf is None and mf is None:
            continue
        tmp[rid] = (st or 0.0, tf or 0.0, mf or 0.0)
    parsed: list[tuple[str, int, str, tuple[float, float, float]]] = []
    for rid, tup in tmp.items():
        pr = _export_parse_rack(rid)
        if pr:
            parsed.append((pr[0].lower(), pr[1], rid, tup))
    parsed.sort(key=lambda x: (x[0], x[1]))
    if not parsed:
        return []
    npr = _export_nodes_per_rack()
    return [(rid, npr, t[0], t[1], t[2]) for _a, _b, rid, t in parsed]


def _workbook_style_and_write_table(
    ws,
    *,
    title: str,
    headers: list[str],
    data_rows: list[list[object]],
    merge_to_col: int,
) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    title_fill = PatternFill("solid", fgColor="2F4F2F")
    hdr_fill = PatternFill("solid", fgColor="375623")
    data_fill = PatternFill("solid", fgColor="3A3D40")
    font_title = Font(color="FFFFFF", bold=True, size=12)
    font_hdr = Font(color="FFFFFF", bold=True, size=11)
    font_data = Font(color="E6EDF3", size=11)
    thin = Side(style="thin", color="5C6B7A")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left = Alignment(horizontal="left", vertical="center")

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=merge_to_col)
    c1 = ws.cell(row=1, column=1, value=title)
    c1.fill = title_fill
    c1.font = font_title
    c1.alignment = left
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=2, column=col, value=h)
        cell.fill = hdr_fill
        cell.font = font_hdr
        cell.alignment = center
        cell.border = border
    for ri, row in enumerate(data_rows, start=3):
        for ci, val in enumerate(row, start=1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.fill = data_fill
            cell.font = font_data
            cell.border = border
            cell.alignment = center if ci > 1 else left


def _workbook_bytes_from_parser_export(parsed: dict) -> bytes:
    """Build .xlsx: NCCL sheets + llm match rack summary table layout (title + Rack / Nodes / …)."""
    try:
        from openpyxl import Workbook
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError(
            "openpyxl is required for Excel export (pip install -r requirements.txt)"
        ) from e

    wb = Workbook()
    ws_ar = wb.active
    ws_ar.title = "nccl all_reduce"
    ws_aa = wb.create_sheet("nccl all_to_all")
    ws_ag = wb.create_sheet("nccl all_gather")
    ws_llm = wb.create_sheet("llm")

    nccl = parsed.get("nccl_peak_out_of_place_busbw_gbps")
    if not isinstance(nccl, list):
        nccl = []

    ar_map = _export_nccl_coalesce_max(nccl, "all_reduce_gbps")
    aa_map = _export_nccl_coalesce_max(nccl, "alltoall_gbps")
    ag_map = _export_nccl_coalesce_max(nccl, "all_gather_gbps")

    all_nccl_racks = sorted(set(ar_map) | set(aa_map) | set(ag_map))
    title_nccl = _export_title_racks_flat(all_nccl_racks)

    def nccl_body(rows: list[tuple[str, int, float]]) -> list[list[object]]:
        return [[a, b, round(c, 2)] for a, b, c in rows]

    ar_rows = _export_formatted_metric_rows(ar_map)
    aa_rows = _export_formatted_metric_rows(aa_map)
    ag_rows = _export_formatted_metric_rows(ag_map)

    _workbook_style_and_write_table(
        ws_ar,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All Reduce GB/s"],
        data_rows=nccl_body(ar_rows),
        merge_to_col=3,
    )
    _workbook_style_and_write_table(
        ws_aa,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All to All GB/s"],
        data_rows=nccl_body(aa_rows),
        merge_to_col=3,
    )
    _workbook_style_and_write_table(
        ws_ag,
        title=title_nccl,
        headers=["Rack", "Nodes", "NCCL All Gather GB/s"],
        data_rows=nccl_body(ag_rows),
        merge_to_col=3,
    )

    llm = parsed.get("nemotron_5h_56b_train_window")
    if not isinstance(llm, list):
        llm = []
    llm_by = _export_llm_coalesce_latest_job(llm)
    _llm_base = _export_title_racks_flat(llm_by.keys())
    _ct_inner = _validation_ct_inner()
    llm_title_str = (
        f"{_llm_base} · ct[{_ct_inner}]" if _llm_base != "Racks" else f"Racks ct[{_ct_inner}]"
    )
    llm_rows_fmt = _export_formatted_llm_rows(llm_by)
    llm_data: list[list[object]] = []
    for rack, nodes, st, tf, mf in llm_rows_fmt:
        mf_s = f"{mf:.1f}%" if mf is not None else ""
        llm_data.append([rack, nodes, round(st, 2), round(tf, 2), mf_s])

    _workbook_style_and_write_table(
        ws_llm,
        title=llm_title_str,
        headers=["Rack", "Nodes", "Steps [s]", "Per GPU TFLOPs", "MFU [%]"],
        data_rows=llm_data,
        merge_to_col=5,
    )

    for ws in (ws_ar, ws_aa, ws_ag, ws_llm):
        for col in range(1, ws.max_column + 1):
            letter = get_column_letter(col)
            maxlen = 10
            for row in range(1, ws.max_row + 1):
                v = ws.cell(row=row, column=col).value
                if v is not None:
                    maxlen = max(maxlen, min(52, len(str(v))))
            ws.column_dimensions[letter].width = maxlen + 2.0

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _parser_excel_report_copy_dir() -> Path:
    """Directory for an on-disk copy of each successful parser Excel export.

    Override with ``RACK_PARSER_EXCEL_REPORT_DIR`` (absolute or ``~`` path; directory is created).

    Default: ``<RACK_BUG_REPORT_REMOTE_DIR>/parser-excel`` (typically ``/home/cmsupport/reports/parser-excel``).
    """
    raw = (_eget("RACK_PARSER_EXCEL_REPORT_DIR") or "").strip()
    if raw:
        # An exec-host path: expanduser/resolve would answer from this process's
        # filesystem and home directory, which is not where the reports live.
        return _rpath(raw)
    return _bug_report_local_base_path() / "parser-excel"


@bp.route("/api/parser-export-excel")
@action(get="export:read")
def api_parser_export_excel():
    """Run parser.sh --json on export-selected logs (log root only; no ``failed/``; one successful 1Rack log per rack with nemo=done), then .xlsx."""
    if os.environ.get("RACK_PARSER_EXPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "export disabled (RACK_PARSER_EXPORT_DISABLED)"}), 403
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    paths = list_log_txt_paths_for_export(log_dir, include_multirack_under_failed=False)
    if not paths:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"no exportable .txt logs matching {_parser_export_glob()!r} under {log_dir} "
                    "(Excel: log_dir root only, no failed/; NEMO 1Rack: one successful log per rack with "
                    "nemo=done in rack_status, unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
                ),
            }
        ), 404
    parser = _parser_path()
    if not _script_exists(parser):
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    root = _rpath(log_dir)
    rel_args: list[str] = []
    for p in paths:
        # Already absolute exec-host paths from the listing; .resolve() would ask
        # the *local* filesystem about a directory that may live elsewhere.
        try:
            rel_args.append(str(PurePosixPath(str(p)).relative_to(root)))
        except ValueError:
            rel_args.append(str(p))
    env = _subprocess_env({"NO_COLOR": "1"})
    cmd = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_args]
    timeout = max(60, _env_int("RACK_PARSER_EXPORT_TIMEOUT", 900))
    try:
        # parser.sh reads the logs, so it has to run where they are: the logs role's
        # host, with the log dir as cwd. A local subprocess found neither.
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(root),
            env=env,
            timeout=timeout,
            role="logs",
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": f"parser.sh --json timed out after {timeout}s"}), 504
    except Exception as e:
        _logger.debug("api_parser_export_excel: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    if proc.returncode != 0:
        return jsonify(
            {
                "ok": False,
                "error": "parser.sh --json exited non-zero",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip(),
                "stdout_head": (proc.stdout or "")[:4000],
            }
        ), 502
    raw_out = (proc.stdout or "").strip()
    if not raw_out:
        return jsonify(
            {
                "ok": False,
                "error": "parser.sh produced empty JSON on stdout",
                "stderr": (proc.stderr or "").strip(),
            }
        ), 502
    try:
        parsed = json.loads(raw_out)
    except json.JSONDecodeError:
        return jsonify(
            {
                "ok": False,
                "error": "parser did not emit valid JSON on stdout",
                "stderr": (proc.stderr or "").strip(),
                "stdout_head": raw_out[:4000],
            }
        ), 502
    if not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "parser JSON root must be an object"}), 502
    try:
        body = _workbook_bytes_from_parser_export(parsed)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = f"rack-parser-export-{stamp}.xlsx"
    hdrs: dict[str, str] = {
        "Content-Disposition": f'attachment; filename="{fname}"',
        "Cache-Control": "no-store, max-age=0",
        "Pragma": "no-cache",
        "X-Export-Log-Count": str(len(paths)),
    }
    try:
        dest = _parser_excel_report_copy_dir() / fname
        # The reports directory belongs to the execution host, so the copy travels
        # through the connection; a local write would land inside the container.
        err = host_exec.write_bytes(dest, body)
        if err:
            raise OSError(err)
        hdrs["X-Export-Report-Copy"] = str(dest)
    except OSError as e:
        _logger.warning("parser Excel: could not write report copy: %s", e)
        hdrs["X-Export-Report-Copy-Error"] = str(e)[:500]
    return Response(
        body,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=hdrs,
    )


@bp.route("/api/parser-export-csv")
@action(get="export:read")
def api_parser_export_csv_legacy():
    """Backward-compatible redirect; exports are now multi-sheet Excel."""
    return redirect(url_for(".api_parser_export_excel", **request.args), code=307)


@bp.route("/api/export-logs-zip")
@action(get="export:read")
def api_export_logs_zip():
    """Zip exportable matching .txt (same rules as parser Excel export: root + multirack under failed/)."""
    if os.environ.get("RACK_LOG_EXPORT_DISABLED", "").strip().lower() in ("1", "true", "yes"):
        return jsonify({"ok": False, "error": "log zip export disabled (RACK_LOG_EXPORT_DISABLED)"}), 403
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    paths = list_log_txt_paths_for_export(log_dir)
    if not paths:
        return jsonify(
            {
                "ok": False,
                "error": (
                    f"no exportable .txt logs matching {_parser_export_glob()!r} under {log_dir} "
                    "(root + multirack under failed/; NEMO 1Rack: one successful log per rack with "
                    "nemo=done in rack_status, unless RACK_LOG_EXPORT_SKIP_NEMO_DONE_FILTER is set)"
                ),
            }
        ), 404
    root = _rpath(log_dir)
    # The logs live on the execution host, so each one is pulled through the
    # connection and written from memory: zipfile.write() resolves and opens the
    # path locally, which finds nothing when the log dir is on the head node.
    cap = max(1, _env_int("RACK_LOG_EXPORT_MAX_BYTES", 64 * 1024 * 1024))
    bio = io.BytesIO()
    written = 0
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                text, meta = host_exec.read_text(p, whole_bytes=cap)
                if meta.get("read_error"):
                    skipped.append(p.name)
                    continue
                try:
                    arc = str(PurePosixPath(str(p)).relative_to(root))
                except ValueError:
                    arc = p.name
                zf.writestr(arc, text)
                written += 1
    except (OSError, zipfile.BadZipFile) as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    if not written:
        return jsonify(
            {"ok": False, "error": f"could not read any of the {len(paths)} matching logs"}
        ), 500
    body = bio.getvalue()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    fname = f"rack-logs-export-{stamp}.zip"
    return Response(
        body,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Export-Log-Count": str(written),
            "X-Export-Skipped": str(len(skipped)),
        },
    )
