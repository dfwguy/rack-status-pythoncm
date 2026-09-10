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
# sign-3s.sh -- sign the macOS app with NVIDIA's 3S service, then notarize and staple it.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# Every step here was established by doing it; the comments record the traps rather than the
# happy path, because the happy path is short and the traps all fail *silently*.
#
#   1. 3S rejects an upload whose FILENAME contains spaces. `productName` is "NVIS Greenlight",
#      so `artifactName` in electron-builder.yml pins space-free names. Contents may have spaces.
#   2. 3S signs `--deep` but SKIPS nested Mach-O under any `Resources/` directory. For this app
#      that is exactly one file, `Squirrel.framework/.../Resources/ShipIt`, and it is the binary
#      Squirrel.Mac uses to APPLY an update. Left ad-hoc it fails notarization. So those are
#      signed individually first, inside-out, and the app is re-sealed over them.
#   3. **3S strips entitlements unless an `.entitlements` file rides along INSIDE the zip**, as a
#      sibling of the .app and sharing its basename. This is the one that cost a day: without it
#      3S still applies hardened runtime, so the app signs, notarizes, staples and passes
#      Gatekeeper -- every gate green -- and then aborts at `v8::Isolate::Initialize`, because V8
#      cannot map JIT pages without `allow-jit` / `allow-unsigned-executable-memory`. An app that
#      passes every check and crashes is worse than one that fails a check.
#   4. Repackage with `ditto`, never `zip`: symlinks and xattrs in a .app do not survive `zip`.
#
# Usage:
#   scripts/sign-3s.sh                          # build dir must already exist
#   scripts/sign-3s.sh --no-notarize            # 3S signing only
#   scripts/sign-3s.sh --app path/to/My.app
#
# Credentials -- see desktop/README.md:
#   3S    interactive browser login, OR NVSEC_SSA_CLIENT_ID / NVSEC_SSA_CLIENT_SECRET /
#         NVSEC_SSA_SCOPE for unattended use.
#   Apple NOTARY_PROFILE (a keychain profile), OR APPLE_API_KEY + APPLE_API_KEY_ID +
#         APPLE_API_ISSUER (App Store Connect key -- the CI-friendly form).

set -euo pipefail

# Resolved BEFORE the cd below, or `--help` reads `$0` relative to the wrong directory.
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
cd "$(dirname "$0")/.."
DESKTOP_DIR=$(pwd)

APP=""
NOTARIZE=1
JOB_TYPE="${NVSEC_JOB_TYPE:-MACOS}"
NSPECT_ID="${NSPECT_ID:-NSPECT-S57Q-EI19}"
ENTITLEMENTS="${ENTITLEMENTS:-$DESKTOP_DIR/resources/entitlements.mac.plist}"
OUT_DIR="${OUT_DIR:-$DESKTOP_DIR/release/signed}"

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --no-notarize) NOTARIZE=0; shift ;;
    -h|--help) sed -n '13,45p' "$SELF"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "sign-3s.sh: macOS only (codesign/ditto/notarytool)" >&2; exit 1; }
command -v nvsec >/dev/null || { echo "sign-3s.sh: nvsec not on PATH (see README)" >&2; exit 1; }

