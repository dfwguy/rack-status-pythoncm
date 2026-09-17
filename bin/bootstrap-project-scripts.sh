#!/usr/bin/env bash
# bootstrap-project-scripts.sh — give one project its own copy of the scripts.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
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
# One server runs several customers' projects. Sharing one script directory meant editing a
# script for one site edited it for every site, so this composes a project's own directory:
#
#     <tree>/multi-platform/     the shared default set        (copied first)
#     <tree>/<platform>/         platform-specific overrides   (copied over the top)
#     <tree>/project-<id>/       the result, plus this project's own scripts
#
# <tree>/custom/ is deliberately NOT copied. It holds scripts operators added, shared across
# projects, and it is already ahead of the shipped layers in the resolver -- copying it in would
# fork every custom script per project, so fixing one would mean fixing it N times. A project
# that wants its own version of a custom script copies that one file itself.
#
# Runs ON THE EXECUTION HOST. The app invokes it through `host_exec`, so it must not assume it
# is on the machine hosting the dashboard, and it uses only tools a BCM/MAAS head node has.
#
# Idempotent, and deliberately careful about the second run: a file the operator has edited in
# the project directory is NOT overwritten unless --force is given. That is the whole point of
# the per-project copy — silently restoring the default would undo the customisation the
# directory exists to hold. `--dry-run` lists what would change and touches nothing.

set -euo pipefail

# Byte collation, so the character-class checks below mean what they say: under some locales
# `[a-z]` also matches upper case, which let `Upper` through as a project id. It also makes the
# rsync listing and `grep` deterministic regardless of the head node's locale.
export LC_ALL=C

# `rsync` is the one non-coreutils binary this script needs, and its absence must not look like a
# bootstrap failure.
#
# Without this check the first copy dies as `line 105: rsync: command not found` with exit 127,
# which surfaces through the API as a failed bootstrap and sends the reader looking at the script
# tree, the permissions, the project id -- anything but a missing package. That is the same
# "an absent tool and a genuine failure must not be indistinguishable" lesson `local-vs-ci-tree.mdc`
# §4 records for `git` in a slim CI image; it cost an afternoon there and turned five tests red
# here.
#
# rsync rather than `cp` is deliberate: `--ignore-existing` is what protects an operator's edit to
# a project's own copy of a script, and `-c` compares by checksum so a redeploy that changes
# nothing reports nothing. Every cluster head node has it, and `bin/deploy-scripts.sh` already
# depends on it.
if ! command -v rsync >/dev/null 2>&1; then
  echo "bootstrap-project-scripts.sh: rsync is not installed on this host." >&2
  echo "  It is required to compose the script layers while protecting local edits." >&2
  echo "  Install it (apt-get install rsync / dnf install rsync) and re-run." >&2
  exit 3
fi

usage() {
  cat >&2 <<'EOF'
usage: bootstrap-project-scripts.sh --project <id> [options]

  --project <id>       Project id; the directory becomes <tree>/project-<id>.
  --platform <id>      Platform layer to copy over the defaults (e.g. gb300-nvl72).
  --tree <path>        Script tree root. Default: $NVIS_SCRIPTS_TREE, else
                       $RACK_REMOTE_BASE_PATH/scripts.
  --source <path>      Where the default set comes from when the tree has no
                       multi-platform/ yet. Default: $RACK_REMOTE_BASE_PATH/bin/scripts.
  --force              Overwrite files that differ in the project directory.
  --dry-run            Report what would change; write nothing.
  --json               Emit a JSON summary (for the API).
  -h, --help           This text.
EOF
}

PROJECT=""; PLATFORM=""; TREE=""; SOURCE=""; FORCE=0; DRY=0; JSON=0

while [ $# -gt 0 ]; do
  case "$1" in
    --project)  PROJECT="${2:-}"; shift 2 ;;
    --platform) PLATFORM="${2:-}"; shift 2 ;;
    --tree)     TREE="${2:-}"; shift 2 ;;
    --source)   SOURCE="${2:-}"; shift 2 ;;
    --force)    FORCE=1; shift ;;
    --dry-run)  DRY=1; shift ;;
    --json)     JSON=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

[ -n "$PROJECT" ] || { echo "--project is required" >&2; exit 2; }

# The id lands in a path, so it is validated here as well as by the catalog. Same character set
# as `catalog.models.valid_id`, which is what an operator will have been allowed to type.
case "$PROJECT" in
  *[!a-z0-9._-]*|""|.|..) echo "invalid project id: $PROJECT" >&2; exit 2 ;;
esac
case "$PLATFORM" in
  *[!a-z0-9._-]*) echo "invalid platform id: $PLATFORM" >&2; exit 2 ;;
esac

BASE="${RACK_REMOTE_BASE_PATH:-}"
TREE="${TREE:-${NVIS_SCRIPTS_TREE:-${BASE:+$BASE/scripts}}}"
[ -n "$TREE" ] || { echo "no script tree: pass --tree or set RACK_REMOTE_BASE_PATH" >&2; exit 2; }

