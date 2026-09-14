# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The first-run setup wizard's own endpoints.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``GET  /api/setup/status``          is setup required, and why
``POST /api/setup/naming/propose``  one example hostname -> candidate naming templates
``POST /api/setup/ssh-key``         store pasted or uploaded key material, return its path
``POST /api/setup/complete``        record that the wizard finished
``POST /api/setup/reopen``          forget that, so the wizard can be run again

**Creating the project is not here.** The wizard posts the assembled document to
``/api/catalog/projects`` and then ``/api/catalog/active`` -- the same two endpoints the Projects
screen uses, with the same validation and the same authorization. A second creation path would
be a second place for the Project schema to be half-understood, and the one that drifts is the
one an operator uses once and never checks.

So these routes are only the parts the catalog API cannot answer: the trigger, the naming
helper, and writing a private key to a file.

Gating: one action per route, which the action registry enforces for anything mutating -- two
mutating routes sharing an action would make them one capability, and these are genuinely
different ones. Writing a private key is not the same permission as marking a deployment
configured, and an administrator should be able to grant the second without the first.
"""

from __future__ import annotations

import logging

import naming_propose
import script_tree
import setup_state
import ssh_key_store
from audit.actions import action
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

bp = Blueprint("setup", __name__, url_prefix="/api/setup")


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


@bp.route("/status", methods=["GET"])
@action(get="catalog:read")
def api_setup_status():
    """Whether the wizard should open, and the layout its last step will act on.

    `/api/auth-check` already carries the boolean so the SPA can gate without a second round
    trip; this is the detailed answer for the wizard itself and for support questions of the
    form "why did it ask me again?".
    """
    status = setup_state.status()
    return jsonify({"ok": True, "setup": status, "scripts": script_tree.describe()})


@bp.route("/naming/propose", methods=["POST"])
@action("catalog:propose_naming")
def api_setup_naming_propose():
    """Candidate naming templates for one example hostname.

    A read despite being a POST: it computes over the body and stores nothing. POST because a
    hostname in a query string ends up in access logs and browser history, and this one names a
    customer's hardware.
    """
    body = request.get_json(silent=True) or {}
    hostname = str(body.get("hostname") or "").strip()
    result = naming_propose.propose(hostname)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@bp.route("/ssh-key", methods=["POST"])
@action("catalog:write_ssh_key")
def api_setup_ssh_key():
    """Write pasted or uploaded key material and return the path to put on the cluster.

    Body::

        {"project": "<id>", "cluster": "<id>", "material": "-----BEGIN ... KEY-----\\n..."}

    The response carries a path and a fingerprint, never the material. The wizard puts the path
    in ``connection.ssh_key``, which is what ``ssh -i`` and ``host_exec`` expect.
    """
    body = request.get_json(silent=True) or {}
    project = str(body.get("project") or "").strip()
    cluster = str(body.get("cluster") or "").strip()
    material = str(body.get("material") or "")
    if not project or not cluster:
        return _err("project and cluster ids are required to name the key file")

    try:
        path, fp = ssh_key_store.write_key(project, cluster, material)
    except ssh_key_store.KeyRejected as exc:
        # The message is written to be shown: it says which half of a keypair was pasted.
        return _err(str(exc))
    except OSError as exc:
        # Deliberately not `str(exc)` alone -- an OSError names the path, which is fine, but the
        # caller needs to know it is a filesystem problem and not a bad key.
        _logger.error("setup: could not store ssh key: %s", exc)
        return _err(f"could not write the key file: {exc.strerror or exc}", 500)

    return jsonify({"ok": True, "path": path, "fingerprint": fp, "key": ssh_key_store.describe(path)})


@bp.route("/complete", methods=["POST"])
@action("catalog:complete_setup")
def api_setup_complete():
    """Record that setup finished, so the wizard stops opening.

    Called after the project and its active selection are written. Separate from project
    creation on purpose: a project created from the Projects screen should not silently mark a
    deployment "set up", and a wizard that failed at its last step should be resumable.
    """
    body = request.get_json(silent=True) or {}
    project = str(body.get("project") or "").strip()
    setup_state.mark_complete(project)
    # The tree may have been created moments ago by the scripts step, and the layout probe is
    # cached -- without this the first launch after the wizard could still resolve against the
    # legacy flat directory.
    script_tree.clear_cache()
    _logger.info("setup: marked complete (project=%s)", project or "-")
    return jsonify({"ok": True, "setup": setup_state.status()})


@bp.route("/reopen", methods=["POST"])
@action("catalog:reopen_setup")
def api_setup_reopen():
    """Forget the completion flag, so the wizard can be run again.

    What the Projects screen's wizard button uses. Note it does not delete anything: a
    deployment with an operator-created project still reports setup as done, so this is only
    useful together with the force flag or on a deployment whose only project is the seeded one.
    """
    setup_state.reopen()
    return jsonify({"ok": True, "setup": setup_state.status()})
