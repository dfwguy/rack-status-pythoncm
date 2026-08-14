# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""
Change the MAC addresses of a single cluster device.

Library module backing the ``macswap`` CLI in change mode. If any
requested MAC is currently held by another device:

  * conflicts with exactly one other device  ->  delegate to
    ``swap_node_macs.run_swap`` on the same cluster connection;
  * conflicts span more than one other device  ->  refuse (exit 3).

Works for any BCM Device subtype. For comparison and display, a
device's "MAC" is its ``device.mac`` if real, otherwise its first
physical/BMC interface MAC.
"""
from __future__ import annotations

import logging
import sys

import swap_node_macs
from _macswap_common import (
    first_physical_iface,
    interface_has_mac,
    is_real_mac,
)

LOGGER = logging.getLogger("macswap.change")


def find_conflict(cluster, mac: str, target_uuid):
    """Return ``(device, location)`` for the first OTHER device that
    currently owns ``mac``, or ``(None, None)``. Searches every Device
    subtype across: ``device.mac`` (or effective fallback when unset)
    and every physical/BMC interface MAC.
    """
    mac = mac.lower()
    if not is_real_mac(mac):
        return None, None
    for node in cluster.get_by_type("Device"):
        if node.uuid == target_uuid:
            continue
        raw = (getattr(node, "mac", "") or "").lower()
        if raw == mac:
            return node, "node mac"
        if not is_real_mac(raw):
            first = first_physical_iface(node)
            if first is not None and (first.mac or "").lower() == mac:
                return node, f"effective node mac (via {first.name})"
        for it in node.interfaces:
            if interface_has_mac(it) and (it.mac or "").lower() == mac:
                return node, f"interface {it.name}"
    return None, None


def collect_changes(node, node_mac: str | None, ifspecs):
    """Build the list of intended changes, validating against ``node``.

    ``ifspecs`` is an iterable of ``(name, mac)`` pairs as produced by
    ``_macswap_common.interface_spec``. Unknown interface names or
    interfaces without a MAC field cause ``sys.exit(2)``.
    """
    changes = []
    if node_mac is not None:
        changes.append(("node", None, node.mac, node_mac))

    ifs_by_name = {it.name: it for it in node.interfaces}
    for name, mac in ifspecs:
        if name not in ifs_by_name:
            LOGGER.error("Node %s has no interface named %r", node.hostname, name)
            sys.exit(2)
        iface = ifs_by_name[name]
        if not interface_has_mac(iface):
            LOGGER.error(
                "Interface %s on %s has no MAC attribute (type=%s)",
                name, node.hostname, type(iface).__name__,
            )
            sys.exit(2)
        changes.append(("interface", iface, iface.mac, mac))
    return changes


class MultiNodeConflict(Exception):
    """Raised when requested MACs conflict with more than one other device."""


def detect_swap_target(cluster, node, changes):
    """Return the single other device that owns every conflicting MAC,
    ``None`` if there are no conflicts, or raise ``MultiNodeConflict``.
    """
    conflicts = []
    for kind, iface, _old, new in changes:
        other, where = find_conflict(cluster, new, node.uuid)
        if other is not None:
            label = "node MAC" if kind == "node" else f"interface {iface.name!r}"
            LOGGER.info("Requested %s %s is currently on %s (%s)",
                        label, new, other.hostname, where)
            conflicts.append(other)

    if not conflicts:
        return None
    unique = {n.uuid: n for n in conflicts}
    if len(unique) > 1:
        names = ", ".join(sorted(n.hostname for n in unique.values()))
        raise MultiNodeConflict(
            f"Requested MACs conflict with multiple other nodes ({names}); "
            "refusing. Resolve the duplicates manually."
        )
    return next(iter(unique.values()))


def apply_changes(node, changes, dry_run: bool) -> int:
    for kind, iface, old, new in changes:
        if kind == "node":
            LOGGER.info("Node %s: node MAC %s -> %s", node.hostname, old, new)
            if not dry_run:
                node.mac = new
        else:
            LOGGER.info("Node %s: interface %s MAC %s -> %s",
                        node.hostname, iface.name, old, new)
            if not dry_run:
                iface.mac = new

    if dry_run:
        LOGGER.info("Dry-run; no changes committed.")
        return 0

    result = node.commit()
    if not result.good:
        LOGGER.error("Commit failed for %s: %s", node.hostname, result)
        return 1
    LOGGER.info("Committed changes to %s", node.hostname)
    return 0


def run_change(cluster, node, node_mac: str | None, ifspecs,
               dry_run: bool) -> int:
    """Apply MAC change(s) to one already-resolved Device entity.

    The caller owns the ``cluster`` connection. On a single-node
    conflict this delegates in-process to ``swap_node_macs.run_swap``
    using the same connection -- no subprocess, no second connect.

    Returns:
        0   -- success (or successful dry-run, or nothing to do).
        1   -- direct commit failed, or delegated swap commit failed.
        2   -- unknown interface name on the target (via collect_changes).
        3   -- requested MACs conflict with more than one other device.
        4   -- delegated swap committed but uniqueness check failed.
    """
    changes = collect_changes(node, node_mac, ifspecs)
    if not changes:
        LOGGER.info("No changes requested.")
        return 0

    try:
        swap_target = detect_swap_target(cluster, node, changes)
    except MultiNodeConflict as exc:
        LOGGER.error("%s", exc)
        return 3

    if swap_target is not None:
        LOGGER.info(
            "MAC(s) belong to %s; performing a full swap with %s instead.",
            swap_target.hostname, node.hostname,
        )
        return swap_node_macs.run_swap(cluster, node, swap_target, dry_run)

    return apply_changes(node, changes, dry_run)
