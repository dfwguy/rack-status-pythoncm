#!/usr/bin/env bash
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