DEFAULTS="$TREE/multi-platform"
# First bootstrap on a deployment that still has only the flat directory: seed the default layer
# from it, so "migrate to the tree" and "give this project a copy" are one step rather than a
# manual prerequisite nobody remembers.
SOURCE="${SOURCE:-${BASE:+$BASE/bin/scripts}}"
DEFAULT_SRC="$DEFAULTS"
if [ ! -d "$DEFAULTS" ]; then
  if [ -n "$SOURCE" ] && [ -d "$SOURCE" ]; then
    if [ "$DRY" -eq 1 ]; then
      # Report against the flat set we *would* seed from. Creating the layer here would make a
      # dry run write, and failing here would make the first question unanswerable on exactly
      # the deployments that need to ask it.
      DEFAULT_SRC="$SOURCE"
    else
      mkdir -p "$DEFAULTS"
      rsync -rlptc --exclude '__pycache__/' --exclude '*.pyc' "$SOURCE/" "$DEFAULTS/"
    fi
  else
    echo "no default script set: $DEFAULTS is absent and --source ($SOURCE) is not a directory" >&2
    exit 1
  fi
fi

DEST="$TREE/project-$PROJECT"
PLATFORM_DIR=""
[ -n "$PLATFORM" ] && [ -d "$TREE/$PLATFORM" ] && PLATFORM_DIR="$TREE/$PLATFORM"

# Compose the layers in a staging directory, then sync that into the project.
#
# Two rules have to hold at once and they fight each other with a naive copy: the platform layer
# must win over the shared default set, and an operator's edit in the project directory must not
# be clobbered. Copying defaults then platform directly into the project cannot do both --
# `--ignore-existing` protects the edit but also skips the platform file, because the default it
# should replace was written a moment earlier by this same run. That was the first version, and
# the test caught it: the gb300 override silently did not arrive.
#
# Staging separates the two questions. The layers compose with platform-wins semantics, and only
# then is the result offered to the project directory, where --ignore-existing means what it says.
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/nvis-scripts-XXXXXX")
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

# --checksum, not timestamps: a copy is made by rsync and then edited in place, so mtimes say
# nothing useful. -c is what `deploy-scripts.sh` uses, for the same reason.
BASE_OPTS=(-rlptc --exclude '__pycache__/' --exclude '*.pyc' --exclude '*.bak*')

rsync "${BASE_OPTS[@]}" "$DEFAULT_SRC/" "$STAGE/"
if [ -n "$PLATFORM_DIR" ]; then
  # No --ignore-existing here: this is where the platform layer is meant to overwrite a default.
  rsync "${BASE_OPTS[@]}" "$PLATFORM_DIR/" "$STAGE/"
fi

INSTALL_OPTS=("${BASE_OPTS[@]}" --out-format='%n')
[ "$DRY" -eq 1 ] && INSTALL_OPTS+=(--dry-run)
# Without --force, a file that already exists in the project is left exactly as it is: that is
# the operator's copy, and restoring the default over it would undo the customisation this
# directory exists to hold. New scripts still arrive.
[ "$FORCE" -eq 1 ] || INSTALL_OPTS+=(--ignore-existing)

[ "$DRY" -eq 1 ] || mkdir -p "$DEST"

# Count the file lines in an rsync --out-format listing.
#
# Not a `| grep | wc` pipeline: `set -o pipefail` is on, and grep exits 1 when nothing matches,
# so an empty (= nothing to copy) listing would abort the script. That is the success case.
count_files() {
  printf '%s' "$1" | grep -v '/$' | grep -c . || true
}

# rsync's own failure must still be fatal, so the call and the counting are separate steps.
installed=$(rsync "${INSTALL_OPTS[@]}" "$STAGE/" "$DEST/")
changed=$(count_files "$installed")

# What a --force would overwrite: files in the project that differ from the composed layers.
customised=0
if [ -d "$DEST" ]; then
  differing=$(rsync -rlptcn --out-format='%n' --exclude '__pycache__/' --exclude '*.pyc' \
    "$STAGE/" "$DEST/")
  customised=$(count_files "$differing")
fi

if [ "$JSON" -eq 1 ]; then
  printf '{"ok":true,"project":"%s","platform":"%s","tree":"%s","dest":"%s","defaults":"%s","copied":%s,"differs_from_defaults":%s,"dry_run":%s,"forced":%s}\n' \
    "$PROJECT" "$PLATFORM" "$TREE" "$DEST" "$DEFAULTS" \
    "${changed:-0}" "${customised:-0}" "$DRY" "$FORCE"
else
  echo "project scripts: $DEST"
  echo "  defaults from: $DEFAULTS  (installed: ${changed:-0})"
  [ -n "$PLATFORM_DIR" ] && echo "  platform layer: $PLATFORM_DIR"
  echo "  differs from defaults: ${customised:-0} file(s)"
  [ "$DRY" -eq 1 ] && echo "  (dry run — nothing written)"
fi
