#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
declare -A active=()

expand_list() {
  local list=$1 raw path
  [[ $list != /* && $list != *..* ]] || { echo "unsafe release list: $list" >&2; return 65; }
  [[ -r $root/$list ]] || { echo "missing release list: $list" >&2; return 66; }
  [[ -z ${active[$list]:-} ]] || { echo "recursive release list: $list" >&2; return 65; }
  active[$list]=1
  while IFS= read -r raw || [[ -n $raw ]]; do
    path=${raw#"${raw%%[![:space:]]*}"}
    path=${path%"${path##*[![:space:]]}"}
    [[ -z $path || $path == \#* ]] && continue
    if [[ $path == @* ]]; then
      expand_list "${path#@}"
    else
      [[ $path != /* && $path != *..* ]] || { echo "unsafe release path: $path" >&2; return 65; }
      printf '%s\n' "$path"
    fi
  done < "$root/$list"
  unset 'active[$list]'
}

expand_list "${1:-release/files.list}"
