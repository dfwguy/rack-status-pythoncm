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

PROJECT="${GITLAB_PROJECT:-${CI_PROJECT_ID:-nvis%2Fnvis-rack-status}}"
HOST="${GITLAB_HOST:-https://gitlab-master.nvidia.com}"
API="${CI_API_V4_URL:-$HOST/api/v4}"
PACKAGE="${PACKAGE_NAME:-nvis-greenlight-desktop}"
RELEASE_DIR="$DESKTOP_DIR/release"
SIGNED_DIR="$RELEASE_DIR/signed"
DRY_RUN=0

[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

# ── One script, two callers: a workstation and a CI job ────────────────────────────────────────
# `release:publish-desktop` runs this rather than reimplementing the upload in YAML, because the
# valuable part is not the `curl` — it is the version-coherence check, the per-file sha512
# cross-check and the idempotent skip. A second copy in the pipeline would be the copy that
# silently loses one of them.
#
# The tokens are not interchangeable. `CI_JOB_TOKEN` exists only inside a job and is scoped to it;
# a PAT is what a workstation has. They even use different headers, which is the sort of detail
# that produces a 401 reading as "wrong password".
if [ -n "${GITLAB_TOKEN:-}" ]; then
  AUTH_HEADER="PRIVATE-TOKEN: $GITLAB_TOKEN"
elif [ -n "${CI_JOB_TOKEN:-}" ]; then
  AUTH_HEADER="JOB-TOKEN: $CI_JOB_TOKEN"
elif [ "$DRY_RUN" -eq 1 ]; then
  AUTH_HEADER="PRIVATE-TOKEN: dry-run"
else
  echo "publish: set GITLAB_TOKEN to a PAT with 'api' scope (or run inside CI)" >&2
  exit 1
fi

# ── Portable SHA-512, because the two callers are different operating systems ──────────────────
# macOS has `shasum` and an unwrapped `base64`; a Debian CI image has `sha512sum` and a GNU
# `base64` that wraps at 76 columns. A wrapped digest compares unequal to the manifest's, so the
# publish would refuse every file with a mismatch that is purely formatting — hence `tr -d`.
sha512_of() {
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha512 -binary "$1" | base64 | tr -d '\n'
  elif command -v sha512sum >/dev/null 2>&1; then
    sha512sum "$1" | cut -d' ' -f1 | xxd -r -p | base64 | tr -d '\n'
  else
    shasum -a 512 "$1" | cut -d' ' -f1 | xxd -r -p | base64 | tr -d '\n'
  fi
}

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
#
# Stripping the stamp is only a *guess* at the tag, and it was wrong the first time it mattered:
# the product's releases are cut by the ci-toolkit job from conventional commits, which had
# already moved to 0.3.1 while version.py still said 0.3.0. So `RELEASE_TAG` is overridable, and
# a miss now prints the tags that do exist instead of the bare "may not exist yet" that let seven
# downloads go missing without anyone noticing.
#
# The desktop client is released under its own `desktop-<version>` tag rather than the product's
# semver line, for two reasons: the ci-toolkit job owns `X.Y.Z` and computes the next one itself,
# and the installers are built from a branch whose code is not in the product release of the same
# number — attaching them there would advertise binaries nobody can reproduce from that commit.
RELEASE_TAG="${RELEASE_TAG:-${VERSION%%-*}}"

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
#
# **Only files carrying this release's version.** `release/` is a working directory that nobody
# empties between builds, so it accumulates: a previous release's deb and rpm, and — the case that
# prompted this — an unstamped `0.3.0-arm64.dmg` left over from a build made before the version
# came from version.py. The manifests are rewritten per build and so are always current, which is
# exactly why the coherence guard above cannot see these: no manifest mentions them. Publishing
# them anyway would attach a months-old installer to a new release, under a name a human reading
# the release page has no reason to question.
for dir in "$SIGNED_DIR" "$RELEASE_DIR"; do
  [ -d "$dir" ] || continue
  for f in "$dir"/*.dmg "$dir"/*.deb "$dir"/*.rpm; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in
      *"$VERSION"*) ;;
      *) echo "    skipping $(basename "$f") — not part of $VERSION" >&2; continue ;;
    esac
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
      # Only meaningful where the tooling exists. In a Linux CI job `xcrun` is absent, and a
      # "command not found" spat out per artifact would read as a signing failure.
      command -v xcrun >/dev/null 2>&1 || continue
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

BASE="$API/projects/$PROJECT/packages/generic/$PACKAGE"
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
  curl -sI --header "$AUTH_HEADER" "$1" 2>/dev/null \
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
    --header "$AUTH_HEADER" \
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
  echo "==> attaching asset links to release $RELEASE_TAG"
  if ! curl --fail --silent --output /dev/null \
       --header "$AUTH_HEADER" \
       "$API/projects/$PROJECT/releases/$RELEASE_TAG"; then
    echo "    no release tagged '$RELEASE_TAG' — nothing attached. Existing releases:"
    curl --silent --header "$AUTH_HEADER" \
      "$API/projects/$PROJECT/releases?per_page=5" \
      | tr ',' '\n' | grep '"tag_name"' | cut -d'"' -f4 | sed 's/^/      /'
    echo "    Re-run with RELEASE_TAG=<one of those>, or create the release first."
    echo "    The packages are published either way; only the release-page links are missing."
  else
    # Labelled by platform AND architecture. The filename alone does not tell a Windows user
    # whether they want `-x64.exe`, `-arm64.exe` or the unsuffixed one that contains both.
    for f in "${ALL[@]}"; do
      name=$(basename "$f")
      case "$name" in
        *.yml) continue ;;
        *arm64-notarized.zip) label="macOS (Apple Silicon)" ;;
        *.zip|*.dmg)          label="macOS" ;;
        *-x64.exe)            label="Windows (x64)" ;;
        *-arm64.exe)          label="Windows (ARM64)" ;;
        *.exe)                label="Windows (x64 + ARM64 installer)" ;;
        *.AppImage)           label="Linux (AppImage, self-updating)" ;;
        *.deb)                label="Linux (Debian/Ubuntu)" ;;
        *.rpm)                label="Linux (RHEL/Fedora)" ;;
        *)                    label="download" ;;
      esac
      if curl --fail-with-body --silent --show-error --request POST \
           --header "$AUTH_HEADER" \
           --data-urlencode "name=$label — $name" \
           --data-urlencode "url=$BASE/$VERSION/$name" \
           --data "link_type=package" \
           "$API/projects/$PROJECT/releases/$RELEASE_TAG/assets/links" >/dev/null 2>&1; then
        echo "    linked $label"
      else
        echo "    WARN: could not link $name (a link of that name may already exist)"
      fi
    done
  fi
fi

echo
echo "==> done${SKIPPED:+ ($SKIPPED already-published file(s) skipped)}"
for f in "${ALL[@]}"; do
  case "$(basename "$f")" in
    *.yml) continue ;;
  esac
  echo "    $BASE/$VERSION/$(basename "$f")"
done
