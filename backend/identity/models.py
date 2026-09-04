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
import zoneinfo as _zoneinfo
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from identity.principals import ANONYMOUS, SEPARATOR, group_principal, user_principal

#: Longest login accepted. Not a security boundary -- a bound, so a pathological value cannot bloat
#: every audit event that names the subject. 128 is generous for ``first.last@long-domain.example``.
MAX_LOGIN = 128
MAX_DISPLAY_NAME = 256

#: RFC 5321 §4.5.3.1.3 caps a reverse-path at 256 octets including the angle brackets, so 254 is the
#: longest address that can actually be delivered. A bound, like MAX_LOGIN, not a security boundary.
MAX_EMAIL = 254

#: Generous for E.164 (15 digits max) plus a ``+`` and the punctuation people type.
MAX_PHONE_NUMBER = 32

#: Deliberately permissive: exactly one ``@`` (both sides exclude it), non-empty local part, and a
#: dotted domain with non-empty labels either side of the final dot. Full RFC 5322 is a famous trap
#: -- it permits quoted strings, comments and bare IP literals -- and an over-strict pattern that
#: rejects a deliverable address is a worse failure here than accepting an odd one, because the
#: consequence of a false rejection is an operator who cannot be created at all.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

#: What survives normalisation: an optional leading ``+`` then digits. Punctuation people type
#: (spaces, dashes, dots, parentheses) is stripped before this is applied rather than rejected.
_PHONE_RE = re.compile(r"^\+?[0-9]{3,20}$")
_PHONE_PUNCTUATION = re.compile(r"[\s().\-]")

#: IANA time-zone names are ``Area/Location``, occasionally with a third component
#: (``America/Argentina/Buenos_Aires``), plus a few bare names (``UTC``). Used as the shape gate
#: always, and as the *only* check when the time-zone database cannot be consulted -- see
#: :func:`canonical_zoneinfo`.
_ZONEINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+){0,2}$")

#: RFC 5646 language tag, the subset that actually occurs: language, optional script, optional
#: region. Deliberately not the full grammar -- extensions and private-use subtags have no consumer
#: here, and a tag this does not match is far more likely to be a typo than a valid exotic tag.
_LOCALE_RE = re.compile(r"^([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?$")

MAX_ZONEINFO = 64
MAX_LOCALE = 35

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


class DuplicateEmailError(IdentityError):
    """Two users cannot hold one email address.

    Its own type so the API can answer ``409 Conflict`` for it while a malformed address stays a
    ``400``. Both arrive as an :class:`IdentityError` from the store, and telling them apart by
    matching on the message text would make an error string load-bearing.
    """


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


def canonical_email(raw: object) -> str:
    """Validate and normalise an email address, or raise. Empty is permitted and means "none".

    Lower-cased, **not** case-folded -- and that difference from :func:`canonical_login` is
    deliberate. ``casefold`` maps ``\u00df`` to ``ss``, which for a login is the point (two spellings
    must not become two principals) but for an address would silently rewrite it into a *different*
    mailbox. Lower-casing is the conventional normalisation for email and cannot change where a
    message is delivered: the domain is case-insensitive per RFC 5321 §2.4, and while the local part
    is formally case-sensitive, no mail provider in practice treats it as such.

    Normalising here rather than at each caller is what makes the store's uniqueness check
    (``Alice@x.com`` and ``alice@x.com`` are one address) work without every call site remembering.
    """
    email = str(raw or "").strip()
    if not email:
        return ""
    if len(email) > MAX_EMAIL:
        raise IdentityError(f"email exceeds {MAX_EMAIL} characters")
    if not _EMAIL_RE.match(email):
        raise IdentityError(
            f"invalid email {email!r}: expected one local part, an @, and a dotted domain"
        )
    return email.lower()


