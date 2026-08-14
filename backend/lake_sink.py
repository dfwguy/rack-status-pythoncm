# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Push every Rack Status run into the Parquet data lake.

Why this exists
---------------
The lake's stated goal is provenance: a validation number must stay traceable to EXACTLY what
produced it. Before this module the lake had real *metrics* but its run tables held only
``FIXTURE-*`` rows — the MNUBERGEMM run on h22 (job 62616, 67 kW) never landed at all. Results
lived in SQLite (`results_history`) and the log files lived on the head node, so six months from
now a busbw or power number would have had no recoverable inputs.

This module is the sink: whenever a log is parsed, the run's normalised result AND the files that
defined it are pushed to `nvis-lakewriter`.

Two destinations, deliberately different:
  * ``POST /v1/artifacts`` (:9410) — verbatim run INPUTS (sbatch, .conf, recipe YAML, env
    snapshot). Synchronous and atomic upstream: a 200 means bytes are on disk. Errors are NOT
    swallowed there, and are not swallowed here either — a silent provenance failure is the exact
    "blank read reported as success" bug this platform exists to catch.
  * ``POST /api/v1/write`` — not used. Run RESULTS go through the artifacts table as a
    ``synthesized:`` artifact plus the parsed metrics, because the results schema is owned by the
    lake's own ingest modules and we must not fork it.

Best-effort by design: the lake must never break a validation run. Every entry point catches and
returns a status dict instead of raising — but it reports failure honestly rather than pretending.

Config
    RACK_LAKE_URL       default http://host.docker.internal:9410
    RACK_LAKE_CLUSTER   default: active catalog cluster/customer, else "unknown-cluster"
    RACK_LAKE_DISABLED  1 -> no-op (still returns an honest status)
    RACK_LAKE_TIMEOUT   default 20
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

_logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://host.docker.internal:9410"
# Last resort when neither the lake override nor the active catalog names the cluster. Must
# satisfy ``_RUN_ID_RE`` since upstream validates the cluster tag too.
_UNKNOWN_CLUSTER = "unknown-cluster"
# Upstream enforces this on run_id AND cluster; reject locally so the failure names the real cause
# instead of surfacing as an opaque upstream 400.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
# Anything larger is pushed as a reference rather than inlined; the writer also caps this itself.
_INLINE_MAX = 1 << 20


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def lake_url() -> str:
    return _env("RACK_LAKE_URL", _DEFAULT_URL).rstrip("/")


def cluster() -> str:
    """Cluster tag every lake record is filed under.

    ``RACK_LAKE_CLUSTER`` wins; otherwise fall back to the active catalog cluster/customer,
    which ``catalog.context`` materialises into the environment. This used to default to a
    single site's name, so any other deployment silently filed its runs under that site —
    the exact provenance corruption this module exists to prevent. An unidentified cluster
    is labelled as such rather than mislabelled.
    """
    return (
        _env("RACK_LAKE_CLUSTER")
        or _env("RACK_CLUSTER_NAME")
        or _env("RACK_CLUSTER_CUSTOMER_NAME")
        or _UNKNOWN_CLUSTER
    )


def is_disabled() -> bool:
    return _env("RACK_LAKE_DISABLED").lower() in ("1", "true", "yes", "on")


def _timeout() -> int:
    try:
        return max(5, int(_env("RACK_LAKE_TIMEOUT", "20")))
    except ValueError:
        return 20


