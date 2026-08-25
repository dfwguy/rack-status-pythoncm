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
"""User, group and membership records for the local identity store (FR-1, FR-2, FR-8).

The load-bearing decision these records encode: **the login IS the identity** (FR-2). There is no
second opaque id, which is what makes an audit event say ``user:rkoike`` rather than a uuid the
reader has to resolve. The price is that a login must be immutable (FR-2) and never reused (FR-2b),
and both of those are enforced here and in the store rather than left to convention:

* immutability lives in the type -- the records are frozen, and :meth:`User.edited` cannot change a
  login because it does not accept one;
* non-reuse is a store concern (a retired-identifier set), because it is a fact about history rather
  than about a record.

Why non-reuse matters more than it looks: if a login could be recycled, then ``jdoe`` departs, the
account is deleted, a different ``jdoe`` is created -- and every action the first person took is now
attributed to the second, while any policy naming ``user:jdoe`` hands over their grants. That is not
an ambiguous trail, it is a confidently wrong one, about the product's own acceptance evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from identity.principals import ANONYMOUS, SEPARATOR, group_principal, user_principal

#: Longest login accepted. Not a security boundary -- a bound, so a pathological value cannot bloat
#: every audit event that names the subject. 128 is generous for ``first.last@long-domain.example``.
MAX_LOGIN = 128
MAX_DISPLAY_NAME = 256

#: Printable, no whitespace, and explicitly **no colon** -- the principal grammar separates on it, so
#: a login containing one would parse back as a different kind/id split. Email shapes must pass,
#: because federation will supply them (`rkoike@nvidia.com`).
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._@+-]*[A-Za-z0-9])?$")

#: Logins that would make a trail ambiguous to a human even though the grammar keeps them distinct.
#: ``anonymous`` is the reserved unauthenticated principal; a user named that would read as one.
_RESERVED_LOGINS = frozenset({ANONYMOUS, "system", "none", "null"})

AUTH_LOCAL = "local"
AUTH_FEDERATED = "federated"
AUTH_SOURCES = (AUTH_LOCAL, AUTH_FEDERATED)


class IdentityError(ValueError):
    """A record that cannot exist as described."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_login(raw: object) -> str:
    """Validate and canonicalise a login, or raise (FR-2a).

    Case-folded, not merely lower-cased: ``casefold`` handles the non-ASCII pairs ``lower`` misses,
    and the point is that two logins differing only in case can never both exist -- nor render as two
    different principal strings for one subject, which is how a policy comes to match one and miss
    the other.
    """
    login = str(raw or "").strip()
    if not login:
        raise IdentityError("login must not be empty")
    if len(login) > MAX_LOGIN:
        raise IdentityError(f"login exceeds {MAX_LOGIN} characters")
    if SEPARATOR in login:
        raise IdentityError(f"login may not contain {SEPARATOR!r} (it separates the principal grammar)")
    if not _LOGIN_RE.match(login):
        raise IdentityError(
            f"invalid login {login!r}: use letters, digits, and . _ @ + - "
            "starting and ending alphanumeric"
        )
    folded = login.casefold()
    if folded in _RESERVED_LOGINS:
        raise IdentityError(f"login {login!r} is reserved")
    return folded


def canonical_group_name(raw: object) -> str:
    """Group names follow the login rules; the reasoning is identical (FR-8, FR-2b).

    A recycled group name would silently re-grant whatever the previous group had, so groups get the
    same immutability and non-reuse treatment as users.
    """
    name = str(raw or "").strip()
    if not name:
        raise IdentityError("group name must not be empty")
    if len(name) > MAX_LOGIN:
        raise IdentityError(f"group name exceeds {MAX_LOGIN} characters")
    if SEPARATOR in name:
        raise IdentityError(f"group name may not contain {SEPARATOR!r}")
    if not _LOGIN_RE.match(name):
        raise IdentityError(f"invalid group name {name!r}")
    return name.casefold()


def _clean_display_name(raw: object) -> str:
    text = str(raw or "").strip()
    if len(text) > MAX_DISPLAY_NAME:
        raise IdentityError(f"display name exceeds {MAX_DISPLAY_NAME} characters")
    return text


