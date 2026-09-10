#!/bin/bash
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
# dmgbuild-patient.sh -- run electron-builder's bundled dmgbuild with a longer detach window.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY THIS EXISTS
#
# On a Mac running Microsoft Defender for Endpoint with real-time protection on, `npm run dist`
# fails in DMG creation:
#
#   hdiutil: couldn't unmount "disk4" - Resource busy
#
# Root cause, from the DiskArbitration log: dmgbuild copies ~180 MB of app into a freshly mounted
# HFS volume, and Defender's real-time scanner (`wdavdaemon_unprivileged`, running as `_mdatp`)
# opens those files to scan them. `hdiutil` asks to unmount 0.9 s after the copy finishes and gets
# EBUSY (`status code 0x00000010`) five times over ~13 s -- dmgbuild's default `--detach-retries`
# is 5 -- and then gives up. A later forced unmount succeeds, which is why nothing is left mounted
# afterwards and the failure looks mysterious.
#
# Unprivileged `lsof` shows nothing holding the volume, which is what makes this so hard to
# diagnose: the holder is a root/`_mdatp` daemon, invisible without sudo. The kernel cannot help
# either -- diskarbitrationd tries `sysctl(vfs.generic.print_busy_vnodes)` and gets ENOENT, because
# that sysctl only exists on debug kernels.
#
# So the volume is not permanently busy, only busy for as long as the scan takes. Waiting is
# sufficient, and waiting changes no security posture -- which the alternative fix, a Defender
# folder exclusion, does. `--detach-retries 30` is dmgbuild's maximum.
#
# electron-builder invokes `dmgbuild -s <settings> <volume-name> <output.dmg>` and passes no retry
# flag, but honours `CUSTOM_DMGBUILD_PATH`. That is the hook this script plugs into.

set -euo pipefail

# `process.arch` in dmgUtil.js, i.e. the HOST arch -- not the target. One bundle serves every
# target arch, so this resolves to a single binary regardless of what is being built.
case "$(uname -m)" in
  arm64) BUNDLE_ARCH=arm64 ;;
  *) BUNDLE_ARCH=x86_64 ;;
esac

CACHE="$HOME/Library/Caches/electron-builder"
REAL=$(/bin/ls -1d "$CACHE"/dmg-builder@*/dmgbuild-bundle-"$BUNDLE_ARCH"-*/dmgbuild 2>/dev/null | tail -1 || true)

if [ -z "$REAL" ] || [ ! -x "$REAL" ]; then
  # The toolset is downloaded lazily by `getDmgVendorPath()` -- and setting CUSTOM_DMGBUILD_PATH
  # skips that code path entirely, so on a machine that has never built a DMG there is nothing to
  # wrap. Say so precisely instead of failing as "file not found".
  cat >&2 <<EOF
dmgbuild-patient.sh: electron-builder's dmgbuild bundle is not in the cache yet.

  looked for: $CACHE/dmg-builder@*/dmgbuild-bundle-$BUNDLE_ARCH-*/dmgbuild

Setting CUSTOM_DMGBUILD_PATH bypasses the code that downloads it, so it has to be fetched once
without this wrapper:

  npx electron-builder --mac dmg --publish never    # may fail on detach; the download still happens
  npm run dist:mac                                   # then this works

EOF
  exit 1
fi

# --- Logging -----------------------------------------------------------------------------------
#
# dmgbuild's own output is the only place the per-attempt detach story appears, and
# electron-builder discards it unless the build fails -- so a *successful but slow* run leaves no
# trace of how close it came. `DEBUG_DMG=true` does not help: it instruments electron-builder's own
# hdiutil calls, not the ones dmgbuild makes internally. Teeing here is what makes the flaky case
# diagnosable after the fact rather than only while it is hung.
LOG="${DMGBUILD_LOG:-${TMPDIR:-/tmp}/dmgbuild-$(date +%Y%m%d-%H%M%S).log}"
{
  echo "=== dmgbuild $(date '+%Y-%m-%d %H:%M:%S') ==="
  echo "real: $REAL"
  echo "args: --detach-retries 30 $*"
} >>"$LOG"

# Flag before the positionals: argparse tolerates interspersed arguments, but electron-builder's
# last argument is a path, and keeping options first removes any question of how it is parsed.
#
# `set -e` is disabled around the pipeline so a non-zero dmgbuild exit is recorded in the log
# before this script propagates it -- otherwise the one run worth reading is the one that logs
# nothing about how it ended.
set +e
"$REAL" --detach-retries 30 "$@" 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
{
  echo "=== exit: $rc at $(date '+%H:%M:%S') ==="
  # Whether the volume survived the run. dmgbuild can produce a DMG and still leave its scratch
  # volume mounted, which is invisible in a green build and is the state that makes the *next*
  # run fail.
  df 2>/dev/null | grep -i greenlight || echo "(no greenlight volume left mounted)"
} >>"$LOG"
echo "dmgbuild log: $LOG" >&2
exit "$rc"