def canonical_phone_number(raw: object) -> str:
    """Validate and normalise a phone number, or raise. Empty is permitted and means "none".

    Stored in E.164 shape (optional ``+`` then digits) because that is the one representation that
    is unambiguous internationally -- but the punctuation people actually type is stripped rather
    than refused, so ``+1 (555) 010-4477`` is accepted and stored as ``+15550104477``. No country
    or length-per-country validation: that needs a maintained numbering-plan database, and getting
    it wrong rejects a real number.
    """
    raw_text = str(raw or "").strip()
    if not raw_text:
        return ""
    if len(raw_text) > MAX_PHONE_NUMBER:
        raise IdentityError(f"phone number exceeds {MAX_PHONE_NUMBER} characters")
    digits = _PHONE_PUNCTUATION.sub("", raw_text)
    if not _PHONE_RE.match(digits):
        raise IdentityError(
            f"invalid phone number {raw_text!r}: expected digits, optionally starting with +"
        )
    return digits


def canonical_zoneinfo(raw: object) -> str:
    """Validate an IANA time-zone name, or raise. Empty is permitted and means "none".

    **Accepts what this process can resolve, and degrades rather than refusing.** The zone set is
    not a constant -- it comes from whichever time-zone database the runtime found. Measured
    2026-09-04: macOS offers 598 zones, ``python:3.12-slim`` 486, and the 113-zone difference is
    entirely legacy aliases (``America/Buenos_Aires``, ``America/Indianapolis``) that Debian omits
    and macOS still ships. Every canonical zone exists in both, so validating against the live
    database is right; hardcoding a list would rot, and pinning one environment's list would reject
    values the other accepts.

    If the database is absent altogether the name is checked for shape only. That is the *opposite*
    of the fail-closed rule governing security controls, and deliberately so: a display preference
    that cannot be saved because the base image lacks a package is a worse outcome than an
    unverified zone string, and this value grants nothing.
    """
    zone = str(raw or "").strip()
    if not zone:
        return ""
    if len(zone) > MAX_ZONEINFO:
        raise IdentityError(f"zoneinfo exceeds {MAX_ZONEINFO} characters")
    if not _ZONEINFO_RE.match(zone):
        # Runs before any lookup, so a traversal or a null byte never reaches `ZoneInfo`, which
        # resolves keys against filesystem paths.
        raise IdentityError(
            f"invalid zoneinfo {zone!r}: expected an IANA name like 'America/New_York'"
        )

    try:
        _zoneinfo.ZoneInfo(zone)
    except _zoneinfo.ZoneInfoNotFoundError as exc:
        # Distinguish "this zone is not in the database" from "there is no database at all". The
        # first is the caller's mistake; the second is the deployment's, and must not be reported
        # as theirs.
        try:
            _zoneinfo.ZoneInfo("UTC")
        except _zoneinfo.ZoneInfoNotFoundError:
            return zone
        raise IdentityError(
            f"unknown zoneinfo {zone!r}: not in this system's time-zone database"
        ) from exc
    except (ValueError, OSError) as exc:
        raise IdentityError(f"invalid zoneinfo {zone!r}") from exc
    return zone


