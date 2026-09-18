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
"""The policy document schema — Phase 3, FR-7a, FR-17a, FR-17b (TC-7, TC-8, TC-9).

**The class of bug this file exists to catch is disagreement between two validators.** The server
validates with pydantic; the Monaco editor validates with the exported JSON Schema. If they differ,
the operator gets a document the editor blessed and the server refused — at submit time, after they
believed it was correct. So `TestTheExportAgreesWithPydantic` runs both against the same inputs with
an *independent* validator, because validating the export with pydantic would only establish that
pydantic agrees with itself.

That test already earned its place: it found that `_no_bare_star_in_allow` is a pydantic
`field_validator` and therefore invisible to schema generation, so the editor would have accepted an
accidental grant of every action in the product.
"""

from __future__ import annotations

import jsonschema
import pytest
from authz import schema as S


def doc(**overrides) -> dict:
    base = {
        "id": "ops-power",
        "description": "Operators may power-cycle racks",
        "statements": [{"effect": "allow", "actions": ["rack.power:*"], "resources": ["rack.power:a01"]}],
    }
    return base | overrides


def file_with(document: dict) -> dict:
    return {"policies": [document]}


@pytest.fixture(scope="module")
def validator():
    return jsonschema.Draft202012Validator(S.json_schema())


class TestAValidDocument:
    def test_it_parses(self):
        parsed = S.parse_document(doc())
        assert parsed.id == "ops-power"
        assert parsed.statements[0].effect is S.Effect.allow

    def test_resources_default_to_everything_of_that_type(self):
        """Most rules are type-wide. Requiring `["*"]` explicitly would train operators to type it
        without thinking, which is the habit that makes an accidental global grant likely."""
        parsed = S.parse_document({"id": "x", "statements": [{"effect": "deny", "actions": ["a:b"]}]})
        assert parsed.statements[0].resources == ["*"]

    def test_a_statement_id_is_optional_and_echoed(self):
        parsed = S.parse_document(doc(statements=[
            {"sid": "s1", "effect": "deny", "actions": ["rack.power:cycle"]},
        ]))
        assert parsed.statements[0].sid == "s1"


class TestTheRetiredInlineModelIsRefused:
    """FR-7a, TC-8. A document that half-works is worse than one refused."""

    @pytest.mark.parametrize("field", S.RETIRED_FIELDS)
    def test_a_principal_field_on_the_document_is_refused(self, field):
        with pytest.raises(ValueError, match="no longer name principals"):
            S.parse_document(doc(**{field: ["user:a"]}))

    @pytest.mark.parametrize("field", S.RETIRED_FIELDS)
    def test_a_principal_field_inside_a_statement_is_refused(self, field):
        with pytest.raises(ValueError, match="attach the policy"):
            S.parse_document(doc(statements=[
                {"effect": "allow", "actions": ["a:b"], field: ["user:a"]},
            ]))

    def test_the_message_says_what_to_do_instead(self):
        """An operator migrating from the inline model needs the next step, not a schema complaint.

        `extra="forbid"` alone would say "extra inputs are not permitted", which is true and
        useless — it does not mention attachment.
        """
        with pytest.raises(ValueError) as exc:
            S.parse_document(doc(principals=["user:a"]))
        assert "attach it to a user or group" in str(exc.value)


class TestABareStarInAnAllow:
    """The most dangerous single mistake available in this schema."""

    def test_it_is_refused(self):
        with pytest.raises(ValueError):
            S.parse_document(doc(statements=[{"effect": "allow", "actions": ["*"]}]))

    def test_it_is_refused_even_beside_a_narrow_pattern(self):
        """`["rack.power:*", "*"]` reads as a narrow grant and is a total one."""
        with pytest.raises(ValueError):
            S.parse_document(doc(statements=[
                {"effect": "allow", "actions": ["rack.power:*", "*"]},
            ]))

    def test_a_bare_star_in_a_deny_is_permitted(self):
        """A wildcard in a deny *narrows* access, so it needs no guard — "nobody does anything" is a
        legitimate and occasionally necessary rule."""
        parsed = S.parse_document(doc(statements=[{"effect": "deny", "actions": ["*"]}]))
        assert parsed.statements[0].actions == ["*"]


