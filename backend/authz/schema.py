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
"""The policy document schema (FR-17a, FR-17b).

**One definition, two consumers.** The server validates every write against these models, and the
same models export JSON Schema for the Monaco editor to validate against in the browser. The
alternative — a schema here and a hand-maintained copy in TypeScript — drifts, and the drift
presents as a document the editor accepts and the server rejects, which is the worst place to
discover a disagreement.

WHAT IS DELIBERATELY *NOT* IN THIS SCHEMA
------------------------------------------
**Principals.** A policy document names actions and resources; who may use it is decided by
attachment (FR-7a). The retired inline ``principals`` field is *rejected* rather than ignored — a
document that half-works is worse than one refused, because the operator would see it stored and
assume it applies.

**Valid action names.** They come from a runtime registry that grows every phase, so a static enum
here would be stale from the next merge. The editor gets them from the server (FR-19a); this schema
constrains their *shape* only.

WHY THE EFFECT IS A STRING ENUM AND NOT A BOOLEAN
--------------------------------------------------
``effect: "deny"`` reads correctly in a diff and in an audit event. ``allow: false`` does not: a
reviewer scanning a merge request sees the field name before the value, and a policy whose
prohibition depends on a boolean two columns to the right is a policy that gets misread. The
evaluation semantics make this load-bearing — a deny halts evaluation, so misreading one inverts the
outcome.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from strenum import StrEnum

#: Actions look like ``service.sub:verb`` — the Phase 1 vocabulary grammar. A trailing ``*`` is
#: permitted so a policy can name a family (``rack.power:*``) without enumerating it, which is what
#: keeps a policy file maintainable at 236 actions.
#:
#: **A leading ``*`` is permitted only with a concrete verb** — ``*:read``, ``*:list`` — which is how
#: a read-only role becomes one clause. Before this it was unwritable: the type had to be a literal
#: name, a bare ``*`` is refused in an allow (correctly), and enumerating 50 types × 2 read verbs is
#: 100 patterns against a cap of 64. So the narrow role the whole phase exists to encourage was the
#: one thing an operator could not express, and the available answer was ``["*"]`` — the opposite.
#:
#: ``*:*`` is deliberately NOT matched. It is a bare ``*`` spelled differently, and refusing it in
#: the pattern means pydantic and the exported JSON Schema agree without a bridge, unlike the
#: bare-``*`` rule that needed one.
ACTION_PATTERN = (
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*:([a-z][a-z0-9_]*|\*)$"
    r"|^\*:[a-z][a-z0-9_]*$"
    r"|^\*$"
)

#: Resources look like ``type:id`` per ``docs/RESOURCE_MODEL.md``, with ``*`` allowed in the id and
#: an optional cluster qualifier (``slurm.job:mayo:127391``).
RESOURCE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*:[A-Za-z0-9_.:@+*-]+$|^\*$"

#: Policy ids are referenced from attachments and appear in audit reasons, so they are constrained
#: to something safe to log and to use in a URL path.
POLICY_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"

MAX_STATEMENTS = 64
MAX_PATTERNS_PER_FIELD = 64


class Effect(StrEnum):
    allow = "allow"
    deny = "deny"


class Statement(BaseModel):
    """One rule: these actions, on these resources, allowed or denied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sid: Annotated[str, Field(pattern=POLICY_ID_PATTERN)] | None = Field(
        default=None,
        description="Optional statement id, echoed in the audit reason so a verdict names its rule.",
    )
    effect: Effect
    actions: Annotated[
        list[Annotated[str, Field(pattern=ACTION_PATTERN)]],
        Field(min_length=1, max_length=MAX_PATTERNS_PER_FIELD),
    ]
    #: Defaults to every resource of the named type rather than being required, because most rules
    #: are type-wide and forcing `["*"]` everywhere would train operators to type it without
    #: thinking — which is exactly the habit that makes an accidental global grant likely.
    resources: Annotated[
        list[Annotated[str, Field(pattern=RESOURCE_PATTERN)]],
        Field(min_length=1, max_length=MAX_PATTERNS_PER_FIELD),
    ] = ["*"]

    @field_validator("actions")
    @classmethod
    def _no_bare_star_in_allow(cls, value: list[str], info) -> list[str]:
        """A bare ``*`` in an *allow* grants every action the product will ever add.

        Refused, because it is nearly always a mistake, and because when it genuinely is not,
        ``AllowAllEngine`` already exists and records ``policy_engine: allow-all`` in the trail —
        which tells an auditor the truth instead of burying a blanket grant in a policy file.

        A ``*`` in a *deny* narrows access and needs no guard: "nobody does anything" is a
        legitimate and occasionally necessary rule.
        """
        if info.data.get("effect") == Effect.allow and "*" in value:
            raise ValueError(
                "an allow statement may not use a bare '*' for actions; narrow it (e.g. "
                "'rack.power:*'), or run the allow-all engine, which is honest about being "
                "permissive"
            )
        return value


