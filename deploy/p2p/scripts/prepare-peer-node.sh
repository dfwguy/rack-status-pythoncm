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
# prepare-peer-node.sh -- make one node ready for an NVIS-P2P peer. Run as ROOT on the node.
# Author: Rafael M. Koike
#
# What a reimage wipes and the peer deploy cannot restore for itself (it runs as the service
# account, with that account's sudo): the account's primary group, the container directory's
# ownership, the ownership of peer state that outlived the reimage on the data partition, and --
# where the cluster has one -- the node-healthcheck allowlist that keeps a running peer from draining
# the node. Each step is idempotent and changes nothing that is already right. census-peer-node.sh
# reports a node that needs this as needs-prep.
#
# Identity is corrected, never invented: the account must already exist (the cluster's directory --
# BCM, LDAP -- owns accounts) with the expected uid. When only its primary group is wrong, a local
# passwd entry with the right group shadows the directory one; that needs nsswitch to consult files
# first, and the script refuses rather than rewrite an existing local account.
#
# Env (required): NVIS_SERVICE_USER, NVIS_SERVICE_UID, NVIS_SERVICE_GROUP, NVIS_SERVICE_GID
# Env (optional):
#   NVIS_SERVICE_HOME                   home for a local entry (default /home/<user>)
#   NVIS_SERVICE_SHELL                  shell for a local entry (default /bin/bash)
#   NVIS_P2P_BASE                       peer state directory (default /opt/containers/.nvis-p2p)
#   NVIS_P2P_CONTAINER_DIR              image directory (default /opt/containers)
#   NVIS_P2P_CONTAINER_OWNER            its owner (default root:<service group>)
#   NVIS_P2P_CONTAINER_MODE             its mode (default 775)
#   NVIS_P2P_BCM_ROGUEPROCESS_CONFIG    BCM rogueprocess healthcheck config to allowlist the account
#                                       in; unset: no allowlist step
# Prints one line: PREP <host> passwd=.. containers=.. state=.. allowlist=.. uid=.. gid=.. sudo=..
# Exit: 0 ready, 2 bad parameters, 3 identity refused (nothing changed).
set -euo pipefail

user=${NVIS_SERVICE_USER:?NVIS_SERVICE_USER is required}
uid=${NVIS_SERVICE_UID:?NVIS_SERVICE_UID is required}
group=${NVIS_SERVICE_GROUP:?NVIS_SERVICE_GROUP is required}
gid=${NVIS_SERVICE_GID:?NVIS_SERVICE_GID is required}
home=${NVIS_SERVICE_HOME:-/home/$user}
shell=${NVIS_SERVICE_SHELL:-/bin/bash}
base=${NVIS_P2P_BASE:-/opt/containers/.nvis-p2p}
containers=${NVIS_P2P_CONTAINER_DIR:-/opt/containers}
owner=${NVIS_P2P_CONTAINER_OWNER:-root:$group}
mode=${NVIS_P2P_CONTAINER_MODE:-775}
rogue=${NVIS_P2P_BCM_ROGUEPROCESS_CONFIG:-}
# Test hook only: prefix for the /etc files edited below.
etc=${NVIS_PREP_SYSROOT:-}/etc

for v in "$uid" "$gid"; do [[ $v =~ ^[0-9]+$ ]] || { echo "PREP_REJECTED reason=non-numeric-id value=$v" >&2; exit 2; }; done
[[ $user =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "PREP_REJECTED reason=bad-user value=$user" >&2; exit 2; }
[[ $mode =~ ^[0-7]{3,4}$ ]] || { echo "PREP_REJECTED reason=bad-mode value=$mode" >&2; exit 2; }
refuse() { echo "PREP_REFUSED host=$(hostname) reason=$1" >&2; exit 3; }

# 1. Identity.
entry=$(getent passwd "$user") || refuse "account-$user-missing:provision-it-in-the-cluster-directory"
[[ $(cut -d: -f3 <<< "$entry") == "$uid" ]] || refuse "uid-mismatch:$user-has-$(cut -d: -f3 <<< "$entry")-expected-$uid"
getent group "$gid" >/dev/null || refuse "group-$gid-missing"
if [[ $(cut -d: -f4 <<< "$entry") == "$gid" ]]; then
  passwd=ok
elif grep -q "^$user:" "$etc/passwd"; then
  refuse "local-$user-entry-has-gid-$(cut -d: -f4 <<< "$entry"):not-rewriting-a-local-account"
elif [[ $(awk '$1 == "passwd:" { print $2; exit }' "$etc/nsswitch.conf" 2>/dev/null) != files ]]; then
  refuse "nsswitch-does-not-consult-files-first:a-local-entry-would-not-take-effect"
else
  (
    command -v flock >/dev/null && flock 9
    cp -p "$etc/passwd" "$etc/passwd.bak-nvis-p2p"
    cp -p "$etc/shadow" "$etc/shadow.bak-nvis-p2p"
    printf '%s:x:%s:%s::%s:%s\n' "$user" "$uid" "$gid" "$home" "$shell" >> "$etc/passwd"
    # Locked password: the account logs in by key only, exactly as through the directory.
    grep -q "^$user:" "$etc/shadow" || printf '%s:!:%s:0:99999:7:::\n' "$user" "$(( $(date +%s) / 86400 ))" >> "$etc/shadow"
  ) 9> "$etc/.pwd.lock"
  command -v nscd >/dev/null && nscd -i passwd 2>/dev/null || true
  passwd=added
fi

# 2. The container directory: usable by the service account and by jobs that read images.
if [[ -d $containers ]]; then
  have=$(stat -c '%U:%G:%a' "$containers")
  if [[ $have == "$owner:${mode#0}" ]]; then
    containers_state=ok
  else
    chown "$owner" "$containers"
    chmod "$mode" "$containers"
    containers_state="fixed($have)"
  fi
else
  install -d -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" "$containers"
  containers_state=created
fi

# 3. Peer state that outlived a reimage under a previous owner makes the daemon crash-loop on its
#    storage lock.
if [[ ! -d $base ]]; then
  state=absent
elif [[ -n $(find "$base" ! -user "$user" -print -quit 2>/dev/null) ]]; then
  chown -R "$user:$group" "$base"
  state=fixed
else
  state=ok
fi

# 4. Optional: BCM's rogueprocess healthcheck fails -- and drains -- a node running a process for an
#    account it does not list.
if [[ -z $rogue ]]; then
  allowlist=n/a
else
  [[ -f $rogue ]] || refuse "rogueprocess-config-missing:$rogue"
  allowlist=$(NVIS_USER="$user" "${NVIS_PREP_PYTHON:-python3}" - "$rogue" <<'PY'
import os, shutil, sys
path, user = sys.argv[1], os.environ["NVIS_USER"]
text = open(path).read()
if f'"{user}"' in text:
    print("present"); sys.exit(0)
anchor = "legitUsers = [\n"
if anchor not in text:
    print("anchor-missing"); sys.exit(0)
shutil.copy2(path, path + ".bak-nvis-p2p")
open(path, "w").write(text.replace(anchor, f'{anchor}    "{user}",\n', 1))
print("added")
PY
)
fi

sudo_state=no
sudo -n -u "$user" sudo -n true 2>/dev/null && sudo_state=yes
now=$(getent passwd "$user")
echo "PREP $(hostname) passwd=$passwd containers=$containers_state state=$state allowlist=$allowlist" \
     "uid=$(cut -d: -f3 <<< "$now") gid=$(cut -d: -f4 <<< "$now") sudo=$sudo_state"