def make_run_id(workload: str, rack: str, job_id: str | int | None, ts: float | None = None) -> str:
    """Stable, filesystem-safe run id. ``run_id`` is a DIRECTORY in the lake, so it must be a
    path-safe token and it must be identical across re-ingests of the same run."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts or time.time()))
    parts = [cluster(), "rackstatus", workload or "unknown", rack or "cluster", str(job_id or stamp)]
    rid = "-".join(re.sub(r"[^A-Za-z0-9._-]+", "_", str(p)) for p in parts if p)
    return rid[:200]


def _post(path: str, payload: dict[str, Any]) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{lake_url()}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 400 = our payload is wrong, 429 = lake backpressure (disk). Both are real and must be
        # reported, never smoothed into "ok".
        return exc.code, exc.read().decode("utf-8", "replace")


def _artifact(path: str, raw: bytes, artifact_type: str = "", mtime: float | None = None) -> dict:
    """Encode one file for the wire. utf8 when it round-trips, base64 otherwise — the writer
    verifies sha256 against the RAW bytes either way, so the digest is computed pre-encoding."""
    sha = hashlib.sha256(raw).hexdigest()
    try:
        content, encoding = raw.decode("utf-8"), "utf8"
    except UnicodeDecodeError:
        content, encoding = base64.b64encode(raw).decode(), "base64"
    out = {
        "artifact_path": path,
        "encoding": encoding,
        "content": content,
        "sha256": sha,
        "size_bytes": len(raw),
    }
    if artifact_type:
        out["artifact_type"] = artifact_type
    if mtime:
        out["mtime"] = mtime
    return out


def push_run(
    *,
    run_id: str,
    workload: str,
    rack: str = "",
    job_id: str | int | None = None,
    nodes: list[str] | None = None,
    status: str = "unknown",
    parsed: dict[str, Any] | None = None,
    log_path: str = "",
    log_text: str = "",
    inputs: list[dict[str, Any]] | None = None,
    source_host: str = "",
) -> dict[str, Any]:
    """Push one run's result + inputs to the lake. Never raises.

    ``parsed`` is a universal_parser record. ``inputs`` is an optional list of
    ``{"path": str, "bytes": bytes, "type": str}`` describing the sbatch/conf/YAML that defined
    the run — this is what makes the result reproducible later.
    """
    out: dict[str, Any] = {"ok": False, "run_id": run_id, "lake": lake_url(), "error": None}
    if is_disabled():
        out["error"] = "disabled"
        return out
    if not _RUN_ID_RE.match(run_id or ""):
        out["error"] = f"invalid run_id {run_id!r}"
        return out

    artifacts: list[dict[str, Any]] = []

    # 1. The parsed result itself. Synthesized — it never existed as a file — so the reader can
    #    tell a derived record from a captured one.
    if parsed is not None:
        artifacts.append(_artifact(
            f"synthesized:result-{workload or 'run'}.json",
            json.dumps(parsed, default=str, sort_keys=True, indent=1).encode(),
            "parsed_result"))

    # 2. The raw log, verbatim. The parser will change over time; the bytes it read must not.
    if log_text:
        artifacts.append(_artifact(
            log_path or f"synthesized:{workload or 'run'}.log",
            log_text.encode("utf-8", "replace"),
            "run_log"))

    # 3. The run's defining inputs (sbatch, conf, recipe yaml, env snapshot).
    for item in inputs or []:
        raw = item.get("bytes")
        if not raw:
            continue
        artifacts.append(_artifact(
            str(item.get("path") or "synthesized:input"),
            raw if isinstance(raw, bytes) else str(raw).encode(),
            str(item.get("type") or "")))

    if not artifacts:
        out["error"] = "nothing to push"
        return out

    payload = {
        "run_id": run_id,
        "cluster": cluster(),
        "job_id": str(job_id) if job_id is not None else None,
        "harness": "rackstatus",
        "capture_kind": "run",
        "captured_by": "nvis-rack-status",
        "source_host": source_host or _env("RACK_SSH_HOST", ""),
        "fixture": False,
        "captured_at": time.time(),
        "resolve_context": {
            "workload": workload,
            "rack": rack,
            "status": status,
            "nodes": ",".join(nodes or [])[:4000],
        },
        "artifacts": artifacts,
    }

    try:
        code, body = _post("/v1/artifacts", payload)
    except Exception as exc:  # network/DNS/timeout
        _logger.debug("push_run: ignoring error", exc_info=True)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["http_status"] = code
    if code == 200:
        try:
            resp = json.loads(body)
            out.update(ok=True, rows=resp.get("rows"), bytes=resp.get("bytes"),
                       skipped_duplicate=resp.get("skipped_duplicate"))
        except json.JSONDecodeError:
            out.update(ok=True, raw=body[:200])
    else:
        # 400 = bad payload (ours), 429 = lake backpressure (disk full). Surface both verbatim.
        out["error"] = f"HTTP {code}: {body[:300]}"
    return out


def health() -> dict[str, Any]:
    """Is the lake reachable and accepting writes?"""
    out: dict[str, Any] = {"ok": False, "lake": lake_url(), "disabled": is_disabled(), "error": None}
    if is_disabled():
        out["error"] = "disabled"
        return out
    try:
        req = urllib.request.Request(f"{lake_url()}/healthz")
        with urllib.request.urlopen(req, timeout=8) as resp:
            out["ok"] = resp.status == 200
            out["http_status"] = resp.status
    except Exception as exc:
        _logger.debug("health: ignoring error", exc_info=True)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
