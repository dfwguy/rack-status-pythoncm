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
# run-local-nscale-tx.sh — run the dashboard on this Mac against the Nscale TX cluster.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# THE ROUTE, AND WHY IT IS NOT THE OBVIOUS ONE
#
# Three hops, three different auth mechanisms:
#
#   this Mac --Teleport cert--> mobilekit device2 --PASSWORD--> nvis@192.168.10.3 (OVA)
#                                                  --KEY id_bcm--> cmods@192.168.10.2 (BCM head)
#
# The head takes a KEY, not a password. Do NOT use the `nscale-tx-node` alias in ~/.ssh/config for
# this: it sets `PubkeyAuthentication no` and jumps via device2 straight at the head, so it offers
# the one thing the head rejects and you get "Permission denied" twice and a broken pipe. This
# script goes the documented way instead -- jump through the OVA, authenticate with the key.
#
# Only ONE step needs a human: the password hop to the OVA. The app runs ssh with `BatchMode=yes`
# (a web request must never block on a prompt), so it can never type that password. Open a
# ControlMaster to the OVA yourself and every jump the app makes reuses that socket silently.
#
# Usage:
#   ssh -o ControlPersist=8h nscale-tx-ova 'echo ok'   # once, by a human: the password hop
#   dist/run-local-nscale-tx.sh --check                # preflight only, no server
#   dist/run-local-nscale-tx.sh                        # then run it
#
# The alias's own ControlPersist is 10m, which is why the one-liner above overrides it to 8h -- a
# ten-minute master lapses mid-session and the dashboard starts reporting the cluster unreachable.
# If that happens, re-open the master rather than debugging the app.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

# --- Cluster identity ---------------------------------------------------------------------------
# Announce it, because this repo serves four clusters and the wrong host silently hits the wrong one.
echo "=============================================================="
echo " Target cluster : Nscale TX  (GB300 NVL72, BCM 11 + Slurm 25.05)"
echo " Head node      : cmods@192.168.10.2  (key auth, unprivileged Slurm operator)"
echo " Path           : Teleport -> device2 -> OVA (password master) -> head (key)"
echo "=============================================================="

# --- Preflight: the key, and the OVA master that carries the password hop -------------------------
BCM_KEY="${RACK_SSH_KEY:-$HOME/.ssh/id_bcm_nscale}"
if [[ ! -f "$BCM_KEY" ]]; then
  echo "STOP: BCM head key not found at $BCM_KEY (the OVA calls it /home/nvis/.ssh/id_bcm)." >&2
  echo "      Set RACK_SSH_KEY=/path/to/key and re-run." >&2
  exit 1
fi

# The app cannot create the OVA master itself (password + BatchMode), so a missing one is a hard
# stop with an actionable message rather than a dashboard full of 500s.
OVA_SOCK="$HOME/.ssh/cm-nvis@192.168.10.3:22"
_open_ova_master_hint() {
  cat <<EOF

Open it yourself (one password prompt), then re-run this script:

    ssh -o ControlPersist=8h nscale-tx-ova 'echo ok'

The alias's own ControlPersist is 10m, which lapses mid-session; 8h covers a working day. If that
command fails with "Connection closed by UNKNOWN port 65535" the Teleport jumphost is offline --
not a credential problem. Check 'tsh status' and re-run 'tsh login' if the cert has expired.

EOF
}
if [[ ! -S "$OVA_SOCK" ]]; then
  echo; echo "STOP: no ssh ControlMaster to the OVA (the password hop)."; _open_ova_master_hint
  exit 1
fi
# A socket file outlives its connection; -O check is the only proof it is still usable. This is the
# "Master running is not health" trap from docs/EXECUTION_ARCHITECTURE.md 6.1.
if ! ssh -O check nscale-tx-ova >/dev/null 2>&1; then
  echo; echo "STOP: the OVA ControlMaster socket exists but is DEAD (a stale socket never self-heals)."
  echo "      rm -f '$OVA_SOCK'"; _open_ova_master_hint
  exit 1
