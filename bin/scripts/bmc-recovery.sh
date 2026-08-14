#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# bmc-recovery.sh -- cold-reset a node's BMC via ipmitool.
#
# Looks up the BMC interface's MAC in BCM (cmsh), finds the latest DHCP
# offer for that MAC in syslog, then issues `ipmitool mc reset cold`
# against that IP using the credentials from the node's bmcsettings.
#
# Usage:
#   bmc-recovery.sh <node> [interface]
#
# Defaults:
#   interface = rf0
#

set -u
set -o pipefail

# BCM installs cmsh at an absolute path and does NOT put it on PATH (Nscale TX: BCM 11 keeps
# it at /cm/local/apps/cmd/bin/cmsh). RACK_CMSH is the same knob the backend uses.
CMSH=${RACK_CMSH:-cmsh}

PROG=$(basename -- "$0")

SYSLOG_PRIMARY=${SYSLOG_PRIMARY:-/var/log/syslog}
SYSLOG_FALLBACK=${SYSLOG_FALLBACK:-/var/log/syslog.1}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<EOF
Usage: $PROG <node> [interface]

  node        BCM device/node name (e.g. ghx02).
  interface   BMC interface name on the node. Default: rf0.

Reads MAC + credentials from cmsh, finds the latest DHCP-offered IP for
that MAC in $SYSLOG_PRIMARY (falling back to $SYSLOG_FALLBACK), and
runs 'ipmitool mc reset cold' against that BMC.
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    "")
        usage >&2
        exit 2
        ;;
esac

NODE=$1
IFACE=${2:-rf0}

# --- 1. Get the interface MAC from cmsh --------------------------------------

mac_out=$("$CMSH" -c "device; use ${NODE}; interfaces; get ${IFACE} mac;" 2>&1) || true

# Detect "node not found" -- cmsh emits something like:
#   Unable to find 'ghx02' (type 'list' to see valid names)
if printf '%s\n' "$mac_out" | grep -q "Unable to find"; then
    printf '%s\n' "$mac_out" >&2
    die "Node '${NODE}' not found in cmsh. Check the node name (typo?) and try again."
fi

# Happy path: cmsh prints just the MAC on one line.
MAC=$(printf '%s' "$mac_out" | tr -d '[:space:]' | tr 'A-F' 'a-f')

if [ "$MAC" = "00:00:00:00:00:00" ]; then
    die "Interface '${IFACE}' on '${NODE}' has MAC 00:00:00:00:00:00 -- no BMC MAC configured."
fi

printf '%s: %s MAC = %s\n' "$NODE" "$IFACE" "$MAC"

# --- 2. Get BMC username + password from cmsh --------------------------------

# Returns exactly two lines: line 1 = username, line 2 = password.
{ read -r BMC_USER; read -r BMC_PASS; } < <(
    "$CMSH" -c "device; use ${NODE}; bmcsettings; get username; get password;"
)

printf '%s: BMC username = %s\n' "$NODE" "$BMC_USER"

# --- 3. Find the latest DHCP-offered IP for that MAC -------------------------

# dhcpd logs look like:
#   ... dhcpd[1234]: DHCPOFFER on 10.1.2.3 to aa:bb:cc:dd:ee:ff via eth0
# Read the file from the bottom (tac) and stop at the first match
# (grep -m1) -- that's the most recent DHCPOFFER, and in the common
# case we only touch the tail of the log.
find_latest_offer() {
    local logfile=$1
    [ -r "$logfile" ] || return 1
    tac -- "$logfile" 2>/dev/null \
        | grep -m1 -iE "DHCPOFFER on ([0-9]{1,3}\.){3}[0-9]{1,3} to ${MAC}\b" \
        | grep -Eoi 'DHCPOFFER on ([0-9]{1,3}\.){3}[0-9]{1,3}' \
        | awk '{print $3}'
}

IP=$(find_latest_offer "$SYSLOG_PRIMARY" || true)
SRC=$SYSLOG_PRIMARY
if [ -z "${IP:-}" ]; then
    IP=$(find_latest_offer "$SYSLOG_FALLBACK" || true)
    SRC=$SYSLOG_FALLBACK
fi

if [ -z "${IP:-}" ]; then
    cat <<EOF >&2
MAC not found, confirm the mac address is correct in BMC. proceed by manually flapping the port on the switch.

  searched: $SYSLOG_PRIMARY, $SYSLOG_FALLBACK
  MAC:      $MAC
  node:     $NODE  (interface $IFACE)
EOF
    exit 3
fi

printf '%s: latest DHCP offer IP = %s (from %s)\n' "$NODE" "$IP" "$SRC"

# --- 4. ipmitool mc reset cold -----------------------------------------------

printf '%s: running ipmitool mc reset cold against %s ...\n' "$NODE" "$IP"

ipmitool -I lanplus -H "$IP" -U "$BMC_USER" -P "$BMC_PASS" mc reset cold
rc=$?

if [ $rc -ne 0 ]; then
    die "ipmitool mc reset cold failed (exit $rc) against ${IP} for ${NODE}."
fi

printf '%s: BMC cold reset issued.\n' "$NODE"
