# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""One file, one JSON document, more than one process. The substrate under every store here.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Two TinyDB-backed stores (the catalog and the identity store) and a dozen hand-written JSON
files share one situation: a single file, rewritten whole, by a process that is not always the
only one running. A deploy's ``cli.py check`` overlaps a serving container; a developer runs two
instances on two ports; an operator adds a user from the CLI while the dashboard is up.

**This module exists because the naive version of that fails in three ways, and only the first
one is visible.** All three were measured on the catalog with four concurrent writers:

1. a **shared temp name** (``<path>.tmp``) turns two writers into a crash -- the first
   ``os.replace`` moves it away and the second gets ``FileNotFoundError``;
2. TinyDB caches a ``_next_id`` counter, so two processes claim the same document id
   (``ValueError: Document with ID 4 already exists``);
3. every write-back is computed from a stale whole-file read, so writes are **silently lost** --
   63 of 240 documents survived.

The third is the dangerous one. A store that loses three quarters of its writes raises nothing,
so the only way to notice is to count documents, which is what
``test_catalog_store_concurrency.py`` does.

Three pieces, and using fewer than all three leaves one of the failures above in place:

``AtomicJSONStorage``   unique temp per writer, mode given at creation, atomic replace
``exclusive_lock``      serialises a whole read-modify-write across processes
``drop_caches``         makes the read inside that cycle see the committed file

**Why the mode is a class attribute rather than a chmod afterwards.** An atomic replace installs
the *temp file's* mode, and a plain ``open()`` takes the umask -- so a ``chmod`` after the fact
is a correction applied once a world-readable inode already exists. That is
``identity-auth-traps.mdc`` §3, and it is why the identity store can ask for ``0o600`` and get a
file that was never anything else.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from tinydb.storages import Storage, touch

try:  # fcntl is POSIX-only; degrade gracefully elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def unique_temp_path(path: str | os.PathLike[str]) -> str:
    """A temp path beside *path* that no other writer can be using.

    The pid alone is not enough: two threads in one process write concurrently too, and the
    ``threading`` lock is per store instance rather than per file.
    """
    return f"{path}.{os.getpid()}.{uuid4().hex[:8]}.tmp"


