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
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

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
    place), this writes to a *uniquely named* sibling temp file and ``os.replace``s it, so a
    reader never sees a half-written catalog and two writers never fight over one temp name.

    ``read`` takes a shared advisory lock. The exclusive half lives one layer up, in
    :class:`TinyDBRepo`, because the thing that must be serialised is the whole
    read-modify-write cycle TinyDB performs per operation -- locking only this ``write`` would
    make each write atomic while still letting two processes lose one another's edits.
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
        # A temp name unique to this writer.
        #
        # It used to be a fixed `<path>.tmp`, shared by every process opening the same catalog,
        # which turns two concurrent writers into a crash: both create it, the first
        # `os.replace` moves it away, and the second gets `FileNotFoundError: catalog.json.tmp
        # -> catalog.json`. Seen on a laptop running two local instances on different ports --
        # both seed the catalog at boot, so the window is ~44 writes wide -- and it took out the
        # whole bootstrap ("catalog bootstrap failed; continuing with legacy env behavior").
        # A deploy's `cli.py check` overlapping a running server is the same shape.
        tmp = f"{self._path}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
        try:
            # O_EXCL so a collision is an error rather than a silent overwrite, and the mode is
            # given at creation rather than chmod-ed after (`identity-auth-traps.mdc` §3: an
            # atomic replace installs the *temp file's* mode, and a plain `open` takes the
            # umask). 0644 deliberately -- this file is configuration, not a secret; the
            # identity store and `policies.json` are the owner-only ones.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True, **self._kwargs)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp, self._path)
        except BaseException:
            # Leaving the temp behind would litter `var/store` with one file per failed write,
            # and the next reader cannot tell them from the real catalog by name alone.
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

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

    @contextlib.contextmanager
    def _exclusive(self):
        """Serialise a whole read-modify-write against other *processes*.

        `_lock` is a `threading.RLock`, so it only ever covered threads inside one interpreter.
        Every TinyDB mutation is a read of the whole file, an edit in memory and a write back,
        so two processes interleaving that lose one another's edits -- and until the temp file
        got a unique name, they crashed instead, which is how this was noticed.

        A sidecar lock file rather than the catalog itself: the catalog is *replaced* on every
        write, so a lock held on it protects an inode that is about to stop being the catalog.

        Without `fcntl` (Windows) this degrades to the thread lock alone, which is what the
        module already does for reads -- the alternative is refusing to run there at all.
        """
        if fcntl is None:
            yield
            return
        # The handle has to outlive the `try`, since the lock is held across the `yield`, and
        # closing it is what releases the lock -- POSIX drops an `flock` on the last close of
        # the descriptor, so `ExitStack` closing the file is the release.
        with contextlib.ExitStack() as stack:
            try:
                handle = stack.enter_context(open(f"{self._path}.lock", "a+"))
                fcntl.flock(handle, fcntl.LOCK_EX)
            except OSError:
                # An unwritable directory must not make the catalog unwritable too. Same call
                # the rest of this module makes: degrade rather than take the deployment down.
                pass
            self._drop_caches()
            yield

    def _drop_caches(self) -> None:
        """Forget what TinyDB remembers about a file another process may have rewritten.

        **The lock alone is not enough, and this is the half that is easy to miss.** TinyDB
        keeps a per-table query cache and a `_next_id` counter in memory. Serialising the file
        access still leaves each process acting on the state it last saw, which produces two
        distinct failures, both measured with four concurrent writers:

        * `ValueError: Document with ID 4 already exists` -- both processes believed the same
          id was next;
        * silent loss -- 63 of 240 documents survived, because each write-back was computed
          from a stale read of the whole file.

        Called with the exclusive lock held, so the re-read that follows sees the committed
        file. `tables()` reads storage itself, which is what makes the refresh real.
        """
        try:
            for name in self._db.tables():
                table = self._db.table(name)
                table.clear_cache()
                # Private, and deliberately: TinyDB offers no public way to invalidate the id
                # counter, and `None` is the documented sentinel its own `_get_next_id` treats
                # as "recompute from the stored documents".
                table._next_id = None
        except (OSError, ValueError):
            # A corrupt or unreadable file is the read path's problem to report, not this
            # helper's -- it must not turn a cache refresh into a failed write.
            pass

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
        with _lock, self._exclusive():
            q = Query()
            self._table(table).upsert(dict(doc), q.id == doc_id)
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
                tbl.upsert(dict(doc), q.id == doc_id)
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