def canonical_locale(raw: object) -> str:
    """Validate an RFC 5646 language tag and rewrite it in conventional casing, or raise.

    Tags are case-insensitive per RFC 5646 §2.1.1, but the *conventional* form is lower-case
    language, title-case script, upper-case region (``en-US``, ``zh-Hant-TW``). Normalising to it
    means ``EN-us`` and ``en-US`` are one stored value rather than two -- the same reason logins are
    case-folded: a value that renders two ways is a value that compares wrong.
    """
    tag = str(raw or "").strip()
    if not tag:
        return ""
    if len(tag) > MAX_LOCALE:
        raise IdentityError(f"locale exceeds {MAX_LOCALE} characters")
    match = _LOCALE_RE.match(tag)
    if not match:
        raise IdentityError(f"invalid locale {tag!r}: expected a language tag like 'en-US'")
    language, script, region = match.groups()
    parts = [language.lower()]
    if script:
        parts.append(script.capitalize())
    if region:
        parts.append(region.upper())
    return "-".join(parts)


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
    #: Contact attributes, named for the OpenID Connect Core 1.0 §5.1 standard claims so a future
    #: ``/userinfo`` can return them without a mapping layer. Both default to absent: the first-boot
    #: bootstrap admin has no address, and neither does any account created before this field
    #: existed, so requiring one *here* would invalidate records that are legitimately valid. The
    #: requirement that a **newly created** user supplies an email lives at the API boundary instead.
    email: str = ""
    phone_number: str = ""
    #: The rest of the OIDC §5.1 profile set worth carrying here. `given_name`/`family_name` exist
    #: mainly so federation becomes a mapping of identical names rather than a translation layer;
    #: `zoneinfo` and `locale` earn their place in a product whose every screen is timestamps.
    given_name: str = ""
    family_name: str = ""
    zoneinfo: str = ""
    locale: str = ""
    #: Whether the provider asserts the address above was verified. Only ever set by an explicit,
    #: audited admin action -- and cleared automatically by :meth:`edited` when the address changes,
    #: because a stale assertion is an account-linking hazard rather than a cosmetic mistake.
    email_verified: bool = False
    phone_number_verified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "login", canonical_login(self.login))
        object.__setattr__(self, "display_name", _clean_display_name(self.display_name))
        object.__setattr__(self, "email", canonical_email(self.email))
        object.__setattr__(self, "phone_number", canonical_phone_number(self.phone_number))
        object.__setattr__(self, "given_name", _clean_display_name(self.given_name))
        object.__setattr__(self, "family_name", _clean_display_name(self.family_name))
        object.__setattr__(self, "zoneinfo", canonical_zoneinfo(self.zoneinfo))
        object.__setattr__(self, "locale", canonical_locale(self.locale))
        object.__setattr__(self, "email_verified", bool(self.email_verified) and bool(self.email))
        object.__setattr__(
            self, "phone_number_verified", bool(self.phone_number_verified) and bool(self.phone_number)
        )
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
        email: str | None = None,
        phone_number: str | None = None,
        email_verified: bool | None = None,
        phone_number_verified: bool | None = None,
        given_name: str | None = None,
        family_name: str | None = None,
        zoneinfo: str | None = None,
        locale: str | None = None,
    ) -> User:
        """A copy with the mutable fields changed. **Cannot** change the login (FR-2).

        Note what is absent from the signature: there is no ``login`` parameter and no
        ``auth_source`` parameter. Immutability enforced by the API rather than by a check, so a
        caller cannot get it wrong and be told at runtime.

        **Changing an address clears its verified flag**, and that rule lives here for the same
        reason the login has no parameter: a caller cannot forget it. OpenID Connect Core §5.1
        defines ``email_verified`` as this provider asserting the address was verified, and relying
        parties use it to *link accounts* -- so a ``true`` carried over onto a newly-set address is
        an account-takeover primitive, not an untidy field. Setting an address and asserting it
        verified in one call is refused rather than silently resolved in either direction, because
        whichever way it resolved, one of the two arguments would have been quietly ignored.
        """
        changes: dict[str, Any] = {"updated_at": _utcnow()}
        if display_name is not None:
            changes["display_name"] = _clean_display_name(display_name)
        for value, address_key, flag_key, requested_flag in (
            (email, "email", "email_verified", email_verified),
            (phone_number, "phone_number", "phone_number_verified", phone_number_verified),
        ):
            if value is None:
                continue
            canonical = canonical_email(value) if address_key == "email" else canonical_phone_number(value)
            changes[address_key] = canonical
            if canonical == getattr(self, address_key):
                continue
            if requested_flag:
                raise IdentityError(
                    f"cannot change {address_key} and assert {flag_key} in one call: verification "
                    "describes a specific address, so assert it after the change lands"
                )
            # The address moved, so any standing assertion about it no longer describes anything.
            changes[flag_key] = False
        if email_verified is not None and "email_verified" not in changes:
            changes["email_verified"] = bool(email_verified)
        if phone_number_verified is not None and "phone_number_verified" not in changes:
            changes["phone_number_verified"] = bool(phone_number_verified)
        # No verified-flag coupling on these four: nothing asserts a locale, so they are plain edits.
        if given_name is not None:
            changes["given_name"] = _clean_display_name(given_name)
        if family_name is not None:
            changes["family_name"] = _clean_display_name(family_name)
        if zoneinfo is not None:
            changes["zoneinfo"] = canonical_zoneinfo(zoneinfo)
        if locale is not None:
            changes["locale"] = canonical_locale(locale)
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
            "email": self.email,
            "phone_number": self.phone_number,
            "email_verified": self.email_verified,
            "phone_number_verified": self.phone_number_verified,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "zoneinfo": self.zoneinfo,
            "locale": self.locale,
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
