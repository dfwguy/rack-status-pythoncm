# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Storage layer for the NVIS catalog.

A thin :class:`Repo` interface (``list/get/upsert/delete/query`` + ``meta``) wraps a
backend so the persistence choice is swappable. The default backend is
:class:`TinyDBRepo` (TinyDB over a single JSON file at ``var/store/catalog.json``);
a DynamoDB / cloud backend can implement the same interface later without touching
callers.

Concurrency: the app runs as a single Flask process with ``threaded=True``
(``backend/app.py``), so a module-level ``threading.RLock`` serializes writes. A custom
:class:`_AtomicJSONStorage` writes via ``tmp + os.replace`` so an interrupted write never
corrupts the file (same durability guarantee as ``rack_locks.py`` / ``job_env_overrides``).
A best-effort ``fcntl`` flock guards against an external reader (NVFTP sync) seeing a
partial file.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from tinydb import Query, TinyDB
from tinydb.storages import Storage, touch

try:  # fcntl is POSIX-only; degrade gracefully elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

APP_DIR = Path(__file__).resolve().parent.parent  # backend/
REPO_ROOT = APP_DIR.parent

# Document tables. ``meta`` holds singleton settings (schema_version, active context).
TABLE_PLATFORMS = "platforms"
TABLE_TOOLS = "tools"
TABLE_PROJECTS = "projects"
TABLE_NPI_TOOLS = "npi_tools"
TABLE_META = "meta"

SCHEMA_VERSION = 1

_lock = threading.RLock()

# Bumped on every write so callers (e.g. catalog.context) can cheaply invalidate caches.
_generation = 0


def current_generation() -> int:
    return _generation


def _bump_generation() -> None:
    global _generation
    _generation += 1


def default_catalog_path() -> Path:
    return Path(
        os.environ.get("NVIS_CATALOG_FILE", "")
        or str(REPO_ROOT / "var" / "store" / "catalog.json")
    )


# ---------------------------------------------------------------------------
# Atomic JSON storage for TinyDB
# ---------------------------------------------------------------------------


class _AtomicJSONStorage(Storage):
    """TinyDB storage that reads/writes the whole file atomically.

    Unlike TinyDB's default ``JSONStorage`` (which keeps an open handle and writes in
    place), this writes to a sibling ``.tmp`` and ``os.replace``s it, and takes an
    advisory ``fcntl`` lock around each operation.
    """

    def __init__(self, path: str, **kwargs: Any) -> None:
        self._path = path
        self._kwargs = kwargs
        touch(path, create_dirs=True)

    def _flock(self, handle, exclusive: bool) -> None:
        if fcntl is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError:
            pass

    def _funlock(self, handle) -> None:
        if fcntl is None:
            return
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError:
            pass

    def read(self) -> dict[str, Any] | None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._flock(f, exclusive=False)
                try:
                    data = f.read()
                finally:
                    self._funlock(f)
        except OSError:
            return None
        if not data.strip():
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def write(self, data: dict[str, Any]) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        tmp = f"{self._path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, **self._kwargs)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp, self._path)

    def close(self) -> None:  # pragma: no cover - nothing persistent to close
        pass


# ---------------------------------------------------------------------------
# Repo interface
# ---------------------------------------------------------------------------


class Repo(Protocol):
    """Backend-agnostic document store keyed by a document ``id`` field."""

    def list(self, table: str) -> list[dict[str, Any]]: ...

    def get(self, table: str, doc_id: str) -> dict[str, Any] | None: ...

    def upsert(self, table: str, doc: dict[str, Any]) -> dict[str, Any]: ...

    def delete(self, table: str, doc_id: str) -> bool: ...

    def query(
        self, table: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]]: ...

    def get_meta(self, key: str, default: Any = None) -> Any: ...

    def set_meta(self, key: str, value: Any) -> None: ...

    def bulk_upsert(self, table: str, docs: Iterable[dict[str, Any]]) -> int: ...


# ---------------------------------------------------------------------------
# TinyDB-backed implementation
# ---------------------------------------------------------------------------


class TinyDBRepo:
    """:class:`Repo` backed by TinyDB + :class:`_AtomicJSONStorage`."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path else default_catalog_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = TinyDB(str(self._path), storage=_AtomicJSONStorage)

    @property
    def path(self) -> Path:
        return self._path

    def _table(self, table: str):
        return self._db.table(table)

    def list(self, table: str) -> list[dict[str, Any]]:
        with _lock:
            return [dict(x) for x in self._table(table).all()]

    def get(self, table: str, doc_id: str) -> dict[str, Any] | None:
        if not doc_id:
            return None
        with _lock:
            q = Query()
            row = self._table(table).get(q.id == doc_id)
            return dict(row) if row else None

    def upsert(self, table: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
        if not doc_id:
            raise ValueError("document must have a non-empty 'id'")
        with _lock:
            q = Query()
            self._table(table).upsert(dict(doc), q.id == doc_id)
            _bump_generation()
            return dict(doc)

    def bulk_upsert(self, table: str, docs: Iterable[dict[str, Any]]) -> int:
        n = 0
        with _lock:
            q = Query()
            tbl = self._table(table)
            for doc in docs:
                doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
                if not doc_id:
                    continue
                tbl.upsert(dict(doc), q.id == doc_id)
                n += 1
            if n:
                _bump_generation()
        return n

    def delete(self, table: str, doc_id: str) -> bool:
        if not doc_id:
            return False
        with _lock:
            q = Query()
            removed = self._table(table).remove(q.id == doc_id)
            if removed:
                _bump_generation()
            return bool(removed)

    def query(
        self, table: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]]:
        with _lock:
            return [dict(x) for x in self._table(table).all() if predicate(x)]

    def get_meta(self, key: str, default: Any = None) -> Any:
        with _lock:
            q = Query()
            row = self._table(TABLE_META).get(q.key == key)
            return row.get("value") if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with _lock:
            q = Query()
            self._table(TABLE_META).upsert({"key": key, "value": value}, q.key == key)
            _bump_generation()


# ---------------------------------------------------------------------------
# Singleton accessor (mirrors platforms.get_platform pattern)
# ---------------------------------------------------------------------------

_instance: Repo | None = None


def get_repo() -> Repo:
    """Return the process-wide catalog repository (created on first use)."""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is None:
            _instance = TinyDBRepo()
    return _instance


def set_repo(repo: Repo | None) -> None:
    """Override the repository (used by tests to point at a temp file)."""
    global _instance
    _instance = repo
