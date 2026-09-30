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
"""Structured BCM queries via pythoncm, run out-of-process on the head.

Thin wrapper around ``bin/scripts/nvis_bcm_query.py``. The design decision this encodes is that
we never import pythoncm into this process: it is version-locked to the cluster's cmdaemon, not
on PyPI, and one container serves several clusters, so no single pinned copy could be correct
for all of them. Instead each call runs a short-lived interpreter on the machine whose pythoncm
matches its own cmdaemon by construction, and we read JSON back.

That also keeps the credentials where they already are — the client cert, key and cluster CA
never leave the head — and avoids redistributing BCM's library inside an image we ship elsewhere.

Two things are per-cluster and therefore configurable rather than hard-coded, because they
differ by BCM release:

* ``NVIS_BCM_PYTHON`` — the interpreter. pythoncm needs dependencies (``passlib``) that only
  BCM's bundled python has; the distro python3 on the same head finds the module and then dies
  importing it, which is a confusing failure worth configuring away.
* ``NVIS_PYTHONCM_PATH`` — the module location, for sites keeping per-cluster copies.

Every failure returns ``ok: False`` with a reason rather than raising, so callers degrade to
cmsh instead of losing the feature. :func:`available` reports whether this path works at all.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
from typing import Any

import host_exec
import script_tree

_logger = logging.getLogger(__name__)

#: BCM bundles its own interpreter; the distro one on the same host lacks pythoncm's deps.
DEFAULT_PYTHON = "/cm/local/apps/python312/bin/python3"

#: Long enough for a whole-fleet walk (~3000 devices) over a multi-hop connection, short enough
#: that a wedged cmdaemon does not hold a UI request open.
DEFAULT_TIMEOUT = 120


def _script_path() -> str:
    # Resolve through the script tree (project / custom / platform / multi-platform layers, or
    # the legacy flat dir when there is no tree), exactly like schedulers/pythoncm.py._helper_path.
    # Preferring role_root("scripts") here was the bug that made two resolvers disagree about which
    # copy of the script runs: script_deploy writes the layered tree at {base}/scripts, while the
    # legacy role root points at {base}/bin/scripts -- so on a tree deployment the helper resolved
    # to an empty legacy dir and every query failed with "can't open file". resolve() searches the
    # tree first and still falls back to the legacy dir, so both layouts work.
    return script_tree.resolve("nvis_bcm_query.py")


def build_command(query: str, *, entity_type: str | None = None) -> list[str]:
    """The argv to run on the head. Separated out so tests can assert it without a cluster."""
    cmd = [
        os.environ.get("NVIS_BCM_PYTHON") or DEFAULT_PYTHON,
        _script_path(),
        "--query", query,
    ]
    if entity_type:
        cmd += ["--type", entity_type]
    for flag, env in (
        ("--module-path", "NVIS_PYTHONCM_PATH"),
        ("--host", "NVIS_BCM_HOST"),
        ("--port", "NVIS_BCM_PORT"),
        ("--cert", "NVIS_BCM_CERT"),
        ("--key", "NVIS_BCM_KEY"),
        ("--ca", "NVIS_BCM_CA"),
    ):
        value = os.environ.get(env)
        if value:
            cmd += [flag, value]
    return cmd


def query(kind: str = "nodes", *, entity_type: str | None = None,
          timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one query on the head and return its JSON, or an ``ok: False`` explanation."""
    cmd = build_command(kind, entity_type=entity_type)
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout)
    except Exception as exc:  # transport failures are a fallback signal, not a bug
        _logger.debug("bcm_query transport failed", exc_info=True)
        return {"ok": False, "error": "transport failed", "detail": f"{type(exc).__name__}: {exc}"}

    stdout = (getattr(proc, "stdout", "") or "").strip()
    if not stdout:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        return {"ok": False, "error": "no output from nvis_bcm_query",
                "detail": stderr[:500] or f"rc={getattr(proc, 'returncode', '?')}",
                "command": shlex.join(cmd)}

    # The script prints exactly one JSON object, but an SSH login banner can precede it, so take
    # the last line that parses rather than assuming stdout is clean.
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "error": "unparseable output", "detail": stdout[-500:]}


def nodes(**kw: Any) -> dict[str, Any]:
    """Every compute device with its rack position and BMC address."""
    return query("nodes", **kw)


def switches(**kw: Any) -> dict[str, Any]:
    """Switch devices — on NVL72 these are the NVLink switch trays, each with its own BMC."""
    return query("switches", **kw)


def available(timeout: int = 20) -> tuple[bool, str]:
    """Whether the pythoncm path works here, and why not when it does not.

    Callers use this to decide between the fast structured path and the cmsh fallback without
    paying for a full fleet walk first.
    """
    result = query("version", timeout=timeout)
    if result.get("ok"):
        return True, ""
    reason = str(result.get("error") or "unavailable")
    hint = result.get("hint") or result.get("detail") or ""
    return False, f"{reason}: {hint}".strip(": ")


def index_by_hostname(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Reshape a successful query into the ``{hostname: {...}}`` form the inventory wants.

    An unsuccessful query yields an empty index rather than raising, so the report degrades to
    "no BCM identity" instead of failing outright.
    """
    if not result.get("ok"):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for device in result.get("data") or []:
        hostname = str(device.get("hostname") or "")
        if hostname:
            out[hostname] = device
    return out
