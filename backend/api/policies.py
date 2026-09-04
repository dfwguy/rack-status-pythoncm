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
from authz import schema as authz_schema
from authz import store as policy_store
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
