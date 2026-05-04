#!/usr/bin/env bash
# Run nvlmapper for a rack: writes config_<rack>.yaml from the embedded template
# below (no per-rack YAML on disk), merges credentials from NVLMAPPER_RC into
# username/password lines, then runs ./nvlmapper -c config-<rack>.yaml
#
# ~/.nvlmapperrc format (key=value lines, # comments, blank lines ok):
#   username=admin
#   password=secret
# Optional alias: user=admin  (same as username=)
#
# IP layout matches a typical bianca_72x1 nine-switch management row; edit
# this script if your BMC/mgmt addresses differ.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RC="${NVLMAPPER_RC:-/root/.nvlmapperrc}"
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

TEMPLATE="${SCRIPT_DIR}/config_${RACK}.yaml"
OUT="${SCRIPT_DIR}/config-${RACK}.yaml"

[[ -f "$RC" ]] || { echo "Missing credentials file: $RC (set NVLMAPPER_RC to override)" >&2; exit 1; }
[[ -x "$NVLMAPPER" ]] || { echo "Missing or not executable: $NVLMAPPER" >&2; exit 1; }

# Embedded rack template (structure from bianca_72x1 / nine-switch layout).
# username/password are empty here — filled from RC by the merge step.
cat >"$TEMPLATE" <<EOF
# flavor possible values:   "bianca_72x1"   "bianca_36x2"   "ariel_36x2"
flavor : "bianca_72x1"
sysname : "nscale_${RACK}"
# Keep descending order of the tray indexes to mimic the physical ordering
# Use ZERO-based indexing to match the physical indexing
switches:
  - tray_index: 8
    ip_address: "10.54.150.182"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 7
    ip_address: "10.54.150.183"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 6
    ip_address: "10.54.150.184"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 5
    ip_address: "10.54.150.185"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 4
    ip_address: "10.54.150.186"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 3
    ip_address: "10.54.150.187"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 2
    ip_address: "10.54.150.188"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 1
    ip_address: "10.54.150.189"
    username: ""
    password: ""
    ssh_key: null
  - tray_index: 0
    ip_address: "10.54.150.190"
    username: ""
    password: ""
    ssh_key: null
EOF

python3 - "$RC" "$TEMPLATE" "$OUT" <<'PY'
import re
import sys
from pathlib import Path

rc_path, template_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]


def load_rc(path):
    cfg = {}
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


cfg = load_rc(rc_path)
username = cfg.get("username") or cfg.get("user")
password = cfg.get("password")
if not username or not password:
    sys.stderr.write(
        f"{rc_path}: need username= (or user=) and password= entries\n"
    )
    sys.exit(1)

out_lines: list[str] = []
for line in Path(template_path).read_text(encoding="utf-8").splitlines(keepends=True):
    if re.match(r"^[ \t]*username:[ \t]", line):
        m = re.match(r"^([ \t]*)username:", line)
        assert m
        nl = "\n" if line.endswith("\n") else ""
        out_lines.append(f'{m.group(1)}username: {yaml_double_quoted(username)}{nl}')
    elif re.match(r"^[ \t]*password:[ \t]", line):
        m = re.match(r"^([ \t]*)password:", line)
        assert m
        nl = "\n" if line.endswith("\n") else ""
        out_lines.append(f'{m.group(1)}password: {yaml_double_quoted(password)}{nl}')
    else:
        out_lines.append(line)

Path(out_path).write_text("".join(out_lines), encoding="utf-8")
PY

chmod 600 "$OUT" 2>/dev/null || true
exec "$NVLMAPPER" -c "$OUT" "$@"
