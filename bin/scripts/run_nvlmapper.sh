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
# Run nvlmapper for a rack: builds config-<rack>.yaml from Bright cmsh (NVSwitch
# devices + IPs), merges credentials from NVLMAPPER_RC or env, then runs
# ./nvlmapper -c config-<rack>.yaml
#
# Switch list (same idea as manual check):
#   cmsh -c "device; ls | grep <rack-id> | grep nvsw"
# For each matching device, IPs are resolved via cmsh (see embedded Python).
#
# Credentials (first match wins):
#   1) NVLMAPPER_USERNAME + NVLMAPPER_PASSWORD — written to a short-lived temp file for the merge step.
#   2) NVLMAPPER_RC (default ~/.nvlmapperrc) — key=value lines, # comments, blank lines ok:
#        username=admin   (or user=admin)
#        password=secret
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC_DEFAULT="/root/.nvlmapperrc"
RC_FROM_ENV="${NVLMAPPER_RC:-$RC_DEFAULT}"
RC_MERGED=""
cleanup_rc() {
  if [[ -n "${RC_MERGED}" && -f "${RC_MERGED}" ]]; then
    rm -f "${RC_MERGED}"
  fi
}
trap cleanup_rc EXIT
NVLMAPPER="${SCRIPT_DIR}/nvlmapper"

usage() {
  echo "Usage: $0 <rack-id> [extra args passed to nvlmapper...]" >&2
  echo "Example: $0 p21-r02" >&2
  exit 1
}

[[ $# -ge 1 ]] || usage
RACK="$1"
shift

if [[ ! "$RACK" =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]]; then
  echo "Invalid rack id: $RACK" >&2
  exit 1
fi

OUT="${SCRIPT_DIR}/config-${RACK}.yaml"

if [[ -n "${NVLMAPPER_USERNAME:-}" && -n "${NVLMAPPER_PASSWORD:-}" ]]; then
  RC_MERGED="$(mktemp "${TMPDIR:-/tmp}/nvlmapperrc.XXXXXX")"
  chmod 600 "$RC_MERGED" 2>/dev/null || true
  {
    printf 'username=%s\n' "$NVLMAPPER_USERNAME"
    printf 'password=%s\n' "$NVLMAPPER_PASSWORD"
  } >"$RC_MERGED"
  RC="$RC_MERGED"
elif [[ -f "$RC_FROM_ENV" ]]; then
  RC="$RC_FROM_ENV"
else
  echo "Missing NVLMapper credentials: set NVLMAPPER_USERNAME and NVLMAPPER_PASSWORD, or create a readable file at NVLMAPPER_RC (default $RC_DEFAULT)." >&2
  exit 1
fi
[[ -x "$NVLMAPPER" ]] || { echo "Missing or not executable: $NVLMAPPER" >&2; exit 1; }

python3 - "$RACK" "$RC" "$OUT" <<'PY'
import os
import re
import subprocess
import sys
from pathlib import Path

rack, rc_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]


def cmsh_bin() -> str:
    return (os.environ.get("RACK_CMSH") or "cmsh").strip() or "cmsh"


def cmsh_run(script: str, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [cmsh_bin(), "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def load_rc(path: str) -> dict[str, str]:
    cfg: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        cfg[key] = val
    return cfg


def yaml_double_quoted(s: str) -> str:
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{esc}"'


def nvsw_suffix_num(hostname: str) -> int:
    m = re.search(r"-nvsw-0*(\d+)$", hostname.strip(), flags=re.I)
    if not m:
        return 0
    return int(m.group(1), 10)


def discover_nvsw_hosts(rack: str) -> list[str]:
    # Same filter the operator uses: device names for this rack containing nvsw.
    proc = cmsh_run(f"device; ls | grep {rack} | grep nvsw")
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0 and not re.search(rf"{re.escape(rack)}-nvsw-", text, flags=re.I):
        sys.stderr.write(
            f"cmsh device listing failed (exit {proc.returncode}). "
            f"Command: device; ls | grep {rack} | grep nvsw\n{text[:2000]}\n"
        )
        sys.exit(1)
    pat = re.compile(rf"({re.escape(rack)}-nvsw-\d+)", flags=re.I)
    seen: dict[str, None] = {}
    for m in pat.finditer(text):
        seen[m.group(1).lower()] = None
    hosts = list(seen.keys())
    if not hosts:
        sys.stderr.write(
            f"No NVSwitch devices matched '{rack}-nvsw-*' in cmsh output. "
            f"Try: {cmsh_bin()} -c \"device; ls | grep {rack} | grep nvsw\"\n"
        )
        sys.exit(1)
    # Highest switch index first (matches prior tray_index 8..0 ordering).
    hosts.sort(key=nvsw_suffix_num, reverse=True)
    return hosts


def extract_ipv4(blob: str) -> str:
    for line in blob.splitlines():
        line = line.strip()
        if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", line):
            return line
    m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", blob)
    return m.group(1) if m else ""


def device_ip(hostname: str) -> str:
    for inner in (
        f"device ; use {hostname} ; get ip",
        f"device ; use {hostname} ; interfaces ; get rf0 ip",
    ):
        proc = cmsh_run(inner, timeout=90.0)
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        ip = extract_ipv4(blob)
        if ip:
            return ip
    return ""


cfg = load_rc(rc_path)
username = cfg.get("username") or cfg.get("user")
password = cfg.get("password")
if not username or not password:
    sys.stderr.write(f"{rc_path}: need username= (or user=) and password= entries\n")
    sys.exit(1)

hosts = discover_nvsw_hosts(rack)
blocks: list[str] = []
for h in hosts:
    ip = device_ip(h)
    if not ip:
        sys.stderr.write(f"Could not resolve IP for NVSwitch device {h!r} (tried get ip and interfaces get rf0 ip).\n")
        sys.exit(1)
    n = nvsw_suffix_num(h)
    tray = n - 1  # nvsw-01 -> 0, nvsw-09 -> 8
    blocks.append(
        f"  - tray_index: {tray}\n"
        f"    ip_address: {yaml_double_quoted(ip)}\n"
        f"    username: {yaml_double_quoted(username)}\n"
        f"    password: {yaml_double_quoted(password)}\n"
        f"    ssh_key: null\n"
    )

yaml_body = (
    "# Generated by run_nvlmapper.sh from cmsh (device; ls | grep <rack> | grep nvsw) + per-device IP.\n"
    '# flavor possible values:   "bianca_72x1"   "bianca_36x2"   "ariel_36x2"\n'
    'flavor : "bianca_72x1"\n'
    f'sysname : "nscale_{rack}"\n'
    "# tray_index is zero-based; switches are ordered high tray index first (same as legacy template).\n"
    "switches:\n"
    + "".join(blocks)
)

Path(out_path).write_text(yaml_body, encoding="utf-8")
PY

chmod 600 "$OUT" 2>/dev/null || true
exec "$NVLMAPPER" -c "$OUT" "$@"
