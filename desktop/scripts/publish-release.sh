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
# publish-release.sh -- upload a locally signed macOS build to the Package Registry.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Mirrors the `release:publish-bundle` CI job, which does the same thing for the air-gapped Docker
# image — same registry, same asset-link call — but runs on a workstation, because macOS signing
# cannot happen in this pipeline (no macOS runner; see README).
#
# **Artifacts are never committed to git.** `release/` is gitignored and a 112 MB binary in history
# is a permanent tax on every clone. The generic Package Registry is version-addressed, permanent
# and already the pattern this repo uses.
#
# Two destinations, and both are needed for different readers:
#
#   <pkg>/latest/    the auto-update feed. `electron-builder.yml` bakes this exact URL into every
#                    build, so it MUST NOT contain a version — a 0.3.0 build asking a 0.3.0
#                    directory for news of 0.4.0 waits forever. Overwritten each release, so the
#                    registry's "Reject duplicates" setting needs an exception for it.
#   <pkg>/<version>/ immutable history, and what the GitLab Release links to.
#
# Usage:
#   GITLAB_TOKEN=<pat-with-api-scope> scripts/publish-release.sh
#   GITLAB_TOKEN=... scripts/publish-release.sh --dry-run
#
# `CI_JOB_TOKEN` is not usable here — that only exists inside a CI job. Use a Personal or Project
# Access Token with `api` scope.

set -euo pipefail

cd "$(dirname "$0")/.."
DESKTOP_DIR=$(pwd)

PROJECT="${GITLAB_PROJECT:-nvis%2Fnvis-rack-status}"
HOST="${GITLAB_HOST:-https://gitlab-master.nvidia.com}"
PACKAGE="${PACKAGE_NAME:-nvis-greenlight-desktop}"
MANIFEST="$DESKTOP_DIR/release/latest-mac.yml"
RELEASE_DIR="$DESKTOP_DIR/release/signed"
DRY_RUN=0

[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

[ -f "$MANIFEST" ] || { echo "publish: no $MANIFEST; run scripts/sign-3s.sh first" >&2; exit 1; }
if [ "$DRY_RUN" -eq 0 ] && [ -z "${GITLAB_TOKEN:-}" ]; then
  echo "publish: set GITLAB_TOKEN to a PAT with 'api' scope" >&2
  exit 1
fi

# Read what the manifest actually claims, rather than guessing the filename. The updater resolves
# `path:` relative to the feed URL, so the uploaded name has to match it exactly.
ARTIFACT=$(grep '^path:' "$MANIFEST" | head -1 | sed 's/^path:[[:space:]]*//')
CLAIMED_SHA=$(grep '^sha512:' "$MANIFEST" | head -1 | sed 's/^sha512:[[:space:]]*//')
VERSION=$(grep '^version:' "$MANIFEST" | head -1 | sed 's/^version:[[:space:]]*//')

ARTIFACT_PATH="$RELEASE_DIR/$ARTIFACT"
[ -f "$ARTIFACT_PATH" ] || ARTIFACT_PATH="$DESKTOP_DIR/release/$ARTIFACT"
[ -f "$ARTIFACT_PATH" ] || { echo "publish: manifest names '$ARTIFACT' but it is not in release/ or release/signed/" >&2; exit 1; }

# ── The guard that matters ─────────────────────────────────────────────────────────────────────
# electron-builder writes latest-mac.yml against the UNSIGNED artifact, and signing changes the
# bytes. Publishing that pair means `electron-updater` downloads the update, fails the integrity
# check and refuses it — an auto-update that is broken for everyone, discovered only by users.
# Cross-checking here makes shipping a stale pair impossible.
ACTUAL_SHA=$(shasum -a 512 "$ARTIFACT_PATH" | cut -d' ' -f1 | xxd -r -p | base64)
if [ "$ACTUAL_SHA" != "$CLAIMED_SHA" ]; then
  cat >&2 <<EOF
publish: REFUSING — latest-mac.yml does not describe this artifact.

  artifact : $ARTIFACT_PATH
  claimed  : $CLAIMED_SHA
  actual   : $ACTUAL_SHA

The manifest is stale, usually because it was written before signing. Re-run
scripts/sign-3s.sh, which rewrites it against the signed artifact.
EOF
  exit 1
fi

# Notarization is what makes it installable on someone else's Mac; publishing an un-notarized
# build produces a download that Gatekeeper refuses, which reads to the recipient as "corrupt".
if ! xcrun stapler validate "$ARTIFACT_PATH" >/dev/null 2>&1; then
  case "$ARTIFACT" in
    *.zip) : ;;  # a zipped .app cannot carry a staple; the .app inside it does
    *) echo "publish: WARNING — $ARTIFACT has no stapled notarization ticket" >&2 ;;
  esac
fi

BASE="$HOST/api/v4/projects/$PROJECT/packages/generic/$PACKAGE"
echo "==> version:  $VERSION"
echo "==> artifact: $ARTIFACT_PATH ($(du -h "$ARTIFACT_PATH" | cut -f1))"
echo "==> sha512:   verified against the manifest"
echo "==> feed:     $BASE/latest/"
echo "==> archive:  $BASE/$VERSION/"

upload() { # upload <file> <destdir>
  local file="$1" dest="$2" name
  name=$(basename "$file")
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] PUT $dest/$name"
    return 0
  fi
  echo "    uploading $name -> $dest/"
  curl --fail-with-body --silent --show-error \
    --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    --upload-file "$file" "$dest/$name"
  echo
}

for dest in "$BASE/latest" "$BASE/$VERSION"; do
  upload "$ARTIFACT_PATH" "$dest"
  upload "$MANIFEST" "$dest"
done

# Best-effort, exactly as `release:publish-bundle` treats it: the release tag may not exist yet,
# and the package upload above is the part that actually matters.
if [ "$DRY_RUN" -eq 0 ]; then
  echo "==> attaching asset link to release $VERSION (best-effort)"
  curl --fail-with-body --silent --show-error --request POST \
    --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    --data-urlencode "name=$ARTIFACT (macOS, signed + notarized)" \
    --data-urlencode "url=$BASE/$VERSION/$ARTIFACT" \
    --data "link_type=package" \
    "$HOST/api/v4/projects/$PROJECT/releases/$VERSION/assets/links" >/dev/null \
    || echo "    WARN: could not attach (release $VERSION may not exist yet); the package is uploaded regardless."
fi

echo
echo "==> done"
echo "    download: $BASE/$VERSION/$ARTIFACT"
echo "    updater:  $BASE/latest/$(basename "$MANIFEST")"
