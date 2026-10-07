# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# lib/enroot_build.inc.sh — helpers for recipes that build an image with enroot.
# Author: Rafael M. Koike
#
# Sourced, not run. Kept apart from the recipes so the two decisions that make an image trustworthy
# -- which rootfs gets scrubbed before export, and what the image records about its sources -- can
# be tested without a 40-minute build (backend/tests/test_enroot_build_lib.py).

# Where `enroot create` puts a container's rootfs. Same resolution as enroot itself: ENROOT_DATA_PATH
# from the environment wins, else the value in $ENROOT_SYSCONF_PATH/enroot.conf (default /etc/enroot)
# -- which may hold shell such as "$(id -u)", hence the eval -- else enroot's default under
# $XDG_DATA_HOME. Prints <data path>/<name>.
enroot_rootfs_dir() {
  local data="${ENROOT_DATA_PATH:-}"
  local conf="${ENROOT_SYSCONF_PATH:-/etc/enroot}/enroot.conf"
  if [ -z "$data" ] && [ -r "$conf" ]; then
    data="$(awk '$1 == "ENROOT_DATA_PATH" { $1 = ""; sub(/^[ \t]+/, ""); print; exit }' "$conf")"
    data="$(eval "printf '%s' \"$data\"")"
  fi
  [ -n "$data" ] || data="${XDG_DATA_HOME:-$HOME/.local/share}/enroot"
  printf '%s/%s\n' "$data" "$1"
}

# Provenance of a source tree: its tag, or `git describe` output when it is not exactly at one (with
# a warning on stderr, because an untagged tree makes the image unreproducible), or "unknown" when
# git cannot describe it at all.
src_rev() {
  local rev
  rev="$(git -C "$1" describe --tags --always 2>/dev/null || echo unknown)"
  git -C "$1" describe --tags --exact-match >/dev/null 2>&1 \
    || echo "WARNING: $1 is not at a tag ($rev) -- the image will not be reproducible" >&2
  printf '%s' "$rev"
}
