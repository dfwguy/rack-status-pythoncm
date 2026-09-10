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
# build-in-docker.sh -- build the Linux and Windows installers in a container.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHAT THIS DOES AND DOES NOT SOLVE
#
# Solves: producing AppImage / deb / rpm without a Linux machine, which is what "Linux is a
# first-class target" needs when every developer here is on macOS.
#
# Does NOT solve the DMG problem. A macOS disk image needs `hdiutil` and HFS formatting, so mac
# targets require a Mac -- Docker on macOS is a Linux VM and cannot build them at any price.
# Signing and notarization need macOS tooling too. See the DMG section of README.md.

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)

# The image publishes linux/amd64 only, so on Apple Silicon this runs emulated. Stated explicitly
# rather than left to a warning: it is slow but correct, because electron-builder *downloads*
# prebuilt Electron for the target arch rather than compiling it, and the one production dependency
# (electron-updater) is pure JavaScript with nothing to rebuild natively.
IMAGE=${BUILDER_IMAGE:-electronuserland/builder:latest}
PLATFORM=${BUILDER_PLATFORM:-linux/amd64}

# Host caches, so a rebuild does not re-download ~200 MB of Electron every run.
ELECTRON_CACHE="$HOME/.cache/electron"
BUILDER_CACHE="$HOME/.cache/electron-builder"
mkdir -p "$ELECTRON_CACHE" "$BUILDER_CACHE"

TARGETS=("$@")
if [ ${#TARGETS[@]} -eq 0 ]; then
  TARGETS=(--linux --x64)
fi

echo "building ${TARGETS[*]} in $IMAGE ($PLATFORM)"

docker run --rm \
  --platform "$PLATFORM" \
  -v "$PROJECT_DIR":/project \
  `# An ANONYMOUS volume over node_modules, and this line is the important one. A bind-mounted` \
  `# node_modules would have Linux packages installed into it by the npm ci below, replacing the` \
  `# darwin-arm64 native bindings the host needs -- which is exactly what happened when an earlier` \
  `# container run was pointed at frontend/: rolldown's binding vanished and 'npm test' died on` \
  `# the host with MODULE_NOT_FOUND. Masking the path keeps the two installs entirely separate.` \
  -v /project/node_modules \
  -v "$ELECTRON_CACHE":/root/.cache/electron \
  -v "$BUILDER_CACHE":/root/.cache/electron-builder \
  -w /project \
  "$IMAGE" \
  /bin/bash -c "
    set -euo pipefail
    node -v
    npm ci --no-audit --no-fund
    npm run build
    npx electron-builder ${TARGETS[*]} --publish never
  "

echo
echo "artifacts in $PROJECT_DIR/release:"
ls -1sh release/ 2>/dev/null | grep -viE '^total|linux-unpacked|builder-debug' || echo "(none)"