@dataclass(frozen=True, slots=True)
class User:
    """One operator.

    Frozen on purpose. ``login`` is the identity (FR-2), so the type refuses to let it move: there is
    no setter, and :meth:`edited` does not take one. Changing the identifier is not an operation the
    product has -- a genuine name change moves ``display_name`` (FR-2c), and a typo is corrected by
    creating a new user and retiring the old login.

    ``srp_salt`` and ``srp_verifier`` are opaque hex here. This record deliberately does not know how
    they are produced: ``identity.srp`` owns that, including the fact that the library's defaults are
    SHA-1 with a 4-byte salt and must never be used (FR-4e).
    """

    login: str
    display_name: str = ""
    srp_salt: str = ""
    srp_verifier: str = ""
    auth_source: str = AUTH_LOCAL
    disabled: bool = False
    must_change_password: bool = False
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "login", canonical_login(self.login))
        object.__setattr__(self, "display_name", _clean_display_name(self.display_name))
        if self.auth_source not in AUTH_SOURCES:
            raise IdentityError(f"auth_source must be one of {AUTH_SOURCES}, got {self.auth_source!r}")
        # A local user with no verifier cannot authenticate. Permitted at construction because
        # bootstrap creates the record and sets the credential in two steps, and because a federated
        # user legitimately has none -- so this is checked where it matters (login), not here.

    @property
    def principal(self) -> str:
        """``user:rkoike`` -- the form that appears in audit events and policy (FR-15)."""
        return user_principal(self.login)

    @property
    def has_credential(self) -> bool:
        return bool(self.srp_salt and self.srp_verifier)

    def edited(
        self,
        *,
        display_name: str | None = None,
        disabled: bool | None = None,
        must_change_password: bool | None = None,
        srp_salt: str | None = None,
        srp_verifier: str | None = None,
    ) -> User:
        """A copy with the mutable fields changed. **Cannot** change the login (FR-2).

        Note what is absent from the signature: there is no ``login`` parameter and no
        ``auth_source`` parameter. Immutability enforced by the API rather than by a check, so a
        caller cannot get it wrong and be told at runtime.
        """
        changes: dict[str, Any] = {"updated_at": _utcnow()}
        if display_name is not None:
            changes["display_name"] = _clean_display_name(display_name)
        if disabled is not None:
            changes["disabled"] = bool(disabled)
        if must_change_password is not None:
            changes["must_change_password"] = bool(must_change_password)
        if srp_salt is not None:
            changes["srp_salt"] = str(srp_salt)
        if srp_verifier is not None:
            changes["srp_verifier"] = str(srp_verifier)
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "display_name": self.display_name,
            "srp_salt": self.srp_salt,
            "srp_verifier": self.srp_verifier,
            "auth_source": self.auth_source,
            "disabled": self.disabled,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def public_dict(self) -> dict[str, Any]:
        """What an API may return: everything except the credential material (NFR-4)."""
        row = self.to_dict()
        row.pop("srp_salt", None)
        row.pop("srp_verifier", None)
        row["has_credential"] = self.has_credential
        row["principal"] = self.principal
        return row

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> User:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (row or {}).items() if k in known})


@dataclass(frozen=True, slots=True)
class Group:
    """A flat collection of users (FR-8). A group cannot contain a group.

    Flatness is a decision, not an omission: it removes transitive membership resolution and the
    cycle guard that would need, and it makes the precedence rule (FR-9, a user-level denial beats a
    group-level permission) something an operator can hold in their head.
    """

    name: str
    description: str = ""
    created_at: str = field(default_factory=_utcnow)
    updated_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", canonical_group_name(self.name))
        object.__setattr__(self, "description", _clean_display_name(self.description))

    @property
    def principal(self) -> str:
        return group_principal(self.name)

    def edited(self, *, description: str | None = None) -> Group:
        """A copy with the description changed. **Cannot** rename the group (FR-2b applies)."""
        changes: dict[str, Any] = {"updated_at": _utcnow()}
        if description is not None:
            changes["description"] = _clean_display_name(description)
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    public_dict = to_dict

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Group:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (row or {}).items() if k in known})


@dataclass(frozen=True, slots=True)
class Membership:
    """A ``(group, user)`` edge.

    Not an entity with its own identifier, deliberately (see ``docs/RESOURCE_MODEL.md``): giving an
    edge an id invites a policy that grants membership editing without naming the group it applies
    to, which is the same shape of mistake as a policy that grants "attach" without naming what.
    """

    group: str
    login: str
    created_at: str = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        object.__setattr__(self, "group", canonical_group_name(self.group))
        object.__setattr__(self, "login", canonical_login(self.login))

    @property
    def key(self) -> tuple[str, str]:
        return (self.group, self.login)

    def to_dict(self) -> dict[str, Any]:
        return {"group": self.group, "login": self.login, "created_at": self.created_at}

    public_dict = to_dict

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Membership:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (row or {}).items() if k in known})
