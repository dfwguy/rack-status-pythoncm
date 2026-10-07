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
# enroot_scrub_rootfs.sh — undo what enroot's runtime hooks wrote into a --rw rootfs, before export.
# Author: Rafael M. Koike
#
# WHY: `enroot start --rw` runs enroot's runtime hooks against the writable rootfs. They add the
# invoking user to /etc/passwd|group|shadow|gshadow, create its home directory, point /etc/localtime
# at the build host's zone, and drop an apt.conf.d/enroot -- and `enroot export` captures all of it.
# The exported image then records WHO built it, on WHICH host, in WHAT timezone: two builds of the
# same sources differ, and the image ships one builder's account. (Seen on Nscale NC: a NeMo+NCCL
# rebuild carried /home/<builder> and the builder's passwd entries.) This restores each such path
# from the base image the container was created from, or removes it where the base has none, so the
# export differs from its base only by what the build itself installed.
#
# Usage: enroot_scrub_rootfs.sh ROOTFS BASE_SQSH [BUILDER]
#   ROOTFS     the enroot container's rootfs directory ($ENROOT_DATA_PATH/<name>)
#   BASE_SQSH  the .sqsh the container was created from
#   BUILDER    account whose home directory the hooks created (default: the invoking user)
set -euo pipefail

ROOTFS="${1:?usage: enroot_scrub_rootfs.sh ROOTFS BASE_SQSH [BUILDER]}"
BASE="${2:?usage: enroot_scrub_rootfs.sh ROOTFS BASE_SQSH [BUILDER]}"
BUILDER="${3:-$(id -un)}"

[ -f "$ROOTFS/etc/passwd" ] || { echo "ERROR: not a rootfs (no etc/passwd): $ROOTFS"; exit 2; }
[ -f "$BASE" ]              || { echo "ERROR: base image missing: $BASE"; exit 2; }
command -v unsquashfs >/dev/null || { echo "ERROR: unsquashfs not found"; exit 2; }
# BUILDER becomes a path that is removed recursively, so it must be a plain account name -- never
# empty, never a path. An empty name would otherwise turn "home/$BUILDER" into the whole of home/.
[[ $BUILDER =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || { echo "ERROR: invalid builder name: '$BUILDER'"; exit 2; }

# The paths the hooks are known to write. Anything else the build changed is the build's own output.
PATHS=(
  etc/passwd etc/passwd- etc/group etc/group- etc/shadow etc/shadow- etc/gshadow etc/gshadow-
  etc/localtime etc/apt/apt.conf.d/enroot "home/$BUILDER"
)

staging="$(mktemp -d)"
trap 'rm -rf -- "$staging"' EXIT

restored=0
removed=0
for p in "${PATHS[@]}"; do
  if unsquashfs -n -l "$BASE" "$p" 2>/dev/null | grep -qx "squashfs-root/$p"; then
    # Present in the base: put the base's copy back (file, symlink or directory, as it was there).
    unsquashfs -n -f -no-xattrs -d "$staging/x" "$BASE" "$p" >/dev/null 2>&1
    rm -rf -- "${ROOTFS:?}/$p"
    mkdir -p -- "$(dirname -- "$ROOTFS/$p")"
    cp -a -- "$staging/x/$p" "$ROOTFS/$p"
    restored=$((restored + 1))
  elif [ -e "$ROOTFS/$p" ] || [ -L "$ROOTFS/$p" ]; then
    # Absent from the base: the hooks created it, so it goes.
    rm -rf -- "${ROOTFS:?}/$p"
    removed=$((removed + 1))
  fi
done

echo "SCRUB_DONE restored=$restored removed=$removed rootfs=$ROOTFS"
