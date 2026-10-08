# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Settings Rack Status shares with the cluster's launch host and compute nodes, as files.

Some facts a launcher needs live only in Rack Status's store -- the P2P image catalog is the first:
which image filenames exist, their sizes and their SHA256 pins. The environment is the wrong carrier
for those (they are tables, and a job already running on a node must read them too), so they are
projected as files into the ``config/`` of the scripts the project actually runs
(``script_tree.config_dir``): ``project-<id>/config`` for a bootstrapped project, otherwise the
platform layer's or ``multi-platform``'s. ``config/`` ships with the scripts
(``bin/scripts/config/README.md``), so every layer and every project copy already has one, and a
script reads it relative to its own directory (``bin/scripts/lib/node_config.inc.sh``) -- no path is
configured twice. Both deploy paths ship only that README, so a deploy never overwrites what was
published.

The store stays the source of truth; these files are a projection that callers re-publish when the
underlying document changes and on every script deploy and project bootstrap. This is deliberately
separate from the P2P control plane's own manifest copies, which the P2P sync owns and which tell
P2P whether *it* is in sync -- script execution never reads those.
"""
from __future__ import annotations

import logging
import re
import shlex
import uuid
from typing import Any

import host_exec
import script_tree

_logger = logging.getLogger(__name__)

#: A published file is a bare name: never a path, so a caller cannot write outside the directory.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def publish(files: dict[str, bytes], *, project_id: str = "", platform_id: str = "",
            profile: str = "script", timeout: int = 60) -> dict[str, Any]:
    """Write *files* into this cluster's config directory, each replaced atomically.

    Every file is written under a temporary name first and all are renamed in one command, so a
    launcher reading mid-publish sees the previous set or the new one, never a half-written file.
    Returns a summary rather than raising: callers attach it to an API response or deploy summary.
    """
    bad = [name for name in files if not _NAME_RE.fullmatch(name)]
    if bad:
        return {"ok": False, "error": f"not a bare file name: {', '.join(sorted(bad))}"}
    for label, value in (("project", project_id), ("platform", platform_id)):
        if value and not _NAME_RE.fullmatch(value):
            return {"ok": False, "error": f"invalid {label} id: {value!r}"}
    dest = script_tree.config_dir(project_id, platform_id)
    scripts = dest.rsplit("/", 1)[0]
    summary: dict[str, Any] = {"ok": True, "dest": dest, "files": sorted(files)}
    try:
        conn = host_exec.connection(profile)
        # Prove the target runs commands before writing a byte -- the same rule, for the same
        # reason, as a script deploy: with no SSH host configured the writes below would succeed
        # against the LOCAL filesystem and plant a config/ directory in the app's own tree.
        from script_deploy import cannot_execute

        unreachable = cannot_execute(conn)
        if unreachable:
            return {**summary, "ok": False, "error": unreachable}
        # Only beside scripts that are really there. Creating a scripts directory here would make a
        # layer exist with nothing in it, and resolution would hand it to launchers as their
        # NVIS_SCRIPTS_DIR -- every script would then resolve to a file that is not there.
        if not conn.is_dir(scripts):
            return {**summary, "ok": False,
                    "error": f"no scripts at {scripts}; deploy the scripts first, then publish"}
        error = conn.makedirs(dest)
        if error:
            return {**summary, "ok": False, "error": f"cannot create {dest}: {error}"}
        tag = uuid.uuid4().hex[:8]
        for name, data in files.items():
            error = conn.write_bytes(f"{dest}/.{name}.{tag}", data)
            if error:
                return {**summary, "ok": False, "error": f"cannot write {dest}/{name}: {error}"}
        moves = "; ".join(
            f"mv -f {shlex.quote(f'{dest}/.{name}.{tag}')} {shlex.quote(f'{dest}/{name}')}" for name in files
        )
        proc = conn.run(["sh", "-c", f"set -e; {moves}"], timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return {**summary, "ok": False, "error": f"cannot place {', '.join(files)}: {detail[:300]}"}
    except Exception as exc:  # noqa: BLE001 - the transport raises several unrelated types
        return {**summary, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    _logger.info("node-config: published %s to %s", ", ".join(sorted(files)), dest)
    return summary
