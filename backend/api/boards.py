# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Importing a shared board bundle — the one gated half of board sharing.

Author: Rafael M. Koike & Cursor Agent ("Ada")

**Export is not here, deliberately.** Exporting reads what the caller can already see and
produces a file on their own machine, so an endpoint for it would gate nothing that reading the
board does not already gate. Import is the asymmetric half: it *writes* to the catalog every
operator of this instance shares, so it is the half that needs a permission.

Why the permission is two-layered
---------------------------------
One new action, ``catalog.board:import``, gates the route: may this principal apply a shared
board at all. But "may they import" and "may they *overwrite* our vetted NCCL tool with a
colleague's" are different questions, and the second is the one that can quietly undo a site's
own tuning.

Rather than invent a second action for it, each item additionally requires the action its
underlying write would have needed on its own:

===================  ==========================
the item             also requires
===================  ==========================
a tool we don't have ``catalog.tool:create``
a tool we do have    ``catalog.tool:update``
a workflow           ``workflow:save``
===================  ==========================

That composes with permissions an administrator already grants and reasons about, and it means
a policy can allow importing boards that *add* actions while refusing ones that *replace*
them — which is the distinction that matters. Inventing ``catalog.board:overwrite`` would have
put the same decision in a second vocabulary nobody else consults.

**New-versus-existing is decided here, never by the caller.** The client computes the same diff
to draw its review screen, but a client that lies about it would otherwise turn an overwrite
into a create and slip past the stronger permission.

An item the caller may not write is **skipped and reported**, not fatal: a partial import is a
real outcome ("you got the three new tools, not the replacement of ours"), and failing the whole
request would make the useful part unreachable.
"""

from __future__ import annotations

import logging
from typing import Any

import authz
import workflow_engine
from audit.actions import action
from catalog import models
from catalog.store import TABLE_TOOLS, get_repo
from flask import Blueprint, g, jsonify, request

_logger = logging.getLogger(__name__)

bp = Blueprint("boards", __name__)

#: Mirrors `BUNDLE_KIND` / `BUNDLE_VERSION` in frontend/src/components/widgets/boardBundle.ts.
BUNDLE_KIND = "greenlight.board"
BUNDLE_VERSION = 1


def _principal() -> str:
    """The authenticated principal, as the request gate resolved it.

    Read from ``g`` rather than re-derived: the gate stashes it there precisely so a second
    question is asked about the same principal the first one was.
    """
    return str(getattr(g, "auth_principal", "") or "")


def _may(action_name: str, resource: str) -> bool:
    return bool(authz.authorize(_principal(), action_name, resource).allowed)


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@bp.route("/import", methods=["POST"])
@action("catalog.board:import")
def api_board_import():
    """Apply the approved items of a board bundle.

    Body::

        {"bundle": {...}, "approve": ["tool:nvloom", "workflow:abc"]}

    Returns a per-item result so the UI can say exactly what happened, including which items
    were refused for want of a permission.
    """
    body = request.get_json(silent=True) or {}
    bundle = body.get("bundle")
    if not isinstance(bundle, dict):
        return _err("a bundle is required")
    if bundle.get("kind") != BUNDLE_KIND:
        return _err(f"not a Greenlight board bundle (expected kind {BUNDLE_KIND!r})")
    if bundle.get("v") != BUNDLE_VERSION:
        return _err(
            f"bundle version {bundle.get('v')!r} is not supported (this build reads "
            f"version {BUNDLE_VERSION})"
        )
    approve = body.get("approve")
    approved = {k for k in approve if isinstance(k, str)} if isinstance(approve, list) else set()

    tools = {t.get("id"): t for t in bundle.get("tools") or [] if isinstance(t, dict)}
    workflows = {w.get("id"): w for w in bundle.get("workflows") or [] if isinstance(w, dict)}

    results: list[dict[str, Any]] = []
    for key in approved:
        kind, _, ident = key.partition(":")
        if not ident:
            results.append(_result(key, "failed", "unrecognised item key"))
            continue
        if kind == "tool":
            results.append(_apply_tool(ident, tools.get(ident)))
        elif kind == "workflow":
            results.append(_apply_workflow(ident, workflows.get(ident)))
        else:
            results.append(_result(key, "failed", f"unknown item kind {kind!r}"))

    applied = sum(1 for r in results if r["status"] == "applied")
    _logger.info(
        "board import by %s: %d applied, %d skipped, %d failed",
        _principal() or "<anonymous>",
        applied,
        sum(1 for r in results if r["status"] == "skipped"),
        sum(1 for r in results if r["status"] == "failed"),
    )
    return jsonify({"ok": True, "applied": applied, "results": results})


def _result(key: str, status: str, detail: str = "") -> dict[str, Any]:
    return {"key": key, "status": status, "detail": detail}


def _apply_tool(tool_id: str, doc: dict[str, Any] | None) -> dict[str, Any]:
    key = f"tool:{tool_id}"
    if doc is None:
        return _result(key, "skipped", "the bundle carried no definition for this tool")

    repo = get_repo()
    existing = repo.get(TABLE_TOOLS, tool_id)
    resource = f"catalog.tool:{tool_id}"
    # The stronger permission for the more consequential act. Decided from the store, not from
    # anything the caller told us.
    needed = "catalog.tool:update" if existing else "catalog.tool:create"
    if not _may(needed, resource):
        return _result(
            key,
            "skipped",
            f"not authorized to {'replace' if existing else 'create'} this tool ({needed})",
        )

    incoming = dict(doc)
    incoming["id"] = tool_id
    # Provenance is ours, not the sender's: a document that arrives from a peer and is written
    # here is a local edit, and marking it "seed" would make the boot-time backfill treat it as
    # a built-in and refresh it out from under the operator.
    incoming["source"] = "user"
    tool = models.Tool.from_dict(incoming)
    problems = tool.validate()
    if problems:
        # The same validation the CRUD route applies — which is where the `script` shape check
        # lives, so a shared bundle cannot smuggle a shell payload through this path either.
        return _result(key, "failed", "; ".join(problems))
    repo.upsert(TABLE_TOOLS, tool.to_dict())
    return _result(key, "applied", "replaced" if existing else "created")


def _apply_workflow(workflow_id: str, doc: dict[str, Any] | None) -> dict[str, Any]:
    key = f"workflow:{workflow_id}"
    if doc is None:
        return _result(key, "skipped", "the bundle carried no definition for this workflow")
    resource = f"workflow:{workflow_id}"
    if not _may("workflow:save", resource):
        return _result(key, "skipped", "not authorized to save workflows (workflow:save)")

    incoming = dict(doc)
    incoming["id"] = workflow_id
    if not str(incoming.get("name") or "").strip():
        return _result(key, "failed", "workflow name is required")
    try:
        workflow_engine.save_workflow(incoming)
    except ValueError as exc:
        return _result(key, "failed", str(exc))
    except Exception as exc:
        _logger.warning("board import: workflow %s failed", workflow_id, exc_info=True)
        return _result(key, "failed", str(exc))
    return _result(key, "applied", "saved")