fi
echo "[preflight] OVA ControlMaster alive; BCM key $BCM_KEY"

# --- Connection: reproduce the OVA container's own access to the head -----------------------------
# The deployed instance runs ON THE OVA and reaches the head as cmods with the id_bcm key -- an
# unprivileged Slurm-operator account, NOT root. We use the same user and the same key so a local run
# exercises the same permissions; the only difference is one extra hop in front of it.
#
# Note there is deliberately no RACK_SCRIPT_SSH_* profile here. Boston splits privileged work onto a
# root plane; Nscale TX has no root for us, and cmods is a Slurm operator, so `scontrol update` works
# directly while `sudo` would prompt and hang. A privileged profile would be a lie.
export RACK_SSH_HOST=192.168.10.2            # the IP, NOT the nscale-tx-node alias: that alias sets
                                             # PubkeyAuthentication=no and would offer a password,
                                             # which is exactly what the head rejects.
export RACK_SSH_USER=cmods
export RACK_SSH_KEY="$BCM_KEY"
export RACK_SSH_OPTS="-J nscale-tx-ova"      # jump through the OVA, over the master opened above
export RACK_SSH_CONNECT_TIMEOUT=20           # three hops, one of them a Teleport proxy
# App-side multiplexing stays ON (the default) and is load-bearing here: it means ONE ssh master to
# the head, so only one channel is ever opened through the OVA. Turning it off would push every
# command through its own jump and hit the OVA's sshd MaxSessions (10) during a fan-out.

# --- Platform ------------------------------------------------------------------------------------
export RACK_PLATFORM=bcm
# cmsh is NOT on a non-login PATH here, and the app probes it by bare name by default -- which fails
# as "cmsh not in PATH" on the Overview banner with every counter reading 0, since rack collection
# never runs. Same class of problem as the Slurm CLIs below, and the only other bare-binary default
# in the backend.
export RACK_CMSH=/cm/local/apps/cmd/bin/cmsh
export RACK_STATUS_DEMO=0
export RACK_WEB_AUTH_DISABLED=1              # local dev only; the OVA uses token auth

# --- Paths on the HEAD NODE (not this Mac) -------------------------------------------------------
export RACK_REMOTE_BASE_PATH=/home/cmods/nvis-rack-status
export RACK_STATUS_LOG_DIR=/home/cmods/benchmark/harish
# COMMA-separated, and once set it is the ONLY read gate -- every root that must be readable has to
# appear here, including where operators' job StdOut lands outside the log dir.
export RACK_STATUS_ALLOWED_PREFIXES=/home/cmods/benchmark/harish,/home/cmods/bringup,/home/cmods
export RACK_BRINGUP_HOME=/home/cmods/bringup

# --- Naming: rack ids and real tray hostnames ----------------------------------------------------
export NVIS_RACK_ID='ink50-dh01-[a-z][\d+]'
export NVIS_CT_ID='ink50-dh01-[a-z][\d+]-[\d+]'
# Trays are NON-CONTIGUOUS here. The built-in 01-18 default targets ten nodes that do not exist and
# misses ten that do; it only bites when an action runs without an explicit node selection.
export RACK_VALIDATION_CT_RANGE=11-18,28-37

