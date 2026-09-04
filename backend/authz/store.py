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
"""Reading and writing the policy file, for the CRUD surface (FR-19, FR-20).

The engine (`authz/policy.py`) only *reads*, and deliberately: an evaluator that writes is an
evaluator whose cache invalidates itself, which is the Phase 2 revocation-cache bug. Writes live
here instead.

**Atomic, with the mode set on the temp file.** `identity-auth-traps.mdc` trap 3 measured this:
`os.replace` preserves the *source* inode's mode, so a temp created by a plain `open()` takes the
umask and the replaced file becomes 0644 no matter what the constructor chmod'd. Creating the temp
with `os.open(..., 0o600)` means the window never opens, rather than being corrected after a
world-readable inode already exists. Policies are not secret, but they *are* security
configuration, and a file anyone can rewrite is a privilege-escalation path.

**A write always parses first.** `parse_document` runs before anything reaches disk, so the file
the engine reads is never one it would refuse — an unparseable policy set makes the engine deny
every request (correctly), so writing one would be a self-inflicted outage.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from authz import policy as policy_engine
from authz.schema import PolicyDocument, PolicyFile, parse_document

_lock = threading.RLock()


class PolicyStoreError(Exception):
    """A write that would leave the policy set unusable."""


def path() -> Path:
    """Where the policy set lives. Same resolution the engine uses, so they cannot disagree."""
    raw = (os.environ.get("RACK_WEB_POLICY_FILE") or "").strip()
    return Path(raw) if raw else policy_engine.DEFAULT_POLICY_FILE


def read_all() -> list[PolicyDocument]:
    """Every policy, parsed. An absent file is an empty set, not an error.

    A fresh deployment has no policy file and that means default-deny — the same state
    `authz.install_default` treats as "no policy yet", so the CRUD surface must agree with it
    rather than inventing a file on first read.
    """
    target = path()
    try:
        raw = json.loads(target.read_text())
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyStoreError(f"{target} is unreadable: {exc}") from exc

    try:
        parsed = PolicyFile.model_validate(raw if isinstance(raw, dict) else {"policies": raw})
    except ValueError as exc:
        raise PolicyStoreError(f"{target} is not a valid policy file: {exc}") from exc
    return list(parsed.policies)


def _write_all(documents: list[PolicyDocument]) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {"version": 1, "policies": [d.model_dump(exclude_none=True) for d in documents]},
        indent=1,
    )

    # 0o600 on the TEMP file, not a chmod after the replace. See the module docstring.
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".policies-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(body + "\n")
        os.replace(tmp_name, target)
    except BaseException:
        # Leaving a temp behind on failure would accumulate in var/store and eventually confuse
        # somebody reading the directory for the real file.
        Path(tmp_name).unlink(missing_ok=True)
        raise


def get(policy_id: str) -> PolicyDocument | None:
    return next((d for d in read_all() if d.id == policy_id), None)


def create(raw: object) -> PolicyDocument:
    """Add a policy. Refuses a duplicate id rather than overwriting.

    Overwriting on create would make "save" silently replace somebody else's policy when two
    operators pick the same name — and the one who loses never finds out.
    """
    document = parse_document(raw)
    with _lock:
        existing = read_all()
        if any(d.id == document.id for d in existing):
            raise PolicyStoreError(f"a policy named {document.id!r} already exists")
        existing.append(document)
        _write_all(existing)
    return document


def update(policy_id: str, raw: object) -> PolicyDocument:
    """Replace a policy in place. The id in the body must match the id in the URL.

    Allowing a body to rename the policy would let one PUT delete a policy and create another,
    which breaks every attachment pointing at the old name without saying so.
    """
    document = parse_document(raw)
    if document.id != policy_id:
        raise PolicyStoreError(
            f"body names policy {document.id!r} but the URL names {policy_id!r}; a policy cannot "
            f"be renamed by updating it, because every attachment refers to it by name"
        )
    with _lock:
        existing = read_all()
        for index, current in enumerate(existing):
            if current.id == policy_id:
                existing[index] = document
                _write_all(existing)
                return document
    raise PolicyStoreError(f"no policy named {policy_id!r}")


def delete(policy_id: str, *, force: bool = False) -> list[str]:
    """Remove a policy. Refuses while it is still attached, unless forced.

    Returns the subjects that were attached, so the caller can say who is affected.

    Refusing by default is the whole point: deleting an attached policy silently changes what
    somebody can do, and the operator doing it usually cannot see the attachments. `force` exists
    because the alternative is detaching by hand from an unknown number of subjects first.
    """
    from identity.store import IdentityStore

    store = IdentityStore()
    try:
        attached = store.subjects_with_policy(policy_id)
    finally:
        store.close()

    if attached and not force:
        raise PolicyStoreError(
            f"{policy_id} is attached to {len(attached)} subject(s): {', '.join(attached)}. "
            f"Deleting it changes what they can do. Detach it first, or delete with force=true."
        )

    with _lock:
        existing = read_all()
        remaining = [d for d in existing if d.id != policy_id]
        if len(remaining) == len(existing):
            raise PolicyStoreError(f"no policy named {policy_id!r}")
        _write_all(remaining)

    if attached:
        # Forced through. Detach so the store does not keep rows pointing at a policy that is
        # gone -- the engine tolerates those, but they show in "attached to" columns as noise.
        store = IdentityStore()
        try:
            for subject in attached:
                store.detach_policy(subject, policy_id)
        finally:
            store.close()
    return list(attached)
