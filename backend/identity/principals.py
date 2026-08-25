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
"""The one place a principal identifier is formatted or parsed (FR-15a).

Grammar, per ``docs/RESOURCE_MODEL.md``::

    principal ::= "user:" <login> | "group:" <name> | "service:" <name> | "anonymous"

Why a module rather than an f-string at each call site: the action vocabulary already taught this
lesson in Phase 1. Structure encoded in a string acquires a regex at the third call site, that regex
disagrees with the second, and the disagreement is found by reading an audit trail that attributes
actions to the wrong subject. ``AC-25b`` asserts at source level that nothing outside this module
splits or concatenates a principal.

Deliberately NOT ``urn:nvis:identity:user:<id>``. Measured: a URN principal costs +50 bytes on a
397-byte audit event -- 12.6% -- and ``principal`` appears in every event, so it would cut FR-7b's
retention from ~41.8 days to ~37. The ``urn:`` prefix is identical for every principal in the system,
making it simultaneously the most repeated and least informative bytes in the file.
"""

from __future__ import annotations

#: The kinds that can act. ``service`` is unused in Phase 2 and reserved so that adding automation
#: later is not a grammar change -- the audit trail would otherwise need reinterpreting.
KIND_USER = "user"
KIND_GROUP = "group"
KIND_SERVICE = "service"
KINDS = (KIND_USER, KIND_GROUP, KIND_SERVICE)

#: An unauthenticated or refused request. Carried over from Phase 1, where it was the *only* value
#: ``principal`` ever took; it stays because a refusal still needs a subject in the trail.
ANONYMOUS = "anonymous"

SEPARATOR = ":"


class PrincipalError(ValueError):
    """A principal string that cannot be produced or understood."""


def format_principal(kind: str, identifier: str) -> str:
    """``("user", "rkoike") -> "user:rkoike"``.

    Raises rather than coercing. A silently mangled principal is worse than a failed request: the
    request is retried, the audit event is not.
    """
    if kind not in KINDS:
        raise PrincipalError(f"unknown principal kind {kind!r}; expected one of {KINDS}")
    if not identifier:
        raise PrincipalError(f"{kind} principal needs an identifier")
    if SEPARATOR in identifier:
        # The separator is what makes the grammar unambiguous, so an identifier containing it would
        # parse back as a different kind/id split. models.validate_login refuses it at the door;
        # this is the second gate, for identifiers arriving from anywhere else.
        raise PrincipalError(f"{kind} identifier may not contain {SEPARATOR!r}: {identifier!r}")
    return f"{kind}{SEPARATOR}{identifier}"


def parse_principal(principal: str) -> tuple[str, str]:
    """``"user:rkoike" -> ("user", "rkoike")``; ``"anonymous" -> ("anonymous", "")``.

    Splits once, not greedily: an identifier cannot contain the separator (enforced above), so a
    second one means the string is malformed rather than nested.
    """
    raw = (principal or "").strip()
    if raw == ANONYMOUS:
        return ANONYMOUS, ""
    if SEPARATOR not in raw:
        raise PrincipalError(f"not a principal: {principal!r}")
    kind, _, identifier = raw.partition(SEPARATOR)
    if kind not in KINDS:
        raise PrincipalError(f"unknown principal kind {kind!r} in {principal!r}")
    if not identifier or SEPARATOR in identifier:
        raise PrincipalError(f"malformed {kind} principal: {principal!r}")
    return kind, identifier


def user_principal(login: str) -> str:
    """The common case, named so call sites read as intent rather than as string building."""
    return format_principal(KIND_USER, login)


def group_principal(name: str) -> str:
    return format_principal(KIND_GROUP, name)


def is_anonymous(principal: str) -> bool:
    return (principal or "").strip() == ANONYMOUS
