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
"""User, group and membership administration — FR-11, FR-13, FR-8, FR-9, NFR-8.

**Every route here is privileged, and several are privilege escalation.** Whoever can add a member
to a group can grant themselves whatever that group is granted; whoever can create a user can create
one for themselves. So each carries its own `@action` name rather than a shared `identity:write`,
because Phase 3 has to be able to say *this operator may add members but may not create users*, and
a single action name makes that sentence unsayable.

## Creating a user without ever seeing a password

SRP means the server holds a verifier, not a password — so a created user needs a credential the
server can enrol and the operator can pass on. The bootstrap solves this already and this reuses it
exactly: generate a password, return it **once**, store only its verifier, and flag the account for
change at first login. The operator conveys it out of band and it is unrecoverable afterwards, by
construction.

The alternative — the admin choosing a password in a form — is worse in a way that is not obvious:
it puts a real credential in a request body, a browser field and probably a chat message, and the
admin then knows a password the user will keep.

## What is deliberately not here

**No unauthenticated path to any of it** (FR-11). No self-registration, no invite link, no recovery
code. The gate covers these routes because they are not in the bypass set, and that is the whole
mechanism.

**No group inside a group** (FR-8). Membership is users-in-groups, flat, so there is no transitive
resolution and no cycle guard to get wrong.
"""

from __future__ import annotations

import logging

import web_auth
from audit.actions import action
from flask import Blueprint, g, jsonify, request
from identity import bootstrap, principals, revocation
from identity import srp as srp_mod
from identity.models import Group, IdentityError, User, canonical_group_name, canonical_login
from identity.store import IdentityStore

_logger = logging.getLogger("rack_web.api.identity_admin")

bp = Blueprint("identity_admin", __name__, url_prefix="/api/identity")


