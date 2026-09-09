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
"""Policy CRUD, attachment, and the vocabulary the editor validates against (FR-19, FR-20, FR-21).

Three surfaces, and the third is the one that stops the editor lying:

* **CRUD** over policy documents (`/api/policies`).
* **Attachment** to a user or group (`/api/policies/<id>/attachments`). Policies never name
  principals — that is the attachment model, chosen because "attach a policy to a user" must not
  mean *editing a document other users depend on*.
* **`/api/policies/schema`** and **`/api/policies/vocabulary`**, so the Monaco editor validates
  against the same JSON Schema the server enforces and the same action list the server knows.
  Hard-coding either in the frontend is how an editor comes to accept a document the server then
  refuses, which reads as a product bug rather than a typo.

Every write goes through `authz.schema.parse_document` before it reaches disk, so the file the
engine reads is never one it would refuse — an unparseable policy set makes the engine deny every
request, so writing one would be a self-inflicted outage.
"""

from __future__ import annotations

import logging

from audit.actions import action
from audit.request_hook import name_target
from authz import schema as authz_schema
from authz import store as policy_store
from authz.policy import _glob_any
from flask import Blueprint, jsonify, request
from identity.models import IdentityError
from identity.store import IdentityStore

_logger = logging.getLogger(__name__)

bp = Blueprint("policies", __name__, url_prefix="/api/policies")


def _body() -> dict:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _attachments_for(policy_id: str) -> list[str]:
    store = IdentityStore()
    try:
        return store.subjects_with_policy(policy_id)
    finally:
        store.close()


@bp.route("", methods=["GET"])
@action(get="policy:list")
def list_policies():
    """Every policy, with who it is attached to.

    The attachment count ships with the list rather than behind a second call because it is what
    makes the table answerable: "can I delete this?" is the first question an operator has, and a
    list that cannot answer it sends them to click every row.
    """
    try:
        documents = policy_store.read_all()
    except policy_store.PolicyStoreError as exc:
        return _err(str(exc), 500)

    rows = []
    for document in documents:
        attached = _attachments_for(document.id)
        rows.append({
            **document.model_dump(exclude_none=True),
            "attached_to": attached,
            "attachment_count": len(attached),
        })
    return jsonify({"ok": True, "policies": rows, "path": str(policy_store.path())})


@bp.route("/schema", methods=["GET"])
@action(get="policy:read")
def policy_schema():
    """The JSON Schema the server enforces, for the editor to validate against.

    Served rather than duplicated in the frontend so the two cannot disagree. The schema is
    exported from the same pydantic models `parse_document` uses, including the bare-`*`-in-allow
    rule that pydantic expresses as a validator and the export bridges as a conditional — without
    that bridge the editor would accept a document the server refuses.
    """
    # The DOCUMENT schema, not the file schema. The editor edits one policy at a time, and
    # validating a single document against the file schema fails with "additional properties are
    # not allowed ('id', 'statements' were unexpected)" -- found by validating the exact document
    # the API accepts against the schema the API served. The file schema is offered alongside for
    # a caller editing the whole set.
    return jsonify({
        "ok": True,
        "schema": authz_schema.document_json_schema(),
        "file_schema": authz_schema.json_schema(),
    })


@bp.route("/vocabulary", methods=["GET"])
@action(get="policy:read")
def policy_vocabulary():
    """Every action and resource type a policy may name.

    Read from the live action registry, not a list maintained here. A hand-kept vocabulary is
    wrong the first time somebody adds an endpoint, and the failure is an editor that marks a
    valid action as unknown — which teaches operators to ignore the validation.
    """
    from audit import actions as audit_actions
    from authz import resources as authz_resources

    try:
        registry = audit_actions.registry()
    except audit_actions.ActionRegistryError:
        return _err("the action registry is not built yet", 503)

    names = sorted(registry.actions())
    return jsonify({
        "ok": True,
        "actions": names,
        "services": sorted(registry.services()),
        "resource_types": sorted(authz_resources.declared_types()),
        "collection_id": authz_resources.COLLECTION,
        "singleton_id": authz_resources.SINGLETON,
    })


