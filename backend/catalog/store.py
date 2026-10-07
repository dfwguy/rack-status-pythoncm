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

import contextlib
import json
import os
import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol

from json_db import AtomicJSONStorage, drop_caches, exclusive_lock
from tinydb import Query, TinyDB

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
#: Per-cluster NVIS-P2P instance configuration, keyed by cluster id. A document (read/written whole,
#: no joins) -- see models.P2pSettings and citizen 15.
TABLE_P2P_SETTINGS = "p2p_settings"
TABLE_P2P_CATALOG = "p2p_catalog"
TABLE_P2P_ROSTERS = "p2p_rosters"
TABLE_P2P_CAMPAIGNS = "p2p_campaigns"
TABLE_META = "meta"

SCHEMA_VERSION = 2

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


#: The shared substrate, now that two TinyDB stores and a dozen hand-written JSON files need
#: the same three guarantees. Kept under the old name because `identity/store.py` subclasses
#: it from here and several tests import it by this name.
_AtomicJSONStorage = AtomicJSONStorage


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

    @contextlib.contextmanager
    def _exclusive(self):
        """Serialise a whole read-modify-write against other *processes*.

        `_lock` is a `threading.RLock`, so it only ever covered threads inside one interpreter.
        Every TinyDB mutation reads the whole file, edits in memory and writes back, so two
        processes interleaving that lose one another's edits -- and until the temp file got a
        unique name they crashed instead, which is how this was found. The measurements, and
        why the cache drop is needed as well, are in `json_db`.

        The drop happens **inside** the lock, and it has to: it was briefly left outside during
        a refactor and the id collisions came straight back, because dropping caches before the
        lock is held just re-reads a file another writer is about to replace.
        """
        with exclusive_lock(self._path):
            self._drop_caches()
            yield

    def _drop_caches(self) -> None:
        drop_caches(self._db)

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

    @staticmethod
    def _replace(tbl, q, doc_id: str, doc: dict[str, Any]) -> None:
        """Write the document whole, rather than merging it into what is there.

        TinyDB's own ``upsert`` delegates to ``update``, which *merges* — so a field dropped
        from a document would survive in the store. That made the two backends disagree
        observably: :class:`SqliteDocRepo` writes one payload and therefore replaces. A
        document is written whole by definition (it is what DynamoDB's ``PutItem`` does), so
        the SQL backend is right and this is aligned to it. Caught by the parity tests in
        `test_storage_doc_repo.py`, which run every `Repo` method against both backends.

        Both statements sit inside the caller's ``_exclusive()`` block, so another process
        never observes the gap between the remove and the insert.
        """
        existing = tbl.get(q.id == doc_id)
        if existing is not None:
            tbl.remove(doc_ids=[existing.doc_id])
        tbl.insert(dict(doc))

    def upsert(self, table: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
        if not doc_id:
            raise ValueError("document must have a non-empty 'id'")
        with _lock, self._exclusive():
            self._replace(self._table(table), Query(), doc_id, doc)
            _bump_generation()
            return dict(doc)

    def bulk_upsert(self, table: str, docs: Iterable[dict[str, Any]]) -> int:
        n = 0
        with _lock, self._exclusive():
            q = Query()
            tbl = self._table(table)
            for doc in docs:
                doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
                if not doc_id:
                    continue
                self._replace(tbl, q, doc_id, doc)
                n += 1
            if n:
                _bump_generation()
        return n

    def delete(self, table: str, doc_id: str) -> bool:
        if not doc_id:
            return False
        with _lock, self._exclusive():
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
        with _lock, self._exclusive():
            q = Query()
            self._table(TABLE_META).upsert({"key": key, "value": value}, q.key == key)
            _bump_generation()


# ---------------------------------------------------------------------------
# SQLite-backed implementation, for documents that grow without bound
# ---------------------------------------------------------------------------

#: A table name reaches SQL as an identifier, which cannot be parameterised, so the set of
#: legal names is closed rather than escaped. Every caller passes a module constant; this is
#: here so that stays true after someone plumbs a name through from a request.
_TABLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: Same reasoning for a field name reaching SQL inside a `json_extract` path.
_FIELD_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


class SqliteDocRepo:
    """:class:`Repo` backed by one SQLite file, one table per document table.

    Same eight methods as :class:`TinyDBRepo` and interchangeable with it, because the choice
    of backend is not the caller's business. Pick this one when the document count grows
    without bound: :class:`AtomicJSONStorage` reads and rewrites the *entire* file per write,
    which is correct for a catalog of a few dozen platforms and tools and wrong for anything
    that gains a row per launch. Pick TinyDB when the store is small, operator-edited and
    wants to stay human-readable on disk.

    This is deliberately **not** a relational schema. A document is an id and a JSON payload;
    nothing joins it. SQLite is here for its write path and its index on the primary key, not
    for its query planner -- see `storage-engines.mdc`.

    Concurrency: WAL, so a reader never blocks behind the writer, plus the module `_lock` for
    threads within one interpreter. No `exclusive_lock` and no cache drop, because neither
    applies -- those exist for the read-modify-write-whole-file cycle that this backend does
    not have. That asymmetry is the whole reason to prefer it here.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._known: set[str] = set()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @property
    def path(self) -> Path:
        return self._path

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _checked(table: str) -> str:
        name = (table or "").strip().lower()
        if not _TABLE_NAME_RE.match(name):
            raise ValueError(f"illegal document table name: {table!r}")
        return name

    def _ensure(self, conn, table: str) -> str:
        name = self._checked(table)
        if name not in self._known:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {name} "
                "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            self._known.add(name)
        return name

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        """A row that is not a JSON object is skipped, not raised.

        One corrupt payload must not make the whole archive unreadable: the point of an
        append-preserving store is that everything else still answers.
        """
        try:
            doc = json.loads(str(raw or "{}"))
        except (TypeError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def list(self, table: str) -> list[dict[str, Any]]:
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            rows = conn.execute(f"SELECT payload FROM {name}").fetchall()
        out = [self._decode(r["payload"]) for r in rows]
        return [d for d in out if d is not None]

    def get(self, table: str, doc_id: str) -> dict[str, Any] | None:
        if not doc_id:
            return None
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            row = conn.execute(
                f"SELECT payload FROM {name} WHERE id = ?", (doc_id,)
            ).fetchone()
        return self._decode(row["payload"]) if row else None

    def upsert(self, table: str, doc: dict[str, Any]) -> dict[str, Any]:
        doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
        if not doc_id:
            raise ValueError("document must have a non-empty 'id'")
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            conn.execute(
                f"INSERT INTO {name} (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                (doc_id, json.dumps(doc, ensure_ascii=False, sort_keys=True)),
            )
        _bump_generation()
        return dict(doc)

    def bulk_upsert(self, table: str, docs: Iterable[dict[str, Any]]) -> int:
        payloads = [
            (
                (d.get("id") or "").strip(),
                json.dumps(d, ensure_ascii=False, sort_keys=True),
            )
            for d in docs
            if isinstance(d, dict) and (d.get("id") or "").strip()
        ]
        if not payloads:
            return 0
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            conn.executemany(
                f"INSERT INTO {name} (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
                payloads,
            )
        _bump_generation()
        return len(payloads)

    def delete(self, table: str, doc_id: str) -> bool:
        if not doc_id:
            return False
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            cur = conn.execute(f"DELETE FROM {name} WHERE id = ?", (doc_id,))
            removed = cur.rowcount > 0
        if removed:
            _bump_generation()
        return removed

    def query(
        self, table: str, predicate: Callable[[dict[str, Any]], bool]
    ) -> list[dict[str, Any]]:
        """Scan and filter in Python, same as :class:`TinyDBRepo`.

        The predicate is a callable, so it cannot be pushed into SQL. That is a deliberate
        limit of the interface rather than an oversight: a document store filters on a handful
        of fields, and anything needing an indexed multi-column query is relational data that
        belongs in its own schema.
        """
        return [d for d in self.list(table) if predicate(d)]

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.get(TABLE_META, key)
        return row.get("value", default) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.upsert(TABLE_META, {"id": key, "key": key, "value": value})

    def upsert_if_newer(self, table: str, doc: dict[str, Any], *, clock: str) -> bool:
        """Compare-and-set on ``doc[clock]``: write only if not older than what is stored.

        Beyond :class:`Repo`, and needed by any append-preserving archive. Without it a stale
        snapshot -- a background reconcile holding a row read minutes ago -- silently
        overwrites a fresher one, and the store quietly goes backwards. The archive this was
        extracted from enforced it in SQL; carrying it here is what let that code move without
        losing the guarantee.

        Deliberately one statement, so the compare and the write cannot be interleaved by
        another writer. A read-then-upsert in the caller would look equivalent and would not
        be. This is also why it belongs in the backend rather than above it: DynamoDB spells
        the same thing as a ``ConditionExpression``, so the next backend can honour it too.

        Returns True when the row was written or refreshed, False when an equal-or-newer row
        was already there. A missing or empty clock value sorts lowest, so a document with no
        clock never displaces one that has a real timestamp.
        """
        doc_id = (doc.get("id") or "").strip() if isinstance(doc, dict) else ""
        if not doc_id:
            raise ValueError("document must have a non-empty 'id'")
        # `clock` reaches SQL inside a JSON path, which cannot be parameterised either.
        if not _FIELD_NAME_RE.match(clock or ""):
            raise ValueError(f"illegal clock field name: {clock!r}")
        payload = json.dumps(doc, ensure_ascii=False, sort_keys=True, default=str)
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            # The clock is read back out of the payload rather than kept in its own column:
            # one copy, so the two cannot disagree after an out-of-band edit.
            cur = conn.execute(
                f"INSERT INTO {name} (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload "
                f"WHERE COALESCE(json_extract(excluded.payload, '$.{clock}'), '') >= "
                f"COALESCE(json_extract({name}.payload, '$.{clock}'), '')",
                (doc_id, payload),
            )
            written = cur.rowcount > 0
        if written:
            _bump_generation()
        return written

    def count(self, table: str) -> int:
        """Row count without decoding any payload. Not part of :class:`Repo`.

        Here because a migration needs to ask "is this already populated?" and answering it
        through ``list()`` would deserialise the entire archive to discard the result.
        """
        with _lock, self._connect() as conn:
            name = self._ensure(conn, table)
            return int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])


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
