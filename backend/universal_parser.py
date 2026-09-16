# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""universal_parser.py — one normaliser for EVERY log that lands in the NVIS log dir.

WHY THIS EXISTS
---------------
``bin/scripts/parser.sh`` recognises exactly four shapes (NCCL, two NeMo dialects, MNUBERGEMM)
and runs all four extractors on *every* input file, so a log it does not understand comes back
as a fully-formed placeholder row (rack ``?``, job ``?``, busbw ``---``, status ``FAIL``) that
the UI paints red.  That is worse than useless: a real MNUBERGEMM soak that hit its walltime,
and a job that died at launch and wrote a 182-byte header, are rendered identically.

This module replaces the guessing with a two-stage design:

  1. ``sniff_kind()``  — classify by CONTENT first, filename second.  Abstains (``"unknown"``)
     instead of mislabelling.
  2. ``parse()``       — return ONE normalised dict no matter what the kind is.  Per-kind
     extractors add precision; the GENERIC FALLBACK guarantees a never-before-seen log still
     yields a job id, a node list, timestamps, an exit status, numeric ``key: value`` pairs
     with units, and error/warning markers.  A parse NEVER returns empty.

GROUNDING (what each extractor is built from — nothing here is invented)
-----------------------------------------------------------------------
  nccl        bin/scripts/nccl_parse.inc.sh  (the collective start/concluded markers, the
              ``NF>=9 && $1 numeric && $8 numeric`` data-row shape, the out-of-bounds line)
  mnubergemm  bin/scripts/parser.sh:187-215 ``mnubergemm_stats_tsv`` (``MNUB [`` gate,
              ``^HOSTLIST:``, ``GFlops:<n>``, the Xid/ECC/CUDART/abort fail set) and
              bin/scripts/mnubergemm.sbatch (the ``HOSTLIST:``/``Image:`` banner it prints)
  nemo        bin/scripts/rack_status.sh:354-372 (``iteration <cur>/<tot>`` completion test,
              the Traceback/OOM fatal set) and parser.sh:221-249 (the two training dialects).
              NOTE: the exact column layout of the ``Step Time … MODEL_TFLOP/s/GPU`` dialect
              was NOT sampled on this cluster, so this module uses self-describing regexes
              instead of awk field positions; parser.sh stays authoritative for those tables.
  nvloom      frontend/src/lib/nvloomHeatmapParse.ts (the in-repo port of nvloom's own
              plot_heatmaps.py: ``Process`` lines with ``rackGuid:``, ``Running <name>``,
              matrix rows, ``Min <name> <value> <unit>``) + bin/scripts/nvloom_singlerack.sbatch
  clusterkit  bin/scripts/clusterkit.sbatch:129-171 (``### ClusterKit rack=…``,
              ``ERROR: clusterkit.sh failed for HCA …``, ``OK: clusterkit finished on …``)
  bringup     bin/scripts/checker.sh (``INFO: Health check PASSED|FAILED``, its ``ERROR:``/
              ``WARN:`` lines, ``reason=`` drain tokens, the nvbandwidth SKIPPED banner)
  nvbandwidth bin/scripts/checker.sh:402-465 (``INFO: nvbandwidth -t <t>``,
              ``ERROR: nvbandwidth <t> failed (rc=N)``, the SKIPPED banner) plus nvbandwidth's
              own ``nvbandwidth Version:`` banner and ``SUM <test> <value>`` summary lines.
  dcgm        bin/scripts/checker.sh:380-400 (``dcgmi diag -r <lvl>`` wrapper verdicts) plus
              the dcgmi diag table rows.
  hpl         netlib HPL's standard ``T/V N NB P Q Time Gflops`` result row and the
              ``||Ax-b||…  PASSED/FAILED`` residual line.  NOT sampled on this cluster —
              treated as best-effort and it degrades to the generic fallback if absent.
  slurm       ``srun: error: <host>: task N: Exited with exit code M``,
              ``slurmstepd: error: *** JOB <id> ON <host> CANCELLED AT <ts> DUE TO TIME LIMIT ***``
              and friends — stock Slurm wording, also the pair parser.sh:218 greps for.

THE THREE TRAPS THIS PLATFORM EXISTS TO CATCH
---------------------------------------------
  * A MNUBERGEMM soak that outlives its walltime ends ``DUE TO TIME LIMIT``.  That is the
    EXPECTED end of a soak, not a failure => status ``"timeout"``, never ``"fail"``.
  * Slurm writes a stdout file for a job that died at launch (0 bytes, or a header and
    nothing else) => status ``"fail"`` with an explicit error, NEVER ``"pass"``.
  * A blank / unreadable read is NEVER success.  ``status`` only becomes ``"pass"`` on
    positive evidence (a zero exit, an explicit PASSED verdict, or real measurements).

OPERATIONAL NOTES
-----------------
  * Pure stdlib.  No Flask, no repo imports that are required — ``nvis_id_templates`` is
    soft-imported for rack/hostname attribution and there is a regex fallback if it is absent.
  * Multi-GB safe: reads are CAPPED (head N MB + tail N MB, joined by an explicit truncation
    marker).  Nothing is ever slurped whole.  ``.gz`` is streamed with a rolling tail buffer.
  * Non-UTF8 safe: ``errors="replace"`` everywhere.
  * Import side effects: none.

This file is bind-mount-only backend code.  To make it visible inside the container add::

    - /opt/observability/nvis-rack-status-v3/backend/universal_parser.py:/app/backend/universal_parser.py:ro
"""

from __future__ import annotations

import gzip
import logging
import math
import os
import re
import statistics
import time as _time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from typing import Any

_logger = logging.getLogger(__name__)

__all__ = [
    "KINDS",
    "PARSER_VERSION",
    "STATUSES",
    "empty_result",
    "parse",
    "parse_path",
    "read_capped",
    "sniff_kind",
]

PARSER_VERSION = 1

#: Every value ``sniff_kind`` / ``parse`` can put in ``result["kind"]``.
#: ``nemo`` is an addition to the brief's list — NeMo/Nemotron training stdout is the single
#: most common log on this cluster and silently bucketing it as ``unknown`` would be a defect.
#: Consumers that only know the brief's set should treat ``nemo`` like any other named kind.
KINDS = (
    "nccl",
    "nemo",
    "mnubergemm",
    "nvbandwidth",
    "hpl",
    "dcgm",
    "nvloom",
    "clusterkit",
    "bringup",
    "slurm",
    "unknown",
)

#: Every value ``parse`` can put in ``result["status"]``.
#: ``timeout`` is deliberately NOT a flavour of ``fail`` (see module docstring).
STATUSES = ("pass", "fail", "timeout", "running", "unknown")


# --------------------------------------------------------------------------------------
# tunables (env-overridable so the container can be retuned without an image rebuild)
# --------------------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


DEFAULT_HEAD_BYTES = _env_int("NVIS_UP_HEAD_BYTES", 2 * 1024 * 1024)
DEFAULT_TAIL_BYTES = _env_int("NVIS_UP_TAIL_BYTES", 1 * 1024 * 1024)
#: ceiling on decompressed bytes pulled from a .gz (zip-bomb guard).  A gzip stream cannot be
#: seeked, so reaching the END of the file is the only way to see the walltime kill / exit line —
#: hence this is deliberately generous and paired with a wall-clock deadline instead of a small
#: byte cap.  Hitting either sets ``gz_tail_incomplete`` so nothing downstream mistakes a
#: mid-file cut for the end of the job.
DEFAULT_GZ_LIMIT = _env_int("NVIS_UP_GZ_LIMIT_BYTES", 8 * 1024 * 1024 * 1024)
#: wall-clock budget for decompressing a .gz (it must not stall a web request)
DEFAULT_GZ_DEADLINE_SEC = _env_int("NVIS_UP_GZ_DEADLINE_SEC", 20)
#: bytes of the head handed to sniff_kind
SNIFF_BYTES = _env_int("NVIS_UP_SNIFF_BYTES", 256 * 1024)
#: characters of trailing text kept in result["tail"]
TAIL_CHARS = _env_int("NVIS_UP_TAIL_CHARS", 4000)
#: caps so one pathological log cannot blow up the response
MAX_METRICS = _env_int("NVIS_UP_MAX_METRICS", 400)
MAX_ERRORS = _env_int("NVIS_UP_MAX_ERRORS", 40)
MAX_WARNINGS = _env_int("NVIS_UP_MAX_WARNINGS", 40)
MAX_NODES = _env_int("NVIS_UP_MAX_NODES", 4096)
MAX_LINE_CHARS = 400

TRUNCATION_MARKER = "\n...[universal_parser: {n} bytes elided]...\n"


# --------------------------------------------------------------------------------------
# capped / streaming reader
# --------------------------------------------------------------------------------------
def _decode(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


def _read_gz_capped(
    path: str, head_bytes: int, tail_bytes: int, gz_limit: int, deadline_sec: int
) -> tuple[bytes, bytes, int, bool, bool]:
    """Stream a gzip file keeping the first ``head_bytes`` and a rolling last ``tail_bytes``.

    Returns ``(head, tail, decompressed_bytes_seen, truncated, tail_incomplete)``.

    Memory is O(head_bytes + tail_bytes) regardless of stream size: the tail is a bounded
    bytearray trimmed on every chunk, and everything between head and tail is decompressed and
    thrown away.  ``tail_incomplete`` is True when the byte ceiling or the wall-clock deadline
    stopped the read BEFORE EOF — in that case the "tail" is a mid-file slice, not the end of
    the job, and the caller must not read the absence of an end marker as "still running".
    """
    head = bytearray()
    tail = bytearray()
    total = 0
    truncated = False
    tail_incomplete = False
    chunk_size = 1024 * 1024
    started = _time.monotonic()
    with gzip.open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if len(head) < head_bytes:
                head.extend(chunk[: head_bytes - len(head)])
            tail.extend(chunk)
            if len(tail) > tail_bytes:
                del tail[: len(tail) - tail_bytes]
                truncated = True
            if total >= gz_limit or (_time.monotonic() - started) > deadline_sec:
                truncated = True
                tail_incomplete = True
                break
    if total <= head_bytes:
        # Everything read fit in the head; do not double-report the same bytes as a tail.
        # ``tail_incomplete`` must still propagate: a deadline that bit on the very first chunk
        # also means we never saw the end of the stream.
        return bytes(head), b"", total, tail_incomplete, tail_incomplete
    return (
        bytes(head),
        bytes(tail),
        total,
        truncated or total > head_bytes + tail_bytes,
        tail_incomplete,
    )


def read_capped(
    path: str | os.PathLike[str],
    *,
    head_bytes: int = DEFAULT_HEAD_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
    gz_limit: int = DEFAULT_GZ_LIMIT,
    gz_deadline_sec: int = DEFAULT_GZ_DEADLINE_SEC,
) -> tuple[str, dict[str, Any]]:
    """Read a log without ever slurping it.

    Small file  -> returned whole.
    Large file  -> first ``head_bytes`` + explicit marker + last ``tail_bytes``.
    ``.gz``     -> streamed and decompressed with a rolling tail buffer (``gz_limit`` ceiling).

    Returns ``(text, meta)`` where ``meta`` has ``bytes`` (on-disk size), ``read_bytes``,
    ``truncated``, ``gzip`` and, on failure, ``read_error``.  A read failure is data, not an
    exception: the caller still gets a result and ``parse()`` turns it into a ``fail``.
    """
    p = str(path)
    meta: dict[str, Any] = {
        "bytes": None,
        "read_bytes": 0,
        "truncated": False,
        "gzip": p.endswith(".gz"),
        "read_error": None,
    }
    try:
        meta["bytes"] = os.path.getsize(p)
    except OSError as exc:
        meta["read_error"] = f"{type(exc).__name__}: {exc}"
        return "", meta

    try:
        if meta["gzip"]:
            head, tail, total, truncated, tail_incomplete = _read_gz_capped(
                p, head_bytes, tail_bytes, gz_limit, gz_deadline_sec
            )
            meta["read_bytes"] = len(head) + len(tail)
            meta["truncated"] = truncated
            meta["uncompressed_bytes"] = total
            meta["gz_tail_incomplete"] = tail_incomplete
            if not tail:
                return _decode(head), meta
            elided = max(0, total - len(head) - len(tail))
            return _decode(head) + TRUNCATION_MARKER.format(n=elided) + _decode(tail), meta

        size = int(meta["bytes"] or 0)
        with open(p, "rb") as fh:
            if size <= head_bytes + tail_bytes:
                data = fh.read(head_bytes + tail_bytes)
                meta["read_bytes"] = len(data)
                return _decode(data), meta
            head = fh.read(head_bytes)
            fh.seek(max(0, size - tail_bytes))
            tail = fh.read(tail_bytes)
        meta["read_bytes"] = len(head) + len(tail)
        meta["truncated"] = True
        elided = size - len(head) - len(tail)
        return _decode(head) + TRUNCATION_MARKER.format(n=elided) + _decode(tail), meta
    except OSError as exc:  # unreadable, vanished mid-read, permission, corrupt gz
        meta["read_error"] = f"{type(exc).__name__}: {exc}"
        return "", meta
    except EOFError as exc:  # truncated gzip stream (job still writing)
        meta["read_error"] = f"truncated gzip stream: {exc}"
        return "", meta


# --------------------------------------------------------------------------------------
# naming: rack / node attribution (soft-imports the repo's template engine)
# --------------------------------------------------------------------------------------
try:  # pragma: no cover - exercised only when running inside the app
    from nvis_id_templates import (  # type: ignore
        naming_matcher_from_env,
        rack_id_scan_re,
    )

    _HAVE_NVIS_NAMING = True
except Exception:  # noqa: BLE001 - standalone use, or module not on sys.path
    _HAVE_NVIS_NAMING = False

# Fallback rack pattern: the same generic site fallback parser.sh:163 uses
# (``<word>-<word>-<letter><digits>``, e.g. ink50-dh01-h22).  Overridable with the same env var.
_FALLBACK_RACK_RE = re.compile(
    os.environ.get("NVIS_RACK_FNAME_RE") or r"[a-z0-9]+-[a-z0-9]+-[a-z]\d+", re.IGNORECASE
)
# Tray/node = rack id + "-<slot>" (ink50-dh01-h22-12); "-nv" suffix marks an NVSwitch.
_FALLBACK_NODE_RE = re.compile(r"\b[a-z0-9]+-[a-z0-9]+-[a-z]\d+-\d+(?:-nv)?\b", re.IGNORECASE)


def _rack_scan_re() -> re.Pattern[str]:
    if _HAVE_NVIS_NAMING:
        try:
            return rack_id_scan_re(os.environ.get("NVIS_RACK_ID"))
        except Exception:
            _logger.debug("_rack_scan_re: ignoring error", exc_info=True)
    return _FALLBACK_RACK_RE


def _find_racks(text: str, *, limit: int = 64) -> list[str]:
    """Distinct rack ids appearing in *text*, in first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _rack_scan_re().finditer(text):
        rid = m.group(0)
        key = rid.casefold()
        if key not in seen:
            seen.add(key)
            out.append(rid)
            if len(out) >= limit:
                break
    return out


def _find_nodes(text: str, *, limit: int = MAX_NODES) -> list[str]:
    """Distinct compute-node hostnames in *text*.

    Uses the repo's ``NVIS_CT_ID`` matcher when available so site naming stays authoritative,
    otherwise the ``rack-<slot>`` fallback.
    """
    out: list[str] = []
    seen: set[str] = set()
    if _HAVE_NVIS_NAMING:
        try:
            matcher = naming_matcher_from_env()
            for m in matcher.ct_re.finditer(text):
                tok = m.group(0)
                key = tok.casefold()
                if key not in seen:
                    seen.add(key)
                    out.append(tok)
                    if len(out) >= limit:
                        return out
            if out:
                return out
        except Exception:
            _logger.debug("_find_nodes: ignoring error", exc_info=True)
    for m in _FALLBACK_NODE_RE.finditer(text):
        tok = m.group(0)
        key = tok.casefold()
        if key not in seen:
            seen.add(key)
            out.append(tok)
            if len(out) >= limit:
                break
    return out


_BRACKET_RANGE_RE = re.compile(r"^(\d+)-(\d+)$")


def _split_top_level(s: str) -> list[str]:
    """Split a Slurm nodelist on commas that are NOT inside ``[...]``."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def expand_nodelist(spec: str, *, limit: int = MAX_NODES) -> list[str]:
    """Expand a Slurm nodelist (``a[01-03,07],b02``) into hostnames.

    Handles the single-bracket form Slurm emits for these racks
    (``ink50-dh01-h22-[01-18]``).  Anything it cannot expand is returned verbatim rather than
    dropped, so a novel nodelist syntax still surfaces something usable.
    """
    out: list[str] = []
    for token in _split_top_level(spec or ""):
        m = re.match(r"^(?P<pfx>[^\[\]]+)\[(?P<inner>[^\]]*)\](?P<sfx>[^\[\]]*)$", token)
        if not m:
            out.append(token)
            if len(out) >= limit:
                break
            continue
        pfx, inner, sfx = m.group("pfx"), m.group("inner"), m.group("sfx")
        for piece in inner.split(","):
            piece = piece.strip()
            if not piece:
                continue
            rng = _BRACKET_RANGE_RE.match(piece)
            if rng:
                lo_s, hi_s = rng.group(1), rng.group(2)
                width = len(lo_s)
                try:
                    lo, hi = int(lo_s), int(hi_s)
                except ValueError:
                    out.append(f"{pfx}{piece}{sfx}")
                    continue
                if hi < lo or (hi - lo) > limit:
                    out.append(f"{pfx}{piece}{sfx}")
                    continue
                for i in range(lo, hi + 1):
                    out.append(f"{pfx}{str(i).zfill(width)}{sfx}")
                    if len(out) >= limit:
                        break
            else:
                out.append(f"{pfx}{piece}{sfx}")
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    # dedupe, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for n in out:
        if n and n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


# --------------------------------------------------------------------------------------
# generic scanners: timestamps, job id, exit status, key/value metrics, markers
# --------------------------------------------------------------------------------------
# ISO-8601-ish, as produced by `date -Is` (run_mnubergemm_stress.sh:63) and by Slurm
# ("CANCELLED AT 2026-07-27T10:00:00"), plus the "YYYY-MM-DD HH:MM:SS" python/NeMo form.
_ISO_TS_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)\b"
)
# `date` default form: "Mon Jul 27 10:00:00 UTC 2026"
_CTIME_TS_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
    r"\d{2}:\d{2}:\d{2}(?:\s+[A-Z]{2,5})?\s+\d{4}"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _normalise_ts(raw: str) -> str:
    """Normalise a matched timestamp to ``YYYY-MM-DDTHH:MM:SS[offset]``; never raises."""
    s = raw.strip()
    m = _ISO_TS_RE.search(s)
    if m:
        v = m.group(1).replace(" ", "T").replace(",", ".")
        return v
    m2 = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+"
        r"(\d{2}:\d{2}:\d{2})(?:\s+[A-Z]{2,5})?\s+(\d{4})",
        s,
        re.IGNORECASE,
    )
    if m2:
        mon = _MONTHS.get(m2.group(1).lower())
        if mon:
            return f"{m2.group(4)}-{mon:02d}-{int(m2.group(2)):02d}T{m2.group(3)}"
    return s


def _scan_timestamps(text: str) -> tuple[str | None, str | None]:
    """First timestamp anywhere and last timestamp anywhere, normalised.

    ``re.finditer`` over a capped read is cheap; we only keep the first and the last so memory
    stays flat regardless of how many timestamps the log carries.
    """
    first: str | None = None
    last: str | None = None
    for m in _ISO_TS_RE.finditer(text):
        if first is None:
            first = m.group(1)
        last = m.group(1)
    if first is None:
        for m2 in _CTIME_TS_RE.finditer(text):
            if first is None:
                first = m2.group(0)
            last = m2.group(0)
    return (_normalise_ts(first) if first else None, _normalise_ts(last) if last else None)


_JOBID_CONTENT_RES = (
    re.compile(r"\bSLURM_JOB_?ID[=: ]\s*(\d+)"),
    re.compile(r"\bSubmitted batch job\s+(\d+)"),
    re.compile(r"slurmstepd:.*?\*\*\*\s+(?:JOB|STEP)\s+(\d+)"),
    re.compile(r"\bsrun:\s+job\s+(\d+)\b"),
    re.compile(r"\bjob[_ ]?id[=: ]\s*(\d+)", re.IGNORECASE),
)
# parser.sh:170-185 filename forms: "...-18N-61762.txt" (NEMO) and the generic "...-<jid>.txt"
_JOBID_FNAME_NEMO_RE = re.compile(r"N-(\d+)\.(?:txt|out|log)(?:\.gz)?$", re.IGNORECASE)
_JOBID_FNAME_GEN_RE = re.compile(r"-(\d+)\.(?:txt|out|log|err)(?:\.gz)?$", re.IGNORECASE)


def _scan_job_id(basename: str, text: str) -> str | None:
    for rx in _JOBID_CONTENT_RES:
        m = rx.search(text)
        if m:
            return m.group(1)
    m = _JOBID_FNAME_NEMO_RE.search(basename)
    if m:
        return m.group(1)
    m = _JOBID_FNAME_GEN_RE.search(basename)
    if m:
        return m.group(1)
    return None


# Slurm / runtime terminal markers.  Wording is stock Slurm; ``srun:.*error:.*Exited`` and
# ``error:.*SIGNAL`` are the exact pair parser.sh:218 / rack_status.sh:351 grep for.
_RE_TIME_LIMIT = re.compile(r"DUE TO TIME LIMIT", re.IGNORECASE)
_RE_NODE_FAILURE = re.compile(r"DUE TO NODE FAILURE", re.IGNORECASE)
_RE_PREEMPTION = re.compile(r"DUE TO PREEMPTION", re.IGNORECASE)
_RE_CANCELLED = re.compile(r"\*\*\*\s+(?:JOB|STEP)\s+\S+.*?CANCELLED AT", re.IGNORECASE)
_RE_SRUN_EXITED = re.compile(r"srun:.*error:.*Exited with exit code\s+(\d+)", re.IGNORECASE)
_RE_SRUN_HARD_FAIL = re.compile(r"srun:.*error:.*Exited|error:.*SIGNAL", re.IGNORECASE)
_RE_EXIT_CODE = re.compile(r"\bexit(?:ed with)?(?:\s+code)?[:= ]\s*(\d+)\b", re.IGNORECASE)
_RE_RC = re.compile(r"\brc[=: ]\s*(\d+)\b", re.IGNORECASE)

# error / warning markers.  The fatal-python set mirrors rack_status.sh:369-372.
_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("traceback", re.compile(r"Traceback \(most recent call last\)")),
    ("oom", re.compile(
        r"torch\.cuda\.OutOfMemoryError|torch\.OutOfMemoryError|CUDA out of memory"
        r"|CudaOutOfMemoryError|out-of-memory|oom-kill|Out of memory", re.IGNORECASE)),
    ("cuda_error", re.compile(r"CUDA error|CUDART error|CUDA_ERROR_[A-Z_]+|cudaError", re.IGNORECASE)),
    ("xid", re.compile(r"\bXid\b")),
    ("ecc", re.compile(r"ECC error|uncorrectable", re.IGNORECASE)),
    ("nccl_error", re.compile(r"NCCL (?:ERROR|FATAL)|ncclInternalError|ncclUnhandled", re.IGNORECASE)),
    ("abort", re.compile(r"Aborted \(core dumped\)|Segmentation fault|core dumped|signal 11|SIGSEGV")),
    ("slurm_error", re.compile(r"srun:\s+error:|slurmstepd:\s+error:|sbatch:\s+error:", re.IGNORECASE)),
    ("container_error", re.compile(
        r"pyxis:.*(?:error|fail)|enroot.*(?:error|fail)|couldn't start container", re.IGNORECASE)),
    ("script_error", re.compile(r"^\s*(?:\[[^\]]*\]\s*)?ERROR:", re.MULTILINE)),
    ("failed", re.compile(r"\bFAILED\b")),
)
_WARNING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("nccl_warn", re.compile(r"NCCL WARN")),
    ("skipped", re.compile(r"SKIPPED", re.IGNORECASE)),
    ("script_warn", re.compile(r"^\s*(?:\[[^\]]*\]\s*)?WARN(?:ING)?:", re.MULTILINE)),
    ("deprecated", re.compile(r"DeprecationWarning|FutureWarning")),
)

# "key: value unit" / "key = value unit".  Deliberately conservative: the key must start with a
# letter and the number must be a bare numeric token, so log prose does not become metrics.
_UNIT_ALTS = (
    r"GB/s|GiB/s|MB/s|MiB/s|TB/s|Gb/s|Gbps|Mbps"
    r"|GFLOP/s|GFLOPs|GFlops|GFLOPS|TFLOP/s|TFLOPs|TFLOPS|FLOP/s"
    r"|usec|us|µs|msec|ms|nsec|ns|sec|s"
    r"|mW|kW|MW|W|degC|°C|C"
    r"|%|MiB|GiB|KiB|TiB|MB|GB|KB|TB"
    r"|MHz|GHz|kHz|Hz"
    r"|samples/s|tokens/s|it/s|iter/s"
)
# The trailing lookahead is load-bearing and each character in it earns its place:
#   ``:``  a timestamp ("…T09:00:01") and a PCI/BDF triple ("(00000008:06:00)") otherwise parse
#          as key/value pairs and flood the bag with junk like ``t09 = 0``;
#   ``.``  stops the regex from BACKTRACKING INTO a number that a read cap or a live writer cut
#          in half — ``GFlops:8.3011e+`` would otherwise silently yield ``gflops = 8``, a wrong
#          value that looks perfectly plausible.  Rejecting the whole match is the honest answer;
#   A-Za-z0-9_/ ordinary "number glued to a word" false positives.
_KV_RE = re.compile(
    r"(?P<key>[A-Za-z][A-Za-z0-9_.\-/() ]{0,48}?)\s*[:=]\s*"
    r"(?P<val>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"
    r"\s*(?P<unit>" + _UNIT_ALTS + r")?(?![A-Za-z0-9_/:.])"
)
# keys that are pure noise as metrics (paths, ids, env dumps)
_KV_KEY_DENY = re.compile(
    r"^(?:https?|ftp|file|version|port|pid|uid|gid|line|col|column|char|errno|index|idx|"
    r"argv|arg|env|path|dir|host|node|rank|world_size|local_rank|seed|"
    r"cuda_visible_devices|slurm_[a-z_]*|nccl_[a-z_]*|ucx_[a-z_]*|ompi_[a-z_]*)$"
)
_UNIT_CANON = {
    "gflops": "GFLOP/s", "gflop/s": "GFLOP/s", "gflops/s": "GFLOP/s",
    "tflops": "TFLOP/s", "tflop/s": "TFLOP/s",
    "gib/s": "GiB/s", "gb/s": "GB/s", "mb/s": "MB/s", "mib/s": "MiB/s", "tb/s": "TB/s",
    "usec": "us", "µs": "us", "msec": "ms", "nsec": "ns", "sec": "s",
    "degc": "C", "°c": "C",
}


def _snake(key: str) -> str:
    k = key.strip().strip("-_.| \t")
    k = re.sub(r"[^\w]+", "_", k)
    k = re.sub(r"_+", "_", k).strip("_").lower()
    return k


def _canon_unit(u: str | None) -> str | None:
    if not u:
        return None
    return _UNIT_CANON.get(u.strip().lower(), u.strip())


def _to_num(s: str) -> float | int | None:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):  # NaN / inf
        return None
    if v.is_integer() and abs(v) < 1e15:
        return int(v)
    return v


class _MetricBag:
    """Ordered, deduplicated, capped metric accumulator.

    Same (name, unit, labels) seen twice => LAST value wins and ``occurrences`` is bumped.
    That is the right default for logs, where a periodic line's final value is the summary.
    """

    def __init__(self, cap: int = MAX_METRICS) -> None:
        self._d: OrderedDict[tuple, dict[str, Any]] = OrderedDict()
        self._cap = cap

    def add(
        self,
        name: str,
        value: Any,
        unit: str | None = None,
        labels: dict[str, Any] | None = None,
    ) -> None:
        if value is None or not name:
            return
        labels = {str(k): str(v) for k, v in (labels or {}).items() if v is not None and v != ""}
        key = (name, unit or "", tuple(sorted(labels.items())))
        cur = self._d.get(key)
        if cur is not None:
            cur["value"] = value
            cur["labels"]["occurrences"] = str(int(cur["labels"].get("occurrences", "1")) + 1)
            return
        if len(self._d) >= self._cap:
            return
        self._d[key] = {"name": name, "value": value, "unit": unit, "labels": labels}

    def __len__(self) -> int:
        return len(self._d)

    def has(self, name: str) -> bool:
        return any(k[0] == name for k in self._d)

    def first(self, name: str) -> dict[str, Any] | None:
        for v in self._d.values():
            if v["name"] == name:
                return v
        return None

    def as_list(self) -> list[dict[str, Any]]:
        return list(self._d.values())


def _collect_lines(text: str, patterns: Sequence[tuple[str, re.Pattern[str]]], cap: int) -> list[dict[str, str]]:
    """Return the matching lines for *patterns*, deduped by (kind, line), capped."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, rx in patterns:
        for m in rx.finditer(text):
            ls = text.rfind("\n", 0, m.start()) + 1
            le = text.find("\n", m.end())
            if le < 0:
                le = len(text)
            line = text[ls:le].strip()
            if not line:
                continue
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "…"
            key = (kind, line)
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "line": line})
            if len(out) >= cap:
                return out
    return out


