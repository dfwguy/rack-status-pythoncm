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
# Mirror .cursor/rules/*.mdc as .cursor/claude-rules/*.md symlinks so AGENTS.md can @-import them.
# Claude Code silently skips @-imports of .mdc files; a .md symlink to the same file loads.
#
#   scripts/sync_claude_rules.sh          create missing symlinks, remove stale ones
#   scripts/sync_claude_rules.sh --check  change nothing; exit 1 on any drift (CI)
#
# --check also verifies AGENTS.md: every rule is listed, every alwaysApply rule is @-imported
# through its .cursor/claude-rules/*.md symlink, no @-import names a .mdc, and every @-import resolves.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

check=0
case "${1:-}" in
  "") ;;
  --check) check=1 ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

src_dir=".cursor/rules"
link_dir=".cursor/claude-rules"
agents="AGENTS.md"
problems=()

[[ $check -eq 0 ]] && mkdir -p "$link_dir"

for mdc in "$src_dir"/*.mdc; do
  [[ -e "$mdc" ]] || continue
  stem="$(basename "$mdc" .mdc)"
  link="$link_dir/$stem.md"
  want="../rules/$stem.mdc"
  if [[ -L "$link" && "$(readlink "$link")" == "$want" ]]; then
    continue
  fi
  if [[ $check -eq 1 ]]; then
    problems+=("missing or wrong symlink: $link -> $want")
  else
    if [[ -e "$link" && ! -L "$link" ]]; then
      echo "ERROR: $link is a regular file, not a symlink; refusing to overwrite it" >&2
      exit 1
    fi
    ln -sfn "$want" "$link"
    echo "linked  $link -> $want"
  fi
done

# Only symlinks pointing into .cursor/rules are ours to prune; anything else here is left alone.
for link in "$link_dir"/*.md; do
  [[ -L "$link" ]] || continue
  target="$(readlink "$link")"
  [[ "$target" == "../rules/"* ]] || continue
  [[ -e "$link" ]] && continue
  if [[ $check -eq 1 ]]; then
    problems+=("stale symlink: $link -> $target")
  else
    rm "$link"
    echo "removed $link (target gone)"
  fi
done

if [[ $check -eq 1 ]]; then
  for mdc in "$src_dir"/*.mdc; do
    [[ -e "$mdc" ]] || continue
    stem="$(basename "$mdc" .mdc)"
    if ! grep -qE "(\.cursor/rules/$stem\.mdc|\.cursor/claude-rules/$stem\.md)" "$agents"; then
      problems+=("$agents does not mention rule '$stem'")
    fi
    if awk 'NR==1 && /^---$/ {f=1; next} f && /^---$/ {exit} f' "$mdc" | grep -qE '^alwaysApply:[[:space:]]*true'; then
      if ! grep -qF "@$link_dir/$stem.md" "$agents"; then
        problems+=("alwaysApply rule '$stem' is not @-imported as @$link_dir/$stem.md in $agents")
      fi
    fi
  done

  while IFS= read -r ref; do
    path="${ref#@}"
    case "$path" in
      *.mdc) problems+=("$agents imports $ref; Claude Code ignores .mdc imports") ;;
      *) [[ -e "$path" ]] || problems+=("$agents imports $ref, which does not exist") ;;
    esac
  done < <(grep -oE '@\.?[A-Za-z0-9_./-]+\.mdc?' "$agents" | sort -u)

  if ((${#problems[@]} > 0)); then
    echo "ERROR: Claude Code rule imports are out of sync:" >&2
    printf '  %s\n' "${problems[@]}" >&2
    echo "Fix with: bash scripts/sync_claude_rules.sh, then update $agents" >&2
    exit 1
  fi
  echo "OK: .cursor/claude-rules mirrors .cursor/rules and $agents imports resolve"
fi
