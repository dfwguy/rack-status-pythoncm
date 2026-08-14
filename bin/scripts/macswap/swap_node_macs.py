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
Swap the MAC addresses of two cluster devices.

Library module backing the ``macswap`` CLI in swap mode. Swaps both the
device-level ``mac`` and the ``mac`` of every physical/BMC interface
whose name exists on both devices. Works for any BCM Device subtype
(compute nodes, head nodes, switches, generic devices, chassis, PDUs,
...) -- the "node" language is historical.

If a device's resolved post-swap ``device.mac`` would be NULL (because
its partner's pre-swap raw ``mac`` was NULL), the swap substitutes the
device's post-swap first physical/BMC interface MAC instead. That keeps
effective-MAC swap semantics exact AND avoids BCM's silent restoration
of a real->NULL write made inside a multi-commit session.
"""
from __future__ import annotations

import logging

from _macswap_common import (
    NULL_MAC,
    effective_node_mac,
    interface_has_mac,
    is_real_mac,
)

LOGGER = logging.getLogger("macswap.swap")


def _set_node_macs(node, node_mac: str, iface_macs: dict[str, str]) -> None:
    """Locally set node-level and named interface MACs (no commit)."""
    node.mac = node_mac
    by_name = {it.name: it for it in node.interfaces if interface_has_mac(it)}
    for name, mac in iface_macs.items():
        if name in by_name:
            by_name[name].mac = mac


def _resolve_target_node_mac(node, target_raw: str,
                             target_iface_macs: dict[str, str]) -> str:
    """If ``target_raw`` is real, return it. Otherwise fall back to the
    device's post-swap first physical/BMC interface MAC so the
    node-level field is never written NULL. Mirrors the read-side
    ``effective_node_mac()`` definition.
    """
    if is_real_mac(target_raw):
        return target_raw
    for iface in node.interfaces:
        if not interface_has_mac(iface):
            continue
        candidate = target_iface_macs.get(iface.name)
        if is_real_mac(candidate):
            LOGGER.info(
                "%s: target node-level MAC is unset; deriving %s from "
                "first physical/BMC interface %r",
                node.hostname, candidate, iface.name,
            )
            return candidate
    return target_raw or NULL_MAC


def _verify_macs(node, expected_mac: str,
                 expected_iface_macs: dict[str, str]) -> list[str]:
    """Re-read ``node`` from the cluster and return human-readable
    mismatch descriptions; empty list on success.
    """
    node.revert()
    out: list[str] = []
    actual = (node.mac or "").lower()
    want = (expected_mac or "").lower()
    if actual != want:
        out.append(f"{node.hostname}.mac: expected {want!r}, got {actual!r}")
    by_name = {it.name: it for it in node.interfaces if interface_has_mac(it)}
    for name, expected in expected_iface_macs.items():
        if name not in by_name:
            out.append(f"{node.hostname}.{name}: missing")
            continue
        actual = (by_name[name].mac or "").lower()
        want = (expected or "").lower()
        if actual != want:
            out.append(f"{node.hostname}.{name}: expected {want!r}, got {actual!r}")
    return out


def _commit_step(node, expected_mac, expected_iface_macs, label: str) -> bool:
    """Commit ``node`` with ``force=True``, then verify BCM persisted
    every requested MAC. Returns True iff everything matches.
    """
    result = node.commit(force=True)
    if not result.good:
        LOGGER.error("%s commit failed for %s: %s", label, node.hostname, result)
        return False
    mismatches = _verify_macs(node, expected_mac, expected_iface_macs)
    if mismatches:
        LOGGER.error(
            "%s commit reported success but BCM did not persist:\n  %s",
            label, "\n  ".join(mismatches),
        )
        return False
    LOGGER.debug("%s OK for %s", label, node.hostname)
    return True


def perform_swap(node_a, node_b, dry_run: bool) -> bool:
    """Swap node-level and matching interface MACs between ``node_a`` and
    ``node_b``.

    Uses a 3-step single-entity commit protocol so no individual commit
    ever creates a duplicate MAC: park one side at NULL, set the other
    to its final state, then set the parked side to its final state.

    Interfaces present on only one side produce a warning and are
    skipped; only interfaces named on both sides participate.
    """
    a_ifs = {it.name: it for it in node_a.interfaces if interface_has_mac(it)}
    b_ifs = {it.name: it for it in node_b.interfaces if interface_has_mac(it)}
    common = sorted(a_ifs.keys() & b_ifs.keys())

    for name in sorted(a_ifs.keys() - b_ifs.keys()):
        LOGGER.warning("Interface %r exists on %s but not on %s; skipping",
                       name, node_a.hostname, node_b.hostname)
    for name in sorted(b_ifs.keys() - a_ifs.keys()):
        LOGGER.warning("Interface %r exists on %s but not on %s; skipping",
                       name, node_b.hostname, node_a.hostname)

    orig_a_mac, orig_b_mac = node_a.mac, node_b.mac
    orig_a_ifaces = {name: a_ifs[name].mac for name in common}
    orig_b_ifaces = {name: b_ifs[name].mac for name in common}

    LOGGER.info(
        "Node-level MAC: %s (raw=%s, effective=%s) <-> %s (raw=%s, effective=%s)",
        node_a.hostname, orig_a_mac or "<unset>",
        effective_node_mac(node_a) or "<unset>",
        node_b.hostname, orig_b_mac or "<unset>",
        effective_node_mac(node_b) or "<unset>",
    )
    for name in common:
        LOGGER.info("Interface %r: %s (%s) <-> %s (%s)",
                    name, node_a.hostname, orig_a_ifaces[name],
                    node_b.hostname, orig_b_ifaces[name])

    if dry_run:
        return True

    new_a_mac = _resolve_target_node_mac(node_a, orig_b_mac, orig_b_ifaces)
    new_b_mac = _resolve_target_node_mac(node_b, orig_a_mac, orig_a_ifaces)
    null_ifaces = {name: NULL_MAC for name in common}

    # Step 1: park B (releases B's current values).
    # Step 2: assign A its final state (takes B's old values).
    # Step 3: assign B its final state (takes A's old values).
    _set_node_macs(node_b, NULL_MAC, null_ifaces)
    if not _commit_step(node_b, NULL_MAC, null_ifaces, "step 1/3"):
        return False

    _set_node_macs(node_a, new_a_mac, orig_b_ifaces)
    if not _commit_step(node_a, new_a_mac, orig_b_ifaces, "step 2/3"):
        return False

    _set_node_macs(node_b, new_b_mac, orig_a_ifaces)
    if not _commit_step(node_b, new_b_mac, orig_a_ifaces, "step 3/3"):
        return False

    # Final cross-entity verify: per-step checks only re-read the entity
    # just committed, not its partner.
    final = _verify_macs(node_a, new_a_mac, orig_b_ifaces)
    final += _verify_macs(node_b, new_b_mac, orig_a_ifaces)
    if final:
        LOGGER.error("Final post-swap verification failed:\n  %s",
                     "\n  ".join(final))
        return False

    LOGGER.info("Swap committed for %s and %s", node_a.hostname, node_b.hostname)
    return True


def check_effective_uniqueness(cluster, node_a, node_b) -> bool:
    """Ensure ``node_a`` and ``node_b``'s effective MACs are distinct
    and not held by any other Device in the cluster.
    """
    a_eff = effective_node_mac(node_a)
    b_eff = effective_node_mac(node_b)
    if a_eff and a_eff == b_eff:
        LOGGER.error("After swap, %s and %s share effective MAC (%s)",
                     node_a.hostname, node_b.hostname, a_eff)
        return False
    ok = True
    for other in cluster.get_by_type("Device"):
        if other.uuid in (node_a.uuid, node_b.uuid):
            continue
        other_eff = effective_node_mac(other)
        if not other_eff:
            continue
        for ours, name in ((a_eff, node_a.hostname), (b_eff, node_b.hostname)):
            if ours and ours == other_eff:
                LOGGER.error("Effective MAC %s on %s collides with %s",
                             ours, name, other.hostname)
                ok = False
    return ok


def run_swap(cluster, node_a, node_b, dry_run: bool) -> int:
    """Drive a full swap between two already-resolved Device entities.

    The caller owns the ``cluster`` connection; this function does not
    connect or disconnect.

    Returns:
        0   -- success (or successful dry-run).
        1   -- swap commit / final verification failed.
        4   -- swap committed but a third device holds one of the new
               effective MACs.
    """
    if node_a.mac in ("", NULL_MAC) and node_b.mac in ("", NULL_MAC):
        LOGGER.warning("Both nodes have an empty/null node MAC (%s)", NULL_MAC)

    if not perform_swap(node_a, node_b, dry_run):
        LOGGER.error("Swap failed; cluster may be in an inconsistent state.")
        return 1

    if dry_run:
        LOGGER.info("Dry-run; no changes committed.")
        return 0

    if not check_effective_uniqueness(cluster, node_a, node_b):
        LOGGER.error("Post-swap effective MAC uniqueness violated.")
        return 4

    LOGGER.info("Successfully swapped MACs between %s and %s",
                node_a.hostname, node_b.hostname)
    return 0