def _drop_partial_lines(text: str) -> str:
    """Remove lines that a cap or a live writer cut in half.

    Two places produce them: the tail of a log a job is still writing, and the last line of the
    head half of a head+tail capped read.  A halved number is a WRONG number — ``GFlops:8.3011e+``
    backtracks to ``8`` — so those lines must not reach the metric scanner.
    """
    out = text
    if TRUNCATION_MARKER.split("{n}")[0] in out:
        pre, _, post = out.partition(TRUNCATION_MARKER.split("{n}")[0])
        pre = pre[: pre.rfind("\n") + 1] if "\n" in pre else ""
        _, _, post = post.partition("\n")  # drop the rest of the marker line
        post = post.partition("\n")[2] if "\n" in post else ""
        out = pre + "\n" + post
    if out and not out.endswith("\n"):
        cut = out.rfind("\n")
        out = out[: cut + 1] if cut >= 0 else ""
    return out


def _scan_kv_metrics(text: str, bag: _MetricBag, *, prefix: str = "") -> None:
    """Generic ``key: value [unit]`` harvest.  The backbone of the unknown-log fallback."""
    for m in _KV_RE.finditer(text):
        raw_key = m.group("key")
        # A key that swallowed a whole sentence is prose, not a metric.
        if raw_key.count(" ") > 4:
            raw_key = raw_key.rsplit(" ", 1)[-1] if " " in raw_key else raw_key
        # A hostname used as a key ("ink50-dh01-h22-01 L: 0") is structure, not a measurement.
        # Matched against the RAW key so the site's real rack pattern applies, rather than a
        # loose shape that would also swallow honest keys like "interconnect latency p99".
        if _rack_scan_re().search(raw_key):
            continue
        name = _snake(raw_key)
        if not name or name[0].isdigit() or len(name) < 2:
            continue
        if _KV_KEY_DENY.match(name):
            continue
        val = _to_num(m.group("val"))
        if val is None:
            continue
        unit = _canon_unit(m.group("unit"))
        # A bare unitless integer key like "count: 3" is still useful, but a date fragment is not.
        bag.add(f"{prefix}{name}" if prefix else name, val, unit)
        if len(bag) >= MAX_METRICS:
            return


