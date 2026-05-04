#!/usr/bin/env bash
# nvis-rack-status: stage checker.sh on nodes (pdcp) and run it via pdsh as root.
# Remote runs with RACK_WEB_HW_CHECK=1 so checker.sh never calls scontrol drain/undrain.
set -uo pipefail

PDSH_W="${1:-}"
shift || true
if [[ "${1:-}" == "--" ]]; then shift; fi
if [[ -z "${PDSH_W}" ]]; then
  echo "usage: hw_check_pdsh.sh <pdsh-w-pattern> [--] [checker.sh args...]" >&2
  exit 2
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL="${DIR}/checker.sh"
if [[ ! -f "${LOCAL}" ]]; then
  echo "hw_check_pdsh.sh: checker.sh not found in ${DIR}" >&2
  exit 3
fi
if ! command -v pdcp >/dev/null 2>&1; then
  echo "hw_check_pdsh.sh: pdcp not in PATH (install pdsh with pdcp)" >&2
  exit 4
fi
if ! command -v pdsh >/dev/null 2>&1; then
  echo "hw_check_pdsh.sh: pdsh not in PATH" >&2
  exit 4
fi

TMP="/tmp/rack_web_checker.$$.$RANDOM.sh"
cleanup() {
  pdsh -w "${PDSH_W}" -f 64 "rm -f ${TMP}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

pdcp -w "${PDSH_W}" -f 64 "${LOCAL}" "${TMP}" || exit 5

args_q=()
for a in "$@"; do args_q+=("$(printf '%q' "$a")"); done
qargs="${args_q[*]}"

pdsh -w "${PDSH_W}" -f 64 "RACK_WEB_HW_CHECK=1 bash ${TMP} ${qargs}"
exit $?
