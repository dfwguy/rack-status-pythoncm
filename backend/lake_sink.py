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

What may leave the site, and how
-------------------------------
This module is an **egress** path: it takes data out of a customer's cluster. A PLC design
validation on 2026-08-24 found it was doing more of that than anyone had written down, so three
controls are now explicit rather than incidental.

* **Credential-bearing endpoints are never captured.** ``NEVER_CAPTURE`` is checked before anything
  is read. ``POST /api/login`` carries ``{"password": ...}`` and ``PUT /api/vault/nvftp-token``
  carries a token; both used to be captured verbatim into ``request_body``. Following the audit
  trail's precedent for query strings, the fix is to *not take* the data rather than to filter it —
  a credential is invisible to key-name redaction as often as not.
* **Redaction is applied anyway**, as defence in depth, by key name *and* by value using
  ``logging_config``'s existing secret-value machinery. One control that has to be remembered per
  endpoint is a control that will be forgotten.
* **TLS is supported and verified.** ``RACK_LAKE_CA_BUNDLE`` lets an ``https://`` lake present a
  self-signed or internal-CA certificate. Verification is never disabled: there is deliberately no
  skip-verify switch, because the failure it hides is the one that matters.

Request capture is **opt-in** (``RACK_LAKE_CAPTURE`` defaults to ``off``). It was on by default,
which meant a site that had never heard of the lake still tried to ship to it; that only looked
harmless because usually nothing listens on the default port.

Config
    RACK_LAKE_URL         default http://host.docker.internal:9410
    RACK_LAKE_CLUSTER     default: active catalog cluster/customer, else "unknown-cluster"
    RACK_LAKE_DISABLED    1 -> no-op (still returns an honest status)
    RACK_LAKE_TIMEOUT     default 20
    RACK_LAKE_CA_BUNDLE   PEM bundle used to verify an https:// lake (default: system trust)
    RACK_LAKE_QUEUE_MAX   bounded async queue depth for submit_run (default 256)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://host.docker.internal:9410"

#: Paths whose bodies must never reach the lake, matched as prefixes. These carry secrets that no
#: amount of redaction can be trusted to find: the login password arrives under a key this module
#: would have to guess, and the vault token is the entire point of its request. Enforced at the
#: capture predicate AND again in ``scrub_record`` so a second caller cannot reintroduce it.
NEVER_CAPTURE: tuple[str, ...] = (
    "/api/login",
    "/api/logout",
    "/api/vault/",
    "/api/auth",
)

#: Mapping keys whose values are replaced wholesale. Broader than ``logging_config``'s env-name
#: pattern on purpose: that one deliberately omits a bare ``KEY`` because env vars named ``*_KEY``
#: hold file paths, but a JSON body key called ``key`` is far more likely to hold the real thing.
SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass|passwd|password|secret|token|credential|api[-_]?key|^key$|auth|bearer|cookie|session)",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"
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


def never_capture(path: str) -> bool:
    """Is this path forbidden from being captured at all? (prefix match on ``NEVER_CAPTURE``)"""
    p = (path or "").rstrip("/") or "/"
    return any(p == pre.rstrip("/") or p.startswith(pre) for pre in NEVER_CAPTURE)


def _known_secret_values() -> tuple[str, ...]:
    """Secret values sourced from the environment, longest first.

    Reuses ``logging_config`` rather than growing a second list: that module already resolves which
    env vars look sensitive, already refuses to redact values under 8 characters (a 2-character
    secret matches inside ordinary words), and already sorts longest-first so a secret containing
    another as a substring does not leave a recognisable tail. A parallel implementation here would
    drift from it, and the drift would be silent.
    """
    try:
        import logging_config as lc

        return lc.sensitive_values(tuple(lc.LOG_SENSITIVE_KEYS.resolve()))
    except Exception:
        _logger.debug("_known_secret_values: falling back to key-name redaction only", exc_info=True)
        return ()