# --------------------------------------------------------------------------------------
# sniff_kind
# --------------------------------------------------------------------------------------
# Content signatures, most-specific first.  Each entry: (kind, weight, regex).
# Weight is only used to break ties between two kinds that both matched.
_CONTENT_SIGNATURES: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    # bringup wins over dcgm/nvbandwidth: checker.sh EMBEDS their output, so the wrapper verdict
    # ("INFO: Health check PASSED/FAILED", checker.sh:645-647) is the authoritative classifier.
    ("bringup", 100, re.compile(r"^\s*(?:\S+:\s*)?INFO:\s*Health check (?:PASSED|FAILED)", re.MULTILINE)),
    ("bringup", 70, re.compile(r"INFO:\s*Running on installer node|f_check_(?:gpu|nvlink|imex|cpu|mem)")),
    ("mnubergemm", 100, re.compile(r"^\s*(?:\S+:\s*)?MNUB \[", re.MULTILINE)),
    ("mnubergemm", 80, re.compile(r"GFlops:[0-9.eE+-]+")),
    ("mnubergemm", 70, re.compile(r"/opt/mnubergemm/mnubergemm|--MM_force_cublas_algo|mnubergemm-\d+")),
    ("nccl", 100, re.compile(r"^#\s*Collective test (?:starting|concluded):", re.MULTILINE)),
    ("nccl", 90, re.compile(r"^#\s*Out of bounds values", re.MULTILINE)),
    ("nccl", 85, re.compile(r"^#\s*Avg bus bandwidth", re.MULTILINE)),
    ("nccl", 70, re.compile(r"\ball_reduce_perf(?:_mpi)?\b|\ballto_?all_perf(?:_mpi)?\b|\ball_gather_perf(?:_mpi)?\b")),
    ("nccl", 60, re.compile(r"^nccl_allreduce:", re.MULTILINE)),
    ("nvloom", 100, re.compile(r"rackGuid:")),
    ("nvloom", 80, re.compile(r"^\s*(?:\d+:\s*)?Min\s+\S+\s+[\d.]+\s+\S+/s", re.MULTILINE)),
    ("nvloom", 70, re.compile(r"nvloom_cli|gpuToRackSamples")),
    ("clusterkit", 100, re.compile(r"^###\s*ClusterKit rack=", re.MULTILINE)),
    ("clusterkit", 70, re.compile(r"clusterkit\.sh|OK:\s*clusterkit finished")),
    ("nemo", 95, re.compile(r"train_step_timing.*consumed_samples")),
    ("nemo", 95, re.compile(r"Step Time.*MODEL_TFLOP/s/GPU")),
    ("nemo", 60, re.compile(r"^\[NeMo [IWE]|megatron|nemotron", re.MULTILINE | re.IGNORECASE)),
    ("nvbandwidth", 100, re.compile(r"^nvbandwidth Version:", re.MULTILINE)),
    ("nvbandwidth", 85, re.compile(r"^\s*SUM\s+\w+_memcpy_\w+\s+[\d.]+", re.MULTILINE)),
    ("nvbandwidth", 70, re.compile(r"INFO:\s*nvbandwidth (?:-t|mode=)")),
    ("dcgm", 100, re.compile(r"dcgmi diag\b")),
    ("dcgm", 70, re.compile(r"^\s*\|\s*Deployment\s*\|", re.MULTILINE)),
    ("hpl", 100, re.compile(r"^T/V\s+N\s+NB\s+P\s+Q\s+Time\s+Gflops", re.MULTILINE)),
    ("hpl", 80, re.compile(r"HPLinpack|HPL_pdgesv|\|\|Ax-b\|\|")),
    # weakest: it is a Slurm stdout but no tool identified itself
    ("slurm", 20, re.compile(r"^\s*srun:|^\s*slurmstepd:|^SLURM_JOB_ID=|^HOSTLIST:", re.MULTILINE)),
)

