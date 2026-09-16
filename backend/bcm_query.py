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
"""Structured BCM queries via pythoncm, owned out-of-process on the head.

The production path sends serialized requests to ``nvis_pythoncm_broker.py``, which keeps exactly
one cluster connection alive.  Rack Status still never imports pythoncm into this process: it is
version-locked to the cluster's cmdaemon, not on PyPI, and one container can serve different
clusters.  When the broker is unavailable during boot or recovery, the original one-shot helper
remains a slower availability fallback.

That also keeps the credentials where they already are — the client cert, key and cluster CA
never leave the head — and avoids redistributing BCM's library inside an image we ship elsewhere.

Two things are per-cluster and therefore configurable rather than hard-coded, because they
differ by BCM release:

* ``NVIS_BCM_PYTHON`` — the interpreter. pythoncm needs dependencies (``passlib``) that only
  BCM's bundled python has; the distro python3 on the same head finds the module and then dies
  importing it, which is a confusing failure worth configuring away.
* ``NVIS_PYTHONCM_PATH`` — the module location, for sites keeping per-cluster copies.

Every failure returns ``ok: False`` with a reason rather than raising, so callers can surface a
precise unavailable state instead of losing the whole request. :func:`available` reports whether
this path works at all.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any

import host_exec
import pythoncm_broker_client

_logger = logging.getLogger(__name__)

#: BCM bundles its own interpreter; the distro one on the same host lacks pythoncm's deps.
DEFAULT_PYTHON = "/cm/local/apps/python312/bin/python3"

#: Long enough for a whole-fleet walk (~3000 devices) over a multi-hop connection, short enough
#: that a wedged cmdaemon does not hold a UI request open.
DEFAULT_TIMEOUT = 120

def _script_path() -> str:
    # role_root("scripts") already points at the scripts directory on the target host; joining
    # "bin/scripts" onto it again yields .../bin/scripts/bin/scripts and a confusing ENOENT.
    base = host_exec.role_root("scripts") or ""
    if base:
        return str(Path(base) / "nvis_bcm_query.py")
    remote_base = (os.environ.get("RACK_REMOTE_BASE_PATH") or "").strip()
    if remote_base:
        return str(Path(remote_base) / "bin" / "scripts" / "nvis_bcm_query.py")
    # This module lives under <repo>/backend. Deriving the in-tree script directly removes the
    # old import-app cycle and keeps command construction usable in isolated provider tests.
    return str(Path(__file__).resolve().parent.parent / "bin" / "scripts" / "nvis_bcm_query.py")


def build_command(
    query: str,
    *,
    entity_type: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
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
        ("--ca-out", "NVIS_BCM_CA_OUT"),
        ("--username", "NVIS_BCM_USERNAME"),
        ("--password-file", "NVIS_BCM_PASSWORD_FILE"),
        ("--cookie-file", "NVIS_BCM_COOKIE_FILE"),
    ):
        value = os.environ.get(env)
        if value:
            cmd += [flag, value]
    if (os.environ.get("NVIS_BCM_CA_BOOTSTRAP") or "").strip().lower() in {"1", "true", "yes"}:
        cmd.append("--bootstrap-ca")
    # NVIS_BCM_PASSWORD and NVIS_BCM_COOKIE are intentionally absent. Raw secrets are inherited
    # only in local execution; SSH deployments use the head-node *_FILE options above so a secret
    # never appears in argv, command displays, or transport logs.
    if extra_args:
        cmd.extend(str(arg) for arg in extra_args)
    return cmd


def query(
    kind: str = "nodes",
    *,
    entity_type: str | None = None,
    extra_args: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Run one query on the head and return its JSON, or an ``ok: False`` explanation."""
    cmd = build_command(kind, entity_type=entity_type, extra_args=extra_args)
    broker_socket = (os.environ.get("NVIS_PYTHONCM_SOCKET") or "").strip()
    if broker_socket:
        try:
            return _query_broker(broker_socket, cmd[2:], timeout=timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            # During boot/systemd recovery, retain the proven one-shot path. Once
            # the broker socket accepts a request, its structured answer is final;
            # never duplicate a possibly-mutating request through a fallback.
            _logger.warning("PythonCM broker unavailable; using one-shot helper")
        except Exception as exc:
            _logger.warning("PythonCM broker request failed", exc_info=True)
            return {
                "ok": False,
                "error": "persistent PythonCM broker failed",
                "detail": f"{type(exc).__name__}: {exc}",
            }
    try:
        proc = host_exec.run_on_host(cmd, timeout=timeout, role="control")
    except Exception as exc:  # transport failures become an unavailable response, not a 500
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


def _query_broker(socket_path: str, argv: list[str], *, timeout: int) -> dict[str, Any]:
    """Send one serialized query to the host-local persistent PythonCM owner."""
    return pythoncm_broker_client.request(
        socket_path, "query", argv, timeout=timeout
    )


def nodes(**kw: Any) -> dict[str, Any]:
    """Every compute device with its rack position and BMC address."""
    return query("nodes", **kw)


def racks(**kw: Any) -> dict[str, Any]:
    """Every BCM rack entity, including racks that currently contain no compute node."""
    return query("racks", **kw)


def topology(**kw: Any) -> dict[str, Any]:
    """BCM racks and NodeGroups from one cached-entity PythonCM session."""
    return query("topology", **kw)


def rf0(**kw: Any) -> dict[str, Any]:
    """Physical-node BMC/rf0 addresses from PythonCM network-interface entities."""
    return query("rf0", **kw)


def switches(**kw: Any) -> dict[str, Any]:
    """Switch devices — on NVL72 these are the NVLink switch trays, each with its own BMC."""
    return query("switches", **kw)


def capabilities(**kw: Any) -> dict[str, Any]:
    """Report the exact operations exposed by this head's matching pythoncm package."""
    return query("capabilities", **kw)


def node_states(target: str = "", **kw: Any) -> dict[str, Any]:
    """Physical-node device, drain, allocation, and reason state in one query."""
    args = ["--target", target] if target else []
    return query("node-states", extra_args=args, **kw)


def power(target: str, action: str, **kw: Any) -> dict[str, Any]:
    """Perform one parallel BCM power operation and wait for its per-device result."""
    if action not in {"on", "off", "reset", "status"}:
        raise ValueError(f"unsupported BCM power action: {action}")
    return query("power", extra_args=["--target", target, "--action", action], **kw)


def drain(target: str, reason: str = "", **kw: Any) -> dict[str, Any]:
    args = ["--target", target]
    if reason:
        args += ["--reason", reason]
    return query("drain", extra_args=args, **kw)


def undrain(target: str, **kw: Any) -> dict[str, Any]:
    return query("undrain", extra_args=["--target", target], **kw)


def drain_status(target: str, **kw: Any) -> dict[str, Any]:
    return query("drain-status", extra_args=["--target", target], **kw)


def available(timeout: int = 45) -> tuple[bool, str]:
    """Whether the pythoncm path works here, and why not when it does not.

    The capability query still establishes and tears down a full PythonCM session. Forty-five
    seconds accommodates the observed ~20 second BCM 11 connect/disconnect cycle without using
    the 120 second whole-fleet query budget.
    """
    result = query("capabilities", timeout=timeout)
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
