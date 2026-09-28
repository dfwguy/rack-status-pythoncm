# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Build a routed, multi-hop transport from a declarative topology.

A *topology* is two lists of plain dicts:

``endpoints``
    Places the app can execute. ``{"id", "type": local|ssh, "host", "user", "key",
    "port", "via", "sudo", "opts"}``. ``via`` names another endpoint, and each leg
    runs *on* its predecessor, so ``app -> bastion -> head -> node`` is expressed by
    three ``via`` links rather than by any special two-hop code path. Chains nest to
    any depth (see :class:`~transport.hop.HopExecutor`).

``roles``
    What call sites ask for. ``{"id", "endpoint", "root", "sudo"}``. A role binds a
    name — ``control``, ``scripts``, ``logs``, ``bcm``, or anything a site invents —
    to an endpoint plus an optional base directory on it. Code names the role; the
    deployment decides which host that is. That indirection is what lets scripts,
    logs and the cluster manager live on three different machines, and lets the same
    build run on a laptop, in a container, or on the head node.

Kept free of any catalog/Flask import so the transport layer stays standalone and
unit-testable: callers pass plain dicts (``backend/host_exec.py`` translates the
catalog documents into this shape).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from transport.base import Connection, Executor
from transport.compose import (
    EnvForwardingExecutor,
    EnvResolver,
    RoutingConnection,
    SudoExecutor,
)
from transport.hop import HopExecutor
from transport.local import LocalExecutor
from transport.ssh import SSHConfig, SSHExecutor

_logger = logging.getLogger(__name__)

#: Route used when a caller names no role, and the fallback for an unknown one.
DEFAULT_ROLE = "control"

#: Historical name of the privileged route (docs/EXECUTION_ARCHITECTURE.md §1.2).
LEGACY_PRIVILEGED_ROLE = "script"


@dataclass
class SSHDefaults:
    """Connection tuning shared by every ssh endpoint that does not override it."""

    port: str = "22"
    connect_timeout: str = "10"
    alive_interval: str = "5"
    alive_count: str = "2"
    multiplex: bool = True
    mux_persist: str = "60"
    extra_opts: list[str] = field(default_factory=list)
    key: str = ""
    user: str = "root"


class TopologyError(ValueError):
    """The topology cannot be resolved (unknown ``via``, cycle, missing endpoint)."""