# Filename markers — the SECOND-pass classifier, used only when content is silent.
# Ordered longest/most-specific first so "mnubergemm" is not shadowed by "ubergemm".
_FNAME_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mnubergemm", ("mnubergemm", "ubergemm")),
    ("nvbandwidth", ("nvbandwidth",)),
    ("clusterkit", ("clusterkit", "root-ck-")),
    ("nvloom", ("nvloom",)),
    ("nccl", ("nccl",)),
    ("nemo", ("nemotron", "nemo", "llama", "1rack")),
    ("dcgm", ("dcgm",)),
    ("hpl", ("hpl",)),
    ("bringup", ("healthcheck", "health-check", "bringup", "sysinfo", "checker")),
)


def _basename(path: str | os.PathLike[str] | None) -> str:
    if not path:
        return ""
    s = str(path).replace("\\", "/")
    return s.rsplit("/", 1)[-1]


def sniff_kind(path: str | os.PathLike[str] | None, head_text: str | None) -> str:
    """Classify a log.  CONTENT first, filename second, ``"unknown"`` if neither is conclusive.

    ``head_text`` may be the whole log or just its head — only the first ``SNIFF_BYTES``
    characters are examined either way, so this is cheap on huge files.

    Deliberate precedences:
      * ``bringup`` beats ``dcgm``/``nvbandwidth`` — checker.sh embeds both tools' stdout, and
        the wrapper's own ``Health check PASSED/FAILED`` verdict is what the operator cares about.
      * ``mnubergemm`` beats ``nccl`` on a tie — mnubergemm.sbatch exports ``NCCL_DEBUG=INFO``,
        so a soak log is full of NCCL chatter that must not reclassify it.
    """
    text = (head_text or "")[:SNIFF_BYTES]
    scores: dict[str, int] = {}
    if text.strip():
        for kind, weight, rx in _CONTENT_SIGNATURES:
            if rx.search(text):
                scores[kind] = max(scores.get(kind, 0), weight)
    if scores:
        best = max(scores.values())
        winners = [k for k, v in scores.items() if v == best]
        if len(winners) == 1:
            return winners[0]
        for pref in ("bringup", "mnubergemm", "nccl", "nemo", "nvloom", "clusterkit",
                     "nvbandwidth", "dcgm", "hpl", "slurm"):
            if pref in winners:
                return pref
        return winners[0]

    low = _basename(path).lower()
    if low:
        for kind, markers in _FNAME_MARKERS:
            if any(m in low for m in markers):
                return kind
    # A file that is plainly Slurm stdout but named nothing useful.
    if re.search(r"^\s*(?:srun|slurmstepd|sbatch):", text, re.MULTILINE):
        return "slurm"
    return "unknown"


# --------------------------------------------------------------------------------------
# per-kind extractors
#   each returns a dict of overrides merged onto the generic result:
#     {"metrics": [...], "headline": {...}, "status": "...", "errors": [...],
#      "warnings": [...], "nodes": [...], "extra": {...}}
#   An extractor that finds nothing simply returns {} and the generic fallback carries the log.
# --------------------------------------------------------------------------------------
def _strip_task_prefix(line: str) -> str:
    """Strip ``srun --label`` / pdsh prefixes (``0: `` or ``host: ``)."""
    return re.sub(r"^\s*(?:\d+:|[A-Za-z0-9_.-]+:)\s*", "", line).strip()


# ---- NCCL -----------------------------------------------------------------------------
_NCCL_START_RE = re.compile(r"^#\s*Collective test starting:\s*(\S+)", re.MULTILINE)
_NCCL_COLL_MAP = {"all_reduce_perf": "all_reduce", "alltoall_perf": "alltoall", "all_gather_perf": "all_gather"}
_NCCL_OOB_RE = re.compile(r"^#\s*Out of bounds values\s*:\s*(\d+)\s*(\w+)?", re.MULTILINE)
# LLMB's completed-run collector writes this manifest before concatenating the per-test
# rank-0 logs.  It is stronger than counting markers alone: a failed test has no
# ``Collective test starting/concluded`` pair, so 22 starts + 22 conclusions can still mean
# that only 22 of the requested 24 tests ran (Nscale TX job 133460 was the concrete case).
_NCCL_LLMB_EXPECTED_RE = re.compile(r"^#\s*job=\S+.*\btests=(\d+)\b", re.MULTILINE)
# The table header nccl-tests always prints above a sweep, in either of its two lines:
#   #                              out-of-place                       in-place
#   #       size   count   type   redop   root   time   algbw   busbw #wrong   …
_NCCL_TABLE_HDR_RE = re.compile(r"^#.*(?:out-of-place|size.*busbw)")
_NCCL_AVG_RE = re.compile(r"^#\s*Avg bus bandwidth\s*:\s*([\d.]+)", re.MULTILINE)


def _extract_nccl(text: str, bag: _MetricBag) -> dict[str, Any]:
    """Peak out-of-place busbw per collective — the same rule as nccl_parse.inc.sh.

    Data rows have >=9 whitespace fields with a numeric size in $1 and a numeric out-of-place
    busbw in $8; the collective comes from the preceding ``# Collective test starting:`` line.

    ARMING: that row shape is loose enough to hit ordinary text, so rows count only after the
    file has proved it holds an nccl-tests table — a ``starting:`` marker or the table header.
    A NeMo log run with NCCL_DEBUG=INFO prints channel rank lists whose wrap continuations are
    bare numeric runs (" 23 24 … 35"), which used to be read as size=23 / busbw=30 and charted
    as a real result. The header is what keeps the marker-less standalone nccl-tests launcher
    working: it prints no ``starting:`` line but does print the header. Mirrors the awk's
    ``hdr`` gate in nccl_parse.inc.sh — keep the two in step.
    """
    cur = ""
    seen_marker = False
    seen_header = False
    peak: dict[str, tuple[int, float]] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.lstrip().startswith("#"):
            if _NCCL_TABLE_HDR_RE.search(line):
                seen_header = True
            m = _NCCL_START_RE.match(line.lstrip())
            if m:
                cur = _NCCL_COLL_MAP.get(m.group(1).replace("_mpi", ""), m.group(1))
                seen_marker = True
                continue
            if "Collective test concluded" in line:
                cur = ""
                continue
            continue
        if not (seen_marker or seen_header):
            continue
        f = line.split()
        if len(f) < 9:
            continue
        if not f[0].isdigit():
            continue
        try:
            size = int(f[0])
            busbw = float(f[7])
        except ValueError:
            continue
        coll = cur if seen_marker else "all_reduce"
        if not coll:
            continue
        prev = peak.get(coll)
        if prev is None or size >= prev[0]:
            peak[coll] = (size, busbw)

    for coll, (size, bw) in peak.items():
        bag.add("nccl_busbw_peak", bw, "GB/s", {"collective": coll, "size_bytes": size})

    oob_total = 0
    oob_seen = False
    for m in _NCCL_OOB_RE.finditer(text):
        oob_seen = True
        oob_total += int(m.group(1))
    if oob_seen:
        bag.add("nccl_out_of_bounds_values", oob_total, None)
    for m in _NCCL_AVG_RE.finditer(text):
        v = _to_num(m.group(1))
        if v is not None:
            bag.add("nccl_avg_busbw", v, "GB/s")

    started = len(_NCCL_START_RE.findall(text))
    concluded = text.count("# Collective test concluded:")
    expected_match = _NCCL_LLMB_EXPECTED_RE.search(text)
    expected = int(expected_match.group(1)) if expected_match else None
    if expected is not None:
        bag.add("nccl_sweeps_expected", expected, None)
        bag.add("nccl_sweeps_started", started, None)
        bag.add("nccl_sweeps_concluded", concluded, None)

    out: dict[str, Any] = {}
    if "all_reduce" in peak:
        out["headline"] = {"label": "NCCL all_reduce peak busbw", "value": peak["all_reduce"][1], "unit": "GB/s"}
    elif peak:
        k = next(iter(peak))
        out["headline"] = {"label": f"NCCL {k} peak busbw", "value": peak[k][1], "unit": "GB/s"}
    if expected is not None and (started != expected or concluded != expected):
        out["errors"] = [{
            "kind": "nccl_incomplete_sweep",
            "line": (
                f"LLMB requested {expected} collective tests but only "
                f"{started} started and {concluded} concluded"
            ),
        }]
        out["status"] = "fail"
    elif oob_seen and oob_total > 0:
        out["errors"] = [{"kind": "nccl_out_of_bounds", "line": f"NCCL reported {oob_total} out-of-bounds values"}]
        out["status"] = "fail"
    elif peak:
        out["status_hint_pass"] = True
    out["extra"] = {
        "nccl_collectives": sorted(peak),
        "nccl_sweeps_started": started,
        "nccl_sweeps_concluded": concluded,
        "nccl_sweeps_expected": expected,
    }
    return out