class TestMalformedDocuments:
    """TC-7. Each rejected, and the store never sees them."""

    @pytest.mark.parametrize(("label", "document"), [
        ("no statements", {"id": "x", "statements": []}),
        ("unknown effect", {"id": "x", "statements": [{"effect": "permit", "actions": ["a:b"]}]}),
        ("no actions", {"id": "x", "statements": [{"effect": "allow", "actions": []}]}),
        ("unknown field", {"id": "x", "nope": 1, "statements": [{"effect": "allow", "actions": ["a:b"]}]}),
        ("missing id", {"statements": [{"effect": "allow", "actions": ["a:b"]}]}),
    ])
    def test_it_is_refused(self, label, document):
        with pytest.raises(ValueError):
            S.parse_document(document)

    @pytest.mark.parametrize("action", [
        "Rack Power", "rack power:on", "RACK.POWER:ON", "rack.power:", ":on", "rack..power:on",
    ])
    def test_a_malformed_action_is_refused(self, action):
        with pytest.raises(ValueError):
            S.parse_document(doc(statements=[{"effect": "allow", "actions": [action]}]))

    @pytest.mark.parametrize("policy_id", ["Ops Policy", "ops policy", "../etc", "", "A" * 80])
    def test_a_malformed_policy_id_is_refused(self, policy_id):
        """Policy ids reach audit reasons and URL paths, so they are constrained to what is safe in
        both."""
        with pytest.raises(ValueError):
            S.parse_document(doc(id=policy_id))

    def test_a_duplicate_statement_id_is_refused(self):
        """Two statements with one id make an audit reason ambiguous about which rule fired."""
        with pytest.raises(ValueError, match="duplicate statement id"):
            S.parse_document(doc(statements=[
                {"sid": "s", "effect": "allow", "actions": ["a:b"]},
                {"sid": "s", "effect": "deny", "actions": ["c:d"]},
            ]))

    def test_a_duplicate_policy_id_in_a_file_is_refused(self):
        with pytest.raises(ValueError, match="duplicate policy id"):
            S.PolicyFile.model_validate({"policies": [doc(), doc()]})


class TestTheExportAgreesWithPydantic:
    """FR-17b, TC-9 — and the reason this file exists.

    Validated with `jsonschema`, not pydantic. Checking the export with the library that produced it
    would establish only self-consistency; the question is whether *Monaco* will reach the same
    verdict as the server, and Monaco speaks JSON Schema.
    """

    @pytest.mark.parametrize(("label", "document", "valid"), [
        ("a good document", doc(), True),
        ("unknown effect", doc(statements=[{"effect": "permit", "actions": ["a:b"]}]), False),
        ("no statements", doc(statements=[]), False),
        ("malformed action", doc(statements=[{"effect": "allow", "actions": ["Rack Power"]}]), False),
        ("unknown field", doc(nope=1), False),
        ("retired principals", doc(principals=["user:a"]), False),
        ("malformed policy id", doc(id="Ops Policy!"), False),
        # The bridged rule. Left to schema generation this would be VALID, because the pydantic
        # field_validator that refuses it is imperative Python and invisible to the exporter.
        ("bare * in an allow", doc(statements=[{"effect": "allow", "actions": ["*"]}]), False),
        ("bare * beside a narrow allow",
         doc(statements=[{"effect": "allow", "actions": ["rack.power:*", "*"]}]), False),
        # ...and the deny case must stay legal, or bridging the rule would have broken it.
        ("bare * in a deny", doc(statements=[{"effect": "deny", "actions": ["*"]}]), True),
    ])
    def test_both_validators_reach_the_same_verdict(self, validator, label, document, valid):
        by_schema = not list(validator.iter_errors(file_with(document)))

        try:
            S.parse_document(document)
            by_pydantic = True
        except ValueError:
            by_pydantic = False

        assert by_schema == valid, f"JSON Schema disagreed on {label!r}"
        assert by_pydantic == valid, f"pydantic disagreed on {label!r}"

    def test_the_schema_is_draft_2020_12_and_self_consistent(self):
        """A malformed schema would silently accept everything in Monaco."""
        jsonschema.Draft202012Validator.check_schema(S.json_schema())

    def test_the_bridged_rule_carries_a_human_explanation(self):
        """Monaco surfaces `description` on hover, so the operator gets the reason rather than only
        a red squiggle."""
        statement = S.json_schema()["$defs"]["Statement"]
        assert "then" in statement, "the bare-* bridge is missing"
        described = statement["then"]["properties"]["actions"]["description"]
        assert "allow-all engine" in described