def _endpoints_by_id(endpoints: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for raw in endpoints:
        eid = str(raw.get("id") or "").strip()
        if eid:
            out[eid] = raw
    return out


def _truthy(v: object) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def build_executors(
    endpoints: list[dict],
    *,
    defaults: SSHDefaults | None = None,
) -> dict[str, Executor]:
    """Resolve every endpoint to an :class:`Executor`, following ``via`` chains.

    Each endpoint is built once and memoised, so a chain shared by several roles
    (the common case: everything reached through one head node) reuses a single
    executor — and therefore a single multiplexed ssh master.
    """
    d = defaults or SSHDefaults()
    by_id = _endpoints_by_id(endpoints)
    built: dict[str, Executor] = {}
    building: list[str] = []  # doubles as the cycle path for the error message

    def build(eid: str) -> Executor:
        if eid in built:
            return built[eid]
        if eid in building:
            raise TopologyError(f"endpoint via chain forms a cycle: {' -> '.join([*building, eid])}")
        raw = by_id.get(eid)
        if raw is None:
            raise TopologyError(f"unknown endpoint: {eid!r}")
        building.append(eid)
        try:
            ex = _build_one(raw, build, d)
        finally:
            building.pop()
        built[eid] = ex
        return ex

    for eid in by_id:
        build(eid)
    return built


def _build_one(
    raw: dict,
    build: Callable[[str], Executor],
    d: SSHDefaults,
) -> Executor:
    """One endpoint: local leaf, ssh leaf, or an ssh hop through its ``via`` base."""
    etype = str(raw.get("type") or "ssh").strip().lower()
    eid = str(raw.get("id") or "").strip()
    via = str(raw.get("via") or "").strip()
    sudo = _truthy(raw.get("sudo"))
    opts = [str(x) for x in (raw.get("opts") or [])] or list(d.extra_opts)

    if etype == "local":
        if via:
            raise TopologyError(f"endpoint {eid!r}: a local endpoint cannot have a via hop")
        return SudoExecutor(LocalExecutor()) if sudo else LocalExecutor()

    host = str(raw.get("host") or "").strip()
    if not host:
        raise TopologyError(f"endpoint {eid!r}: ssh endpoints need a host")
    user = str(raw.get("user") or "").strip() or d.user
    key = str(raw.get("key") or "").strip() or d.key

    if via:
        # A hop runs `ssh host <payload>` *on* the base endpoint, so it inherits that
        # host's credentials and reachability. HopExecutor applies sudo to the decoded
        # payload itself, which is why it is passed down rather than wrapped here.
        return HopExecutor(
            build(via),
            host,
            user=user or None,
            key=key,
            extra_opts=opts,
            connect_timeout=d.connect_timeout,
            sudo=sudo,
            gateway=True,
        )

    leaf: Executor = SSHExecutor(
        SSHConfig(
            host=host,
            user=user,
            key=key,
            port=str(raw.get("port") or "").strip() or d.port,
            extra_opts=opts,
            connect_timeout=d.connect_timeout,
            alive_interval=d.alive_interval,
            alive_count=d.alive_count,
            multiplex=d.multiplex,
            mux_persist=d.mux_persist,
        )
    )
    return SudoExecutor(leaf) if sudo else leaf


def build_topology(
    endpoints: list[dict],
    roles: list[dict],
    *,
    resolver: EnvResolver | None = None,
    defaults: SSHDefaults | None = None,
    sudo_password: str = "",
    sudo_preserve_env: bool = True,
) -> RoutingConnection:
    """Compose ``{role -> Connection}`` and route by role name.

    Env forwarding wraps each endpoint once, outside any role-level sudo, so the
    ``export …`` prefix is emitted in the login shell before ``sudo -E`` carries it
    across the escalation boundary (same ordering as the single-target factory).
    """
    executors = build_executors(endpoints, defaults=defaults)
    if resolver is not None:
        executors = {k: EnvForwardingExecutor(v, resolver) for k, v in executors.items()}

    role_list = [r for r in roles if str(r.get("id") or "").strip()]
    if not role_list:
        raise TopologyError("topology declares no roles")

    control = next((r for r in role_list if r.get("id") == DEFAULT_ROLE), None)
    if control is None:
        raise TopologyError(f"topology has no {DEFAULT_ROLE!r} role")
    control_endpoint = str(control.get("endpoint") or "").strip()

    targets: dict[str, Connection] = {}
    for raw in role_list:
        rid = str(raw["id"]).strip()
        # An unbound role follows control, so a site can name a role for clarity
        # (or for a future split) before it actually lives anywhere else.
        eid = str(raw.get("endpoint") or "").strip() or control_endpoint
        ex = executors.get(eid)
        if ex is None:
            raise TopologyError(f"role {rid!r}: unknown endpoint {eid!r}")
        if _truthy(raw.get("sudo")):
            ex = SudoExecutor(
                ex, password=sudo_password or None, preserve_env=sudo_preserve_env
            )
        targets[rid] = Connection(ex)

    # Keep the historical privileged profile resolvable even when a site only
    # declares the new role names; workflow YAML and older call sites still say
    # profile="script".
    #
    # It falls back to control, not to the similarly-named "scripts" role. The two
    # are different axes that happen to read alike: "script" is a *trust domain*
    # (the root/pdsh route, RACK_SCRIPT_SSH_*), while "scripts" is a *location*
    # (where the bin/scripts tree lives). Aliasing them would silently send
    # privileged node operations to whichever host merely stores the scripts.
    # Control is what the flat env contract falls back to, so this keeps parity; a
    # site with a separate privileged route declares a "script" role explicitly.
    if LEGACY_PRIVILEGED_ROLE not in targets:
        targets[LEGACY_PRIVILEGED_ROLE] = targets[DEFAULT_ROLE]

    return RoutingConnection(targets, default=DEFAULT_ROLE)


def role_roots(roles: list[dict]) -> dict[str, str]:
    """``{role -> base directory}`` for the roles that declare a ``root``."""
    out: dict[str, str] = {}
    for raw in roles:
        rid = str(raw.get("id") or "").strip()
        root = str(raw.get("root") or "").strip()
        if rid and root:
            out[rid] = root
    return out


def describe_topology(endpoints: list[dict], roles: list[dict]) -> dict[str, object]:
    """Human-readable summary: the hop chain behind each role (for /api + logs)."""
    by_id = _endpoints_by_id(endpoints)

    def chain(eid: str) -> list[str]:
        seen: list[str] = []
        cur = eid
        while cur and cur in by_id and cur not in seen:
            seen.append(cur)
            cur = str(by_id[cur].get("via") or "").strip()
        return list(reversed(seen))

    control = next((r for r in roles if r.get("id") == DEFAULT_ROLE), None)
    control_endpoint = str((control or {}).get("endpoint") or "").strip()
    out: dict[str, object] = {}
    for raw in roles:
        rid = str(raw.get("id") or "").strip()
        if not rid:
            continue
        eid = str(raw.get("endpoint") or "").strip() or control_endpoint
        hops = chain(eid)
        out[rid] = {
            "endpoint": eid,
            "path": ["app", *hops],
            "hops": max(0, len([h for h in hops if by_id.get(h, {}).get("type") != "local"])),
            "root": str(raw.get("root") or "").strip(),
        }
    return out