def _bad(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


def _not_found(what: str):
    # 404 rather than a 403 that would confirm existence. The brief flags this as a Phase 3 decision
    # per resource type; for identity the conservative answer is the right default now, because
    # "which logins exist" is exactly what an attacker wants from this surface.
    return jsonify({"ok": False, "error": f"{what} not found"}), 404


def _store() -> IdentityStore:
    return IdentityStore()


def _caller_login() -> str:
    """The signed-in administrator's login, or empty when the gate is disabled."""
    principal = str(getattr(g, "auth_principal", "") or "")
    if not principal:
        principal = web_auth.authenticated_principal(request.headers, request.cookies)
    if not principal:
        return ""
    try:
        kind, login = principals.parse_principal(principal)
    except principals.PrincipalError:
        return ""
    return login if kind == principals.KIND_USER else ""


def _lockout_refusal(store: IdentityStore, login: str, *, verb: str):
    """Refuse an operation that would leave nobody able to sign in. ``None`` when it is safe.

    Two guards, and neither is paranoia — FR-12 says recovery from losing every administrative
    credential is *redeploying the identity store*, which on a customer cluster means losing every
    user, group and membership. A misclick should not cost that.

    **You cannot act on yourself.** Borrowed from the Workshop Studio portal's users page, which
    disables the row menu on your own row for exactly this reason. Enforced here rather than only in
    the UI, because a guard that lives in the client is a guard against accidents only.

    **You cannot remove the last account that can still sign in.** The self check alone does not
    cover it: two administrators can disable each other, or one can disable the other and then
    delete themselves. This counts what would remain.
    """
    caller = _caller_login()
    if caller and caller == login:
        return _bad(
            f"you cannot {verb} your own account. Ask another administrator, or use a different "
            "account — this refusal exists so a misclick cannot lock you out.",
            409,
        )

    remaining = [u for u in store.list_users() if not u.disabled and u.login != login]
    if not remaining:
        return _bad(
            f"refusing to {verb} the last account that can sign in. Recovery from losing every "
            "credential is redeploying the identity store (FR-12), which discards every user, "
            "group and membership. Create another account first.",
            409,
        )
    return None


# --- users ----------------------------------------------------------------------------------


@bp.route("/users", methods=["GET"])
@action("identity.user:list")
def list_users():
    store = _store()
    try:
        users = [u.public_dict() for u in store.list_users()]
        # Group membership is what an operator actually wants to see next to a user, and fetching
        # it per row from the client would be N+1 requests against a store that already has it.
        groups = {g.name: set(store.members_of(g.name)) for g in store.list_groups()}
    finally:
        store.close()
    for row in users:
        row["groups"] = sorted(name for name, members in groups.items() if row["login"] in members)
    return jsonify({"ok": True, "users": users})


@bp.route("/users", methods=["POST"])
@action("identity.user:create")
def create_user():
    """Create a user and return its one-time password.

    The password appears in this response and nowhere else, ever again — same contract as the
    first-boot bootstrap, for the same reason (FR-3).
    """
    body = request.get_json(silent=True) or {}
    raw_login = str(body.get("login") or "").strip()
    display_name = str(body.get("display_name") or "").strip()
    if not raw_login:
        return _bad("a login is required")

    try:
        login = canonical_login(raw_login)
    except IdentityError as exc:
        return _bad(str(exc))

    password = bootstrap.generate_password()
    salt, verifier = srp_mod.enroll(login, password)

    store = _store()
    try:
        store.create_user(
            User(
                login=login,
                display_name=display_name or login,
                srp_salt=salt,
                srp_verifier=verifier,
                must_change_password=True,
            )
        )
    except IdentityError as exc:
        # Covers both "already exists" and "this login was retired" (FR-2b). The message from the
        # store distinguishes them, and both are the caller's problem to resolve rather than ours.
        return _bad(str(exc), 409)
    finally:
        store.close()

    return jsonify({
        "ok": True,
        "user": {"login": login, "display_name": display_name or login},
        "password": password,
        "note": "Copy this now. It is not stored and cannot be shown again.",
    }), 201


@bp.route("/users/<login>", methods=["GET"])
@action("identity.user:read")
def read_user(login: str):
    store = _store()
    try:
        user = store.get_user(login)
        if user is None:
            return _not_found("user")
        row = user.public_dict()
        row["groups"] = sorted(
            g.name for g in store.list_groups() if user.login in store.members_of(g.name)
        )
    finally:
        store.close()
    return jsonify({"ok": True, "user": row})


@bp.route("/users/<login>", methods=["PATCH"])
@action("identity.user:update")
def update_user(login: str):
    """Change the display name. **Not** the login — that is immutable (FR-2).

    Enabling and disabling is a separate action and a separate route, because disabling an account
    is a security operation and renaming one is housekeeping. Sharing an action name between them
    would make them equally privileged in whatever policy Phase 3 writes.
    """
    body = request.get_json(silent=True) or {}
    if "login" in body:
        return _bad("a login cannot be changed; it is the principal (FR-2)")
    if "display_name" not in body:
        return _bad("nothing to update")

    store = _store()
    try:
        user = store.get_user(login)
        if user is None:
            return _not_found("user")
        updated = store.update_user(user.edited(display_name=str(body["display_name"]).strip()))
    except IdentityError as exc:
        return _bad(str(exc))
    finally:
        store.close()
    return jsonify({"ok": True, "user": updated.public_dict()})


@bp.route("/users/<login>/disabled", methods=["PUT"])
@action("identity.user:set_disabled")
def set_user_disabled(login: str):
    """Disable or re-enable an account.

    Disabling revokes immediately rather than waiting for tokens to expire. Without that the
    operation would be advisory for up to the access-token lifetime, which is precisely the window
    an offboarding is trying to close.
    """
    body = request.get_json(silent=True) or {}
    if "disabled" not in body:
        return _bad("`disabled` is required")
    disabled = bool(body["disabled"])

    store = _store()
    try:
        user = store.get_user(login)
        if user is None:
            return _not_found("user")
        if disabled:
            # Only this direction can lock anyone out; re-enabling is always safe.
            refusal = _lockout_refusal(store, user.login, verb="disable")
            if refusal is not None:
                return refusal
        updated = store.update_user(user.edited(disabled=disabled))
    finally:
        store.close()

    principal = principals.user_principal(updated.login)
    try:
        if disabled:
            revocation.revoke_subject(principal, reason="account disabled")
        else:
            revocation.restore_subject(principal)
    except Exception:
        _logger.exception("could not update revocation for %s", principal)

    return jsonify({"ok": True, "user": updated.public_dict()})


@bp.route("/users/<login>/credential", methods=["POST"])
@action("identity.user:reset_credential")
def reset_credential(login: str):
    """Issue a new one-time password for a user — FR-13a.

    **The most consequential thing this surface can do**, which is why it is its own action rather
    than a variant of `identity.user:update`: it is taking over an account. Policy has to be able to
    withhold it from an operator who may still rename and disable, and a shared action name would
    make that impossible to express.

    FR-12 forbids an *unauthenticated* recovery path — no code, no self-service reset, no "forgot
    password". This is not that: it requires an authenticated administrator and cannot help a caller
    with no credential at all, so it does not reopen the hole FR-12 closes. The requirement's
    absolute wording was narrowed on 2026-09-01 to say so, because FR-2b (logins are never reused)
    made the literal reading untenable — "delete and recreate" cannot restore a forgotten password,
    and the person could never hold their login again.

    Sessions are revoked, and that is the half most easily forgotten. A reset whose previous holder
    keeps working until their token expires is the opposite of the operation.
    """
    store = _store()
    try:
        user = store.get_user(login)
        if user is None:
            return _not_found("user")

        password = bootstrap.generate_password()
        salt, verifier = srp_mod.enroll(user.login, password)
        store.update_user(
            user.edited(srp_salt=salt, srp_verifier=verifier, must_change_password=True)
        )
    except IdentityError as exc:
        return _bad(str(exc))
    finally:
        store.close()

    try:
        revocation.revoke_all_for(principals.user_principal(login))
    except Exception:
        # The credential is already replaced, so the account is secured either way; the residue is
        # that the previous holder's tokens live out their remaining minutes. Worth logging loudly
        # rather than failing the request and leaving the operator unsure whether it took effect.
        _logger.exception("credential reset: sessions were not revoked for %s", login)

    return jsonify({
        "ok": True,
        "user": {"login": user.login},
        "password": password,
        "note": "Copy this now. It is not stored and cannot be shown again.",
    })


@bp.route("/users/<login>", methods=["DELETE"])
@action("identity.user:delete")
def delete_user(login: str):
    """Delete a user. **The login is retired permanently** (FR-2b) and cannot be recreated.

    That is deliberate and worth knowing before clicking: audit records name principals by login, so
    reusing one would silently reattribute a departed operator's history to a new person. Disabling
    is the reversible operation; this one is not.
    """
    store = _store()
    try:
        user = store.get_user(login)
        if user is None:
            return _not_found("user")
        refusal = _lockout_refusal(store, user.login, verb="delete")
        if refusal is not None:
            return refusal
        store.delete_user(user.login)
    finally:
        store.close()

    try:
        revocation.revoke_subject(principals.user_principal(login), reason="account deleted")
    except Exception:
        _logger.exception("could not revoke the deleted principal %s", login)

    return jsonify({"ok": True, "retired": login})


# --- groups ---------------------------------------------------------------------------------


@bp.route("/groups", methods=["GET"])
@action("identity.group:list")
def list_groups():
    store = _store()
    try:
        groups = []
        for group in store.list_groups():
            groups.append({
                "name": group.name,
                "description": group.description,
                "members": sorted(store.members_of(group.name)),
            })
    finally:
        store.close()
    return jsonify({"ok": True, "groups": groups})


@bp.route("/groups", methods=["POST"])
@action("identity.group:create")
def create_group():
    body = request.get_json(silent=True) or {}
    raw_name = str(body.get("name") or "").strip()
    if not raw_name:
        return _bad("a name is required")
    try:
        name = canonical_group_name(raw_name)
        store = _store()
        try:
            store.create_group(
                Group(name=name, description=str(body.get("description") or "").strip())
            )
        finally:
            store.close()
    except IdentityError as exc:
        return _bad(str(exc), 409)
    return jsonify({"ok": True, "group": {"name": name}}), 201


@bp.route("/groups/<name>", methods=["PATCH"])
@action("identity.group:update")
def update_group(name: str):
    body = request.get_json(silent=True) or {}
    if "name" in body:
        return _bad("a group cannot be renamed; the name is its identifier")
    if "description" not in body:
        return _bad("nothing to update")
    store = _store()
    try:
        group = store.get_group(name)
        if group is None:
            return _not_found("group")
        updated = store.update_group(group.edited(description=str(body["description"]).strip()))
    finally:
        store.close()
    return jsonify({"ok": True, "group": {"name": updated.name, "description": updated.description}})


@bp.route("/groups/<name>", methods=["DELETE"])
@action("identity.group:delete")
def delete_group(name: str):
    store = _store()
    try:
        if store.get_group(name) is None:
            return _not_found("group")
        store.delete_group(name)
    finally:
        store.close()
    return jsonify({"ok": True, "deleted": name})


# --- membership -----------------------------------------------------------------------------
#
# The two routes below are, with `policy:attach` in Phase 3, the highest-privilege pair in the
# product: group membership is how grants are conferred, so whoever can edit it can grant themselves
# whatever the group has. Their own authorization matters more than that of anything they administer.


@bp.route("/groups/<name>/members", methods=["POST"])
@action("identity.group:add_member")
def add_member(name: str):
    body = request.get_json(silent=True) or {}
    login = str(body.get("login") or "").strip()
    if not login:
        return _bad("a login is required")

    store = _store()
    try:
        if store.get_group(name) is None:
            return _not_found("group")
        # FR-8: groups are flat. Refusing here rather than relying on the store means the API says
        # so explicitly, and AC-16 has something to assert against.
        if store.get_group(login) is not None and store.get_user(login) is None:
            return _bad("a group cannot be a member of a group (FR-8)")
        if store.get_user(login) is None:
            return _not_found("user")
        store.add_member(name, login)
    except IdentityError as exc:
        return _bad(str(exc))
    finally:
        store.close()
    return jsonify({"ok": True, "group": name, "login": login}), 201


@bp.route("/groups/<name>/members/<login>", methods=["DELETE"])
@action("identity.group:remove_member")
def remove_member(name: str, login: str):
    store = _store()
    try:
        if store.get_group(name) is None:
            return _not_found("group")
        store.remove_member(name, login)
    finally:
        store.close()
    return jsonify({"ok": True, "group": name, "removed": login})
