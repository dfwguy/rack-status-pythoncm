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

import contextlib
import os
import threading
import time
from pathlib import Path
from typing import Any

# The one thing borrowed from the catalog: its atomic-write storage class, which takes only a path
# and knows nothing of catalog tables. `touch` comes from tinydb directly rather than through
# catalog.store, which merely re-exports it — importing a name a module happens to have imported
# breaks the day it stops needing it.
from catalog.store import _AtomicJSONStorage
from json_db import drop_caches, exclusive_lock
from tinydb import Query, TinyDB
from tinydb.storages import touch

from identity.models import (
    DuplicateEmailError,
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
#: Phase 3. Separate table rather than a field on the user, so an attachment is one row and
#: detaching cannot rewrite an unrelated part of a user record.
_TABLE_POLICY_ATTACHMENTS = "policy_attachments"
_TABLE_RETIRED = "retired"
#: Revocation lives in the identity file rather than beside it: it is identity state, and sharing the
#: file means it also shares the atomic write, the flock and — the part that matters — the generation
#: counter `identity.revocation` keys its cache on (FR-6, Decision 4).
_TABLE_REVOKED_SUBJECTS = "revoked_subjects"
_TABLE_REVOKED_TOKENS = "revoked_tokens"
_TABLE_NOT_BEFORE = "not_before"


class _OwnerOnlyAtomicStorage(_AtomicJSONStorage):
    """The catalog's atomic storage, plus owner-only permissions that survive a write.

    Subclassed rather than fixed upstream because the catalog holds configuration and does not want
    `0o600` — its file is legitimately readable, and narrowing it would be an unrelated behaviour
    change to a shared component.

    The bug this exists to prevent, found by a test rather than by reasoning: the parent writes to a
    sibling temp file and `os.replace`s it, so **every write installs a new inode carrying whatever
    mode that temp file had** — the process umask, typically `0o644`, unless the mode is given at
    creation. A `chmod` in the store's constructor is therefore undone by the first save, and the
    file holding SRP verifiers quietly becomes world-readable.

    **The fix is now the mode, not a correction after the fact.** `file_mode` is passed to the
    `os.open` that creates the temp, so the inode is never anything but `0o600` — where the previous
    version replaced a `0o644` file and then narrowed it, leaving a window open on every single
    write. That is the second half of `identity-auth-traps.mdc` §3: create with the mode you want
    rather than shrinking the margin around the defect. `audit/sink.py` still has the older shape.

    The `chmod` below is kept as belt and braces for a file that already exists with the wrong mode
    from an earlier version or a restore — it cannot open a window, because by the time it runs the
    replaced inode is already `0o600`.
    """

    file_mode = 0o600

    def write(self, data: dict[str, Any]) -> None:
        super().write(data)
        try:
            os.chmod(self._path, self.file_mode)
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

    @contextlib.contextmanager
    def _exclusive(self):
        """Serialise a whole read-modify-write against other *processes*.

        `_lock` is a `threading.RLock`, so it only ever covered threads inside one interpreter --
        and more than one process writes this file: a `cli.py` command run while the server is up,
        two local instances, a container restarting while the old one drains.

        Here the stakes are higher than in the catalog. TinyDB computes each write from a
        whole-file read, so an interleaved write silently discards the other process's rows --
        measured at 63 of 240 documents surviving, on the file holding SRP verifiers and the
        revocation table. A dropped revocation row is INV-19 in `identity-auth-traps.mdc` §8: the
        one failure mode that *grants* access rather than denying it.
        """
        with exclusive_lock(self._path):
            # Inside the lock, or the re-read that follows sees a file another writer is about to
            # replace. `json_db` has the measurements and the two failures this prevents.
            drop_caches(self._db)
            yield

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
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_USERS)
            if table.get(Query().login == user.login):
                raise IdentityError(f"login {user.login!r} already exists")
            if self._is_retired(_TABLE_USERS, user.login):
                raise IdentityError(
                    f"login {user.login!r} has been used before and cannot be reused; "
                    "choose another (identifiers are retired permanently, FR-2b)"
                )
            self._refuse_duplicate_email(table, user)
            table.insert(user.to_dict())
            _bump()
        return user

    def update_user(self, user: User) -> User:
        """Replaces a user's mutable fields. The login is the key, and :meth:`User.edited` cannot
        change it, so this cannot become a rename by accident."""
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_USERS)
            if not table.get(Query().login == user.login):
                raise IdentityError(f"no such user {user.login!r}")
            self._refuse_duplicate_email(table, user)
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
        with _lock, self._exclusive():
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
        with _lock, self._exclusive():
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
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_GROUPS)
            if not table.get(Query().name == group.name):
                raise IdentityError(f"no such group {group.name!r}")
            table.update(group.to_dict(), Query().name == group.name)
            _bump()
        return group

    def delete_group(self, name: str) -> None:
        key = canonical_group_name(name)
        with _lock, self._exclusive():
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
        with _lock, self._exclusive():
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
        with _lock, self._exclusive():
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

    # -- policy attachment (Phase 3, FR-18) ----------------------------------------------------
    #
    # Deliberately the same shape as membership above. An attachment is the same kind of relation —
    # a named edge between a principal and a thing — and a second mechanism for it would drift from
    # the first, which is how two tables come to disagree about whether a user still has a grant.
    #
    # One difference, and it is the reason `attach_policy` cannot simply copy `add_member`: the
    # policy documents live in a FILE the operator edits (`var/store/policies.json`), not in this
    # store. So this cannot verify the policy end exists the way `add_member` verifies the group —
    # see the note there.

    def attach_policy(self, subject: str, policy_id: str) -> None:
        """Attach a policy to `user:<login>` or `group:<name>`.

        The *principal* end is verified; the *policy* end is not, and that asymmetry is deliberate
        rather than an omission. Policies are a file the operator edits outside this store, so
        requiring the document to exist first would forbid the natural order of work — attach the
        role, then write the rule — and would make an attachment fail because of an unrelated
        editing state. An attachment to a policy that does not exist is safe: the engine logs it and
        skips it, so the effect is default-deny rather than an accidental grant.
        """
        kind, _, name = str(subject).partition(":")
        if kind not in ("user", "group") or not name:
            raise IdentityError(
                f"attachment subject must be 'user:<login>' or 'group:<name>', got {subject!r}"
            )
        with _lock, self._exclusive():
            if kind == "user":
                if not self._db.table(_TABLE_USERS).get(Query().login == canonical_login(name)):
                    raise IdentityError(f"no such user {name!r}")
                normalised = f"user:{canonical_login(name)}"
            else:
                if not self._db.table(_TABLE_GROUPS).get(
                    Query().name == canonical_group_name(name)
                ):
                    raise IdentityError(f"no such group {name!r}")
                normalised = f"group:{canonical_group_name(name)}"

            table = self._db.table(_TABLE_POLICY_ATTACHMENTS)
            if table.get((Query().subject == normalised) & (Query().policy == policy_id)):
                return  # idempotent, like add_member
            table.insert({"subject": normalised, "policy": policy_id})
            _bump()

    def detach_policy(self, subject: str, policy_id: str) -> None:
        with _lock, self._exclusive():
            removed = self._db.table(_TABLE_POLICY_ATTACHMENTS).remove(
                (Query().subject == subject) & (Query().policy == policy_id)
            )
            if removed:
                _bump()

    def policies_for(self, subject: str) -> list[str]:
        """Policy ids attached to one subject. The engine's hot path."""
        with _lock:
            rows = self._db.table(_TABLE_POLICY_ATTACHMENTS).search(Query().subject == subject)
        return sorted(str(r.get("policy", "")) for r in rows)

    def subjects_with_policy(self, policy_id: str) -> list[str]:
        """Who a policy is attached to.

        Powers the "attached to" column (FR-20) and the question behind it: deleting a policy that
        is still attached silently changes access, so the UI has to be able to show that first.
        """
        with _lock:
            rows = self._db.table(_TABLE_POLICY_ATTACHMENTS).search(Query().policy == policy_id)
        return sorted(str(r.get("subject", "")) for r in rows)

    # -- retirement ----------------------------------------------------------------------------

    def _retire(self, kind: str, identifier: str) -> None:
        table = self._db.table(_TABLE_RETIRED)
        if not table.get((Query().kind == kind) & (Query().id == identifier)):
            table.insert({"kind": kind, "id": identifier})

    @staticmethod
    def _refuse_duplicate_email(table: Any, user: User) -> None:
        """An email may identify at most one user. **Call with ``_lock`` held.**

        Inside the caller's lock for the same reason the duplicate-login check is: two threads
        racing to claim one address must not both win, and a check taken outside the lock is a
        check that can be true when read and false when written.

        **An absent address is exempt.** This is the distinction the obvious implementation gets
        wrong: every account predating this field has ``email == ""``, so a plain "is anyone using
        this value" test would let the first such user through and then refuse every other one,
        reporting a duplicate address to an operator who supplied no address at all. Uniqueness is a
        statement about addresses that exist -- the same reason a SQL unique index tolerates many
        NULLs.

        Comparison needs no case-folding here because :func:`canonical_email` already lower-cased
        both sides on construction; that normalisation is what makes one stored form comparable.
        """
        if not user.email:
            return
        clash = table.get(Query().email == user.email)
        if clash and str(clash.get("login") or "") != user.login:
            raise DuplicateEmailError(
                f"email {user.email!r} is already used by {clash.get('login')!r}; "
                "an address may identify at most one user"
            )

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

    # -- revocation (FR-6) ---------------------------------------------------------------------

    def revoke_subject(self, principal: str, reason: str = "") -> None:
        """Deny every token for a principal until explicitly restored."""
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_REVOKED_SUBJECTS)
            row = {"principal": principal, "reason": reason, "at": time.time()}
            if table.get(Query().principal == principal):
                table.update(row, Query().principal == principal)
            else:
                table.insert(row)
            _bump()

    def restore_subject(self, principal: str) -> None:
        with _lock, self._exclusive():
            if self._db.table(_TABLE_REVOKED_SUBJECTS).remove(Query().principal == principal):
                _bump()

    def revoke_token(self, jti: str, expires_at: float, reason: str = "") -> None:
        """Deny one token by id — logging out a single session, or burning a leaked refresh token.

        ``expires_at`` is stored so the row can be dropped once the token would have expired anyway.
        Without it this table grows for the life of the deployment, and it sits on the hot path.
        """
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_REVOKED_TOKENS)
            if not table.get(Query().jti == jti):
                table.insert(
                    {
                        "jti": jti,
                        "expires_at": float(expires_at),
                        "reason": reason,
                        "at": time.time(),
                    }
                )
            self._prune_expired_tokens_locked()
            _bump()

    def set_not_before(self, principal: str, moment: float) -> None:
        """Deny every token for a principal *issued before* ``moment``.

        The mechanism behind "log out everywhere" and "revoke on password change". Needed because
        outstanding token ids cannot be enumerated — only their issue time can be compared.
        """
        with _lock, self._exclusive():
            table = self._db.table(_TABLE_NOT_BEFORE)
            row = {"principal": principal, "at": float(moment)}
            if table.get(Query().principal == principal):
                table.update(row, Query().principal == principal)
            else:
                table.insert(row)
            _bump()

    def revocation_snapshot(self) -> dict[str, Any]:
        """Everything a revocation decision needs, in one read.

        One call rather than four because the caller caches the result on the generation counter, and
        four separate reads would be four chances to observe a half-applied change.

        Includes ``disabled`` and ``retired`` logins deliberately. Without them, deleting or
        disabling a user and forgetting to *also* write a revocation row would leave their existing
        tokens working until expiry — a gap that costs nothing to close while the whole snapshot is
        being cached anyway.

        **Strictly read-only, and that is load-bearing.** This originally pruned expired token rows
        here, which was self-defeating twice over: TinyDB's ``remove()`` rewrites the file *even when
        nothing matches*, so every read performed a write, and that write changed the file's mtime —
        which is part of the cache key ``identity.revocation`` uses, so the cache was invalidated by
        its own reads and never hit once. Caught by a test asserting the cache actually serves
        reads. Expired rows are filtered out in memory instead and cleared by the next write.
        """
        now = time.time()
        with _lock:
            return {
                "subjects": {
                    str(r["principal"]): str(r.get("reason", ""))
                    for r in self._db.table(_TABLE_REVOKED_SUBJECTS).all()
                },
                "tokens": {
                    str(r["jti"]): float(r.get("expires_at", 0))
                    for r in self._db.table(_TABLE_REVOKED_TOKENS).all()
                    if float(r.get("expires_at", 0)) >= now
                },
                "not_before": {
                    str(r["principal"]): float(r.get("at", 0))
                    for r in self._db.table(_TABLE_NOT_BEFORE).all()
                },
                "disabled": {
                    str(r.get("login", ""))
                    for r in self._db.table(_TABLE_USERS).all()
                    if r.get("disabled")
                },
                "retired": {
                    str(r.get("id", ""))
                    for r in self._db.table(_TABLE_RETIRED).search(Query().kind == _TABLE_USERS)
                },
            }

    def _prune_expired_tokens_locked(self) -> None:
        """Drop revocation rows for tokens that have expired on their own.

        Safe because an expired token is refused by :func:`identity.tokens.verify` regardless, so the
        row stops carrying information the moment ``expires_at`` passes.

        Called from **write** paths only. ``remove()`` rewrites the file even with nothing to remove,
        so calling it on a read turned every lookup into a write — see
        :meth:`revocation_snapshot`.
        """
        self._db.table(_TABLE_REVOKED_TOKENS).remove(Query().expires_at < time.time())

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