# --- Slurm: nothing is on a non-login PATH -------------------------------------------------------
# BCM 11 splits /cm/shared by arch and does not export SLURM_CONF for non-login ssh, so even absolute
# paths die with "Could not establish a configuration source". RACK_SLURM_CONF (the RACK_-prefixed
# name -- a bare SLURM_CONF here would be ignored) is re-exported in the remote shell by the platform
# provider.
export RACK_SLURM_SKIP_MODULE_LOAD=1
export RACK_SLURM_CONF=/cm/shared-ubuntu2404-x86_64/apps/slurm/etc/slurm/slurm.conf
_SLURM_BIN=/cm/local/apps/slurm/25.05/bin
export RACK_SLURM_BIN="$_SLURM_BIN"
export RACK_SLURM_SQUEUE="$_SLURM_BIN/squeue"
export RACK_SLURM_SINFO="$_SLURM_BIN/sinfo"
export RACK_SLURM_SCONTROL="$_SLURM_BIN/scontrol"
export RACK_SLURM_SCANCEL="$_SLURM_BIN/scancel"
export RACK_SLURM_SACCT="$_SLURM_BIN/sacct"
export RACK_SLURM_SBATCH="$_SLURM_BIN/sbatch"
export RACK_SLURM_SRUN="$_SLURM_BIN/srun"

# --- Local server -------------------------------------------------------------------------------
# 5050, never 5000: on macOS :5000 is the AirPlay Receiver and answers 403.
export PORT="${PORT:-5050}"
# The SPA bundle in static/spa is current; skip the multi-minute rebuild. Set BUILD_FRONTEND=1 when
# frontend source has actually changed.
export BUILD_FRONTEND="${BUILD_FRONTEND:-0}"

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo
  echo "[check] probing the cluster through the app's own transport..."
  .venv/bin/python - <<'PY'
import sys
sys.path.insert(0, "backend")
import host_exec
from paths import _resolve_log_dir
from platforms import get_platform

plat = get_platform()
# sinfo_cmd/squeue_cmd take a STRING of extra args and return the full remote command string,
# already wrapped with the SLURM_CONF export this site needs. Passing a list here silently produced
# a mangled command that exited 0 with no output -- which looks like "the cluster said nothing".
import os

# Probe cmsh exactly the way the app resolves it, not by a hardcoded path: a path that works here
# while RACK_CMSH is unset would prove nothing, and "cmsh not in PATH" is precisely how that gap
# showed up in the UI (banner + every counter at 0).
_cmsh = os.environ.get("RACK_CMSH", "cmsh")
probes = (
    ("hostname", "hostname"),
    ("cmsh on PATH", f"command -v {_cmsh} >/dev/null && echo resolved || echo 'NOT FOUND'"),
    ("cmsh racks", f"{_cmsh} -c 'rack; list' | tail -n +3 | wc -l"),
    ("sinfo states", plat.sinfo_cmd('-h -o "%T %D"')),
    ("squeue jobs", plat.squeue_cmd('-h -o "%i" | wc -l')),
)
for label, cmd in probes:
    try:
        p = host_exec.run_on_host(cmd, timeout=60)
        body = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        shown = body.splitlines()[:3] if body else (["<empty>  stderr: " + err[:120]] if err else ["<empty>"])
        print(f"  {label:13} rc={p.returncode}  {shown}")
    except Exception as e:
        print(f"  {label:13} FAILED: {e}")

# The log dir and the allow-list are the two settings that make every read fail when wrong, and they
# fail with a 400 in the UI rather than anything obviously about configuration.
try:
    print(f"  {'log dir':13} {_resolve_log_dir(None)}  (readable, inside allowed prefixes)")
except Exception as e:
    print(f"  {'log dir':13} FAILED: {e}")

# Naming + tray range: this site's trays are 11-18,28-37, so a wrong template or the built-in 01-18
# default silently targets ten nodes that do not exist.
try:
    from node_targets import validation_target_for_rack
    # override is a required positional (None = "no explicit node selection"), which is the path
    # where the tray range actually matters.
    tgt = validation_target_for_rack("ink50-dh01-h18", None)
    print(f"  {'node target':13} ink50-dh01-h18 -> {tgt}")
except Exception as e:
    print(f"  {'node target':13} FAILED: {e}")
PY
  echo
  echo "[check] done — no server started."
  exit 0
fi

echo
echo "[run] dashboard -> http://localhost:${PORT}  (Ctrl-C to stop)"
exec ./start.sh