class TestActionPatternHelper:
    @pytest.mark.parametrize("pattern", ["rack.power:on", "rack.power:*", "identity.user:create", "*"])
    def test_valid_shapes(self, pattern):
        assert S.action_pattern_is_valid(pattern)

    @pytest.mark.parametrize("pattern", ["Rack Power", "rack.power", ":on", "rack..power:on"])
    def test_invalid_shapes(self, pattern):
        assert not S.action_pattern_is_valid(pattern)

    def test_it_answers_shape_not_existence(self):
        """A syntactically valid action that no route declares is still shape-valid.

        Whether an action *exists* is a registry question, deliberately not answered here: a static
        list would be stale from the next merge (FR-19a).
        """
        assert S.action_pattern_is_valid("nosuch.service:verb")


class TestTheSchemaTheEditorActuallyGets:
    """The suite above validates `json_schema()` — the whole-**file** schema. Monaco is served
    `document_json_schema()`, a single document, by `GET /api/policies/schema`.

    Testing the wrong one is a subtle way for FR-17b to pass while failing: the two are generated
    from different models, so a rule bridged into one is not necessarily in the other, and the
    editor is the artifact whose disagreement operators actually experience.
    """

    @pytest.fixture
    def served(self):
        return jsonschema.Draft202012Validator(S.document_json_schema())

    @pytest.mark.parametrize(("label", "document", "valid"), [
        ("a good document", doc(), True),
        ("unknown effect", doc(statements=[{"effect": "permit", "actions": ["a:b"]}]), False),
        ("no statements", doc(statements=[]), False),
        ("malformed action", doc(statements=[{"effect": "allow", "actions": ["Rack Power"]}]), False),
        ("unknown field", doc(nope=1), False),
        ("retired principals", doc(principals=["user:a"]), False),
        ("malformed policy id", doc(id="Ops Policy!"), False),
        ("bare * in an allow", doc(statements=[{"effect": "allow", "actions": ["*"]}]), False),
        ("bare * in a deny", doc(statements=[{"effect": "deny", "actions": ["*"]}]), True),
        # The read-only role, which the grammar refused until 2026-09-08.
        ("a type wildcard with a pinned verb",
         doc(statements=[{"effect": "allow", "actions": ["*:read", "*:list"]}]), True),
        ("*:* — a bare star spelled differently",
         doc(statements=[{"effect": "allow", "actions": ["*:*"]}]), False),
    ])
    def test_the_served_schema_agrees_with_the_server(self, served, label, document, valid):
        by_schema = not list(served.iter_errors(document))
        try:
            S.parse_document(document)
            by_pydantic = True
        except ValueError:
            by_pydantic = False

        assert by_schema == valid, f"the SERVED schema disagreed on {label!r}"
        assert by_pydantic == valid, f"pydantic disagreed on {label!r}"

    def test_the_served_schema_is_self_consistent(self):
        jsonschema.Draft202012Validator.check_schema(S.document_json_schema())


class TestTheOneDivergenceJsonSchemaCannotExpress:
    """A duplicate `sid` is refused by the server and accepted by the editor — **on purpose**, and
    recorded here so nobody mistakes the editor for authoritative.

    JSON Schema has `uniqueItems`, which compares whole array items, and no keyword for "unique by
    property". So `_unique_sids` cannot be bridged the way `_no_bare_star_in_allow` was; expressing
    it would mean changing the document shape to key statements by `sid`, which is a format change
    for a validation nicety.

    The consequence is a real one and belongs in the UI rather than in the schema: the editor must
    check for duplicate sids before submitting, so the operator learns at the keyboard instead of on
    save. Asserting the divergence here means that gap is *known* — the alternative is a suite that
    silently implies parity FR-17b never achieved.
    """

    def _duplicate_sid(self) -> dict:
        return doc(statements=[
            {"sid": "same", "effect": "allow", "actions": ["rack:read"]},
            {"sid": "same", "effect": "allow", "actions": ["rack:list"]},
        ])

    def test_the_server_refuses_it(self):
        with pytest.raises(ValueError):
            S.parse_document(self._duplicate_sid())

    def test_the_editor_schema_accepts_it_and_that_is_the_known_gap(self):
        served = jsonschema.Draft202012Validator(S.document_json_schema())
        assert not list(served.iter_errors(self._duplicate_sid())), (
            "the served schema now catches a duplicate sid. That is an improvement — bridge it "
            "properly and delete this test rather than inverting it."
        )

    def test_the_servers_refusal_names_the_offending_sid(self):
        """Since the editor cannot pre-empt it, the save-time message is the operator's only
        signal, so it has to say which sid rather than that something is wrong."""
        with pytest.raises(ValueError, match="same"):
            S.parse_document(self._duplicate_sid())