class PolicyDocument(BaseModel):
    """A named, reusable set of statements. Attached to principals; it does not name them."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=POLICY_ID_PATTERN)]
    description: Annotated[str, Field(max_length=512)] = ""
    statements: Annotated[list[Statement], Field(min_length=1, max_length=MAX_STATEMENTS)]

    @field_validator("statements")
    @classmethod
    def _unique_sids(cls, value: list[Statement]) -> list[Statement]:
        """Two statements with one id make an audit reason ambiguous about which rule fired."""
        seen: set[str] = set()
        for statement in value:
            if statement.sid is None:
                continue
            if statement.sid in seen:
                raise ValueError(f"duplicate statement id {statement.sid!r}")
            seen.add(statement.sid)
        return value


class PolicyFile(BaseModel):
    """The on-disk shape: a list of documents."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    policies: Annotated[list[PolicyDocument], Field(default_factory=list)]

    @field_validator("policies")
    @classmethod
    def _unique_ids(cls, value: list[PolicyDocument]) -> list[PolicyDocument]:
        seen: set[str] = set()
        for policy in value:
            if policy.id in seen:
                raise ValueError(f"duplicate policy id {policy.id!r}")
            seen.add(policy.id)
        return value


#: Fields the retired inline model used. Named explicitly so a document written against the old
#: shape is refused with an explanation rather than silently accepted-and-ignored by `extra=forbid`,
#: whose message ("extra inputs are not permitted") would not say why.
RETIRED_FIELDS = ("principals", "principal", "subjects")


def parse_document(raw: object) -> PolicyDocument:
    """Validate one document, or raise ``ValueError`` naming what is wrong.

    Checks the retired fields *before* pydantic, so an operator migrating from the inline model gets
    "policies no longer name principals" rather than a generic extra-field complaint.
    """
    if isinstance(raw, dict):
        for field in RETIRED_FIELDS:
            if field in raw:
                raise ValueError(
                    f"{field!r} is not a policy field: policies no longer name principals. "
                    f"Create the policy, then attach it to a user or group."
                )
        for statement in raw.get("statements") or []:
            if isinstance(statement, dict):
                for field in RETIRED_FIELDS:
                    if field in statement:
                        raise ValueError(
                            f"{field!r} is not a statement field: attach the policy to a principal "
                            f"instead of naming one inside it."
                        )
    return PolicyDocument.model_validate(raw)


def json_schema() -> dict:
    """JSON Schema for the editor (FR-17b).

    Emitted from the models above, so the browser and the server cannot disagree about what a valid
    document is.

    ``mode="validation"`` is the correct choice because the editor validates *input*, but for these
    models it currently makes no difference — measured, and the two modes produce an identical
    schema, because nothing here has a computed field or a serialization-only alias. It is named
    explicitly anyway: the modes diverge the moment one is added, and the failure would be an editor
    demanding fields the server fills in.

    **One rule has to be bridged by hand, and finding that out is the reason this function is
    tested against an independent validator.** ``_no_bare_star_in_allow`` is a pydantic
    ``field_validator``: imperative Python, invisible to schema generation. Left alone, the editor
    would happily accept ``{"effect": "allow", "actions": ["*"]}`` — an accidental grant of every
    action the product will ever have — and only the server would refuse it, at submit time, after
    the operator believed the document was valid.

    JSON Schema 2020-12 *can* express it (``if``/``then`` with ``not``/``contains``), so it is
    injected below rather than documented as a limitation. The pydantic model stays authoritative;
    this is a translation of one rule it cannot emit, and
    ``test_authz_schema.py::TestTheExportAgreesWithPydantic`` is what keeps the translation honest.
    """
    schema = _bridge_bare_star(PolicyFile.model_json_schema(mode="validation"))
    return schema


def document_json_schema() -> dict:
    """JSON Schema for ONE policy document — what the editor actually edits.

    Separate from `json_schema()` because the two describe different things and conflating them
    breaks the editor: `json_schema()` describes the whole *file* (`{"version": 1, "policies":
    [...]}`), so validating a single document against it fails with "additional properties are not
    allowed ('id', 'statements' were unexpected)". Found by validating the real document the API
    accepts against the schema the API served — which is the assertion worth keeping, since a
    served schema that rejects valid input is worse than none.
    """
    return _bridge_bare_star(PolicyDocument.model_json_schema(mode="validation"))


def _bridge_bare_star(schema: dict) -> dict:
    """Inject the one rule pydantic cannot emit. See `json_schema`."""
    statement = schema.get("$defs", {}).get("Statement")
    if statement is not None:
        statement["if"] = {"properties": {"effect": {"const": Effect.allow.value}}}
        statement["then"] = {
            "properties": {
                "actions": {
                    "not": {"contains": {"const": "*"}},
                    # Surfaced by Monaco on hover, so the operator sees the reason rather than only
                    # a red squiggle.
                    "description": (
                        "An allow statement may not use a bare '*'. Narrow it (e.g. "
                        "'rack.power:*'), or run the allow-all engine, which records that it is "
                        "permissive."
                    ),
                }
            }
        }
    return schema


def action_pattern_is_valid(pattern: str) -> bool:
    """Whether a string is a syntactically valid action pattern.

    Shape only. Whether the action *exists* is a registry question, deliberately not answered here —
    a static list would be stale from the next merge (FR-19a).
    """
    return bool(re.match(ACTION_PATTERN, pattern))