# ---- MNUBERGEMM -----------------------------------------------------------------------
_MNUB_GATE_RE = re.compile(r"MNUB \[")
_MNUB_GFLOPS_RE = re.compile(r"GFlops:([0-9.eE+-]+)")
_MNUB_FREQ_RE = re.compile(r"freq:([0-9.]+)Hz")
_MNUB_DUTY_RE = re.compile(r"duty:([0-9.]+)")
_MNUB_P_RE = re.compile(r"\bP:([0-9.]+)")
_MNUB_HOSTLIST_RE = re.compile(r"^HOSTLIST:\s*(.+)$", re.MULTILINE)
# parser.sh:207 verdict=FAIL set, verbatim.
_MNUB_FAIL_RE = re.compile(
    r"Aborted \(core dumped\)|srun:.*error:.*Exited|Xid|ECC error|CUDART error"
    r"|returning QUAL_FAIL|MNUB \[E\]"
)
_MNUB_QUAL_OK_RE = re.compile(r"returning QUAL_OK")
_MNUB_QUAL_OK_RANK_RE = re.compile(
    r"\bG:\s*(\d+)\b[^\r\n]*\breturning QUAL_OK\b"
)
_MNUB_NORMAL_TEARDOWN_RE = re.compile(r"normal mpi teardown")


def _extract_mnubergemm(text: str, bag: _MetricBag) -> dict[str, Any]:
    """MNUBERGEMM soak.  Mirrors parser.sh ``mnubergemm_stats_tsv`` and adds the timeout rule.

    THE TIMEOUT RULE: mnubergemm runs for exactly ``--time_to_run`` seconds and is submitted with
    a walltime that is frequently shorter (mnubergemm.sbatch defaults ``--time_to_run 86400``;
    run_mnubergemm_stress.sh defaults ``RACK_MNUBERGEMM_TIME=02:30:00``).  Slurm therefore kills
    a healthy soak with ``DUE TO TIME LIMIT``.  That is the soak completing, not failing.
    """
    if not _MNUB_GATE_RE.search(text):
        return {}

    gflops = [v for v in (_to_num(m.group(1)) for m in _MNUB_GFLOPS_RE.finditer(text)) if v is not None]
    rank_samples = len(gflops)

    nodes: list[str] = []
    hm = _MNUB_HOSTLIST_RE.search(text)
    if hm:
        nodes = [h.strip() for h in hm.group(1).split(",") if h.strip()]

    qualified_rank_ids = {int(match.group(1)) for match in _MNUB_QUAL_OK_RANK_RE.finditer(text)}
    qualified_ranks = len(qualified_rank_ids)
    expected_ranks = len(nodes) * 4 if nodes else None

    if gflops:
        bag.add("mnubergemm_gflops_max", max(gflops), "GFLOP/s")
        bag.add("mnubergemm_gflops_median", statistics.median(gflops), "GFLOP/s")
        bag.add("mnubergemm_gflops_min", min(gflops), "GFLOP/s")
    if expected_ranks is not None:
        bag.add("mnubergemm_ranks", expected_ranks, None)
    bag.add("mnubergemm_rank_samples", rank_samples, None)
    bag.add("mnubergemm_qualified_ranks", qualified_ranks, None)
    if nodes:
        bag.add("mnubergemm_nodes", len(nodes), None)
        bag.add("mnubergemm_gpus", len(nodes) * 4, None)  # parser.sh:476: gpus = nodes * 4

    freqs = [v for v in (_to_num(m.group(1)) for m in _MNUB_FREQ_RE.finditer(text)) if v is not None]
    if freqs:
        bag.add("mnubergemm_freq_max", max(freqs), "Hz")
    duties = [v for v in (_to_num(m.group(1)) for m in _MNUB_DUTY_RE.finditer(text)) if v is not None]
    if duties:
        bag.add("mnubergemm_duty_max", max(duties), None)
    # `P:` is a per-rank field in the MNUB line.  parser.sh does not decode it and its unit is
    # NOT documented anywhere in this repo, so it is emitted raw with unit=None rather than
    # guessed at as watts.
    ps = [v for v in (_to_num(m.group(1)) for m in _MNUB_P_RE.finditer(text)) if v is not None]
    if ps:
        bag.add("mnubergemm_p_raw_max", max(ps), None, {"unit": "undocumented"})

    out: dict[str, Any] = {
        "extra": {
            "mnubergemm_rank_samples": rank_samples,
            "mnubergemm_qualified_ranks": qualified_ranks,
            "mnubergemm_expected_ranks": expected_ranks,
        }
    }
    if nodes:
        out["nodes"] = nodes
    if gflops:
        out["headline"] = {"label": "MNUBERGEMM peak GFLOP/s", "value": max(gflops), "unit": "GFLOP/s"}

    hard_fail = bool(_MNUB_FAIL_RE.search(text))
    timed_out = bool(_RE_TIME_LIMIT.search(text))
    positive_work = any(value > 0 for value in gflops)
    qual_ok = bool(_MNUB_QUAL_OK_RE.search(text))
    qualification_complete = expected_ranks is not None and qualified_ranks == expected_ranks
    normal_teardown = bool(_MNUB_NORMAL_TEARDOWN_RE.search(text))
    out["extra"].update({
        "mnubergemm_positive_work": positive_work,
        "mnubergemm_qual_ok": qual_ok,
        "mnubergemm_qualification_complete": qualification_complete,
        "mnubergemm_normal_teardown": normal_teardown,
    })
    if hard_fail:
        out["status"] = "fail"
    elif timed_out:
        # walltime kill on a soak == expected end, provided the soak actually produced work
        out["status"] = "timeout" if positive_work else "fail"
        if not positive_work:
            out["errors"] = [{"kind": "mnubergemm_no_work",
                              "line": "job hit the walltime without producing positive GFlops work"}]
    elif normal_teardown:
        if positive_work and qual_ok and qualification_complete:
            out["status"] = "pass"
        else:
            out["status"] = "fail"
            kind = (
                "mnubergemm_no_positive_work"
                if not positive_work
                else "mnubergemm_qualification_incomplete"
            )
            line = (
                "MNUberGEMM completed with no positive GFlops work"
                if not positive_work
                else (
                    "MNUberGEMM completed with "
                    f"{qualified_ranks}/{expected_ranks or '?'} qualified ranks"
                )
            )
            out["errors"] = [{"kind": kind, "line": line}]
    elif positive_work:
        out["status_hint_pass"] = True
    else:
        out["status"] = "unknown"
    return out


# ---- NeMo / Nemotron ------------------------------------------------------------------
_NEMO_ITER_RE = re.compile(r"iteration\s+(\d+)/\s*(\d+)")
_NEMO_STEP_TIMING_RE = re.compile(r"train_step_timing in s:\s*([\d.]+)")
_NEMO_TFLOP_RE = re.compile(r"(?:MODEL_TFLOP/s/GPU[:=]?\s*([\d.]+)|([\d.]+)\s*MODEL_TFLOP/s/GPU)")
_NEMO_FATAL_RE = re.compile(
    r"Traceback \(most recent call last\)|torch\.cuda\.OutOfMemoryError|torch\.OutOfMemoryError"
    r"|CUDA out of memory|CudaOutOfMemoryError"
)


