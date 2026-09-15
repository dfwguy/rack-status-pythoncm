# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Durable storage for generated reports.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A report costs a fleet-wide sweep -- minutes of SSH across two thousand trays -- and until now
the finished document lived only in the job table, freed the moment it was collected. Navigating
away from Analytics before it finished therefore threw the whole sweep away, with nothing to show
for it and no way to get it back except paying for another one.

Reports are now written to disk as they complete, whether or not anyone is still watching. The
worker saves unconditionally, so closing the tab is no longer destructive: the document turns up
under Reports afterwards.

Layout: one payload file plus a JSON sidecar per report, under ``var/reports/``.

    var/reports/<id>.<ext>       the document itself (CSV / JSON / PDF bytes)
    var/reports/<id>.json        metadata: kind, format, filename, size, when, cluster, nodes

Two files rather than one blob with an embedded header, because the payload is served straight to
the browser as bytes -- a PDF has to leave here byte-identical to what reportlab produced. Listing
reads only the sidecars, so enumerating never touches a multi-megabyte payload.

No database: this follows the same file-based persistence as the rest of ``var/`` and lands on the
``rack-data`` volume, so saved reports survive a container restart and a redeploy.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from json_db import atomic_write, atomic_write_json

_logger = logging.getLogger("rack_web.report_store")

#: Oldest reports are pruned beyond this count. A JSON report for 2000 nodes runs to several MB,
#: so an unbounded directory would quietly consume the data volume the catalog also lives on.
MAX_REPORTS = 40

#: Extensions we are willing to write and serve. A format not listed here cannot create a file,
#: which keeps a caller from talking this into writing, say, an ``.html`` next to the SPA.
ALLOWED_EXTENSIONS = frozenset({"csv", "json", "pdf"})

#: Ids are generated here, never supplied by a caller, but the download route looks one up from a
#: URL -- so the shape is enforced on read as well to keep path traversal impossible.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_lock = threading.RLock()


def reports_dir() -> Path:
    """Directory holding saved reports, created on first use."""
    base = os.environ.get("NVIS_REPORTS_DIR", "").strip()
    if base:
        path = Path(base)
    else:
        path = Path(__file__).resolve().parent.parent / "var" / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sidecar(entry_id: str) -> Path:
    """Metadata path for *entry_id*.

    ``.meta.json``, not ``.json``: a JSON-format report's payload is ``<id>.json``, so a plain
    ``.json`` sidecar is the *same path* and the metadata silently overwrites the document. That
    would corrupt precisely the Nscale customer deliverable, which is JSON only.
    """
    return reports_dir() / f"{entry_id}.meta.json"


def _payload(entry_id: str, ext: str) -> Path:
    return reports_dir() / f"{entry_id}.{ext}"


def _safe_name(name: str) -> str:
    """A filename safe to hand back in a Content-Disposition header.

    The stem reaches us from report metadata, which includes a cluster name an operator typed --
    so it can contain spaces, slashes or quotes, any of which would break the header or point the
    download somewhere unintended.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-._")
    return cleaned[:120] or "report"


def save(
    *,
    kind: str,
    fmt: str,
    filename: str,
    data: bytes | str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a finished report and return its metadata entry.

    Raises ``ValueError`` for a format we do not serve. The payload is written first and the
    sidecar second: a crash between the two leaves an orphan payload, which ``list_entries``
    ignores, whereas the reverse order would advertise a report whose bytes are missing.
    """
    ext = (fmt or "").strip().lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"refusing to store unsupported report format: {fmt!r}")

    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    entry_id = uuid.uuid4().hex
    stem = _safe_name(filename)

    entry = {
        "id": entry_id,
        "kind": kind,
        "format": ext,
        "filename": f"{stem}.{ext}",
        "size": len(payload),
        "created": time.time(),
        **(meta or {}),
    }

    with _lock:
        target = _payload(entry_id, ext)
        atomic_write(target, payload)
        atomic_write_json(_sidecar(entry_id), entry)

        _prune()

    _logger.info("saved %s report %s (%s, %d bytes)", kind, entry_id, ext, len(payload))
    return entry


def list_entries() -> list[dict[str, Any]]:
    """Every saved report, newest first.

    A sidecar without its payload is skipped rather than listed: offering a download that cannot
    succeed is worse than not showing the row, and that pairing is how an interrupted save looks.
    """
    out: list[dict[str, Any]] = []
    with _lock:
        for side in reports_dir().glob("*.meta.json"):
            try:
                entry = json.loads(side.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _logger.debug("unreadable report sidecar: %s", side, exc_info=True)
                continue
            entry_id = str(entry.get("id") or "")
            ext = str(entry.get("format") or "")
            if not _ID_RE.match(entry_id) or ext not in ALLOWED_EXTENSIONS:
                continue
            if not _payload(entry_id, ext).is_file():
                continue
            out.append(entry)
    out.sort(key=lambda e: float(e.get("created") or 0), reverse=True)
    return out


def load(entry_id: str) -> tuple[bytes, dict[str, Any]] | None:
    """The stored bytes and metadata for *entry_id*, or None when it is not there."""
    if not _ID_RE.match(entry_id or ""):
        return None
    with _lock:
        side = _sidecar(entry_id)
        if not side.is_file():
            return None
        try:
            entry = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        ext = str(entry.get("format") or "")
        if ext not in ALLOWED_EXTENSIONS:
            return None
        payload = _payload(entry_id, ext)
        if not payload.is_file():
            return None
        return payload.read_bytes(), entry


def delete(entry_id: str) -> bool:
    """Remove a saved report. True when something was removed."""
    if not _ID_RE.match(entry_id or ""):
        return False
    with _lock:
        side = _sidecar(entry_id)
        if not side.is_file():
            return False
        try:
            entry = json.loads(side.read_text(encoding="utf-8"))
            ext = str(entry.get("format") or "")
        except (OSError, ValueError):
            ext = ""
        # Sidecar first: it is what listing reads, so the report disappears from the UI even if
        # unlinking the payload then fails, rather than lingering as an undeletable row.
        side.unlink(missing_ok=True)
        if ext in ALLOWED_EXTENSIONS:
            _payload(entry_id, ext).unlink(missing_ok=True)
    return True


def _prune() -> None:
    """Drop the oldest reports past ``MAX_REPORTS``. Caller holds the lock."""
    entries = []
    for side in reports_dir().glob("*.meta.json"):
        try:
            entry = json.loads(side.read_text(encoding="utf-8"))
            entries.append((float(entry.get("created") or 0), str(entry.get("id") or ""),
                            str(entry.get("format") or "")))
        except (OSError, ValueError):
            continue
    if len(entries) <= MAX_REPORTS:
        return
    entries.sort(key=lambda t: t[0], reverse=True)
    for _created, entry_id, ext in entries[MAX_REPORTS:]:
        if not _ID_RE.match(entry_id):
            continue
        _sidecar(entry_id).unlink(missing_ok=True)
        if ext in ALLOWED_EXTENSIONS:
            _payload(entry_id, ext).unlink(missing_ok=True)
        _logger.info("pruned old report %s", entry_id)