def _redact_text(text: str, secrets: tuple[str, ...]) -> str:
    for value in secrets:
        if value and value in text:
            text = text.replace(value, REDACTED)
    return text


def redact(obj: Any, secrets: tuple[str, ...] | None = None) -> Any:
    """Recursively replace sensitive values, by key name and by known secret value.

    Structure is preserved — a reader of the lake can still see that a field was present and that
    it was withheld, which is the same choice the audit trail makes. Returning a shape that differs
    from the real request would make the provenance record misleading in a new way.
    """
    if secrets is None:
        secrets = _known_secret_values()
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and SENSITIVE_KEY_PATTERN.search(k):
                out[k] = REDACTED
            else:
                out[k] = redact(v, secrets)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        return _redact_text(obj, secrets)
    return obj


def scrub_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Make a ``rackstatus_request`` record safe to ship, or refuse it outright.

    Returns ``None`` when the record must not leave at all. This is the second gate, after the
    capture predicate: the same rule enforced in two places so that a future caller building its own
    record cannot bypass the first one. Cheap, and the failure it prevents is a credential in a
    Parquet file that is retained on purpose and forever.

    ``request_body`` and the response text are parsed as JSON when possible so redaction can work on
    keys; a body that is not JSON is redacted by value only, and a body that is not JSON *and*
    contains no known secret is passed through — there is nothing structural to key off.
    """
    path = str(record.get("path") or "")
    if never_capture(path):
        return None

    secrets = _known_secret_values()
    out = dict(record)

    # The query string gets the audit trail's treatment: a credential travelling as ?token=... is
    # invisible to key-name redaction on the body, so filter the mapping rather than trust it.
    if isinstance(out.get("query"), dict):
        out["query"] = redact(out["query"], secrets)

    for field in ("request_body", "response_body"):
        raw = out.get(field)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            out[field] = _redact_text(raw, secrets)
            continue
        out[field] = json.dumps(redact(parsed, secrets), default=str, sort_keys=True)

    return out


def make_run_id(workload: str, rack: str, job_id: str | int | None, ts: float | None = None) -> str:
    """Stable, filesystem-safe run id. ``run_id`` is a DIRECTORY in the lake, so it must be a
    path-safe token and it must be identical across re-ingests of the same run."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts or time.time()))
    parts = [cluster(), "rackstatus", workload or "unknown", rack or "cluster", str(job_id or stamp)]
    rid = "-".join(re.sub(r"[^A-Za-z0-9._-]+", "_", str(p)) for p in parts if p)
    return rid[:200]


def ca_bundle() -> str:
    return _env("RACK_LAKE_CA_BUNDLE")


def _ssl_context(url: str) -> ssl.SSLContext | None:
    """Verifying context for an ``https://`` lake, or ``None`` for plain HTTP.

    There is deliberately **no** skip-verify switch. An unverified TLS connection to a provenance
    store is worse than an honest plaintext one: it looks secure in a review and authenticates
    nothing, so a misdirected endpoint is indistinguishable from the real one. If the lake presents
    a self-signed or internal-CA certificate, point ``RACK_LAKE_CA_BUNDLE`` at the CA —
    ``bin/gen-tls-cert.py --mode ca`` already produces a suitable one.

    Consequence worth stating: switching the URL to https:// without a trusted certificate makes
    posts FAIL rather than silently downgrade. That is the intended behaviour.
    """
    if not url.lower().startswith("https://"):
        return None
    bundle = ca_bundle()
    if bundle:
        return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