def atomic_write(
    path: str | os.PathLike[str],
    data: str | bytes,
    *,
    mode: int = 0o644,
    encoding: str = "utf-8",
) -> None:
    """Replace *path* with *data*, atomically, owner-only if asked.

    The one helper every JSON-file store here should use. Thirteen call sites had written their
    own version of this with a fixed ``<path>.tmp``, which is the crash above waiting for a
    second process -- and `test_atomic_writes.py` now fails the build on a new one.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = unique_temp_path(target)
    payload = data if isinstance(data, bytes) else data.encode(encoding)
    try:
        # O_EXCL so a collision is an error rather than a silent overwrite; the mode is given
        # here because `os.replace` installs the temp file's mode (see the module docstring).
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            with contextlib.suppress(OSError):
                os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        # Otherwise a failing writer litters the directory with one temp per attempt, and none
        # of them is distinguishable from the real file by name alone.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(
    path: str | os.PathLike[str],
    document: Any,
    *,
    mode: int = 0o644,
    indent: int | None = 2,
    sort_keys: bool = False,
    default: Any = None,
) -> None:
    """`atomic_write` for a JSON document, serialised before anything is opened.

    Serialising first matters: a document that cannot be encoded must not have already replaced
    the previous file, and `json.dump` straight into the temp fails halfway through.
    """
    text = json.dumps(document, indent=indent, sort_keys=sort_keys, default=default) + "\n"
    atomic_write(path, text, mode=mode)


#: Per-thread nesting depth per lock path. See `exclusive_lock` for why this is not optional.
_held = threading.local()


@contextlib.contextmanager
def exclusive_lock(path: str | os.PathLike[str]):
    """Hold an exclusive advisory lock covering *path*, for the whole `with` body.

    **A sidecar lock file, not the file itself.** The target is *replaced* on every write, so a
    lock held on it protects an inode that is about to stop being the file -- the next writer
    opens the new inode and locks nothing.

    **Re-entrant, and that is a correctness requirement rather than a convenience.** `flock` is
    per *descriptor*, so a thread that already holds this lock and opens a second handle blocks
    against itself, forever. Today no store method calls another locked method -- checked with
    an AST walk over all sixteen identity mutators, not assumed -- but the day somebody makes
    `revoke_subject` call `revoke_token`, the failure is a hung login rather than an error, and
    nothing in the code would look wrong. Counting depth removes the whole class.

    Without `fcntl` (Windows) this is a no-op, which is what the rest of this codebase does for
    the same reason: degrading is better than refusing to run. Closing the handle is what
    releases the lock, since POSIX drops an `flock` on the last close of the descriptor.
    """
    if fcntl is None:
        yield
        return

    key = str(path)
    depths: dict[str, int] = getattr(_held, "depths", None) or {}
    _held.depths = depths
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    with contextlib.ExitStack() as stack:
        try:
            handle = stack.enter_context(open(f"{key}.lock", "a+"))
            fcntl.flock(handle, fcntl.LOCK_EX)
        except OSError:
            # An unwritable directory must not make the store unwritable too.
            pass
        depths[key] = 1
        try:
            yield
        finally:
            depths[key] = 0


def drop_caches(db: Any) -> None:
    """Forget what TinyDB remembers about a file another process may have rewritten.

    **The lock alone is not enough, and this is the half that is easy to miss.** TinyDB keeps a
    per-table query cache and a ``_next_id`` counter in memory, so serialising the file access
    still leaves each process acting on the state it last saw -- which is failures 2 and 3 in
    the module docstring.

    Call with the lock held, so the re-read that follows sees the committed file. ``tables()``
    reads storage itself, which is what makes the refresh real.
    """
    try:
        for name in db.tables():
            table = db.table(name)
            table.clear_cache()
            # Private, and deliberately: TinyDB offers no public way to invalidate the id
            # counter, and `None` is the sentinel its own `_get_next_id` treats as "recompute
            # from the stored documents".
            table._next_id = None
    except (OSError, ValueError):
        # A corrupt or unreadable file is the read path's problem to report; a cache refresh
        # must not turn into a failed write.
        pass


class AtomicJSONStorage(Storage):
    """TinyDB storage that rewrites the whole file atomically.

    Unlike TinyDB's default ``JSONStorage`` (which keeps an open handle and writes in place),
    this writes a uniquely named sibling temp file and ``os.replace``s it, so a reader never
    sees a half-written document and two writers never fight over one temp name.

    ``read`` takes a shared advisory lock. The exclusive half belongs to the repository layer,
    because what must be serialised is the whole read-modify-write cycle TinyDB performs per
    operation: locking only this ``write`` would make each write atomic while still letting two
    processes lose one another's edits.

    Subclasses set :attr:`file_mode` to change the permissions of the file they produce. It is a
    class attribute rather than a ``chmod`` after the write for the reason in the module
    docstring -- a correction after the fact still opens a window.
    """

    #: Mode the produced file is *created* with. Configuration by default; the identity store
    #: narrows it to owner-only.
    file_mode: int = 0o644

    def __init__(self, path: str, **kwargs: Any) -> None:
        self._path = path
        self._kwargs = kwargs
        touch(path, create_dirs=True)

    def _flock(self, handle, exclusive: bool) -> None:
        if fcntl is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)

    def _funlock(self, handle) -> None:
        if fcntl is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        try:
            with open(self._path, encoding="utf-8") as f:
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
        atomic_write_json(
            self._path,
            data,
            mode=self.file_mode,
            indent=2,
            sort_keys=True,
        )

    def close(self) -> None:  # pragma: no cover - nothing persistent to close
        pass
