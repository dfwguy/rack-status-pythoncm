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
"""Persistence for users, groups and membership — and the retirement ledger (FR-1, FR-2b, NFR-5).

**Why this is not a table in the catalog**, since that was the obvious cheap option and it is
disqualified by one fact: ``NVIS_CATALOG_FILE`` selects *which catalog file* a process reads (Firmus
``catalog.json``, Boston ``catalog-boston.json``), so users in the catalog would mean an environment
variable decides who can log in. FR-12 forbids exactly that. The catalog also holds configuration
operators legitimately hand-edit and exports as plaintext, neither acceptable for credential
material. The catalog answers *what this deployment can run*; identity answers *who may use it*.

What it **does** reuse is the mechanism: ``catalog.store._AtomicJSONStorage`` is parameterised only by
path — no coupling to catalog tables — so one implementation of write-to-tmp-then-``os.replace``
under an ``fcntl`` flock serves both stores. NFR-5 satisfied without a second copy of the tricky part.

The retirement ledger is the part that does not exist in any other store, and it is the reason this
module is not a thin CRUD wrapper. FR-2b: a login is never reused. Deleting a user retires its login
forever, so a later create with that login is refused. Without it, ``jdoe`` departs, the account is
deleted, a different ``jdoe`` is created, and every action the first person took is attributed to the
second while any policy naming ``user:jdoe`` hands over their grants. Retirement is **not** erasure:
a retired login stays resolvable so prior audit events keep their subject (AC-2b).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

# The one thing borrowed from the catalog: its atomic-write storage class, which takes only a path
# and knows nothing of catalog tables. `touch` comes from tinydb directly rather than through
# catalog.store, which merely re-exports it — importing a name a module happens to have imported
# breaks the day it stops needing it.
from catalog.store import _AtomicJSONStorage
from tinydb import Query, TinyDB
from tinydb.storages import touch

from identity.models import (
    Group,
    IdentityError,
    Membership,
    User,
    canonical_group_name,
    canonical_login,
)

APP_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = APP_DIR.parent

#: Serialises writers in this process. The deployment runs one gunicorn worker with several threads
#: (``--workers 1 --threads N --worker-class gthread``), so in-process concurrency is the real case;
#: the storage layer's ``fcntl`` flock covers a second process (an operator with a shell).
_lock = threading.RLock()

#: Bumped on every write. Exists so a hot read path can cache and still notice a change without a
#: TTL to tune — the pattern Phase 1 arrived at the hard way, when a per-request catalog read cost
#: 4.74 ms and breached the audit latency budget. ``identity.revocation`` will key on this.
_generation = 0

_TABLE_USERS = "users"
_TABLE_GROUPS = "groups"
_TABLE_MEMBERS = "memberships"
_TABLE_RETIRED = "retired"


class _OwnerOnlyAtomicStorage(_AtomicJSONStorage):
    """The catalog's atomic storage, plus owner-only permissions that survive a write.

    Subclassed rather than fixed upstream because the catalog holds configuration and does not want
    `0o600` — its file is legitimately readable, and narrowing it would be an unrelated behaviour
    change to a shared component.

    The bug this exists to prevent, found by a test rather than by reasoning: the parent writes to a
    sibling `.tmp` and `os.replace`s it, so **every write installs a new inode carrying the process
    umask** — typically `0o644`. A `chmod` in the store's constructor is therefore undone by the
    first save, and the file holding SRP verifiers quietly becomes world-readable. `audit/sink.py`
    hit the identical trap with log rotation and solves it the same way: re-apply the mode after the
    file is replaced, not once when it is created.
    """

    def write(self, data: dict[str, Any]) -> None:
        super().write(data)
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            # A mounted volume may refuse chmod. Losing the tightening is worth reporting but not
            # worth failing a write that has already landed.
            pass


def current_generation() -> int:
    return _generation


def _bump() -> None:
    global _generation
    _generation += 1


def identity_file() -> str:
    """Where the store lives. ``NVIS_IDENTITY_FILE`` overrides, for tests and for an operator who
    keeps state elsewhere.

    Default is under ``var/``, which on a container deployment is the ``rack-data`` named volume —
    so the store survives a ``docker compose up -d`` recreate for the same reason the audit trail
    does (Phase 1, TC-40). A store that vanished on redeploy would lock everyone out.
    """
    return (os.environ.get("NVIS_IDENTITY_FILE") or "").strip() or str(
        REPO_ROOT / "var" / "identity" / "identity.json"
    )


class IdentityStore:
    """The only writer of the identity file.

    Reads return records; writes take records. Every method that mutates bumps the generation, and
    every method that could violate uniqueness or reuse checks it *inside* the lock — a
    check-then-write across a lock boundary is the classic way two concurrent creates both succeed.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or identity_file()
        touch(self._path, create_dirs=True)
        # Owner-only: the file holds SRP verifiers (NFR-4). Set here for the freshly-touched file,
        # and re-applied after every write by the storage subclass — because os.replace installs a
        # new inode with the umask's permissions and would otherwise undo this on the first save.
        try:
            os.chmod(self._path, 0o600)
            os.chmod(Path(self._path).parent, 0o700)
        except OSError:
            pass
        self._db = TinyDB(self._path, storage=_OwnerOnlyAtomicStorage)

    # -- users ---------------------------------------------------------------------------------

    def get_user(self, login: str) -> User | None:
        key = canonical_login(login)
        with _lock:
            row = self._db.table(_TABLE_USERS).get(Query().login == key)
        return User.from_dict(dict(row)) if row else None

    def list_users(self) -> list[User]:
        with _lock:
            rows = self._db.table(_TABLE_USERS).all()
        return sorted((User.from_dict(dict(r)) for r in rows), key=lambda u: u.login)

    def create_user(self, user: User) -> User:
        """Refuses a duplicate login and a retired one (FR-2a, FR-2b).

        Both checks are inside the lock with the insert, so two threads racing to create the same
        login cannot both win.
        """
        with _lock:
            table = self._db.table(_TABLE_USERS)
            if table.get(Query().login == user.login):
                raise IdentityError(f"login {user.login!r} already exists")
            if self._is_retired(_TABLE_USERS, user.login):
                raise IdentityError(
                    f"login {user.login!r} has been used before and cannot be reused; "
                    "choose another (identifiers are retired permanently, FR-2b)"
                )
            table.insert(user.to_dict())
            _bump()
        return user

    def update_user(self, user: User) -> User:
        """Replaces a user's mutable fields. The login is the key, and :meth:`User.edited` cannot
        change it, so this cannot become a rename by accident."""
        with _lock:
            table = self._db.table(_TABLE_USERS)
            if not table.get(Query().login == user.login):
                raise IdentityError(f"no such user {user.login!r}")
            table.update(user.to_dict(), Query().login == user.login)
            _bump()
        return user

    def delete_user(self, login: str) -> None:
        """Removes the record and **retires the login permanently** (FR-2b).

        Memberships referencing the user go too — a dangling edge would let a recreated login (if
        reuse were ever allowed) inherit group grants, and even without reuse it is a row pointing
        at nothing.
        """
        key = canonical_login(login)
        with _lock:
            table = self._db.table(_TABLE_USERS)
            if not table.get(Query().login == key):
                raise IdentityError(f"no such user {key!r}")
            table.remove(Query().login == key)
            self._db.table(_TABLE_MEMBERS).remove(Query().login == key)
            self._retire(_TABLE_USERS, key)
            _bump()

    # -- groups --------------------------------------------------------------------------------

    def get_group(self, name: str) -> Group | None:
        key = canonical_group_name(name)
        with _lock:
            row = self._db.table(_TABLE_GROUPS).get(Query().name == key)
        return Group.from_dict(dict(row)) if row else None

    def list_groups(self) -> list[Group]:
        with _lock:
            rows = self._db.table(_TABLE_GROUPS).all()
        return sorted((Group.from_dict(dict(r)) for r in rows), key=lambda g: g.name)

    def create_group(self, group: Group) -> Group:
        with _lock:
            table = self._db.table(_TABLE_GROUPS)
            if table.get(Query().name == group.name):
                raise IdentityError(f"group {group.name!r} already exists")
            if self._is_retired(_TABLE_GROUPS, group.name):
                raise IdentityError(
                    f"group name {group.name!r} has been used before and cannot be reused; "
                    "a recycled group name would silently re-grant what the old group had"
                )
            table.insert(group.to_dict())
            _bump()
        return group

    def update_group(self, group: Group) -> Group:
        with _lock:
            table = self._db.table(_TABLE_GROUPS)
            if not table.get(Query().name == group.name):
                raise IdentityError(f"no such group {group.name!r}")
            table.update(group.to_dict(), Query().name == group.name)
            _bump()
        return group

    def delete_group(self, name: str) -> None:
        key = canonical_group_name(name)
        with _lock:
            table = self._db.table(_TABLE_GROUPS)
            if not table.get(Query().name == key):
                raise IdentityError(f"no such group {key!r}")
            table.remove(Query().name == key)
            self._db.table(_TABLE_MEMBERS).remove(Query().group == key)
            self._retire(_TABLE_GROUPS, key)
            _bump()

    # -- membership ----------------------------------------------------------------------------

    def add_member(self, group: str, login: str) -> Membership:
        """Both ends must exist. A membership naming a group or user that does not exist is a
        silent no-op grant waiting to become a real one when the name is created."""
        edge = Membership(group=group, login=login)
        with _lock:
            if not self._db.table(_TABLE_GROUPS).get(Query().name == edge.group):
                raise IdentityError(f"no such group {edge.group!r}")
            if not self._db.table(_TABLE_USERS).get(Query().login == edge.login):
                raise IdentityError(f"no such user {edge.login!r}")
            table = self._db.table(_TABLE_MEMBERS)
            existing = table.get((Query().group == edge.group) & (Query().login == edge.login))
            if existing:
                return Membership.from_dict(dict(existing))
            table.insert(edge.to_dict())
            _bump()
        return edge

    def remove_member(self, group: str, login: str) -> None:
        g, u = canonical_group_name(group), canonical_login(login)
        with _lock:
            removed = self._db.table(_TABLE_MEMBERS).remove(
                (Query().group == g) & (Query().login == u)
            )
            if removed:
                _bump()

    def members_of(self, group: str) -> list[str]:
        g = canonical_group_name(group)
        with _lock:
            rows = self._db.table(_TABLE_MEMBERS).search(Query().group == g)
        return sorted(str(r.get("login", "")) for r in rows)

    def groups_of(self, login: str) -> list[str]:
        """The membership lookup NFR-7 puts a latency budget on."""
        u = canonical_login(login)
        with _lock:
            rows = self._db.table(_TABLE_MEMBERS).search(Query().login == u)
        return sorted(str(r.get("group", "")) for r in rows)

    # -- retirement ----------------------------------------------------------------------------

    def _retire(self, kind: str, identifier: str) -> None:
        table = self._db.table(_TABLE_RETIRED)
        if not table.get((Query().kind == kind) & (Query().id == identifier)):
            table.insert({"kind": kind, "id": identifier})

    def _is_retired(self, kind: str, identifier: str) -> bool:
        return bool(
            self._db.table(_TABLE_RETIRED).get((Query().kind == kind) & (Query().id == identifier))
        )

    def is_login_retired(self, login: str) -> bool:
        """Public because the trail needs it: a retired login must stay *resolvable* so an old
        audit event naming it is still attributable (AC-2b). Retirement is not erasure."""
        with _lock:
            return self._is_retired(_TABLE_USERS, canonical_login(login))

    def retired_logins(self) -> list[str]:
        with _lock:
            rows = self._db.table(_TABLE_RETIRED).search(Query().kind == _TABLE_USERS)
        return sorted(str(r.get("id", "")) for r in rows)

    # -- lifecycle -----------------------------------------------------------------------------

    def is_empty(self) -> bool:
        """No users at all — what the first-boot bootstrap tests for (FR-10, FR-10b).

        Deliberately *users*, not the file: an existing file with no users still needs the
        bootstrap, which is what makes the upgrade from shared-token auth not a lockout.
        """
        with _lock:
            return not self._db.table(_TABLE_USERS).all()

    def close(self) -> None:
        self._db.close()

    def raw(self) -> dict[str, Any]:
        """Whole store as plain data. For the encrypted export (FR-14) and for tests."""
        with _lock:
            return {name: self._db.table(name).all() for name in self._db.tables()}


_store: IdentityStore | None = None


def store() -> IdentityStore:
    """Process-wide store. Rebuilt when ``NVIS_IDENTITY_FILE`` changes, so a test that repoints the
    path does not silently keep writing to the previous one."""
    global _store
    with _lock:
        wanted = identity_file()
        if _store is None or _store._path != wanted:
            _store = IdentityStore(wanted)
        return _store


def reset_for_tests() -> None:
    """Drop the cached handle. Named for what it is rather than pretending to be general."""
    global _store, _generation
    with _lock:
        if _store is not None:
            _store.close()
        _store = None
        _generation = 0