def _warn_plaintext_once(url: str) -> None:
    """Warn when shipping to a non-local lake over plain HTTP.

    Not an error: the default target is ``host.docker.internal``, i.e. the same machine over
    Docker's bridge, where plaintext is a defensible choice. Crossing a real network with it is not,
    and the operator should hear about it once rather than never.
    """
    if url.lower().startswith("https://") or _warn_plaintext_once.done:  # type: ignore[attr-defined]
        return
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "host.docker.internal"):
        return
    _warn_plaintext_once.done = True  # type: ignore[attr-defined]
    _logger.warning(
        "lake: posting to %s over plain HTTP; captured request/response data crosses the network "
        "unencrypted. Use an https:// URL (RACK_LAKE_CA_BUNDLE for a self-signed lake).", url,
    )


_warn_plaintext_once.done = False  # type: ignore[attr-defined]


def _post(path: str, payload: dict[str, Any]) -> tuple[int, str]:
    url = lake_url()
    _warn_plaintext_once(url)
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{url}{path}", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=_timeout(), context=_ssl_context(url)) as resp:
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


def _queue_max() -> int:
    try:
        return max(1, int(_env("RACK_LAKE_QUEUE_MAX", "256")))
    except ValueError:
        return 256


_q: queue.Queue[dict[str, Any]] | None = None
_worker: threading.Thread | None = None
_q_lock = threading.Lock()
#: Incremented when the queue is full. Exposed through ``queue_stats`` so a dropped record is
#: countable rather than invisible -- the product's own rule about absent-versus-zero applies to
#: its own telemetry.
_dropped = 0


def _drain() -> None:
    while True:
        kwargs = (_q.get() if _q else None) or {}
        try:
            push_run(**kwargs)
        except Exception:
            _logger.debug("lake worker: ignoring error", exc_info=True)
        finally:
            if _q:
                _q.task_done()


def submit_run(**kwargs: Any) -> dict[str, Any]:
    """Enqueue a push and return immediately. Never blocks the caller, never raises.

    ``push_run`` stays synchronous for its original callers: the results-ingest path wants the
    honest status back, and this module's contract is that a provenance failure is reported rather
    than swallowed. The **request-capture** path is different -- it runs inside ``after_request``, so
    a synchronous POST there puts the lake's availability directly in the operator's latency path.
    With a 20 s default timeout and ~100 capturable endpoints, a lake that is slow rather than
    absent would make the dashboard feel broken, and the operator would have no way to know why.

    Bounded on purpose: if the lake is wedged, the queue fills and further records are dropped and
    counted. Dropping telemetry is the correct trade against growing memory inside a web process.
    """
    global _q, _worker, _dropped
    if is_disabled():
        return {"ok": False, "queued": False, "error": "disabled"}
    with _q_lock:
        if _q is None:
            _q = queue.Queue(maxsize=_queue_max())
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_drain, name="lake-sink", daemon=True)
            _worker.start()
    try:
        _q.put_nowait(kwargs)
        return {"ok": True, "queued": True}
    except queue.Full:
        _dropped += 1
        _logger.warning("lake: queue full (%d dropped); the lake is not keeping up", _dropped)
        return {"ok": False, "queued": False, "error": "queue full"}


def queue_stats() -> dict[str, Any]:
    """Depth and drop count, for the health endpoint."""
    return {
        "queued": _q.qsize() if _q else 0,
        "dropped": _dropped,
        "max": _queue_max(),
        "worker_alive": bool(_worker and _worker.is_alive()),
    }


def health() -> dict[str, Any]:
    """Is the lake reachable and accepting writes?"""
    url = lake_url()
    out: dict[str, Any] = {
        "ok": False, "lake": url, "disabled": is_disabled(), "error": None,
        "tls": url.lower().startswith("https://"),
        "ca_bundle": ca_bundle() or None,
        "queue": queue_stats(),
    }
    if is_disabled():
        out["error"] = "disabled"
        return out
    try:
        req = urllib.request.Request(f"{url}/healthz")
        with urllib.request.urlopen(req, timeout=8, context=_ssl_context(url)) as resp:
            out["ok"] = resp.status == 200
            out["http_status"] = resp.status
    except Exception as exc:
        _logger.debug("health: ignoring error", exc_info=True)
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
