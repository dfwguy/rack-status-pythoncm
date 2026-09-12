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
# publish-release.sh -- upload the built desktop artifacts to the Package Registry.
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
#                    directory for news of 0.4.0 waits forever. Overwritten each release.
#   <pkg>/<version>/ immutable history, and what the GitLab Release links to.
#
# ── What "one release" means here ─────────────────────────────────────────────────────────────
#
# Three platforms, built on different machines at different times: macOS is signed and notarized
# on a workstation, Linux and Windows come out of containers. Each writes its own updater manifest
# (`latest-mac.yml`, `latest-linux.yml`, `latest.yml`) and they all land in the SAME `latest/`
# directory, because that is the one URL every build was compiled to poll.
#
# So this script publishes whatever manifests are present and **refuses a set that disagrees about
# the version**. That guard is the whole reason the script grew past its macOS-only original: with
# per-platform clock-read stamps, "release 0.3.0-x" would mean a different build on each platform,
# `latest/` would hold three mutually inconsistent answers, and nothing would ever look wrong —
# each platform reads only its own manifest, so every client would be individually satisfied while
# the release as a whole was incoherent. Pass `BUILD_STAMP` to every build of one release.
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
RELEASE_DIR="$DESKTOP_DIR/release"
SIGNED_DIR="$RELEASE_DIR/signed"
DRY_RUN=0

[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ "$DRY_RUN" -eq 0 ] && [ -z "${GITLAB_TOKEN:-}" ]; then
  echo "publish: set GITLAB_TOKEN to a PAT with 'api' scope" >&2
  exit 1
fi

# ── Locate a file by name ──────────────────────────────────────────────────────────────────────
# `signed/` first: for macOS the signed, notarized artifact is the one the manifest describes, and
# the unsigned original of the same name may still be sitting in release/. Publishing the unsigned
# one would produce a download Gatekeeper refuses, which reads to the recipient as "corrupt".
locate() {
  local name="$1"
  for dir in "$SIGNED_DIR" "$RELEASE_DIR"; do
    [ -f "$dir/$name" ] && { echo "$dir/$name"; return 0; }
  done
  return 1
}

sha512_of() { shasum -a 512 "$1" | cut -d' ' -f1 | xxd -r -p | base64; }

# ── Collect the manifests ──────────────────────────────────────────────────────────────────────
# `latest.yml` is Windows; the name has no platform in it because it is electron-updater's default
# channel file and Windows came first historically.
MANIFESTS=()
for m in latest-mac.yml latest-linux.yml latest.yml; do
  [ -f "$RELEASE_DIR/$m" ] && MANIFESTS+=("$m")
done