def _extract_nemo(text: str, bag: _MetricBag) -> dict[str, Any]:
    """NeMo/Nemotron training stdout.

    Uses SELF-DESCRIBING regexes (``train_step_timing in s: <v>``, ``MODEL_TFLOP/s/GPU``) rather
    than parser.sh's awk column indices, because the exact column layout of the ``Step Time``
    dialect was never sampled on this cluster.  parser.sh stays authoritative for the
    Nemotron table in the Parser Results dialog; this is for history/summary only.
    Completion test is rack_status.sh's: last ``iteration <cur>/<tot>`` with cur>=tot, i.e. the
    run reached the target it declared.  ``NEMO_TARGET_STEPS`` is an OPT-IN acceptance floor on
    that declared total; unset it accepts a 50-step benchmark as readily as a 2010-step soak.
    """
    steps = [v for v in (_to_num(m.group(1)) for m in _NEMO_STEP_TIMING_RE.finditer(text)) if v is not None]
    tflops: list[float] = []
    for m in _NEMO_TFLOP_RE.finditer(text):
        v = _to_num(m.group(1) or m.group(2))
        if v is not None:
            tflops.append(float(v))

    last_iter: tuple[int, int] | None = None
    for m in _NEMO_ITER_RE.finditer(text):
        try:
            last_iter = (int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue

    if steps:
        bag.add("nemo_step_time_mean", round(statistics.fmean(steps), 4), "s")
        bag.add("nemo_step_time_last", steps[-1], "s")
        if len(steps) > 1:
            bag.add("nemo_step_time_stdev", round(statistics.pstdev(steps), 5), "s")
    if tflops:
        bag.add("nemo_model_tflops_per_gpu_mean", round(statistics.fmean(tflops), 2), "TFLOP/s")
        bag.add("nemo_model_tflops_per_gpu_last", tflops[-1], "TFLOP/s")
    if last_iter:
        bag.add("nemo_iteration", last_iter[0], None, {"total": last_iter[1]})
        bag.add("nemo_iteration_total", last_iter[1], None)

    out: dict[str, Any] = {}
    if tflops:
        out["headline"] = {"label": "NeMo MODEL_TFLOP/s/GPU (mean)",
                           "value": round(statistics.fmean(tflops), 2), "unit": "TFLOP/s"}
    elif steps:
        out["headline"] = {"label": "NeMo step time (mean)",
                           "value": round(statistics.fmean(steps), 4), "unit": "s"}

    try:
        target = int(os.environ.get("NEMO_TARGET_STEPS", "0"))
    except ValueError:
        target = 0
    finished = bool(
        last_iter and last_iter[1] > 0 and last_iter[0] >= last_iter[1] and last_iter[1] >= target
    )
    out["extra"] = {"nemo_training_finished": finished,
                    "nemo_last_iteration": list(last_iter) if last_iter else None}

    if _NEMO_FATAL_RE.search(text):
        out["status"] = "fail"
    elif finished and (steps or tflops):
        # reaching the target step count IS the terminal verdict (rack_status.sh's own rule)
        out["status"] = "pass"
    elif steps or tflops:
        out["status"] = "running"  # producing steps but never reached the target step count
    return out


# ---- nvloom ---------------------------------------------------------------------------
_NVLOOM_MIN_RE = re.compile(r"^\s*(?:\d+:\s*)?Min\s+(\S+)\s+([\d.eE+-]+)\s+(\S+)", re.MULTILINE)
_NVLOOM_PROCESS_RE = re.compile(r"^\s*(?:\d+:\s*)?Process\s+\d+\s+\(([^)]+)\)", re.MULTILINE)
_NVLOOM_RUNNING_RE = re.compile(r"^\s*(?:\d+:\s*)?Running\s+(\S+)\s*$", re.MULTILINE)


def _extract_nvloom(text: str, bag: _MetricBag) -> dict[str, Any]:
    """nvloom_cli.  Grammar per frontend/src/lib/nvloomHeatmapParse.ts (the in-repo port of
    nvloom's own plot_heatmaps.py): ``Process N (host): device D: … rackGuid: G``,
    ``Running <testcase>``, matrix rows, then ``Min <testcase> <value> <unit>``.
    """
    testcases = [m.group(1) for m in _NVLOOM_RUNNING_RE.finditer(text)]
    mins: list[tuple[str, float, str]] = []
    for m in _NVLOOM_MIN_RE.finditer(text):
        v = _to_num(m.group(2))
        if v is None:
            continue
        unit = _canon_unit(m.group(3)) or "GB/s"
        mins.append((m.group(1), float(v), unit))
        bag.add("nvloom_min_bandwidth", v, unit, {"testcase": m.group(1)})

    hosts: list[str] = []
    seen: set[str] = set()
    gpu_procs = 0
    for m in _NVLOOM_PROCESS_RE.finditer(text):
        gpu_procs += 1
        host = m.group(1).split(".")[0].strip()
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    if gpu_procs:
        bag.add("nvloom_gpu_processes", gpu_procs, None)
    if testcases:
        bag.add("nvloom_testcases", len(testcases), None)

    out: dict[str, Any] = {"extra": {"nvloom_testcases": testcases[:64]}}
    if hosts:
        out["nodes"] = hosts
    if mins:
        worst = min(mins, key=lambda t: t[1])
        out["headline"] = {"label": f"nvloom worst Min ({worst[0]})", "value": worst[1], "unit": worst[2]}
        out["status_hint_pass"] = True
    if "WAIVED" in text:
        out["warnings"] = [{"kind": "nvloom_waived", "line": "one or more nvloom testcases reported WAIVED"}]
    return out


# ---- ClusterKit -----------------------------------------------------------------------
_CK_BANNER_RE = re.compile(r"^###\s*ClusterKit\s+rack=(\S+)\s+link_tech=(\S+)", re.MULTILINE)
_CK_OK_RE = re.compile(r"^OK:\s*clusterkit finished on\s+(\S+)", re.MULTILINE)
_CK_ERR_RE = re.compile(r"^ERROR:\s*clusterkit\.sh failed for HCA .*?exit\s+(\d+)", re.MULTILINE)


def _extract_clusterkit(text: str, bag: _MetricBag) -> dict[str, Any]:
    """ClusterKit sbatch stdout (clusterkit.sbatch:129-171).

    The numeric results live in ``bandwidth.json`` under ``log_dir/clusterkit/<rack>/`` and are
    handled by /api/clusterkit-results — this extractor only classifies the run.
    """
    out: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    m = _CK_BANNER_RE.search(text)
    if m:
        extra["clusterkit_rack"] = m.group(1)
        extra["clusterkit_link_tech"] = m.group(2)
    errs = _CK_ERR_RE.findall(text)
    if errs:
        bag.add("clusterkit_hca_failures", len(errs), None)
        out["status"] = "fail"
    ok = _CK_OK_RE.search(text)
    if ok:
        extra["clusterkit_nodelist"] = ok.group(1)
        out.setdefault("nodes", expand_nodelist(ok.group(1)))
        if not errs:
            out["status_hint_pass"] = True
    if extra:
        out["extra"] = extra
    return out


# ---- bringup / health check (checker.sh, sysinfo.sh, pdsh hw check) ---------------------
_HC_VERDICT_RE = re.compile(r"^\s*(?:(\S+):\s*)?INFO:\s*Health check (PASSED|FAILED)", re.MULTILINE)
_HC_REASON_RE = re.compile(r"reason=\"?([^\"\n]+)\"?")


def _extract_bringup(text: str, bag: _MetricBag) -> dict[str, Any]:
    """checker.sh health check, optionally pdsh-merged (``<host>: INFO: …``).

    checker.sh:645-647 prints exactly one verdict line per node.  A pdsh fan-out therefore
    yields one verdict per tray and the run is only a pass when EVERY tray passed — a single
    FAILED must not be averaged away.
    """
    verdicts: list[tuple[str | None, bool]] = []
    for m in _HC_VERDICT_RE.finditer(text):
        verdicts.append((m.group(1), m.group(2).upper() == "PASSED"))
    if not verdicts:
        return {}

    n_pass = sum(1 for _, ok in verdicts if ok)
    n_fail = len(verdicts) - n_pass
    bag.add("healthcheck_nodes_total", len(verdicts), None)
    bag.add("healthcheck_nodes_passed", n_pass, None)
    bag.add("healthcheck_nodes_failed", n_fail, None)
    for host, ok in verdicts:
        if host:
            bag.add("healthcheck_pass", 1 if ok else 0, None, {"node": host})

    reasons = sorted({r.strip() for r in _HC_REASON_RE.findall(text) if r.strip()})
    out: dict[str, Any] = {
        "headline": {"label": "Health check nodes passed",
                     "value": f"{n_pass}/{len(verdicts)}", "unit": None},
        "extra": {"healthcheck_reasons": reasons[:32]},
    }
    hosts = [h for h, _ in verdicts if h]
    if hosts:
        out["nodes"] = hosts
    # checker.sh prints its verdict last, after every check has run: it IS terminal.
    out["status"] = "fail" if n_fail else "pass"
    return out


# ---- nvbandwidth ----------------------------------------------------------------------
_NVB_SUM_RE = re.compile(r"^\s*SUM\s+(\S+)\s+([\d.eE+-]+)\s*$", re.MULTILINE)
_NVB_TEST_RE = re.compile(r"INFO:\s*nvbandwidth -t\s+(\S+)")
_NVB_FAIL_RE = re.compile(r"ERROR:\s*nvbandwidth (\S+) failed \(rc=(\d+)\)")
_NVB_SKIP_RE = re.compile(r"nvbandwidth SKIPPED")


def _extract_nvbandwidth(text: str, bag: _MetricBag) -> dict[str, Any]:
    """nvbandwidth.  ``SUM <test> <value>`` is the tool's own per-test summary line; the
    ``INFO:``/``ERROR:``/``SKIPPED`` wrappers come from checker.sh:402-465.
    """
    out: dict[str, Any] = {}
    sums: list[tuple[str, float]] = []
    for m in _NVB_SUM_RE.finditer(text):
        v = _to_num(m.group(2))
        if v is None:
            continue
        sums.append((m.group(1), float(v)))
        bag.add("nvbandwidth_sum", v, "GB/s", {"test": m.group(1)})

    tests = _NVB_TEST_RE.findall(text)
    if tests:
        bag.add("nvbandwidth_tests_run", len(tests), None)
    fails = _NVB_FAIL_RE.findall(text)
    if fails:
        bag.add("nvbandwidth_tests_failed", len(fails), None)
        out["status"] = "fail"
        out["errors"] = [{"kind": "nvbandwidth_fail", "line": f"nvbandwidth {t} failed (rc={rc})"}
                         for t, rc in fails[:MAX_ERRORS]]
    if _NVB_SKIP_RE.search(text):
        # checker.sh is explicit that a SKIPPED nvbandwidth must never read as a pass.
        out.setdefault("warnings", []).append(
            {"kind": "nvbandwidth_skipped",
             "line": "nvbandwidth SKIPPED — this node was NOT bandwidth-tested"})
        out.setdefault("status", "unknown")
    if sums:
        best = max(sums, key=lambda t: t[1])
        out["headline"] = {"label": f"nvbandwidth {best[0]}", "value": best[1], "unit": "GB/s"}
        if not fails:
            out["status_hint_pass"] = True
    return out


# ---- DCGM diag ------------------------------------------------------------------------
_DCGM_ROW_RE = re.compile(r"^\s*\|\s*([^|]{2,40}?)\s*\|\s*(Pass|Fail|Skip|Warn|Skipped|Warning)\b", re.MULTILINE)
_DCGM_WRAP_PASS_RE = re.compile(r"INFO:\s*dcgmi diag -r \S+ PASSED")
_DCGM_WRAP_FAIL_RE = re.compile(r"ERROR:\s*dcgmi diag -r \S+ reported failures")
_DCGM_MISSING_RE = re.compile(r"WARN:\s*dcgmi not found")


def _extract_dcgm(text: str, bag: _MetricBag) -> dict[str, Any]:
    """``dcgmi diag`` output plus checker.sh's wrapper verdict (checker.sh:380-400).

    The wrapper verdict wins when present: checker.sh greps the diag output itself and is the
    thing that drains the node.
    """
    out: dict[str, Any] = {}
    counts = {"pass": 0, "fail": 0, "skip": 0, "warn": 0}
    for m in _DCGM_ROW_RE.finditer(text):
        r = m.group(2).lower()
        if r.startswith("pass"):
            counts["pass"] += 1
        elif r.startswith("fail"):
            counts["fail"] += 1
            bag.add("dcgm_diag_check_failed", 1, None, {"check": _snake(m.group(1))})
        elif r.startswith("skip"):
            counts["skip"] += 1
        else:
            counts["warn"] += 1
    if any(counts.values()):
        for k, v in counts.items():
            bag.add(f"dcgm_diag_{k}_count", v, None)
        out["headline"] = {"label": "DCGM diag checks passed",
                           "value": f"{counts['pass']}/{sum(counts.values())}", "unit": None}

    if _DCGM_MISSING_RE.search(text):
        out.setdefault("warnings", []).append(
            {"kind": "dcgm_missing", "line": "dcgmi not found on node — DCGM diag was skipped"})
        out.setdefault("status", "unknown")
    elif _DCGM_WRAP_FAIL_RE.search(text) or counts["fail"]:
        out["status"] = "fail"
    elif _DCGM_WRAP_PASS_RE.search(text):
        out["status"] = "pass"  # checker.sh's own post-diag verdict — terminal
    elif counts["pass"]:
        out["status_hint_pass"] = True  # diag rows only; wait for an end marker
    return out


# ---- HPL ------------------------------------------------------------------------------
_HPL_ROW_RE = re.compile(
    r"^(W[RC]\w+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s*$", re.MULTILINE
)
_HPL_RESID_RE = re.compile(r"\|\|Ax-b\|\|.*?=\s*([\d.eE+-]+)\s*\.*\s*(PASSED|FAILED)", re.IGNORECASE | re.DOTALL)


def _extract_hpl(text: str, bag: _MetricBag) -> dict[str, Any]:
    """netlib HPL.  ``T/V N NB P Q Time Gflops`` result rows + the residual PASSED/FAILED line.

    NOT sampled on this cluster (no HPL log exists in /home/cmods/benchmark/harish today).
    Built from HPL's stock output format; if the format differs the rows simply do not match
    and the generic fallback carries the log.
    """
    out: dict[str, Any] = {}
    best: tuple[str, float, float] | None = None
    for m in _HPL_ROW_RE.finditer(text):
        t = _to_num(m.group(6))
        g = _to_num(m.group(7))
        if g is None:
            continue
        bag.add("hpl_gflops", g, "GFLOP/s", {"variant": m.group(1), "n": m.group(2), "nb": m.group(3),
                                             "p": m.group(4), "q": m.group(5)})
        if t is not None:
            bag.add("hpl_time", t, "s", {"variant": m.group(1), "n": m.group(2)})
        if best is None or float(g) > best[1]:
            best = (m.group(1), float(g), float(t) if t is not None else 0.0)
    resid = _HPL_RESID_RE.search(text)
    if resid:
        v = _to_num(resid.group(1))
        if v is not None:
            bag.add("hpl_residual", v, None)
        # the residual line is HPL's own verdict and only prints at the end of the run
        if resid.group(2).upper() == "FAILED":
            out["status"] = "fail"
            out["errors"] = [{"kind": "hpl_residual_failed", "line": resid.group(0)[:MAX_LINE_CHARS]}]
        else:
            out["status"] = "pass"
    if best:
        out["headline"] = {"label": "HPL Gflops", "value": best[1], "unit": "GFLOP/s"}
    return out


_EXTRACTORS = {
    "nccl": _extract_nccl,
    "nemo": _extract_nemo,
    "mnubergemm": _extract_mnubergemm,
    "nvloom": _extract_nvloom,
    "clusterkit": _extract_clusterkit,
    "bringup": _extract_bringup,
    "nvbandwidth": _extract_nvbandwidth,
    "dcgm": _extract_dcgm,
    "hpl": _extract_hpl,
}


# --------------------------------------------------------------------------------------
# result shell
# --------------------------------------------------------------------------------------
def empty_result(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """The canonical shape, all fields present.  Every code path returns this shape."""
    return {
        "kind": "unknown",
        "status": "unknown",
        "started_at": None,
        "ended_at": None,
        "rack": None,
        "nodes": [],
        "job_id": None,
        "metrics": [],
        "headline": {"label": None, "value": None, "unit": None},
        "errors": [],
        "warnings": [],
        "tail": "",
        # ---- additive, non-breaking context ----
        "path": str(path) if path else None,
        "log_basename": _basename(path),
        "bytes": None,
        "truncated": False,
        "exit_code": None,
        "racks": [],
        "parser_version": PARSER_VERSION,
        "extra": {},
    }


# A line that labels itself a warning is a warning, even when it quotes scary words.
# Real case: ``NCCL WARN Cuda failure 'out of memory' on rank 3, retrying`` — the job recovered
# and exited 0.  Treating that as fatal would mark a healthy run as failed.
_SELF_LABELLED_WARN_RE = re.compile(r"\bWARN(?:ING)?\b")
_HARD_TOKEN_RE = re.compile(r"\bERROR\b|\bFATAL\b|Traceback|core dumped|\bFAILED\b")
_NEVER_DEMOTE = {"traceback", "abort", "slurm_error", "xid", "ecc", "read_error", "extractor_error"}


def _demote_self_labelled_warnings(
    errors: list[dict[str, str]], warnings: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Move ``… WARN …`` lines out of ``errors`` and into ``warnings``.  Returns the new errors."""
    kept: list[dict[str, str]] = []
    for e in errors:
        line = e.get("line", "")
        if (
            e.get("kind") not in _NEVER_DEMOTE
            and _SELF_LABELLED_WARN_RE.search(line)
            and not _HARD_TOKEN_RE.search(line)
        ):
            if len(warnings) < MAX_WARNINGS and not any(w.get("line") == line for w in warnings):
                warnings.append({"kind": f"{e.get('kind')}_warn", "line": line})
            continue
        kept.append(e)
    return kept


def _merge_marker_lists(dst: list[dict[str, str]], src: Iterable[dict[str, str]], cap: int) -> None:
    seen = {(d.get("kind"), d.get("line")) for d in dst}
    for item in src:
        key = (item.get("kind"), item.get("line"))
        if key in seen:
            continue
        seen.add(key)
        dst.append(item)
        if len(dst) >= cap:
            return


# --------------------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------------------
def parse(
    path: str | os.PathLike[str] | None,
    text: str | None = None,
    *,
    kind: str | None = None,
    running_hint: bool | None = None,
    meta: dict[str, Any] | None = None,
    head_bytes: int = DEFAULT_HEAD_BYTES,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> dict[str, Any]:
    """Normalise ANY log into one shape.  Never returns empty, never raises.

    Args:
      path:         path the log came from (used for filename-based kind/rack/job hints; may be a
                    remote path string that does not exist locally when ``text`` is supplied).
      text:         log content.  ``None`` => read it from ``path``, capped (head+tail).
      kind:         force a kind, skipping ``sniff_kind`` (for callers that already classified).
      running_hint: ``True`` when the caller knows from squeue that the job is still in the
                    queue.  Overrides the terminal-marker heuristic and yields ``"running"``.
      meta:         reader metadata from a previous ``read_capped`` call (bytes/truncated/…).

    Returns the dict documented in ``empty_result()``.

    Status contract:
      ``pass``     positive evidence of successful completion (exit 0 / explicit PASSED /
                   real measurements plus a clean terminal marker).
      ``fail``     hard error, non-zero exit, empty log, or header-only log.
      ``timeout``  killed by the Slurm walltime with no hard error — for a soak this is the
                   expected end, so it is NEVER folded into ``fail``.
      ``running``  no terminal marker and no error: the job is still writing.
      ``unknown``  nothing conclusive.  Never used to mask a failure.
    """
    res = empty_result(path)

    if text is None:
        if not path:
            res["status"] = "fail"
            res["errors"] = [{"kind": "no_input", "line": "parse() called with neither path nor text"}]
            return res
        text, rmeta = read_capped(path, head_bytes=head_bytes, tail_bytes=tail_bytes)
        meta = rmeta if meta is None else {**rmeta, **meta}

    meta = meta or {}
    res["bytes"] = meta.get("bytes") if meta.get("bytes") is not None else len(text.encode("utf-8", "replace"))
    res["truncated"] = bool(meta.get("truncated"))
    if meta.get("read_error"):
        res["errors"].append({"kind": "read_error", "line": str(meta["read_error"])})
    gz_tail_incomplete = bool(meta.get("gz_tail_incomplete"))

    basename = res["log_basename"]

    # ---- trap 1: empty / unreadable => fail, never pass -------------------------------
    if not text or not text.strip():
        res["kind"] = kind if kind in KINDS else sniff_kind(path, "")
        res["status"] = "fail"
        size = res["bytes"]
        if meta.get("read_error"):
            res["errors"].append({"kind": "empty_log",
                                  "line": f"log could not be read ({meta['read_error']}) — treated as a failure"})
        else:
            res["errors"].append({"kind": "empty_log",
                                  "line": f"log is empty ({size} bytes) — the job produced no output; "
                                          f"this is a launch failure, not a pass"})
        res["job_id"] = _scan_job_id(basename, "")
        rk = _rack_from_basename(basename)
        if rk:
            res["rack"] = rk
            res["racks"] = [rk]
        return res

    res["tail"] = text[-TAIL_CHARS:].strip()

    # ---- classify ----------------------------------------------------------------------
    res["kind"] = kind if kind in KINDS else sniff_kind(path, text[:SNIFF_BYTES])

    # ---- generic layer (always runs, for every kind) ------------------------------------
    bag = _MetricBag()
    started, ended = _scan_timestamps(text)
    res["started_at"] = started
    res["ended_at"] = ended
    res["job_id"] = _scan_job_id(basename, text)

    nodes = _nodes_from_text(text)
    racks = _find_racks(basename) or _find_racks(text)
    fname_rack = _rack_from_basename(basename)
    if fname_rack:
        racks = [fname_rack] + [r for r in racks if r.casefold() != fname_rack.casefold()]
    res["racks"] = racks[:64]
    if fname_rack:
        res["rack"] = fname_rack
    elif len(racks) == 1:
        res["rack"] = racks[0]
    elif racks and nodes:
        # unambiguous when every node belongs to the same rack
        node_racks = {r.casefold() for n in nodes for r in _find_racks(n)}
        if len(node_racks) == 1:
            res["rack"] = racks[0]

    exit_code, terminal, term_kind = _scan_terminal(text)
    res["exit_code"] = exit_code

    errors = _collect_lines(text, _ERROR_PATTERNS, MAX_ERRORS)
    warnings = _collect_lines(text, _WARNING_PATTERNS, MAX_WARNINGS)

    # ---- per-kind layer ------------------------------------------------------------------
    kx = _EXTRACTORS.get(res["kind"])
    over: dict[str, Any] = {}
    if kx is not None:
        try:
            over = kx(text, bag) or {}
        except Exception as exc:  # noqa: BLE001 - an extractor bug must never lose the log
            over = {}
            errors.insert(0, {"kind": "extractor_error",
                              "line": f"{res['kind']} extractor raised {type(exc).__name__}: {exc}"})

    # ---- generic key/value harvest -------------------------------------------------------
    # Runs for EVERY kind, after the per-kind pass, so a tool's own summary lines
    # ("Avg latency: 3.2 us") are captured even when a dedicated extractor exists.
    # Partial lines are dropped first: half a number is a wrong number.
    _scan_kv_metrics(_drop_partial_lines(text), bag)

    if over.get("nodes"):
        nodes = _dedupe(list(over["nodes"]) + nodes)
    res["nodes"] = nodes[:MAX_NODES]
    if over.get("errors"):
        _merge_marker_lists(errors, over["errors"], MAX_ERRORS)
    if over.get("warnings"):
        _merge_marker_lists(warnings, over["warnings"], MAX_WARNINGS)
    if over.get("headline"):
        res["headline"] = over["headline"]
    if over.get("extra"):
        res["extra"].update(over["extra"])

    errors = _demote_self_labelled_warnings(errors, warnings)

    if gz_tail_incomplete:
        # The gzip read stopped before EOF, so the end of the job was never seen.  Without this
        # the missing exit/kill line would be read as "the job is still running".
        warnings.append({
            "kind": "gz_tail_incomplete",
            "line": "gzip read stopped before end of file (byte ceiling or time budget) — "
                    "the end of this log was NOT examined; status is inconclusive",
        })
        res["extra"]["gz_tail_incomplete"] = True

    res["metrics"] = bag.as_list()
    res["errors"] = errors
    res["warnings"] = warnings

    # ---- status ---------------------------------------------------------------------------
    res["status"] = _decide_status(
        text=text,
        res=res,
        over=over,
        errors=errors,
        exit_code=exit_code,
        terminal=terminal,
        term_kind=term_kind,
        running_hint=running_hint,
    )
    if gz_tail_incomplete and res["status"] == "running":
        # "running" would be a lie: we simply never reached the end of the stream.
        res["status"] = "unknown"

    # ---- headline of last resort ------------------------------------------------------------
    if res["headline"].get("value") is None and res["metrics"]:
        pick = _pick_headline(res["metrics"])
        if pick:
            res["headline"] = pick

    res["extra"].setdefault("terminal_marker", term_kind)
    res["extra"].setdefault("metric_count", len(res["metrics"]))
    if res["truncated"]:
        # Counting metrics (mnubergemm_ranks, nvloom_gpu_processes, healthcheck_nodes_total …)
        # are counts over the CAPPED window, not over the whole file.  Say so rather than let a
        # downstream chart present a windowed count as a total.
        res["extra"]["metrics_from_truncated_read"] = True
    return res


def parse_path(path: str | os.PathLike[str], **kwargs: Any) -> dict[str, Any]:
    """Convenience: ``read_capped`` + ``parse``."""
    text, meta = read_capped(
        path,
        head_bytes=kwargs.pop("head_bytes", DEFAULT_HEAD_BYTES),
        tail_bytes=kwargs.pop("tail_bytes", DEFAULT_TAIL_BYTES),
    )
    return parse(path, text, meta=meta, **kwargs)


# --------------------------------------------------------------------------------------
# status decision + helpers
# --------------------------------------------------------------------------------------
_FATAL_ERROR_KINDS = {"traceback", "oom", "cuda_error", "xid", "ecc", "nccl_error", "abort",
                      "slurm_error", "container_error", "read_error", "extractor_error"}
# Explicit "the wrapper finished cleanly" markers seen in this repo's drivers.
_SUCCESS_MARKERS = (
    re.compile(r"^\s*OK:\s+\S+ finished", re.MULTILINE),
    re.compile(r"INFO:\s*Health check PASSED"),
    re.compile(r"^nccl_allreduce:\s*exit 0", re.MULTILINE),
    re.compile(r"^\s*=+\s*PASSED\s*=+", re.MULTILINE),
)
# Something that proves the job actually reached an end, whatever the outcome.
_TERMINAL_MARKERS = (
    ("time_limit", _RE_TIME_LIMIT),
    ("node_failure", _RE_NODE_FAILURE),
    ("preemption", _RE_PREEMPTION),
    ("cancelled", _RE_CANCELLED),
    ("srun_exited", _RE_SRUN_EXITED),
    ("epilog", re.compile(r"slurmstepd:.*(?:done|Exceeded|task.*completed)", re.IGNORECASE)),
)


def _scan_terminal(text: str) -> tuple[int | None, bool, str | None]:
    """Return ``(exit_code, saw_terminal_marker, terminal_marker_kind)``."""
    code: int | None = None
    m = _RE_SRUN_EXITED.search(text)
    if m:
        code = int(m.group(1))
    if code is None:
        for rx in (_RE_EXIT_CODE, _RE_RC):
            mm = None
            for mm in rx.finditer(text):
                pass  # last occurrence wins
            if mm:
                try:
                    code = int(mm.group(1))
                except ValueError:
                    code = None
                if code is not None:
                    break
    for name, rx in _TERMINAL_MARKERS:
        if rx.search(text):
            return code, True, name
    for rx in _SUCCESS_MARKERS:
        if rx.search(text):
            return code, True, "success_marker"
    if code is not None:
        return code, True, "exit_code"
    return code, False, None


_HEADER_ONLY_MAX_LINES = _env_int("NVIS_UP_HEADER_ONLY_MAX_LINES", 8)


def _looks_header_only(
    text: str, res: dict[str, Any], over: dict[str, Any], term_kind: str | None
) -> bool:
    """True for the 'Slurm wrote a file for a job that died at launch' case.

    The real examples on this cluster are 182 and 197 bytes: the mnubergemm.sbatch banner
    (``#######``/``HOSTLIST:``/``Image:``) and nothing else, plus at most one ``ERROR:`` line.

    Guarded three ways so a legitimately terse-but-successful log is not condemned:
      * a driver that printed its own success line (``OK: clusterkit finished on …``) is out;
      * a kind extractor that found results is out;
      * ANY numeric metric at all means the job produced output, so it is out.
    """
    if term_kind == "success_marker":
        return False
    if over.get("status") == "pass" or over.get("status_hint_pass"):
        return False
    if res.get("metrics"):
        return False
    if res.get("headline", {}).get("value") is not None:
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    substantive = [ln for ln in lines if not set(ln) <= set("#=-* ")]
    return len(substantive) <= _HEADER_ONLY_MAX_LINES


def _decide_status(
    *,
    text: str,
    res: dict[str, Any],
    over: dict[str, Any],
    errors: list[dict[str, str]],
    exit_code: int | None,
    terminal: bool,
    term_kind: str | None,
    running_hint: bool | None,
) -> str:
    """The single place a status is chosen.  Ordered most-certain first."""
    # 1. an extractor that understands the format has the final say on fail/timeout
    forced = over.get("status")
    if forced in ("fail", "timeout"):
        return forced

    # 2. hard runtime errors
    fatal = [e for e in errors if e.get("kind") in _FATAL_ERROR_KINDS]
    if fatal:
        # ...unless the ONLY hard signal is the walltime kill (Slurm logs that as
        # "slurmstepd: error: *** JOB … CANCELLED AT … DUE TO TIME LIMIT ***", which matches
        # the generic slurm_error pattern).  A soak that ran to walltime is a timeout.
        non_timeout = [e for e in fatal if not _RE_TIME_LIMIT.search(e.get("line", ""))]
        if non_timeout:
            return "fail"

    # 3. walltime kill with no other hard error
    if _RE_TIME_LIMIT.search(text):
        return "timeout"
    if _RE_NODE_FAILURE.search(text) or _RE_PREEMPTION.search(text):
        return "fail"

    # 4. non-zero exit
    if exit_code is not None and exit_code != 0:
        return "fail"

    # 5. the caller (squeue) knows the job is still in flight — beats every heuristic below
    if running_hint:
        return "running"

    # 6. an extractor saw an EXPLICIT success verdict (HPL residual PASSED, every node's
    #    "Health check PASSED", dcgmi diag PASSED, NeMo reaching its target step count).
    #    That is itself terminal, so it does not need a separate end marker.
    if over.get("status") == "pass":
        return "pass"

    # 7. header-only / died-at-launch logs are a FAILURE, never a pass.
    #    Gated on the file actually looking like a job log (a job id, a node, or a recognised
    #    tool): a stray non-log file in the directory should read "unknown", not "failed job".
    looks_like_a_job = bool(res.get("job_id") or res.get("nodes") or res.get("kind") != "unknown")
    if looks_like_a_job and _looks_header_only(text, res, over, term_kind):
        errors.append({
            "kind": "header_only",
            "line": "log contains only a header and no job output — the job died at launch",
        })
        return "fail"

    # 8. weaker evidence: the extractor found real measurements.  Only a pass once the job also
    #    reached an end; otherwise it is a log that is still being written.
    if over.get("status_hint_pass"):
        if terminal or exit_code == 0:
            return "pass"
        return "running"
    for rx in _SUCCESS_MARKERS:
        if rx.search(text):
            return "pass"
    if exit_code == 0 and terminal:
        return "pass"

    # 9. an extractor explicitly abstained
    if over.get("status") in ("unknown", "running"):
        return over["status"]

    # 10. nothing recognisable at all — no tool, no job, no numbers, no markers.  Say so
    #     instead of guessing "running" (this is the stray-file-in-the-log-dir case).
    if not (res.get("metrics") or res.get("job_id") or res.get("nodes")) and not terminal and not errors:
        return "unknown"

    # 11. no terminal marker and no errors: the job is very likely still writing
    if not terminal and not errors:
        return "running"
    return "unknown"


_HEADLINE_UNIT_PRIORITY = ("GB/s", "GiB/s", "TB/s", "TFLOP/s", "GFLOP/s", "tokens/s", "samples/s", "s")


def _pick_headline(metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best-effort headline for a log with no dedicated extractor.

    Prefers a throughput unit; falls back to the first numeric metric.  Never fabricates a
    label — the label is the metric's own name from the log.
    """
    for unit in _HEADLINE_UNIT_PRIORITY:
        for m in metrics:
            if m.get("unit") == unit and isinstance(m.get("value"), (int, float)):
                return {"label": m["name"], "value": m["value"], "unit": unit}
    for m in metrics:
        if isinstance(m.get("value"), (int, float)):
            return {"label": m["name"], "value": m["value"], "unit": m.get("unit")}
    return None


_NODELIST_RES = (
    re.compile(r"^HOSTLIST:\s*(.+)$", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?SLURM_(?:JOB_)?NODELIST=(\S+)", re.MULTILINE),
    re.compile(r"^\s*NODELIST=(\S+)", re.MULTILINE),
    re.compile(r"\bnodelist=(\S+)", re.IGNORECASE),
)


def _nodes_from_text(text: str) -> list[str]:
    """Node list: explicit HOSTLIST/NODELIST first, then any hostname token in the text."""
    out: list[str] = []
    for rx in _NODELIST_RES:
        for m in rx.finditer(text):
            spec = m.group(1).strip().strip("'\"")
            if "," in spec and "[" not in spec:
                out.extend(h.strip() for h in spec.split(",") if h.strip())
            else:
                out.extend(expand_nodelist(spec))
            if len(out) >= MAX_NODES:
                return _dedupe(out)[:MAX_NODES]
    if out:
        return _dedupe(out)[:MAX_NODES]
    return _find_nodes(text)[:MAX_NODES]


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        k = i.casefold()
        if i and k not in seen:
            seen.add(k)
            out.append(i)
    return out


def _rack_from_basename(basename: str) -> str | None:
    """Rack id from a log basename, requiring EXACTLY ONE match.

    Same rule as nvis_id_templates.rack_id_from_log_basename (two rack ids in a basename means
    a multi-rack sweep and must not be attributed to either one).
    """
    if not basename:
        return None
    racks = _find_racks(basename)
    return racks[0] if len(racks) == 1 else None


# --------------------------------------------------------------------------------------
# CLI: ``python3 universal_parser.py <log> [...]`` — prints the normalised JSON.
# Handy for eyeballing a new tool's log before wiring it into an endpoint.  Import-safe.
# --------------------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Normalise any NVIS validation log to one JSON shape.")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--sniff-only", action="store_true", help="print only <kind> <path>")
    ap.add_argument("--compact", action="store_true", help="one line per log")
    args = ap.parse_args()

    for _p in args.paths:
        if args.sniff_only:
            _txt, _ = read_capped(_p, head_bytes=SNIFF_BYTES, tail_bytes=0)
            print(f"{sniff_kind(_p, _txt)}\t{_p}")
            continue
        _r = parse_path(_p)
        if args.compact:
            print(
                "{kind}\t{status}\t{rack}\t{job}\t{n} metrics\t{head}\t{path}".format(
                    kind=_r["kind"], status=_r["status"], rack=_r["rack"] or "-",
                    job=_r["job_id"] or "-", n=len(_r["metrics"]),
                    head=_r["headline"].get("value"), path=_p,
                )
            )
        else:
            print(json.dumps(_r, indent=2, default=str))