@bp.route("/preview", methods=["GET"])
# `policy:read`, not a `policy:preview` verb of its own, and the first attempt got this wrong in a
# way worth recording: a bespoke verb is not covered by `*:read`, so the read-only role this feature
# exists to make legible could not read it — the gate answered 403 and an operator inspecting why
# they were denied would be denied. Self-inspection belongs to the read family. Widening to another
# principal is still authorized separately, inside the handler.
@action(get="policy:read")
def preview_grants():
    """Every declared action, with allow or deny, for one principal — FR-16, AC-16.

    **The other half of the adoption problem.** A read-only role became expressible in one clause,
    and an operator still had no way to see what a grant *means*. Without that, the safe-feeling
    move is widening to `["*"]`, which is the state this phase exists to leave.

    The shape is the product's own default-deny, made visible: build the full action vocabulary with
    **deny** against every entry, then walk the policies attached to the principal — personally and
    through each group — and flip an entry to allow where an allow statement matches it. Deny
    statements are applied last and are not flippable, which is the engine's own
    deny-overrides-allow rule rather than a second opinion about it.

    **Matched on the action, and the resource scope is reported rather than collapsed.** A statement
    reads `allow rack.power:off on rack.power:p17-*`, and answering a bare "allowed: true" would
    tell an operator they may power off any rack. Each allowed entry therefore carries the resource
    patterns that qualify it and the policy and statement that decided, so "yes, for these" is
    legible. That is also why this is not simply 253 calls to `authz.authorize`: there is no single
    resource to ask about, and inventing one would make the answer confidently wrong.

    Three renderings of one dataset (`?format=json|text|table`), never a second computation —
    `cluster_report.py` is the precedent and the reason is the same: a second generator drifts, and a
    report that contradicts the product is worse than no report.

    **Widening past the caller is authorized separately.** Reporting another principal's grants
    would let an unprivileged caller map who can reach which rack, which is how a target gets
    chosen. A `subject` other than the caller's own is judged by the engine, not by a comparison
    here.
    """
    import authz
    from audit import actions as audit_actions
    from flask import g

    caller = str(getattr(g, "auth_principal", "") or "")
    # `principal`, deliberately not `subject`: `authz/resources.py` reads a `subject` parameter as
    # this route's resource id, and a principal contains a colon which the id grammar refuses — so
    # `?subject=user:alice` made the request *unresolvable* and answered 400 before this handler
    # ran. The collision is invisible from here, which is why the name is explained rather than
    # just chosen.
    subject = (request.args.get("principal") or "").strip() or caller
    if not subject:
        return _err("no principal to preview; sign in or pass ?principal=", 400)

    if subject != caller:
        # Gated on `policy:attach`, an ADMINISTRATIVE action — and the obvious choice was wrong.
        # `policy:read` was tried first and it is exactly what a read-only role grants (`*:read`),
        # so every viewer could have enumerated every other principal's grants: a map of who can
        # reach which rack, which is how an attacker picks whom to phish. Whoever manages
        # attachments legitimately needs to see their effect; nobody else does.
        verdict = authz.authorize(caller, "policy:attach", "policy:_all")
        if not verdict.allowed:
            return _err("not authorized to preview another principal", 403)

    fmt = (request.args.get("format") or "json").strip().lower()
    if fmt not in ("json", "text", "table"):
        return _err("format must be json, text or table", 400)

    # --- single question: "may I do X to Y" ------------------------------------------------------
    #
    # Answered by `authz.authorize` — the *same* call the gate makes — so this mode cannot drift
    # from enforcement. The matrix below matches on the action alone, because there is no one
    # resource to ask about across 173 actions; this mode exists for when the operator has a
    # specific resource in mind, and it is the more precise of the two.
    action_name = (request.args.get("action") or "").strip()
    resource_arg = (request.args.get("resource") or "").strip()
    if action_name:
        if not resource_arg:
            # Refused rather than defaulted. A verdict against a guessed resource is a confident
            # answer to a question the operator did not ask, and they would act on it.
            return _err("an action preview needs ?resource= as well", 400)
        verdict = authz.authorize(subject, action_name, resource_arg)
        return jsonify({
            "ok": True,
            "subject": subject,
            "action": action_name,
            "resource": resource_arg,
            "allowed": verdict.allowed,
            "reason": verdict.reason,
            "engine": authz.engine_name(),
        })

    # --- the dataset, built once -----------------------------------------------------------------
    #
    # Default deny for every declared action, which is the product's own posture stated explicitly
    # rather than implied by absence. An action missing from this map would read as "not mentioned"
    # instead of "refused", and those are the same thing here only if the map is complete.
    rows: dict[str, dict] = {
        name: {"action": name, "effect": "deny", "via": "", "policy": "", "sid": "", "resources": []}
        for name in audit_actions.registry().actions()
    }

    store = IdentityStore()
    try:
        groups = store.groups_of(subject.split(":", 1)[-1]) if subject.startswith("user:") else []
        holders = [subject, *(f"group:{name}" for name in groups)]
        attached = [(holder, pid) for holder in holders for pid in store.policies_for(holder)]
    finally:
        store.close()

    documents = {doc.id: doc for doc in policy_store.read_all()}

    def _matching(patterns: list[str]) -> list[str]:
        return [name for name in rows if _glob_any(name, patterns)]

    # Allows first, then denies, so a deny always has the last word however the file is ordered —
    # the same reason `authz/policy.py` runs its two passes in that order.
    for holder, policy_id in attached:
        document = documents.get(policy_id)
        if document is None:
            continue  # a dangling attachment is reported below, not silently treated as a grant
        for statement in document.statements:
            if statement.effect != authz_schema.Effect.allow:
                continue
            for name in _matching(list(statement.actions)):
                rows[name].update({
                    "effect": "allow", "via": holder, "policy": policy_id,
                    "sid": statement.sid or "", "resources": list(statement.resources),
                })

    for holder, policy_id in attached:
        document = documents.get(policy_id)
        if document is None:
            continue
        for statement in document.statements:
            if statement.effect != authz_schema.Effect.deny:
                continue
            for name in _matching(list(statement.actions)):
                rows[name].update({
                    "effect": "deny", "via": holder, "policy": policy_id,
                    "sid": statement.sid or "", "resources": list(statement.resources),
                })

    ordered = [rows[name] for name in sorted(rows)]
    allowed = sum(1 for row in ordered if row["effect"] == "allow")
    payload = {
        "ok": True,
        "subject": subject,
        "engine": authz.engine_name(),
        "groups": list(groups),
        "attachments": [
            {"via": holder, "policy": pid, "found": pid in documents}
            for holder, pid in attached
        ],
        "summary": {"total": len(ordered), "allowed": allowed, "denied": len(ordered) - allowed},
        "actions": ordered,
    }

    if fmt == "json":
        return jsonify(payload)
    rendered = _render_text(payload) if fmt == "text" else _render_table(payload)
    return rendered, 200, {"Content-Type": "text/plain; charset=utf-8"}


