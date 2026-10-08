# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Giving a project its own copy of the scripts, and telling it apart from the defaults.

Author: Rafael M. Koike & Cursor Agent ("Ada")

One server runs several customers' projects out of one script directory, so editing a script to
fix one site edits it for every site. These two routes are how a project gets its own copy:

``GET  /api/catalog/projects/<id>/scripts``   what the layout resolves to, and what differs
``POST /api/catalog/projects/<id>/scripts``   compose the project's directory (the wizard's last step)

Mounted as a sub-resource of the catalog project rather than at a prefix of its own, which two
existing meta-tests insisted on and were right to: `test_authz_resource_coverage` needs the URL
parameter to be one the `catalog.project` resolver reads (`doc_id`), or the gate cannot tell
*which* project a grant refers to and answers 403 to a valid one; and `test_openapi` refuses a
tag that holds a single endpoint family, because a tag per endpoint is no grouping at all.

The work itself is `bin/bootstrap-project-scripts.sh`, run **on the execution host** through
`host_exec` rather than here. Two reasons, and the second is the one that matters: the script
tree lives on the head node, so a local copy would write to the wrong machine entirely; and
composing the layers is shell work that an operator can run by hand when the dashboard is not
the thing in front of them.

**The read is separately gated from the write** (`catalog.project:read_scripts` versus
``:bootstrap_scripts``). Asking "what is customised here?" is something a read-only operator
should be able to do -- it is the question you ask before a deploy -- while composing the
directory writes to a shared filesystem.
"""

from __future__ import annotations

import json
import logging
import shlex

import host_exec
import script_tree
from audit.actions import action
from catalog import models
from catalog.store import TABLE_PROJECTS, get_repo
from flask import Blueprint, jsonify, request

_logger = logging.getLogger(__name__)

bp = Blueprint("project_scripts", __name__, url_prefix="/api/catalog/projects")

#: Where the bootstrap script lives on the execution host, relative to the deployment root.
#: Not resolved through `script_tree`: this is the tool that *creates* the tree, so it cannot be
#: found by searching it.
_BOOTSTRAP_REL = "bin/bootstrap-project-scripts.sh"


def _err(message: str, status: int = 400):
    return jsonify({"ok": False, "error": message}), status


def _project(project_id: str) -> models.Project | None:
    raw = get_repo().get(TABLE_PROJECTS, project_id)
    return models.Project.from_dict(raw) if raw else None


def _platform_for(project: models.Project, cluster_id: str = "") -> str:
    """Platform id of the named cluster, else of the project's first.

    The platform decides which override layer is copied, so a project whose clusters differ in
    platform bootstraps per cluster. In practice a project is one platform, which is why the
    first cluster is a sane default rather than an error.
    """
    if not project.clusters:
        return ""
    if cluster_id:
        for c in project.clusters:
            if c.id == cluster_id:
                return c.platform_id
    return project.clusters[0].platform_id


def _bootstrap_path() -> str:
    base = script_tree.remote_base()
    if base:
        return f"{base}/{_BOOTSTRAP_REL}"
    from paths import REPO_ROOT

    return str(REPO_ROOT / _BOOTSTRAP_REL)


def _run_bootstrap(project_id: str, platform_id: str, *, dry_run: bool, force: bool):
    """Invoke the composer on the execution host and parse its JSON summary."""
    argv = [
        "bash",
        _bootstrap_path(),
        "--project",
        project_id,
        "--json",
    ]
    if platform_id:
        argv += ["--platform", platform_id]
    tree = script_tree.tree_root()
    if tree:
        argv += ["--tree", tree]
    if dry_run:
        argv.append("--dry-run")
    if force:
        argv.append("--force")

    # profile="script": the script tree belongs to the scripts role's host, which on a split
    # deployment (Boston) is not the one that answers cmsh.
    #
    # A `CompletedProcess`, not a tuple. The first version unpacked three values and every call
    # raised `TypeError: cannot unpack non-iterable CompletedProcess object` -- caught by
    # `test_endpoint_smoke`, which is exactly what that test is for: a handler nobody has called
    # yet still has to not crash.
    proc = host_exec.run_on_host(argv, profile="script", timeout=600)
    out = proc.stdout or ""
    if proc.returncode != 0:
        # The script's own stderr is the useful part -- it names the directory it could not
        # read. Truncated because a failed rsync can be verbose and this reaches a browser.
        detail = (proc.stderr or out or "bootstrap failed").strip()
        return None, detail[:2000]
    try:
        return json.loads(out.strip().splitlines()[-1]), ""
    except (ValueError, IndexError):
        return None, f"could not parse bootstrap output: {out[:500]}"


@bp.route("/<doc_id>/scripts", methods=["GET"])
@action(get="catalog.project:read_scripts")
def api_project_scripts_status(doc_id: str):
    """What this project's scripts resolve to, and how far they have drifted from the defaults.

    A dry run, so asking the question changes nothing -- which is what makes it safe to gate as
    a read.
    """
    project = _project(doc_id)
    if project is None:
        return _err(f"unknown project: {doc_id}", 404)

    platform_id = _platform_for(project, (request.args.get("cluster") or "").strip())
    layout = script_tree.describe()
    summary, problem = _run_bootstrap(doc_id, platform_id, dry_run=True, force=False)
    body = {
        "ok": True,
        "project": doc_id,
        "platform": platform_id,
        "layout": layout,
        "project_dir": script_tree.project_dir(doc_id),
    }
    if summary is None:
        # Not an error response: a deployment with no execution host configured cannot answer,
        # and the layout above is still worth returning. Saying so beats a 500 that reads as a
        # broken dashboard (the same reasoning as `paths._resolve_log_dir`).
        body["bootstrap_available"] = False
        body["detail"] = problem
    else:
        body["bootstrap_available"] = True
        body["would_copy"] = summary.get("copied", 0)
        body["differs_from_defaults"] = summary.get("differs_from_defaults", 0)
        body["dest"] = summary.get("dest", "")
    return jsonify(body)


@bp.route("/<doc_id>/scripts", methods=["POST"])
@action("catalog.project:bootstrap_scripts")
def api_project_scripts_bootstrap(doc_id: str):
    """Compose this project's script directory from the platform and default layers.

    Body::

        {"force": false, "cluster": "<cluster-id>"}

    ``force`` overwrites files that differ, which means discarding an operator's edits in that
    directory -- so it is opt-in and the response says how many files it replaced.
    """
    project = _project(doc_id)
    if project is None:
        return _err(f"unknown project: {doc_id}", 404)

    body = request.get_json(silent=True) or {}
    force = bool(body.get("force"))
    platform_id = _platform_for(project, str(body.get("cluster") or "").strip())

    # The destination is computed, not supplied, so this cannot currently fail -- which is the
    # point of asserting it. It states the contract at the one place that writes the tree, so a
    # later change that makes the destination configurable meets the rule instead of quietly
    # gaining the ability to overwrite the shared default set.
    try:
        script_tree.assert_writable(script_tree.project_dir(doc_id))
    except script_tree.ImmutableLayer as exc:
        return _err(str(exc), 409)

    summary, problem = _run_bootstrap(doc_id, platform_id, dry_run=False, force=force)
    if summary is None:
        return _err(problem, 502)

    # The tree may have come into existence just now, and `script_tree` caches the probe that
    # decides whether layering applies. Without this the next launch would still resolve
    # against the flat legacy directory for up to the cache TTL.
    script_tree.clear_cache()

    # A fresh project copy is where launchers look for this project's published settings
    # (config/<cluster-id>/, node_config); fill it now rather than at the next catalog save.
    import p2p_catalog

    published = {
        c.id: p2p_catalog.publish_to_scripts(c.id)
        for c in project.clusters
        if c.p2p_enabled and p2p_catalog.has_catalog(c.id)
    }

    _logger.info(
        "project scripts bootstrapped: project=%s platform=%s dest=%s copied=%s forced=%s",
        doc_id,
        platform_id or "-",
        summary.get("dest", "-"),
        summary.get("copied", 0),
        force,
    )
    return jsonify(
        {
            "ok": True,
            "project": doc_id,
            "platform": platform_id,
            "dest": summary.get("dest", ""),
            "copied": summary.get("copied", 0),
            "differs_from_defaults": summary.get("differs_from_defaults", 0),
            "forced": force,
            "published_config": published,
            "layout": script_tree.describe(),
            "command": " ".join(shlex.quote(a) for a in ["bash", _bootstrap_path(), "--project", doc_id]),
        }
    )