# Default to whichever arch directory electron-builder produced.
if [ -z "$APP" ]; then
  APP=$(/bin/ls -1d "$DESKTOP_DIR"/release/mac*/*.app 2>/dev/null | head -1 || true)
  [ -n "$APP" ] || { echo "sign-3s.sh: no .app under release/mac*/; run 'npx electron-builder --mac --dir' first" >&2; exit 1; }
fi
# Reported separately from the discovery case: "run the build first" is unhelpful advice when the
# caller passed an explicit --app that simply does not exist.
[ -d "$APP" ] || { echo "sign-3s.sh: --app path is not a directory: $APP" >&2; exit 1; }
[ -f "$ENTITLEMENTS" ] || { echo "sign-3s.sh: entitlements not found: $ENTITLEMENTS" >&2; exit 1; }

APP_NAME=$(basename "$APP" .app)

# The published artifact's name, which must NOT be derived from the .app basename: that is
# "NVIS Greenlight", and a space in the filename is refused outright by 3S the moment anyone
# re-submits it. It also has to carry the version and arch, because `latest-mac.yml` addresses the
# file by name and two arches otherwise collide in the same registry directory. Matches the
# `artifactName` convention in electron-builder.yml so the signed and unsigned names agree.
# Read from the BUILT app's Info.plist, not package.json. `npm run pack:mac` injects a stamped
# version via `--config.extraMetadata.version`, deliberately leaving the source file alone — so
# package.json says "0.3.0" while the bundle says "0.3.0-260910184000". Trusting the source would
# name the artifact after a version the app does not report, which is precisely the confusion the
# stamp exists to remove. The plist is what was actually built.
VERSION=$(plutil -extract CFBundleShortVersionString raw "$APP/Contents/Info.plist" 2>/dev/null \
  || node -p "require('$DESKTOP_DIR/package.json').version")
# Multi-line: macOS ships bash 3.2, which will not parse a single-line `case` with `;;` inside a
# command substitution (local-vs-ci-tree.mdc §4 — prefer constructs both shells have).
case "$APP" in
  *mac-arm64*) ARCH=arm64 ;;
  *mac-x64*|*mac/*) ARCH=x64 ;;
  *) ARCH=$(uname -m | sed 's/x86_64/x64/') ;;
esac
ARTIFACT_BASE="nvis-greenlight-${VERSION}-${ARCH}"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sign3s.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT_DIR"

echo "==> app:          $APP"
echo "==> entitlements: $ENTITLEMENTS"
echo "==> work:         $WORK"

# `--client_id`/`--client_secret`/`--scope` are read from these by nvsec itself, so an unattended
# runner needs no extra flags -- and a workstation with none of them set falls through to the
# browser login, which is what makes the same script usable in both places.
submit() { # submit <input.zip> <result.zip> <description>
  nvsec 3s submit \
    --job_type "$JOB_TYPE" \
    --input_file "$1" \
    --result_filename "$(basename "$2")" \
    --result_dir "$(dirname "$2")" \
    --description "$3" \
    --nspect_id "$NSPECT_ID" \
    --download --timeout 1800
}

# ── 1. Nested Mach-O under any Resources/ ──────────────────────────────────────────────────────
# Trap 2. Enumerated rather than hard-coded to ShipIt: a future Electron or Squirrel bump can add
# another one, and the symptom would be a notarization rejection nobody connects to this script.
echo "==> scanning for nested Mach-O under Resources/ (3S --deep skips these)"
NESTED="$WORK/nested.txt"
find "$APP" -type f -perm -u+x -print0 2>/dev/null \
  | xargs -0 -I{} sh -c 'case "$1" in */Resources/*) file -b "$1" | grep -q Mach-O && echo "$1";; esac' _ {} \
  > "$NESTED" || true

NESTED_COUNT=$(wc -l < "$NESTED" | tr -d ' ')
echo "    found $NESTED_COUNT"

i=0
while IFS= read -r bin; do
  [ -n "$bin" ] || continue
  i=$((i + 1))
  base=$(basename "$bin")
  echo "==> [$i/$NESTED_COUNT] signing nested: $base"
  stage="$WORK/nested-$i"
  mkdir -p "$stage/in"
  cp "$bin" "$stage/in/$base"
  # Sibling entitlements, same basename -- trap 3, applied to nested binaries too.
  cp "$ENTITLEMENTS" "$stage/in/$base.entitlements"
  ditto -c -k --sequesterRsrc "$stage/in" "$stage/in.zip"
  submit "$stage/in.zip" "$stage/out.zip" "$APP_NAME nested $base"
  mkdir -p "$stage/out"
  ditto -x -k "$stage/out.zip" "$stage/out"
  cp "$stage/out/$base" "$bin"
done < "$NESTED"

# ── 2. The app itself, with the entitlements sibling ───────────────────────────────────────────
echo "==> signing the app bundle"
STAGE="$WORK/app"
mkdir -p "$STAGE/in"
cp -R "$APP" "$STAGE/in/"
cp "$ENTITLEMENTS" "$STAGE/in/$APP_NAME.entitlements"
# No --keepParent: the .app and the .entitlements must sit at the ZIP ROOT as siblings.
ditto -c -k --sequesterRsrc "$STAGE/in" "$WORK/app-unsigned.zip"
submit "$WORK/app-unsigned.zip" "$WORK/app-signed.zip" "$APP_NAME $(date -u +%Y%m%dT%H%MZ)"
mkdir -p "$STAGE/out"
ditto -x -k "$WORK/app-signed.zip" "$STAGE/out"
SIGNED_APP="$STAGE/out/$APP_NAME.app"
[ -d "$SIGNED_APP" ] || { echo "sign-3s.sh: 3S returned no .app" >&2; exit 1; }

# ── 3. Verify before notarizing ────────────────────────────────────────────────────────────────
# Apple's rejection messages are terse and arrive minutes later; these checks name the exact
# binary immediately. The entitlements assertion is the one that matters -- see trap 3.
echo "==> verifying"
codesign --verify --deep --strict=all "$SIGNED_APP"

fail=0
find "$SIGNED_APP" -type f -perm -u+x -print0 2>/dev/null \
  | xargs -0 -I{} sh -c 'file -b "$1" | grep -q Mach-O && echo "$1"' _ {} \
  > "$WORK/all-macho.txt" || true

while IFS= read -r bin; do
  [ -n "$bin" ] || continue
  info=$(codesign -dv --verbose=2 "$bin" 2>&1 || true)
  echo "$info" | grep -q '6KR3T733EC' || { echo "    NOT NVIDIA-SIGNED: ${bin#"$SIGNED_APP"/}"; fail=1; }
  echo "$info" | grep -q 'runtime' || { echo "    NO HARDENED RUNTIME: ${bin#"$SIGNED_APP"/}"; fail=1; }
done < "$WORK/all-macho.txt"
echo "    $(wc -l < "$WORK/all-macho.txt" | tr -d ' ') Mach-O checked"

# The app AND every helper need JIT entitlements: each Electron helper hosts its own V8.
for target in "$SIGNED_APP" "$SIGNED_APP"/Contents/Frameworks/*.app; do
  [ -d "$target" ] || continue
  if ! codesign -d --entitlements - "$target" 2>&1 | grep -q 'allow-jit'; then
    echo "    NO allow-jit ENTITLEMENT: $(basename "$target")"
    fail=1
  fi
done
[ "$fail" -eq 0 ] || { echo "sign-3s.sh: verification failed; not notarizing" >&2; exit 1; }
echo "    ok: signed, hardened, entitled"

# ── 4. Notarize + staple ───────────────────────────────────────────────────────────────────────
FINAL_ZIP="$OUT_DIR/$ARTIFACT_BASE-notarized.zip"
if [ "$NOTARIZE" -eq 1 ]; then
  if [ -n "${APPLE_API_KEY:-}" ]; then
    NOTARY_ARGS="--key $APPLE_API_KEY --key-id $APPLE_API_KEY_ID --issuer $APPLE_API_ISSUER"
  elif [ -n "${NOTARY_PROFILE:-}" ]; then
    NOTARY_ARGS="--keychain-profile $NOTARY_PROFILE"
  else
    echo "sign-3s.sh: set NOTARY_PROFILE or APPLE_API_KEY/_KEY_ID/_ISSUER, or pass --no-notarize" >&2
    exit 1
  fi

  echo "==> notarizing"
  ditto -c -k --sequesterRsrc --keepParent "$SIGNED_APP" "$WORK/notarize.zip"
  # Retried once: a transient `NSURLErrorDomain Code=-1001 "The request timed out"` reaching
  # appstoreconnect.apple.com is common and succeeds immediately on a second attempt.
  # shellcheck disable=SC2086
  xcrun notarytool submit "$WORK/notarize.zip" $NOTARY_ARGS --wait \
    || { echo "    retrying after transient notary error"; xcrun notarytool submit "$WORK/notarize.zip" $NOTARY_ARGS --wait; }

  echo "==> stapling"
  xcrun stapler staple "$SIGNED_APP"
  xcrun stapler validate "$SIGNED_APP"
  spctl -a -vv -t exec "$SIGNED_APP"
else
  FINAL_ZIP="$OUT_DIR/$ARTIFACT_BASE-signed.zip"
fi

# ── 5. Repackage, and rewrite the updater manifest ─────────────────────────────────────────────
# `ditto`, not `zip`: trap 4.
rm -f "$FINAL_ZIP"
ditto -c -k --sequesterRsrc --keepParent "$SIGNED_APP" "$FINAL_ZIP"

# electron-builder wrote latest-mac.yml against the UNSIGNED zip, and signing changes the bytes --
# so publishing without this makes electron-updater reject every update on a hash mismatch. It is
# the least visible step here and the one that breaks auto-update in production.
#
# Written unconditionally, NOT only when electron-builder happened to leave one behind. The
# recommended build here is `--mac --dir`, which produces no manifest at all — so an
# `if [ -f ... ]` guard meant this step silently did nothing on the normal path, and
# `publish-release.sh` would then refuse for want of a manifest. Generating it is also strictly
# more correct: electron-builder's version describes the *unsigned* zip, and signing changes the
# bytes, so the only manifest worth having is one computed here.
MANIFEST="$DESKTOP_DIR/release/latest-mac.yml"
echo "==> writing $(basename "$MANIFEST") against the signed artifact"
mkdir -p "$(dirname "$MANIFEST")"
sha=$(shasum -a 512 "$FINAL_ZIP" | cut -d' ' -f1 | xxd -r -p | base64)
size=$(stat -f %z "$FINAL_ZIP")
{
  echo "version: $VERSION"
  echo "files:"
  echo "  - url: $(basename "$FINAL_ZIP")"
  echo "    sha512: $sha"
  echo "    size: $size"
  echo "path: $(basename "$FINAL_ZIP")"
  echo "sha512: $sha"
  echo "releaseDate: '$(date -u +%Y-%m-%dT%H:%M:%S.000Z)'"
} > "$MANIFEST"

echo
echo "==> done: $FINAL_ZIP"
ls -1sh "$FINAL_ZIP"
