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
# nvis-rack-status: stage checker.sh on nodes (pdcp) and run it via pdsh as root.
# Remote runs with RACK_WEB_HW_CHECK=1 so checker.sh never calls scontrol drain/undrain.
set -uo pipefail

# Force ssh transport for pdsh/pdcp: this cluster's pdsh defaults to rsh, which is
# refused on compute nodes ("connect: Connection refused"). Matches sysinfo.sh.
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh

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
  pdsh -R ssh -w "${PDSH_W}" -f 64 "rm -f ${TMP}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

pdcp -R ssh -w "${PDSH_W}" -f 64 "${LOCAL}" "${TMP}" || exit 5

args_q=()
for a in "$@"; do args_q+=("$(printf '%q' "$a")"); done
qargs="${args_q[*]}"

# checker.sh hard-requires root. This script's header always claimed it ran "as root", but the
# pdsh line never elevated — it only worked if the pdsh identity was already root, i.e. when the
# privileged profile (RACK_SCRIPT_SSH_USER=root) was configured. On NscaleTX it is not, and
# cmods has NO sudo on the head node — so every node returned "ERROR: Please run as root".
# cmods DOES have NOPASSWD sudo on the compute trays (verified), so elevate there instead.
#
# `sudo -n env VAR=…` is deliberate: sudo strips the environment, and RACK_WEB_HW_CHECK=1 is what
# stops checker.sh calling `scontrol drain`. Passing it as `sudo -n VAR=… bash` would silently drop
# it and a hardware check could DRAIN THE RACK. Do not "simplify" this.
_remote="RACK_WEB_HW_CHECK=1 bash ${TMP} ${qargs}"
pdsh -R ssh -w "${PDSH_W}" -f 64 \
  "if [ \"\$(id -u)\" -ne 0 ] && sudo -n true 2>/dev/null; then sudo -n env ${_remote}; else ${_remote}; fi"
exit $?