def _render_text(payload: dict) -> str:
    """One line per action. Greppable, which is what a terminal reader actually wants."""
    lines = [
        f"subject: {payload['subject']}   engine: {payload['engine']}",
        f"groups:  {', '.join(payload['groups']) or '(none)'}",
        (
            f"summary: {payload['summary']['allowed']} allowed, "
            f"{payload['summary']['denied']} denied, {payload['summary']['total']} total"
        ),
        "",
    ]
    for row in payload["actions"]:
        scope = ",".join(row["resources"]) or "*"
        detail = f"  via {row['via']} ({row['policy']})" if row["policy"] else ""
        lines.append(f"{row['effect'].upper():5}  {row['action']:44}  {scope}{detail}")
    return "\n".join(lines) + "\n"


def _render_table(payload: dict) -> str:
    """Only the allows, aligned. Denials are the default and 250 of them bury the six that matter."""
    allows = [row for row in payload["actions"] if row["effect"] == "allow"]
    width = max((len(row["action"]) for row in allows), default=6)
    lines = [
        f"Effective grants for {payload['subject']}  (engine: {payload['engine']})",
        (
            f"{payload['summary']['allowed']} of {payload['summary']['total']} actions allowed; "
            f"the rest are denied by default."
        ),
        "",
        f"{'ACTION'.ljust(width)}  RESOURCES                 VIA",
        f"{'-' * width}  {'-' * 25}  {'-' * 24}",
    ]
    for row in allows:
        scope = ",".join(row["resources"]) or "*"
        lines.append(f"{row['action'].ljust(width)}  {scope[:25]:25}  {row['via']}")
    if not allows:
        lines.append("(none — every action is denied)")
    return "\n".join(lines) + "\n"


@bp.route("/<policy_id>", methods=["GET"])
@action(get="policy:read")
def get_policy(policy_id: str):
    try:
        document = policy_store.get(policy_id)
    except policy_store.PolicyStoreError as exc:
        return _err(str(exc), 500)
    if document is None:
        return _err(f"no policy named {policy_id!r}", 404)

    attached = _attachments_for(policy_id)
    return jsonify({
        "ok": True,
        "policy": document.model_dump(exclude_none=True),
        "attached_to": attached,
    })