if [ ${#MANIFESTS[@]} -eq 0 ]; then
  echo "publish: no latest*.yml in $RELEASE_DIR — build first (npm run dist:mac / dist:linux / dist:win)" >&2
  exit 1
fi

# ── The coherence guard ────────────────────────────────────────────────────────────────────────
VERSION=""
for m in "${MANIFESTS[@]}"; do
  v=$(grep '^version:' "$RELEASE_DIR/$m" | head -1 | sed 's/^version:[[:space:]]*//')
  if [ -z "$VERSION" ]; then
    VERSION="$v"
  elif [ "$v" != "$VERSION" ]; then
    cat >&2 <<EOF
publish: REFUSING — the manifests describe different releases.

$(for x in "${MANIFESTS[@]}"; do
    printf '  %-18s %s\n' "$x" "$(grep '^version:' "$RELEASE_DIR/$x" | head -1 | sed 's/^version:[[:space:]]*//')"
  done)

All of these are uploaded to the same <pkg>/latest/ feed, so they must agree. Rebuild the
odd one out pinned to the release's stamp:

  BUILD_STAMP=${VERSION#*-} npm run dist:linux     # or dist:win / dist:mac

Or publish one platform at a time by moving the other manifests aside.
EOF
    exit 1
  fi
done

# The GitLab Release to attach downloads to, which is NOT the package version.
#
# Builds carry a timestamp stamp (0.3.0-260910185147), so each gets its own immutable package
# directory -- good for history. But releases are tagged with the plain version, so linking an
# asset to "0.3.0-260910185147" targets a release that does not exist: the POST fails, the
# best-effort warning scrolls past, and the download never appears on the release page. Since the
# release page is the surface users actually visit, that silent miss is the whole feature lost.
RELEASE_TAG="${VERSION%%-*}"

# ── Resolve every file to upload ───────────────────────────────────────────────────────────────
# Two kinds, which differ both in how they are verified and in where they go:
#
#   * FEED — a manifest and the payloads it names. sha512-checked, because electron-builder writes
#     the manifest against the UNSIGNED artifact and signing changes the bytes. Publishing that
#     pair means electron-updater downloads the update, fails the integrity check and refuses it:
#     auto-update broken for everyone, discovered only by users. Checking here makes shipping a
#     stale pair impossible. Goes to BOTH destinations — a manifest names its payload relative to
#     its own URL, so the two must sit side by side in `latest/` or the updater 404s.
#   * ARCHIVE — .dmg, .deb, .rpm. No manifest mentions these (deb and rpm are owned by apt/dnf and
#     cannot self-update), but they are what a human actually downloads, so leaving them behind
#     would publish a release most users cannot install from. Version directory only: putting them
#     in `latest/` would add ~250 MB of upload that nothing ever reads.
FEED=()
ARCHIVE=()
PROBLEMS=()

note_problem() { PROBLEMS+=("$1"); }

already_listed() { # already_listed <path> -- by basename, across both lists
  local want
  want=$(basename "$1")
  local f
  for f in ${FEED[@]+"${FEED[@]}"} ${ARCHIVE[@]+"${ARCHIVE[@]}"}; do
    [ "$(basename "$f")" = "$want" ] && return 0
  done
  return 1
}

for m in "${MANIFESTS[@]}"; do
  # The `files:` block lists each payload as `- url: <name>` followed by `sha512:`. Read the pairs
  # in order rather than assuming one file per manifest: a universal macOS build lists two.
  while IFS=$'\t' read -r name sha; do
    [ -z "$name" ] && continue
    if ! path=$(locate "$name"); then
      note_problem "$m names '$name' but it is in neither release/ nor release/signed/"
      continue
    fi
    actual=$(sha512_of "$path")
    if [ "$actual" != "$sha" ]; then
      note_problem "$m: sha512 mismatch for $name
       claimed  $sha
       actual   $actual
     The manifest is stale, usually because it was written before signing. Re-run
     scripts/sign-3s.sh, which rewrites it against the signed artifact."
      continue
    fi
    already_listed "$path" || FEED+=("$path")
  done < <(awk '
    /^files:/        { infiles = 1; next }
    /^[^ ]/          { infiles = 0 }
    infiles && /url:/    { name = $NF }
    infiles && /sha512:/ { print name "\t" $NF }
  ' "$RELEASE_DIR/$m")
  FEED+=("$RELEASE_DIR/$m")
done

# Installers no manifest mentions. signed/ first, so a signed copy shadows the unsigned original
# of the same name rather than both being published.
for dir in "$SIGNED_DIR" "$RELEASE_DIR"; do
  [ -d "$dir" ] || continue
  for f in "$dir"/*.dmg "$dir"/*.deb "$dir"/*.rpm; do
    [ -f "$f" ] || continue
    already_listed "$f" || ARCHIVE+=("$f")
  done
done

if [ ${#PROBLEMS[@]} -gt 0 ]; then
  echo "publish: REFUSING — ${#PROBLEMS[@]} problem(s):" >&2
  for p in "${PROBLEMS[@]}"; do echo "  - $p" >&2; done
  exit 1
fi

# ── Signing sanity, per platform ───────────────────────────────────────────────────────────────
# Warn rather than refuse: an unsigned Windows installer is still installable (SmartScreen shows a
# "Windows protected your PC" panel the user can click past), and an internal pre-release is a
# legitimate reason to ship one. An unsigned *macOS* build is different — Gatekeeper refuses it
# outright since Sequoia, the right-click-to-open bypass is gone — so that one is worth shouting
# about even though it is still only a warning.
ALL=(${FEED[@]+"${FEED[@]}"} ${ARCHIVE[@]+"${ARCHIVE[@]}"})

for f in "${ALL[@]}"; do
  case "$f" in
    *.zip|*.dmg)
      xcrun stapler validate "$f" >/dev/null 2>&1 && continue
      case "$f" in
        *.zip) : ;;  # a zipped .app cannot carry a staple; the .app inside it does
        *) echo "publish: WARNING — $(basename "$f") has no stapled notarization ticket" >&2 ;;
      esac
      ;;
    *.exe)
      case "$(basename "$f")" in
        *-signed*|*signature*) : ;;
        *) echo "publish: NOTE — $(basename "$f") is not Authenticode-signed; Windows will show a SmartScreen warning" >&2 ;;
      esac
      ;;
  esac
done

BASE="$HOST/api/v4/projects/$PROJECT/packages/generic/$PACKAGE"
echo "==> version:   $VERSION"
echo "==> manifests: ${MANIFESTS[*]}"
echo "==> feed:      $BASE/latest/        (${#FEED[@]} file(s): manifests + update payloads)"
echo "==> archive:   $BASE/$VERSION/   (all ${#ALL[@]})"
for f in "${ALL[@]}"; do
  printf '      %-58s %s\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
done

# Publishing is resumable, and that is not a nicety — it is what makes "add a platform to an
# existing release" a sane operation. macOS is signed and published days before Linux and Windows
# come out of their containers, so the second run would otherwise re-send a 105 MB zip that is
# already sitting there, on a link that moves ~1 MB/s. A full set is over a gigabyte; re-uploading
# the unchanged part of it turns a two-minute step into twenty and gives anyone watching a reason
# to interrupt it halfway.
#
# Size is the comparison, not a hash: the registry exposes Content-Length on a HEAD (verified), so
# this costs one request per file, whereas comparing digests would mean downloading what we are
# trying to avoid sending. Sizes are already sha512-checked locally against the manifest, so a
# same-name/same-size remote file being different bytes would require someone to have uploaded a
# distinct artifact under our exact name and length.
remote_size() { # remote_size <url>
  curl -sI --header "PRIVATE-TOKEN: ${GITLAB_TOKEN:-}" "$1" 2>/dev/null \
    | awk 'BEGIN{IGNORECASE=1} /^content-length:/ {print $2+0; exit}'
}

local_size() { wc -c < "$1" | tr -d ' '; }

SKIPPED=0

upload() { # upload <file> <destdir>
  local file="$1" dest="$2" name
  name=$(basename "$file")
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "    [dry-run] PUT $dest/$name"
    return 0
  fi
  # Manifests are always re-sent: they are tiny, and a stale one in the feed is the whole failure
  # mode this script exists to prevent.
  case "$name" in
    *.yml) : ;;
    *)
      if [ "$(remote_size "$dest/$name")" = "$(local_size "$file")" ]; then
        echo "    already published, skipping $name"
        SKIPPED=$((SKIPPED + 1))
        return 0
      fi
      ;;
  esac
  echo "    uploading $name -> $dest/"
  curl --fail-with-body --silent --show-error \
    --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
    --upload-file "$file" "$dest/$name"
  echo
}

for f in "${FEED[@]}"; do
  upload "$f" "$BASE/latest"
done
for f in "${ALL[@]}"; do
  upload "$f" "$BASE/$VERSION"
done

# Best-effort, exactly as `release:publish-bundle` treats it: the release tag may not exist yet,
# and the package upload above is the part that actually matters.
#
# Only installers get a link. A manifest on the release page is noise to a human — it is a machine
# file the app fetches for itself — and four extra rows would bury the three downloads that matter.
if [ "$DRY_RUN" -eq 0 ]; then
  echo "==> attaching asset links to release $RELEASE_TAG (best-effort)"
  for f in "${ALL[@]}"; do
    name=$(basename "$f")
    case "$name" in
      *.yml) continue ;;
      *.zip|*.dmg) label="macOS" ;;
      *.exe)       label="Windows" ;;
      *.AppImage)  label="Linux (AppImage, self-updating)" ;;
      *.deb)       label="Linux (Debian/Ubuntu)" ;;
      *.rpm)       label="Linux (RHEL/Fedora)" ;;
      *)           label="download" ;;
    esac
    curl --fail-with-body --silent --show-error --request POST \
      --header "PRIVATE-TOKEN: $GITLAB_TOKEN" \
      --data-urlencode "name=$name ($label)" \
      --data-urlencode "url=$BASE/$VERSION/$name" \
      --data "link_type=package" \
      "$HOST/api/v4/projects/$PROJECT/releases/$RELEASE_TAG/assets/links" >/dev/null 2>&1 \
      || echo "    WARN: could not attach $name (release $RELEASE_TAG may not exist yet)"
  done
fi

echo
echo "==> done${SKIPPED:+ ($SKIPPED already-published file(s) skipped)}"
for f in "${ALL[@]}"; do
  case "$(basename "$f")" in
    *.yml) continue ;;
  esac
  echo "    $BASE/$VERSION/$(basename "$f")"
done
