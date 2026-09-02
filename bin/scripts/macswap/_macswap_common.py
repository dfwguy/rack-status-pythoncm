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
Shared helpers for ``swap_node_macs.py`` and ``change_node_macs.py``.

Kept deliberately small: just the bits both scripts need to import the
BCM ``pythoncm`` library, identify MACs, and configure logging in a
consistent way. The leading-underscore module name marks it as
internal: it lives next to the two scripts and is not intended to be
imported by anything else.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys

NULL_MAC = "00:00:00:00:00:00"

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def normalize_mac(mac: str) -> str:
    """Validate a MAC string and return its lowercase form.

    Raises ``argparse.ArgumentTypeError`` on a malformed value so the
    same helper plugs straight into ``argparse`` ``type=`` slots.
    """
    if not MAC_RE.match(mac):
        raise argparse.ArgumentTypeError(
            f"invalid MAC address {mac!r} (expected aa:bb:cc:dd:ee:ff)"
        )
    return mac.lower()


def interface_spec(value: str) -> tuple[str, str]:
    """Parse a ``NAME=MAC`` argparse value into ``(name, normalized_mac)``."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"interface spec {value!r} must look like name=aa:bb:cc:dd:ee:ff"
        )
    name, _, mac = value.partition("=")
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError(f"interface spec {value!r} has empty name")
    return name, normalize_mac(mac)


def import_cluster_or_die():
    """Import ``pythoncm.cluster.Cluster`` or print a hint and exit 127."""
    try:
        from pythoncm.cluster import Cluster
    except ImportError as exc:
        sys.stderr.write(
            f"error: unable to import pythoncm ({exc}).\n"
            "Hint: load the BCM python3 module first:\n"
            "    module load python3\n"
        )
        sys.exit(127)
    return Cluster


def interface_has_mac(interface) -> bool:
    """True iff ``interface`` exposes a ``mac`` field. Physical and BMC
    interfaces do; alias/vlan/bond etc. do not.
    """
    return interface.meta.meta.get_by_name("mac") is not None


def is_real_mac(mac: str | None) -> bool:
    """True iff ``mac`` is set to something other than the NULL MAC."""
    return bool(mac) and mac.lower() != NULL_MAC


def first_physical_iface(node):
    """First interface on ``node`` with a ``mac`` field, or ``None``."""
    for iface in node.interfaces:
        if interface_has_mac(iface):
            return iface
    return None


def effective_node_mac(node) -> str:
    """``node.mac`` if real, otherwise the first physical/BMC interface
    MAC. Returns the empty string if neither is real.
    """
    raw = getattr(node, "mac", "") or ""
    if is_real_mac(raw):
        return raw.lower()
    first = first_physical_iface(node)
    if first is not None and is_real_mac(first.mac):
        return first.mac.lower()
    return ""


def get_device(cluster, name: str, logger: logging.Logger):
    """Resolve a device by hostname, or log and exit 2."""
    device = cluster.get_by_name(name, "Device")
    if device is None:
        logger.error("Node %r not found in cluster", name)
        sys.exit(2)
    return device


def configure_logging(logger: logging.Logger, verbose: int, quiet: bool) -> None:
    """Map -q / -v / -vv to logger levels.

    quiet    : script = WARNING, pythoncm = WARNING
    default  : script = INFO,    pythoncm = WARNING
    -v       : script = INFO,    pythoncm = INFO
    -vv      : script = DEBUG,   pythoncm = DEBUG
    """
    if quiet:
        own_level = lib_level = logging.WARNING
    elif verbose >= 2:
        own_level = lib_level = logging.DEBUG
    elif verbose == 1:
        own_level = lib_level = logging.INFO
    else:
        own_level, lib_level = logging.INFO, logging.WARNING

    logging.basicConfig(
        level=min(own_level, lib_level),
        format="%(levelname)-7s %(message)s",
        force=True,
    )
    logger.setLevel(own_level)
    logging.getLogger("pythoncm").setLevel(lib_level)


def add_verbosity_args(parser) -> None:
    """Add the standard -v / -vv / -q mutually-exclusive group."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity. Repeat (-vv) for DEBUG output.",
    )
    group.add_argument(
        "-q", "--quiet", action="store_true",
        help="Only print warnings and errors.",
    )