@bp.route("", methods=["POST"])
@action("policy:create")
def create_policy():
    try:
        document = policy_store.create(_body())
    except ValueError as exc:
        # A schema rejection is the operator's typo, not a server fault, so it is a 400 carrying
        # the validator's own message -- which names the field and the rule.
        return _err(f"invalid policy: {exc}", 400)
    except policy_store.PolicyStoreError as exc:
        return _err(str(exc), 409)
    return jsonify({"ok": True, "policy": document.model_dump(exclude_none=True)}), 201


@bp.route("/<policy_id>", methods=["PUT"])
@action(put="policy:update")
def update_policy(policy_id: str):
    try:
        document = policy_store.update(policy_id, _body())
    except ValueError as exc:
        return _err(f"invalid policy: {exc}", 400)
    except policy_store.PolicyStoreError as exc:
        status = 404 if "no policy named" in str(exc) else 400
        return _err(str(exc), status)
    return jsonify({"ok": True, "policy": document.model_dump(exclude_none=True)})


@bp.route("/<policy_id>", methods=["DELETE"])
@action(delete="policy:delete")
def delete_policy(policy_id: str):
    """Refuses while the policy is still attached, unless `?force=true`.

    Deleting an attached policy silently changes what somebody can do, and the operator doing it
    usually cannot see the attachments — so the default answer names them and stops.
    """
    force = (request.args.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
    try:
        detached = policy_store.delete(policy_id, force=force)
    except policy_store.PolicyStoreError as exc:
        status = 404 if "no policy named" in str(exc) else 409
        return _err(str(exc), status)
    return jsonify({"ok": True, "deleted": policy_id, "detached_from": detached})


@bp.route("/<policy_id>/attachments", methods=["GET"])
@action(get="policy:read")
def list_attachments(policy_id: str):
    return jsonify({"ok": True, "policy": policy_id, "subjects": _attachments_for(policy_id)})


@bp.route("/<policy_id>/attachments", methods=["POST"])
@action("policy:attach")
def attach_policy(policy_id: str):
    """Attach to `user:<login>` or `group:<name>`.

    The *principal* end is verified by the store; the *policy* end is checked here, which is the
    opposite of the store's own asymmetry and deliberate. The store cannot verify a policy exists
    because policies live in a file an operator may be mid-edit on — but an API caller naming a
    policy that does not exist has simply made a mistake, and telling them beats silently creating
    an attachment that grants nothing.
    """
    subject = str(_body().get("subject") or "").strip()
    if not subject:
        return _err("subject is required, as 'user:<login>' or 'group:<name>'")

    try:
        if policy_store.get(policy_id) is None:
            return _err(f"no policy named {policy_id!r}", 404)
    except policy_store.PolicyStoreError as exc:
        return _err(str(exc), 500)

    store = IdentityStore()
    try:
        store.attach_policy(subject, policy_id)
    except IdentityError as exc:
        return _err(str(exc), 400)
    finally:
        store.close()

    # The durable record, not just the application log: the resolved resource names the policy,
    # so without this the trail cannot say who was granted it (AC-15g). Set after the store call
    # succeeds, so a refused or failed attachment does not claim a grant that never happened.
    name_target(subject)
    _logger.info("authz: attached %s to %s", policy_id, subject)
    return jsonify({"ok": True, "policy": policy_id, "subject": subject}), 201


@bp.route("/<policy_id>/attachments/<path:subject>", methods=["DELETE"])
@action(delete="policy:detach")
def detach_policy(policy_id: str, subject: str):
    """Detach. Idempotent, like the store's own `detach_policy`.

    `<path:subject>` because a subject contains a colon (`user:rkoike`), which a default string
    converter accepts but which reads ambiguously; the path converter makes it explicit that the
    whole remainder is the subject.
    """
    store = IdentityStore()
    try:
        store.detach_policy(subject, policy_id)
    finally:
        store.close()
    name_target(subject)
    return jsonify({"ok": True, "policy": policy_id, "subject": subject, "detached": True})


@bp.route("/subjects/<path:subject>", methods=["GET"])
@action(get="policy:list")
def policies_for_subject(subject: str):
    """What is attached to one subject — the other direction, for the user and group views."""
    store = IdentityStore()
    try:
        return jsonify({"ok": True, "subject": subject, "policies": store.policies_for(subject)})
    finally:
        store.close()
