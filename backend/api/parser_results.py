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
"""Parser results: what a finished run measured, per rack, as the dashboard charts it.

The read side of the results pipeline. ``/api/parser-results`` is what the charts dialog calls; the
clusterkit and nvloom routes serve the two tools that publish structured output of their own instead of
text a parser has to scrape.

Three things here are easy to get wrong:

* A run has STAGES. ``_primary_stage_log`` picks the log that represents the run, and
  ``_parser_secondary_stages`` reports the rest, because a two-stage campaign (NCCL then NeMo) has two
  logs and showing only one silently drops half the evidence.
* ``_drop_parser_placeholder_rows`` is the consumer side of the predicate in ``results_ingest``:
  parser.sh emits a fully-formed row of dashes for a log its extractor did not apply to, so a table is
  not evidence of a measurement. Dropping those rows is what stops the UI reporting a run it never
  measured. ``_parser_row_has_measurement`` is the per-row form.
* Reads of tool-published JSON are capped (``_CLUSTERKIT_JSON_MAX_BYTES``,
  ``_NVLOOM_HEATMAPS_LOG_MAX_BYTES``). These files come off the execution host and a malformed or
  runaway one must cost a bounded read, not the process.

``_maybe_adopt_validation_log`` is the bridge to the durable registry: a run someone launched outside the
dashboard still shows up here, and adopting it means the activity ledger learns about it rather than the
result appearing with no history behind it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import core
import host_exec
import lake_sink
import results_history
import validation_activities
from audit.actions import action
from config_env import _env_int, _job_env_int, _subprocess_env
from core import rpath as _rpath
from core import truthy_env as _truthy_env
from core import utc_iso_z as _iso_utc_z
from flask import Blueprint, jsonify, request
from grafana_embed import (
    _effective_display_tz_for_embed,
    _grafana_dash_slug,
    _grafana_dash_uid,
    _grafana_embed_host_regex_and_mode,
    _grafana_url_timezone_param,
    _job_hosts_for_grafana,
    _rack_web_grafana_port,
    _rack_web_grafana_uri,
    _sanitize_display_timezone_query,
)
from log_discovery import (
    _resolve_nemo_log_basename_in_dir,
    pick_logs_for_rack,
)
from log_time_window import _compute_llm_time_window_unix
from naming import (
    _full_rack_expected_nodes,
    _jobid_from_nemo_log_basename,
    _normalize_rack_id,
    _nvis_naming,
    multirack_display_rack_from_basename,
)
from paths import _parser_path, _resolve_log_dir, _script_exists
from platforms import get_platform as _get_platform
from results_ingest import (
    _history_ingest_parser_json,
    _parser_extra_args,
    _parser_json_has_usable_rows,
    _universal_fallback_for_log,
    _uparse,
)
from slurm_query import (
    _parse_scontrol_job,
    _resolve_slurm_job_stdout_path,
    _run_scontrol_show_job,
)

from ._common import operator_from_request as _operator_from_request

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("parser_results", __name__, url_prefix="")


def _maybe_adopt_validation_log(log_path: Path, log_dir: Path, rack: str) -> None:
    """Assign an activity_id when an external validation log is first accessed."""
    if os.environ.get("RACK_VALIDATION_ACTIVITIES_ADOPT_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return
    try:
        validation_activities.adopt_log(
            str(log_path),
            log_dir=str(log_dir),
            rack=rack,
            operator=_operator_from_request() or "external",
        )
    except Exception as e:
        _logger.debug("validation log adopt skipped for %s: %s", log_path, e, exc_info=True)


def find_nvloom_log_paths_for_rack(log_dir: Path, rack: str) -> list[Path]:
    """Newest-first paths to ``*NVLOOM-<rack>-*.txt`` under ``log_dir/nvloom`` (and ``nvloom/failed``)."""
    try:
        rk = _normalize_rack_id(rack)
    except ValueError:
        return []
    pat = f"*NVLOOM-{rk}-*.txt"
    root = _rpath(log_dir)
    # mtime comes back with the listing, so ordering costs no extra round trip.
    scored: list[tuple[float, Path]] = []
    for base in (root / "nvloom", root / "nvloom" / "failed"):
        for path_s, st in host_exec.stat_dir(base, patterns=(pat,)).items():
            scored.append((st.mtime, PurePosixPath(path_s)))  # type: ignore[arg-type]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for _m, p in scored]


_NVLOOM_HEATMAPS_LOG_MAX_BYTES = 40 * 1024 * 1024


_PARSER_STAGE_TABLES = {
    "nccl": "nccl_peak_out_of_place_busbw_gbps",
    "nemo": "nemotron_5h_56b_train_window",
}


_PARSER_TABLE_VALUE_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "nccl_peak_out_of_place_busbw_gbps": (
        ("all_reduce_gbps", "all_gather_gbps", "alltoall_gbps"),
        ("all_reduce_busbw_series",),
    ),
    "nemotron_5h_56b_train_window": (
        ("step_s", "tflops_per_gpu", "step_time_std", "perf_std", "mfu_percent"),
        ("step_series",),
    ),
}


def _parser_row_has_measurement(row: object, table: str) -> bool:
    """True when *row* carries at least one real number for *table*.

    parser.sh emits a fully-formed placeholder row (every field ``"-"``/``"---"``, status UNK or
    FAIL, empty series) for whatever file it is handed, whether or not that file is the kind the
    table describes. Parsing an NCCL log therefore still yields a Nemotron row. Mirrors the
    abstention already applied in ``results_history.records_from_parser_json``.
    """
    if not isinstance(row, Mapping):
        return False
    scalars, series_keys = _PARSER_TABLE_VALUE_FIELDS.get(table, ((), ()))
    for key in scalars:
        raw = row.get(key)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            float(str(raw).strip())
        except (TypeError, ValueError):
            continue
        return True
    return any(row.get(key) for key in series_keys)


def _drop_parser_placeholder_rows(data: dict[str, object]) -> dict[str, list[str]]:
    """Strip placeholder rows from every stage table in *data*, in place.

    Returns ``{table: [rack…]}`` for what was dropped, so a rack with no run for a stage reads as
    an empty table (the truth) instead of a UNK row keyed to the *other* stage's job id. Without
    this, j15/j16 — NCCL-only racks with no NeMo log at all — rendered a Nemotron row carrying the
    NCCL job id, which reads as "NeMo ran and was inconclusive" when NeMo never ran.
    """
    dropped: dict[str, list[str]] = {}
    for table in _PARSER_STAGE_TABLES.values():
        rows = data.get(table)
        if not isinstance(rows, list) or not rows:
            continue
        keep = [r for r in rows if _parser_row_has_measurement(r, table)]
        if len(keep) == len(rows):
            continue
        gone = [
            str((r or {}).get("rack") or (r or {}).get("job_id") or "?")
            for r in rows
            if not _parser_row_has_measurement(r, table)
        ]
        data[table] = keep
        dropped[table] = gone
    return dropped


def _primary_stage_log(stage_paths: dict[str, Path], job_id: str | None) -> Path | None:
    """The log the request is 'about': the one carrying *job_id*, else NeMo, else NCCL.

    Everything that can only sensibly take one log (the Grafana time window, sn-llama, history
    ingest, log adoption) uses this, so adding the second stage cannot change their behaviour.
    """
    if not stage_paths:
        return None
    j = (job_id or "").strip()
    if j.isdigit():
        suf = f"N-{j}.txt"
        for p in stage_paths.values():
            if p.name.endswith(suf):
                return p
    return stage_paths.get("nemo") or stage_paths.get("nccl")


def _parser_secondary_stages(stage_paths: dict[str, Path], primary: Path) -> list[tuple[str, Path]]:
    """Stages whose log is a DIFFERENT file from *primary*, so they need their own parse.

    On the older combined format both stages resolve to the same file, which yields an empty
    list — the primary parse already produced both tables and must not be run twice.
    """
    seen = {str(primary)}
    out: list[tuple[str, Path]] = []
    for stage, path in stage_paths.items():
        if str(path) in seen:
            continue
        seen.add(str(path))
        out.append((stage, path))
    return out


def _nemotron_row_for_rack(
    parsed: dict | None,
    rack: str,
    job_raw: str | None,
) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    ll = parsed.get("nemotron_5h_56b_train_window")
    if not isinstance(ll, list):
        return None
    rk = rack.strip().lower()
    jwant = (job_raw or "").strip()
    if jwant and jwant != "-":
        for row in ll:
            if not isinstance(row, dict):
                continue
            if str(row.get("rack", "")).strip().lower() != rk:
                continue
            if str(row.get("job_id", "")).strip() == jwant:
                return row
    for row in ll:
        if isinstance(row, dict) and str(row.get("rack", "")).strip().lower() == rk:
            return row
    return None


def _grafana_parser_embed_context(
    log_path: Path,
    rack: str,
    job_raw: str | None,
    parsed: dict | None,
    log_timestamp_tz: str | None = None,
) -> dict[str, object]:
    dtz = _effective_display_tz_for_embed(log_timestamp_tz)
    if _truthy_env("RACK_WEB_GRAFANA_EMBED_DISABLED"):
        return {
            "ok": False,
            "iframe_ready": False,
            "disabled": True,
            "note": "Grafana embed disabled (RACK_WEB_GRAFANA_EMBED_DISABLED).",
            "display_timezone": dtz,
        }
    row = _nemotron_row_for_rack(parsed, rack, job_raw)
    win = _compute_llm_time_window_unix(log_path, row, log_timestamp_tz)
    if not win:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Could not derive LLM time window.",
            "display_timezone": dtz,
        }
    lo, hi, tsrc = win
    if hi <= lo:
        return {
            "ok": False,
            "iframe_ready": False,
            "note": "Invalid time window (end <= start).",
            "display_timezone": dtz,
        }
    hosts, hsrc = _job_hosts_for_grafana(rack, job_raw)
    host_regex, host_regex_mode = _grafana_embed_host_regex_and_mode(rack, hosts)
    return {
        "ok": True,
        "iframe_ready": True,
        "from_ms": int(lo * 1000),
        "to_ms": int(hi * 1000),
        "from_iso_utc": _iso_utc_z(lo),
        "to_iso_utc": _iso_utc_z(hi),
        "host_regex": host_regex,
        "host_regex_mode": host_regex_mode,
        "hosts": hosts,
        "hosts_source": hsrc,
        "time_source": tsrc,
        "dashboard_uid": _grafana_dash_uid(),
        "dash_slug": _grafana_dash_slug(),
        "panel_ids": [1, 2],
        "display_timezone": dtz,
    }


def _demo_parser_results():
    """Return synthetic parser results for demo mode (no parser.sh needed)."""
    rack = (request.args.get("rack") or "demo-rack").strip()
    job_id = (request.args.get("job_id") or "0").strip()
    seed = int(hashlib.md5(f"{rack}:{job_id}".encode()).hexdigest()[:8], 16)
    rng = __import__("random").Random(seed)

    sizes = [2**i for i in range(10, 31)]
    nccl_series = [
        {"size_bytes": s, "bus_bw_gbps": round(min(180, 10 + 170 * math.log2(s) / 30) * rng.uniform(0.85, 1.05), 2)}
        for s in sizes
    ]
    nccl_rows = [{
        "job_id": job_id,
        "rack": rack,
        "nccl_test": "all_reduce_perf_mpi",
        "dtype": "float32",
        "peak_busbw_gbps": round(max(p["bus_bw_gbps"] for p in nccl_series), 2),
        "all_reduce_busbw_series": nccl_series,
    }]

    n_iters = rng.randint(80, 200)
    base_step = rng.uniform(1.2, 2.5)
    step_series = [
        {"iteration": i + 1, "step_time": round(base_step * rng.uniform(0.9, 1.15) + (5.0 if i < 3 else 0), 3)}
        for i in range(n_iters)
    ]
    nemotron_rows = [{
        "job_id": job_id,
        "rack": rack,
        "model": "Nemotron-5H-56B",
        "median_step_time": round(base_step, 3),
        "step_time_series": step_series,
    }]

    return jsonify({
        "ok": True,
        "returncode": 0,
        "rack": rack,
        "log_file": f"root-NEMO-1Rack-{rack}-18N-{job_id}.txt",
        "data": {
            "nccl_peak_out_of_place_busbw_gbps": nccl_rows,
            "nemotron_5h_56b_train_window": nemotron_rows,
            "sn_llama_nemotron": [],
            "parser_flags": [{"flag": "demo_mode", "value": "true"}],
        },
        "grafana_embed": None,
    })


@bp.route("/api/parser-results")
@action(get="results:read")
def api_parser_results():
    """Run parser.sh --json on the log for rack (same cwd convention as rack_status log_dir).

    Optional ``log_file``: basename under log_dir (or failed/) for multirack / explicit logs (must
    contain NEMO or NCCL in the basename). When set, ``rack`` is optional (used for Grafana context).

    Optional ``full_rack=1``: when resolving by rack (no ``log_file``/``path``), only consider logs
    whose basename encodes the full-rack node count (``-18N-`` by default; override with
    ``full_rack_nodes`` or ``RACK_FULL_RACK_NODES``). A row ``job_id`` that points at a partial
    (e.g. ``-1N-``) run is ignored so the newest full-rack log is used instead.

    Optional ``display_timezone`` query (IANA, e.g. ``Europe/Berlin``): zone for naive wall-clock
    timestamps in the NEMO log when deriving the LLM/Grafana time window (same as Charts TZ in the UI).

    Response ``data`` may include ``sn_llama_nemotron``: Nemotron-style rows from a second parser pass
    over ``log_dir/sn-llama/*.txt`` matching the rack (when that directory has LLAMA logs). Empty list
    when none; UI hides the LLAMA tab when empty.
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        return _demo_parser_results()
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    log_fn = (request.args.get("log_file") or "").strip()
    path_arg = (request.args.get("path") or "").strip()
    rack = (request.args.get("rack") or "").strip()
    job_raw = (request.args.get("job_id") or "").strip()
    job_id = job_raw if job_raw and job_raw != "-" else None
    full_rack = (request.args.get("full_rack") or "").strip().lower() in ("1", "true", "yes", "on")
    fr_nodes_raw = (request.args.get("full_rack_nodes") or "").strip()
    full_rack_nodes: int | None = None
    if fr_nodes_raw.isdigit():
        full_rack_nodes = int(fr_nodes_raw, 10) if int(fr_nodes_raw, 10) > 0 else None
    # Populated only when resolving by rack; an explicit ``path``/``log_file`` request means the
    # caller named the exact file it wants, so it keeps the single-log behaviour untouched.
    stage_paths: dict[str, Path] = {}
    if path_arg:
        # Explicit allowlisted path (e.g. a Travis bring-up node log
        # results/<node>/allreduce.txt) — bypasses the NEMO/NCCL basename filter but
        # must live under RACK_STATUS_ALLOWED_PREFIXES (validated by the shared resolver).
        try:
            log_path = _resolve_slurm_job_stdout_path(path_arg, request.args.get("log_dir"))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        rk = rack
    elif log_fn:
        log_path = _resolve_nemo_log_basename_in_dir(log_dir, log_fn)
        if log_path is None:
            return jsonify(
                {
                    "ok": False,
                    "error": (
                        f"log file not found or basename must contain NEMO/NCCL: {Path(log_fn).name!r}"
                    ),
                }
            ), 404
        rk = rack
        if not rk:
            rk = multirack_display_rack_from_basename(log_path.name)
    else:
        try:
            rk = _normalize_rack_id(rack)
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        # One log per stage: NCCL and NeMo are separate jobs since llmb-run, so a single file
        # can only carry one stage's evidence. The primary keeps every downstream consumer
        # (grafana window, sn-llama, history ingest, adoption) on exactly one log as before;
        # a distinct second stage is parsed after and merged in below.
        stage_paths = pick_logs_for_rack(
            log_dir, rack, job_id, full_rack=full_rack, full_rack_nodes=full_rack_nodes
        )
        log_path = _primary_stage_log(stage_paths, job_id)
        if log_path is None:
            need_n = full_rack_nodes if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes()
            detail = (
                f"no matching *-{need_n}N-*.txt for rack {rk!r} under {log_dir} "
                f"(full-rack filter on; uncheck Full rack validation to use the latest partial log)"
                if full_rack
                else (
                    f"no matching *.txt for rack {rk!r} under {log_dir} or {log_dir / 'failed'} "
                    f"(NEMO/NCCL basename, or ClusterKit root-CK-/clusterkit with one rack id)"
                )
            )
            return jsonify({"ok": False, "error": detail}), 404
    adopt_rk = (rk or rack or multirack_display_rack_from_basename(log_path.name) or "").strip()
    if adopt_rk:
        try:
            adopt_rk = _normalize_rack_id(adopt_rk)
        except ValueError:
            pass
    _maybe_adopt_validation_log(log_path, log_dir, adopt_rk)
    parser = _parser_path()
    if not _script_exists(parser):
        return jsonify({"ok": False, "error": f"parser.sh not found: {parser}"}), 500
    env = _subprocess_env({"NO_COLOR": "1"})
    # Route parser.sh through host_exec (SSH when remote) and don't local-resolve a remote path.
    lp = str(_rpath(log_path))
    cmd = [
        "bash",
        str(parser),
        *_parser_extra_args(),
        "--json",
        lp,
    ]
    try:
        proc = host_exec.run_on_host(
            cmd,
            cwd=str(log_dir),
            env=env,
            timeout=max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "parser.sh timed out"}), 504
    except Exception as e:
        _logger.debug("api_parser_results: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
    err_tail = (proc.stderr or "").strip()
    raw_out = proc.stdout or ""
    try:
        parsed = json.loads(raw_out) if raw_out.strip() else None
    except json.JSONDecodeError:
        return jsonify(
            {
                "ok": False,
                "error": "parser did not emit valid JSON on stdout",
                "stderr": err_tail,
                "stdout_head": raw_out[:4000],
                "returncode": proc.returncode,
                "log_file": lp,
            }
        ), 502
    if not isinstance(parsed, dict):
        return jsonify({"ok": False, "error": "parser JSON root must be an object"}), 502
    display_cmd = "./parser.sh --json " + shlex.quote(log_path.name)
    sn_nemo: list[object] = []
    # Secondary sn-llama pass. This used to be skipped unless the app ran on the
    # target host, because it shelled out locally against local files; going
    # through the logs role runs it wherever the logs actually are, so the
    # sn-llama series is no longer missing from the charts on a remote deployment.
    root = _rpath(log_dir)
    sn_paths = _sn_llama_txt_paths_for_rack(log_dir, rk, exclude=log_path)
    if sn_paths:
        try:
            rel_sn = [str(PurePosixPath(str(p)).relative_to(root)) for p in sn_paths]
            cmd_sn = ["bash", str(parser), *_parser_extra_args(), "--json", *rel_sn]
            proc_sn = host_exec.run_on_host(
                cmd_sn,
                cwd=str(root),
                env=env,
                timeout=max(60, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
                role="logs",
            )
            if proc_sn.returncode == 0 and (proc_sn.stdout or "").strip():
                sn_parsed = json.loads(proc_sn.stdout)
                if isinstance(sn_parsed, dict):
                    raw_sn = sn_parsed.get("nemotron_5h_56b_train_window")
                    if isinstance(raw_sn, list):
                        sn_nemo = list(raw_sn)
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            _logger.warning("parser sn-llama bundle failed: %s", e)
    merged_data: dict[str, object] = {**parsed, "sn_llama_nemotron": sn_nemo}

    # --- second stage (2026-08-03) ----------------------------------------------------
    # Stage 1 (NCCL) and stage 2 (NeMo) are separate jobs writing separate logs, so the file
    # parsed above holds evidence for only one of them. Parse the other stage's own log and take
    # ONLY that stage's table from it. Rows are self-describing (each carries rack and job_id),
    # so this is a concatenation. Best-effort: a missing second stage just means one empty tab.
    stage_logs: dict[str, str] = {}
    if stage_paths:
        stage_logs = {stage: str(_rpath(p)) for stage, p in stage_paths.items()}
    for stage, spath in _parser_secondary_stages(stage_paths, log_path):
        table = _PARSER_STAGE_TABLES.get(stage)
        if not table:
            continue
        slp = str(_rpath(spath))
        try:
            proc_s = host_exec.run_on_host(
                ["bash", str(parser), *_parser_extra_args(), "--json", slp],
                cwd=str(log_dir),
                env=env,
                timeout=max(30, _job_env_int("RACK_PARSER_TIMEOUT", 300)),
            )
            if proc_s.returncode != 0 or not (proc_s.stdout or "").strip():
                _logger.warning("parser %s stage returned nothing for %s", stage, slp)
                continue
            parsed_s = json.loads(proc_s.stdout)
            if not isinstance(parsed_s, dict):
                continue
            rows_s = parsed_s.get(table)
            if isinstance(rows_s, list) and rows_s:
                # Replace, not append: parser.sh emits an all-"---" placeholder row for an
                # explicitly-named file whatever its content, so the primary log contributes a
                # junk row to the table it is not the authority for. This stage owns the table.
                merged_data[table] = list(rows_s)
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired, ValueError) as e:
            _logger.warning("parser %s stage failed for %s: %s", stage, slp, e)

    # The replace-the-table step above only fires when the other stage HAS its own log. A rack that
    # never ran a stage (NCCL-only racks such as j15/j16 on 2026-08-07) keeps the placeholder row
    # the primary parse produced for the table it is not the authority for. Drop it by content, so
    # "stage never ran" reads as an empty table rather than a UNK row wearing the other stage's
    # job id. Content-based on purpose: a combined-format log legitimately yields both tables from
    # one parse and must not be stripped.
    dropped_placeholders = _drop_parser_placeholder_rows(merged_data)
    if dropped_placeholders:
        _logger.info("parser placeholder rows dropped for %s: %s", rk or "?", dropped_placeholders)

    log_tz_q = _sanitize_display_timezone_query(request.args.get("display_timezone"))
    # Grafana time-window context is optional; never let it 500 the parser charts (it may touch
    # log files, which over SSH can fail). Degrade to no embed on error.
    try:
        g_embed = _grafana_parser_embed_context(
            log_path, rk, job_raw, parsed, log_timestamp_tz=log_tz_q
        )
    except Exception as e:
        _logger.warning("grafana embed context failed: %s", e, exc_info=True)
        g_embed = None
    pdisp = _grafana_url_timezone_param()
    if isinstance(g_embed, dict) and g_embed.get("display_timezone"):
        pdisp = str(g_embed["display_timezone"])

    # --- universal ingest + durable history (2026-07-28) -----------------------------
    # Additive only: the three parser.sh tables in ``data`` are untouched, so the NCCL /
    # Nemotron / MNUBERGEMM charts keep rendering exactly what they rendered before. When
    # parser.sh produced nothing usable (a foreign tool, or the stale head-node v6 that
    # returns rack='?' and '---' for a real MNUBERGEMM log) a ``data.universal`` key is
    # added with the normalised result so the log still surfaces. Never fails the request.
    universal_res = None
    try:
        if not _parser_json_has_usable_rows(parsed):
            universal_res = _universal_fallback_for_log(log_dir, lp, rack_hint=rk or None)
            if universal_res is not None:
                merged_data = {**merged_data, "universal": universal_res}
    except Exception as e:  # noqa: BLE001 - never break the charts for this
        _logger.warning("universal fallback failed for %s: %s", lp, e)
    try:
        if universal_res is None:
            _history_ingest_parser_json(parsed, log_dir, lp, rack_hint=rk or None,
                                        source="api/parser-results")
    except Exception as e:  # noqa: BLE001 - history is best-effort
        _logger.warning("results_history ingest failed for %s: %s", lp, e)

    return jsonify(
        {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "rack": rk,
            "log_file": lp,
            # Which file fed each tab. Absent keys mean that stage has no log for this rack yet.
            "stage_logs": stage_logs,
            "job_id": _jobid_from_nemo_log_basename(Path(lp).name) or job_raw or "",
            "full_rack": bool(full_rack) if not (path_arg or log_fn) else False,
            "full_rack_nodes": (
                (full_rack_nodes if full_rack_nodes and full_rack_nodes > 0 else _full_rack_expected_nodes())
                if full_rack and not (path_arg or log_fn)
                else None
            ),
            "command_display": display_cmd,
            "data": merged_data,
            # {table: [rack…]} for parser.sh placeholder rows removed above. Non-empty means that
            # stage produced no run for this rack, as opposed to the tab having failed to load.
            "dropped_placeholder_rows": dropped_placeholders,
            "stderr": err_tail,
            "grafana_uri": _rack_web_grafana_uri(),
            "grafana_port": _rack_web_grafana_port(),
            "grafana_embed": g_embed,
            "parser_display_timezone": pdisp,
        }
    )


@bp.route("/api/nvloom-heatmaps-log")
@action(get="results:read")
def api_nvloom_heatmaps_log():
    """Return the newest Slurm stdout for nvloom singlerack (``nvloom/*NVLOOM-<rack>-*.txt``) for heatmap UI.

    Query: ``log_dir``, ``rack`` (same normalization as the rest of rack web).
    Response JSON: ``ok``, ``path`` (resolved file or empty), ``content`` (UTF-8 text, size-capped).
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        demo_txt = (
            "Process  0 (p1-r01-ct01): device 0: NVIDIA GB300 (00000008:06:00); rackGuid: 111\n"
            "Process  1 (p1-r01-ct01): device 1: NVIDIA GB300 (00000009:06:00); rackGuid: 222\n"
            "Running demo_pair\n"
            "A | B\n"
            "0 100 200\n"
            "1 150 220\n"
            "Min demo_pair 90 GB/s\n"
        )
        return jsonify({"ok": True, "path": "(demo)", "content": demo_txt})
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_raw = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    cands = find_nvloom_log_paths_for_rack(log_dir, rk)
    if not cands:
        return jsonify(
            {
                "ok": False,
                "error": f"no nvloom log *NVLOOM-{rk}-*.txt under {log_dir / 'nvloom'}",
            }
        ), 404
    log_path = cands[0]
    st = host_exec.stat(log_path)
    if st is None:
        return jsonify({"ok": False, "error": f"NVLOOM log not readable: {log_path}"}), 404
    sz = st.size
    if sz > _NVLOOM_HEATMAPS_LOG_MAX_BYTES:
        return jsonify(
            {
                "ok": False,
                "error": f"NVLOOM log too large ({sz} bytes; max {_NVLOOM_HEATMAPS_LOG_MAX_BYTES})",
            }
        ), 413
    text, rmeta = host_exec.read_text(
        log_path,
        head_bytes=_NVLOOM_HEATMAPS_LOG_MAX_BYTES,
        tail_bytes=0,
        whole_bytes=_NVLOOM_HEATMAPS_LOG_MAX_BYTES,
    )
    if rmeta.get("read_error"):
        return jsonify({"ok": False, "error": str(rmeta["read_error"])}), 500
    return jsonify({"ok": True, "path": str(_rpath(log_path)), "content": text})


_CLUSTERKIT_JSON_MAX_BYTES = 80 * 1024 * 1024


def _clusterkit_rack_dir(log_dir: Path, rk: str) -> Path:
    return log_dir / "clusterkit" / rk


def _clusterkit_get_cell(links: list[object], i: int, j: int) -> float | None:
    """Upper-triangular storage: same convention as clusterkit ``bwResultAnalyzer.getCell``."""
    if i == j:
        return 0.0
    try:
        if i < j:
            row = links[i]
            if not isinstance(row, list) or j - i >= len(row):
                return None
            return float(row[j - i])
        row = links[j]
        if not isinstance(row, list) or i - j >= len(row):
            return None
        return float(row[i - j])
    except (IndexError, TypeError, ValueError):
        return None


def _clusterkit_parse_bandwidth_payload(
    data: dict[str, object],
) -> tuple[list[str], list[list[float | None]], dict[str, object]] | None:
    """Return ``(labels, matrix, meta)`` from ClusterKit ``bandwidth.json``."""
    nodes = data.get("Nodes")
    links = data.get("Links")
    if not isinstance(nodes, dict) or not isinstance(links, list):
        return None
    try:
        pairs = sorted(((int(v), str(k)) for k, v in nodes.items()), key=lambda x: x[0])
    except (TypeError, ValueError):
        return None
    labels = [p[1] for p in pairs]
    n = len(labels)
    if n < 2 or len(links) < n - 1:
        return None
    for i in range(n - 1):
        row = links[i]
        if not isinstance(row, list) or len(row) != n - i:
            return None
    matrix: list[list[float | None]] = []
    for i in range(n):
        row_out: list[float | None] = []
        for j in range(n):
            if i == j:
                row_out.append(0.0)
            else:
                row_out.append(_clusterkit_get_cell(links, i, j))
        matrix.append(row_out)
    meta: dict[str, object] = {
        "cluster": data.get("Cluster"),
        "user": data.get("User"),
        "testname": data.get("Testname"),
        "date_and_time": data.get("Date_and_Time"),
        "jobid": data.get("JOBID"),
        "ppn": data.get("PPN"),
        "bidirectional": data.get("Bidirectional"),
        "hca_tag": data.get("HCA_Tag"),
        "technology": data.get("Technology"),
        "units": data.get("Units"),
    }
    return labels, matrix, meta


def _clusterkit_pairwise_positive_summary(data: dict[str, object]) -> dict[str, object] | None:
    """Min / max / avg over off-diagonal positive finite cells (bandwidth or latency JSON)."""
    parsed = _clusterkit_parse_bandwidth_payload(data)
    if parsed is None:
        return None
    _labels, matrix, _meta = parsed
    vals: list[float] = []
    for i, row in enumerate(matrix):
        for j, v in enumerate(row):
            if i == j:
                continue
            if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0:
                vals.append(float(v))
    n = len(matrix)
    if not vals:
        return {"node_count": n, "link_count": 0}
    return {
        "node_count": n,
        "link_count": len(vals),
        "min": min(vals),
        "max": max(vals),
        "avg": sum(vals) / len(vals),
    }


def _clusterkit_bandwidth_summary_from_data(data: dict[str, object]) -> dict[str, object] | None:
    return _clusterkit_pairwise_positive_summary(data)


@bp.route("/api/clusterkit-results")
@action(get="results:read")
def api_clusterkit_results():
    """List ClusterKit ``bandwidth.json`` runs under ``log_dir/clusterkit/<rack>/``; optional full matrix for one run.

    Query: ``log_dir``, ``rack``; optional ``run`` = subdirectory name (basename only) with ``bandwidth.json``.
    ``runs`` are sorted newest first by ``bandwidth.json`` mtime. When ``run`` matches, ``detail`` includes
    ``labels``, ``matrix``, ``meta`` (bandwidth). If ``latency.json`` exists beside it, ``detail["latency"]`` has
    the same shape for pairwise latency (see ``bwResultAnalyzer``).
    """
    if _truthy_env("RACK_STATUS_DEMO"):
        run_q = (request.args.get("run") or "").strip()
        demo_runs: list[dict[str, object]] = [
            {
                "dir": "demo_mlx5_0",
                "rel_path": "clusterkit/p1-r01/demo_mlx5_0",
                "mtime": 0,
                "meta": {"hca_tag": "mlx5_0", "units": "MB/s", "date_and_time": "demo"},
                "summary": {"node_count": 3, "link_count": 6, "min": 100.0, "max": 200.0, "avg": 150.0},
                "has_latency": True,
            }
        ]
        out_demo: dict[str, object] = {"ok": True, "rack": "p1-r01", "runs": demo_runs}
        if run_q == "demo_mlx5_0":
            out_demo["detail"] = {
                "dir": "demo_mlx5_0",
                "labels": ["node-a", "node-b", "node-c"],
                "matrix": [[0.0, 100.0, 150.0], [100.0, 0.0, 200.0], [150.0, 200.0, 0.0]],
                "meta": {
                    "cluster": "demo-cluster",
                    "user": "demo",
                    "testname": "ib_write_bw",
                    "date_and_time": "demo",
                    "jobid": "999",
                    "ppn": 1,
                    "bidirectional": "True",
                    "hca_tag": "mlx5_0",
                    "technology": "IB",
                    "units": "MB/s",
                },
                "summary": {"node_count": 3, "link_count": 6, "min": 100.0, "max": 200.0, "avg": 150.0},
                "latency": {
                    "labels": ["node-a", "node-b", "node-c"],
                    "matrix": [[0.0, 1.25, 1.48], [1.25, 0.0, 2.05], [1.48, 2.05, 0.0]],
                    "meta": {
                        "cluster": "demo-cluster",
                        "testname": "ib_write_lat",
                        "date_and_time": "demo",
                        "jobid": "999",
                        "hca_tag": "mlx5_0",
                        "technology": "IB",
                        "units": "usec",
                    },
                    "summary": {"node_count": 3, "link_count": 6, "min": 1.25, "max": 2.05, "avg": 1.593},
                },
            }
        return jsonify(out_demo)
    try:
        log_dir = _resolve_log_dir(request.args.get("log_dir"))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    rack_raw = (request.args.get("rack") or "").strip()
    try:
        rk = _normalize_rack_id(rack_raw)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    root = _rpath(log_dir)
    base = _rpath(_clusterkit_rack_dir(root, rk))
    # One listing covers every run directory under this rack. These files live on
    # the execution host, so the previous iterdir()/stat()/read_text() walk was
    # both a local-filesystem call on a remote path and a round trip per run.
    found = host_exec.stat_dir(
        base, patterns=("bandwidth.json", "latency.json"), maxdepth=2
    )
    if not found:
        return jsonify({"ok": True, "rack": rk, "runs": []})

    def _rel_to_log_dir(p: PurePosixPath) -> str:
        try:
            return str(p.relative_to(root))
        except ValueError:
            return str(p)

    with_latency = {
        PurePosixPath(s).parent for s in found if PurePosixPath(s).name == "latency.json"
    }
    runs_out: list[dict[str, object]] = []
    for path_s, st in found.items():
        bw = PurePosixPath(path_s)
        # Only <rack>/<run>/bandwidth.json; skip a stray file directly under the rack dir.
        if bw.name != "bandwidth.json" or bw.parent == base:
            continue
        entry: dict[str, object] = {
            "dir": bw.parent.name,
            "rel_path": _rel_to_log_dir(bw),
            "mtime": int(st.mtime),
        }
        if st.size > _CLUSTERKIT_JSON_MAX_BYTES:
            entry["error"] = f"bandwidth.json too large ({st.size} bytes)"
            runs_out.append(entry)
            continue
        raw, read_meta = host_exec.read_text(bw, whole_bytes=_CLUSTERKIT_JSON_MAX_BYTES)
        if read_meta.get("read_error"):
            entry["error"] = str(read_meta["read_error"])
            runs_out.append(entry)
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            entry["error"] = str(e)
            runs_out.append(entry)
            continue
        if not isinstance(data, dict):
            continue
        entry["meta"] = {
            "hca_tag": data.get("HCA_Tag"),
            "units": data.get("Units"),
            "date_and_time": data.get("Date_and_Time"),
            "jobid": data.get("JOBID"),
            "bidirectional": data.get("Bidirectional"),
            "cluster": data.get("Cluster"),
        }
        entry["summary"] = _clusterkit_bandwidth_summary_from_data(data) or {}
        entry["has_latency"] = bw.parent in with_latency
        runs_out.append(entry)
    runs_out.sort(key=lambda r: int(r.get("mtime") or 0), reverse=True)
    out: dict[str, object] = {"ok": True, "rack": rk, "runs": runs_out}
    run_q = (request.args.get("run") or "").strip()
    if run_q and run_q == PurePosixPath(run_q).name and re.fullmatch(r"[A-Za-z0-9_.\-]+", run_q):
        # Containment comes from the listing: `found` only holds paths under base,
        # so a traversal attempt simply misses instead of needing a resolve() check.
        sub = base / run_q

        def _load_json(p: PurePosixPath) -> dict | None:
            st = found.get(str(p))
            if st is None or st.size > _CLUSTERKIT_JSON_MAX_BYTES:
                return None
            text, meta = host_exec.read_text(p, whole_bytes=_CLUSTERKIT_JSON_MAX_BYTES)
            if meta.get("read_error"):
                return None
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return None
            return loaded if isinstance(loaded, dict) else None

        data = _load_json(sub / "bandwidth.json")
        parsed = _clusterkit_parse_bandwidth_payload(data) if data is not None else None
        if parsed:
            labels, matrix, bw_meta = parsed
            detail: dict[str, object] = {
                "dir": run_q,
                "labels": labels,
                "matrix": matrix,
                "meta": bw_meta,
                "summary": _clusterkit_bandwidth_summary_from_data(data) or {},
            }
            lat_data = _load_json(sub / "latency.json")
            lat_parsed = (
                _clusterkit_parse_bandwidth_payload(lat_data) if lat_data is not None else None
            )
            if lat_parsed:
                ll, lm, lmeta = lat_parsed
                detail["latency"] = {
                    "labels": ll,
                    "matrix": lm,
                    "meta": lmeta,
                    "summary": _clusterkit_pairwise_positive_summary(lat_data) or {},
                }
            out["detail"] = detail
    return jsonify(out)


def _sn_llama_txt_paths_for_rack(log_dir: Path, rack: str, *, exclude: Path | None = None) -> list[Path]:
    """``log_dir/sn-llama/*.txt`` whose names suggest LLAMA and match *rack* (case-insensitive substring on rack id tokens)."""
    d = _rpath(log_dir) / "sn-llama"
    needles: list[str] = list(_nvis_naming().find_rack_ids_in_string(rack or ""))
    if not needles:
        t = (rack or "").strip().lower()
        if t:
            needles = [t]
    ex = str(_rpath(exclude)) if exclude is not None else None
    out: list[Path] = []
    # One listing on the execution host; mtime arrives with it, so the newest-first
    # ordering below needs no per-file stat.
    entries = host_exec.stat_dir(d, patterns=("*.txt",))
    paths = [
        PurePosixPath(s)
        for s in sorted(entries, key=lambda s: entries[s].mtime, reverse=True)
    ]
    cap = max(1, _env_int("RACK_PARSER_SN_LLAMA_MAX_FILES", 25))
    for p in paths:
        if len(out) >= cap:
            break
        if ex is not None and str(p) == ex:
            continue
        nl = p.name.casefold()
        if "llama" not in nl:
            continue
        # Needles from ``find_rack_ids_in_string`` keep canonical casing (e.g. CBR…); basename is matched case-insensitively.
        if needles and not any(n.casefold() in nl for n in needles):
            continue
        out.append(p)
    return list(reversed(out))


@bp.route("/api/history/capture-job", methods=["GET", "POST"])
@action("results:capture")
@action(get="results:read")
def api_capture_job_attempt():
    """``?job_id=`` — record a Slurm attempt (even one that produced no log) to history + lake."""
    jid = (request.args.get("job_id") or (request.get_json(silent=True) or {}).get("job_id") or "").strip()
    if not jid or not core.SLURM_JOB_ID_RE.fullmatch(jid):
        return jsonify({"ok": False, "error": "valid job_id required"}), 400
    return jsonify(capture_job_attempt(jid))


def capture_job_attempt(job_id: str) -> dict:
    """Record one Slurm job attempt (state, reason, exit code, nodes) to history + the lake.

    Works whether or not the job produced output — that is the entire point. Job 63850
    (MNUBERGEMM, h01) was CANCELLED in the Slurm prolog: RunTime 00:00:00, no stdout file ever
    created. It therefore left NO trace in the log dir, in results_history, or in the lake — the
    attempt was invisible even though it consumed an 18-node allocation and drained a tray.
    Capturing only successful runs is the same blank-read-as-success failure this platform
    exists to catch.
    """
    res: dict[str, object] = {"ok": False, "job_id": job_id, "error": None}
    try:
        raw = _run_scontrol_show_job(job_id)
        text = raw.get("stdout") if isinstance(raw, dict) else str(raw)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"scontrol failed: {e}"
        return res

    f = _parse_scontrol_job(text or "")
    if not f.get("JobId"):
        # scontrol only remembers a job for MinJobAge after it ends. A 40-minute soak checked an
        # hour later is simply GONE — which silently missed three completed MNUBERGEMM runs.
        # sacct reads the accounting DB and retains them, so fall back to it.
        try:
            sa = host_exec.run_slurm_on_host(
                _get_platform().sacct_cmd(
                    f"-j {shlex.quote(job_id)} -X -P -n "
                    "-o JobID,JobName,State,Elapsed,ExitCode,Start,End,NodeList,User"
                )
            )
            line = (sa.stdout or "").strip().splitlines()
            if line:
                c = line[0].split("|")
                if len(c) >= 8:
                    f = {
                        "JobId": c[0], "JobName": c[1], "JobState": c[2].split()[0],
                        "RunTime": c[3], "ExitCode": c[4], "StartTime": c[5],
                        "EndTime": c[6], "NodeList": c[7],
                        "Reason": "(from sacct — not in scontrol)",
                    }
                    res["source"] = "sacct"
        except Exception as e:  # noqa: BLE001
            res["error"] = f"sacct fallback failed: {e}"
            return res
    if not f.get("JobId"):
        res["error"] = "job not found in scontrol or sacct"
        return res

    state = f.get("JobState", "UNKNOWN")
    reason = f.get("Reason", "")
    nodelist = f.get("NodeList", "")
    rack, nodes = _rack_and_nodes_from_nodelist(nodelist)
    # A job cancelled by the prolog reports ExitCode 0:<sig> and RunTime 00:00:00 — treat it as a
    # FAILED attempt, never as a pass, even though Slurm's own ExitCode field starts with 0.
    rt = f.get("RunTime", "00:00:00")
    ran = rt not in ("00:00:00", "")
    # A soak workload (MNUBERGEMM) is EXPECTED to hit the walltime — the soak deliberately outruns
    # it. TIMEOUT after substantial runtime is a PASS on MOP 7.2 criteria; only a TIMEOUT with no
    # runtime is a real failure. Mislabelling it cost h03's successful 75.7 kW run a "fail" record.
    status = {"COMPLETED": "pass", "RUNNING": "running", "PENDING": "pending"}.get(state)
    if status is None:
        if state == "TIMEOUT":
            status = "timeout" if ran else "fail"
        elif not ran or state in ("CANCELLED", "FAILED", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"):
            status = "fail"
        else:
            status = "unknown"

    record = {
        "kind": "job_attempt",
        "status": status,
        "job_id": job_id,
        "rack": rack,
        "nodes": nodes,
        "scontrol": f,
        "state": state,
        "reason": reason,
        "exit_code": f.get("ExitCode", ""),
        "run_time": f.get("RunTime", ""),
        "produced_log": False,
        "tail": text[:4000],
    }
    res["state"], res["reason"], res["status"] = state, reason, status

    try:
        rid = results_history.ingest_run({
            "run_id": f"attempt-{job_id}",
            # sacct does NOT return StdOut, so a job recovered from the accounting DB has no
            # log path. results_history requires one, and without a fallback every sacct-sourced
            # run landed in Parquet but silently failed the SQLite ingest ("source_log_path is
            # required") — i.e. half-captured, which is worse than not captured because the lake
            # looks complete. Synthesise a stable identifier instead.
            "source_log_path": f.get("StdOut") or f"sacct:{job_id}",
            "log_basename": (f.get("StdOut", "") or "").rsplit("/", 1)[-1] or f"job-{job_id}",
            "workload_kind": "job_attempt",
            "rack": rack,
            "nodes": record["nodes"],
            "job_id": job_id,
            "started_at": f.get("StartTime", ""),
            "ended_at": f.get("EndTime", ""),
            "status": status,
            "metrics": [],
            "extra": record,
        })
        res["history_run_id"] = rid
    except Exception as e:  # noqa: BLE001
        res["error"] = f"history: {e}"

    if lake_sink is not None and not lake_sink.is_disabled():
        out = lake_sink.push_run(
            run_id=lake_sink.make_run_id("jobattempt", rack, job_id),
            workload="job_attempt", rack=rack, job_id=job_id,
            nodes=record["nodes"], status=status, parsed=record,
            log_path=f.get("StdOut", ""), log_text=text or "",
        )
        res["lake"] = out
        if not out.get("ok"):
            _logger.warning("lake job-attempt push failed for %s: %s", job_id, out.get("error"))

    res["ok"] = True
    return res


def _rack_and_nodes_from_nodelist(nodelist: str) -> tuple[str, list[str]]:
    """Attribute a Slurm ``NodeList`` to ``(rack id, node hostnames)``.

    Two things make this less trivial than it looks:
      * Slurm reports a *compact* list (``<rack>-[01-18]``), so it has to be expanded before
        the hostnames mean anything, and ``(null)`` / ``None assigned`` for a job that never
        got an allocation.
      * Rack attribution goes through the naming engine (``NVIS_RACK_ID`` / ``NVIS_CT_ID``),
        so every site's convention works. This used to hardcode one cluster's ``ink50``
        prefix, which silently produced an empty rack and no nodes everywhere else.
    """
    spec = (nodelist or "").strip()
    if not spec or spec.lower() in ("(null)", "none assigned"):
        return "", []
    if _uparse is not None:
        hosts = _uparse.expand_nodelist(spec)
    else:  # no normaliser: bracket ranges stay unexpanded, but do not lose the plain names
        hosts = [h for h in re.split(r"[,\s]+", spec) if h]
    naming = _nvis_naming()
    # Prefer recognised trays, but never drop everything: an unrecognised convention should
    # still surface its nodes rather than report the job as having run nowhere.
    nodes = [h for h in hosts if naming.is_tray_hostname(h)] or hosts
    for h in nodes:
        hit = naming.sinfo_rack_from_host(h)
        if hit:
            return (naming.canonical_rack_id(hit[0]) or hit[0]), nodes
    return "", nodes
