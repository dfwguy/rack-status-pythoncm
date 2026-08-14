#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack Status go-live cutover: offline cmsh shim -> live SSH to the BCM head node.

Flips RACK_EXEC_MODE local->ssh, points at nscale-hn1 (192.168.10.2, which holds the
compute-plane route ObsVM lacks), sets the Slurm absolute paths + SLURM_CONF that a
non-login ssh does not provide, mounts the ssh key read-only, drops the offline shim,
and turns web auth ON.

Idempotent. Backs up to docker-compose.override.yml.shim.bak on first run.

    python3 /opt/observability/nvis-rack-status-v3/apply_go_live.py            # apply
    python3 /opt/observability/nvis-rack-status-v3/apply_go_live.py --revert   # undo
"""
import os
import shutil
import sys

D = "/opt/observability/nvis-rack-status-v3"
P = os.path.join(D, "docker-compose.override.yml")
BAK = P + ".shim.bak"

# Supplied per run, never defaulted: this value becomes the dashboard's
# RACK_WEB_AUTH_TOKEN, so a literal here would put the live credential in git
# history and in every copy of this repo.
#     RACK_TOKEN='…' python3 apply_go_live.py
TOKEN = os.environ.get("RACK_TOKEN", "")

ENV_BLOCK = """      - RACK_PLATFORM=bcm
      - RACK_SSH_HOST=192.168.10.2
      - RACK_SSH_USER=nvis
      - RACK_SSH_PORT=22
      - RACK_SSH_KEY_PATH=/home/nvis/.ssh/id_bcm
      - RACK_CMSH=/cm/local/apps/cmd/bin/cmsh
"""

SHIM_MOUNT = "      - /opt/observability/nvis-rack-status-v3/deploy-secrets/cmsh-shim:/usr/local/bin/cmsh:ro\n"
KEY_MOUNT = "      - /home/nvis/.ssh/id_bcm:/home/nvis/.ssh/id_bcm:ro"


def revert():
    if not os.path.exists(BAK):
        sys.exit("no backup at %s — nothing to revert" % BAK)
    shutil.copy(BAK, P)
    print("reverted %s from %s" % (P, BAK))
    print("now run:  docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate rack-status")


def apply():
    if not TOKEN.strip():
        sys.exit("RACK_TOKEN is not set — export the dashboard auth token first, "
                 "e.g. RACK_TOKEN='…' python3 apply_go_live.py")
    if not os.path.exists(P):
        sys.exit("missing %s" % P)
    if not os.path.exists(BAK):
        shutil.copy(P, BAK)
        print("backed up -> %s" % BAK)

    key = "/home/nvis/.ssh/id_bcm"
    if not os.path.exists(key):
        sys.exit("SSH key not found at %s — aborting before touching the config" % key)

    s = open(P).read()
    s = s.replace("# LOCAL, OFFLINE real-data mode", "# LIVE SSH mode (was: LOCAL, OFFLINE)")

    s = s.replace("- RACK_EXEC_MODE=local", "- RACK_EXEC_MODE=ssh")
    s = s.replace("- RACK_WEB_AUTH_DISABLED=1", "- RACK_WEB_AUTH_TOKEN=" + TOKEN)

    if "- RACK_SSH_HOST=" not in s:
        s = s.replace("      - RACK_PLATFORM=bcm", ENV_BLOCK, 1)

    s = s.replace(SHIM_MOUNT, "")

    if KEY_MOUNT not in s:
        s = s.replace("    volumes:", "    volumes:\n" + KEY_MOUNT, 1)

    open(P, "w").write(s)

    checks = [
        ("exec mode = ssh", "RACK_EXEC_MODE=ssh" in s),
        ("auth token set", "RACK_WEB_AUTH_TOKEN=" in s),
        ("auth-disabled removed", "- RACK_WEB_AUTH_DISABLED=1" not in s),
        ("ssh host set", "- RACK_SSH_HOST=192.168.10.2" in s),
        ("cmsh abs path", "- RACK_CMSH_PATH=/cm/local/apps/cmd/bin/cmsh" in s),
        ("ssh key mounted", KEY_MOUNT in s),
        ("offline shim REMOVED", "cmsh-shim:/usr/local/bin/cmsh" not in s),
    ]
    print("\n--- preflight ---")
    ok = True
    for label, good in checks:
        print("  %-24s %s" % (label, "OK" if good else "*** FAILED ***"))
        ok = ok and good

    print("\n--- resulting override ---\n")
    print(s)

    if not ok:
        sys.exit("\nSome checks failed — review above. Revert with --revert if unsure.")
    print("Config written. Next:")
    print("  cd %s && docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate rack-status" % D)


if __name__ == "__main__":
    revert() if "--revert" in sys.argv else apply()
