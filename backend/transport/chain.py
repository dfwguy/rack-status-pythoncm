# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Declarative N-level connection chains.

A chain is an ordered list of hops the app traverses to reach a target:

    local (implicit root) -> [vpn] -> head -> ... -> node

Each hop declares its own transport (``local``/``ssh``; ``vpn`` reserved) and
settings (host/user/key/port/sudo), and is folded onto the previous hop via
:meth:`Connection.hop`, so the whole path is one composed :class:`Connection`.
This is what lets a cluster say "ssh to head as cmods (no sudo), then ssh to the
node as cmods (sudo)" — or, later, "VPN first, then head, then node" — without any
special-casing: it's just more hops.

Fan-out (:mod:`transport.fanout`) uses this to build the shared prefix once and
expand only the final per-node hop across N targets.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from transport.base import Connection
from transport.local import LocalConnection

HOP_KINDS = ("local", "ssh", "vpn")
IMPLEMENTED_HOP_KINDS = ("local", "ssh")


@dataclass
class HopSpec:
    """One hop in a connection chain."""

    kind: str = "ssh"                       # local | ssh  (vpn reserved / not yet impl)
    host: str = ""
    user: str = ""
    key: str = ""
    port: str = "22"
    sudo: bool = False
    extra_opts: list[str] = field(default_factory=list)
    connect_timeout: str = "15"

    @classmethod
    def from_dict(cls, d: dict) -> HopSpec:
        if not isinstance(d, dict):
            raise ValueError("hop spec must be a mapping")  # noqa: TRY004 - routes catch ValueError to answer 400; TypeError would 500
        return cls(
            kind=str(d.get("kind", "ssh")).strip().lower() or "ssh",
            host=str(d.get("host", "")).strip(),
            user=str(d.get("user", "")).strip(),
            key=str(d.get("key", "")).strip(),
            port=str(d.get("port", "22")).strip() or "22",
            sudo=bool(d.get("sudo", False)),
            extra_opts=list(d.get("extra_opts") or []),
            connect_timeout=str(d.get("connect_timeout", "15")).strip() or "15",
        )


def apply_hop(base: Connection, spec: HopSpec, *, host_override: str | None = None) -> Connection:
    """Fold a single hop onto *base* and return the new Connection."""
    host = host_override or spec.host
    if spec.kind == "local":
        return base  # local is only meaningful as the root; passthrough otherwise
    if spec.kind == "vpn":
        raise NotImplementedError(
            "vpn connection kind is reserved but not implemented yet "
            "(add a VpnConnection and handle it here)"
        )
    if spec.kind != "ssh":
        raise ValueError(f"unknown hop kind {spec.kind!r} (expected one of {IMPLEMENTED_HOP_KINDS})")
    if not host:
        raise ValueError("ssh hop requires a host")
    return base.hop(
        host,
        user=spec.user or None,
        key=spec.key,
        extra_opts=spec.extra_opts,
        connect_timeout=spec.connect_timeout,
        sudo=spec.sudo,
    )


def build_chain(specs: list, *, base: Connection | None = None) -> Connection:
    """Fold an ordered list of hop specs/dicts into one composed Connection."""
    conn = base if base is not None else LocalConnection()
    for spec in specs or []:
        hop = spec if isinstance(spec, HopSpec) else HopSpec.from_dict(spec)
        conn = apply_hop(conn, hop)
    return conn
