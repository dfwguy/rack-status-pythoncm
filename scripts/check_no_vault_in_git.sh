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
# Fail if any token-vault secret files are tracked by git (CI / pre-push guard).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "check_no_vault_in_git: not a git repository — skip"
  exit 0
fi

bad=()
while IFS= read -r path; do
  [[ -z "$path" ]] && continue
  case "$path" in
    var/secrets/.gitignore) continue ;;
    var/secrets/*|*.vault.json|*.vault.flock|*/.vault_master_key|.vault_master_key|*/.rack_web_secret_key|.rack_web_secret_key)
      bad+=("$path")
      ;;
  esac
done < <(git ls-files)

if ((${#bad[@]} > 0)); then
  echo "ERROR: token vault files must not be committed to git:" >&2
  printf '  %s\n' "${bad[@]}" >&2
  echo "Remove them with: git rm --cached <file>" >&2
  exit 1
fi

echo "OK: no vault secret files tracked by git"
