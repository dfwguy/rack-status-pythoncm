#!/bin/bash
# Start Grafana in Docker (--network=host) with a provisioned Prometheus datasource
# (https://localhost:8081/prometheus, GET, basic auth from PROMETHEUS_RC) and a small
# dashboard: GPU thermal + GPU power (Bright-style metrics: gpu_temperature,
# gpu_power_usage_instant; labels hostname, parameter).
#
# Usage:
#   PROMETHEUS_RC=/cm/cmsupport/.prometheusrc ./launch-grafana-bright-prometheus.sh
#   GRAFANA_CONTAINER=grafana-bright GRAFANA_HTTP_PORT=3000 ./launch-grafana-bright-prometheus.sh
#
# Defaults: PROMETHEUS_RC tries /cm/cmsupport/.prometheusrc then ~/.prometheusrc
# After start: http://127.0.0.1:${GRAFANA_HTTP_PORT:-3000}/  (admin / value of GF_SECURITY_ADMIN_PASSWORD)
# Anonymous access: enabled by default (same org as provisioned dashboards, org id 1). Override with
#   GF_AUTH_ANONYMOUS_ENABLED=false
# Role: GF_AUTH_ANONYMOUS_ORG_ROLE (default Viewer). Org name: GF_AUTH_ANONYMOUS_ORG_NAME (default Main Org.)
# nvis-rack-status embeds /d-solo/ panels in the parser dialog: GF_SECURITY_ALLOW_EMBEDDING=true is set.
# Dashboard uses text variable job_host_regex (URL var-job_host_regex=...) to restrict Prometheus hostnames.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRAFANA_CONTAINER="${GRAFANA_CONTAINER:-grafana-bright-prometheus}"
GRAFANA_HTTP_PORT="${GRAFANA_HTTP_PORT:-3000}"
GRAFANA_IMAGE="${GRAFANA_IMAGE:-grafana/grafana:latest}"
PROMETHEUS_URL="${PROMETHEUS_URL:-https://localhost:8081/prometheus}"
PROM_DS_UID="${PROM_DS_UID:-prometheus-bright}"
STATE_DIR="${GRAFANA_STATE_DIR:-${TMPDIR:-/tmp}/grafana-bright-$USER}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

resolve_rc() {
  if [[ -n "${PROMETHEUS_RC:-}" && -f "$PROMETHEUS_RC" ]]; then
    echo "$PROMETHEUS_RC"
    return
  fi
  for c in /cm/cmsupport/.prometheusrc "$HOME/.prometheusrc"; do
    if [[ -f "$c" ]]; then
      echo "$c"
      return
    fi
  done
  echo ""
}

PROMETHEUS_RC="$(resolve_rc)"
if [[ -z "$PROMETHEUS_RC" ]]; then
  echo "ERROR: No credentials file. Set PROMETHEUS_RC or install ~/.prometheusrc" >&2
  exit 1
fi

# .prometheusrc sets USER/PASSWORD for curl; preserve login USER for this shell.
__saved_login_user="${USER:-}"
# shellcheck source=/dev/null
set -a
source "$PROMETHEUS_RC"
set +a
PROM_USER="${USER:-root}"
PROM_PASSWORD="${PASSWORD:-}"
if [[ -n "$__saved_login_user" ]]; then
  export USER="$__saved_login_user"
fi
if [[ -z "$PROM_PASSWORD" ]]; then
  echo "ERROR: PASSWORD not set after sourcing $PROMETHEUS_RC" >&2
  exit 1
fi

# YAML-safe single-quoted string: ' -> ''
yaml_single_quote() {
  printf '%s' "$1" | sed "s/'/''/g"
}

load_docker() {
  if command -v module >/dev/null 2>&1; then
    module load docker 2>/dev/null || true
  fi
  command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker not in PATH (try: module load docker)" >&2
    exit 1
  }
}

write_datasource_yaml() {
  local out="$1"
  local u sq p_sq
  u="$(yaml_single_quote "$PROM_USER")"
  p_sq="$(yaml_single_quote "$PROM_PASSWORD")"
  mkdir -p "$(dirname "$out")"
  cat >"$out" <<EOF
apiVersion: 1
datasources:
  - name: Prometheus-Bright
    uid: ${PROM_DS_UID}
    type: prometheus
    access: proxy
    url: ${PROMETHEUS_URL}
    isDefault: true
    jsonData:
      httpMethod: GET
      tlsSkipVerify: true
    basicAuth: true
    basicAuthUser: '${u}'
    secureJsonData:
      basicAuthPassword: '${p_sq}'
EOF
}

write_dashboard_provider_yaml() {
  local out="$1"
  mkdir -p "$(dirname "$out")"
  cat >"$out" <<'EOF'
apiVersion: 1
providers:
  - name: bright-gpu
    orgId: 1
    folder: Bright
    type: file
    disableDeletion: false
    allowUiUpdates: true
    updateIntervalSeconds: 30
    options:
      path: /etc/grafana/provisioning/dashboards/json
EOF
}

write_dashboard_json() {
  local out="$1"
  mkdir -p "$(dirname "$out")"
  python3 - "$out" "$PROM_DS_UID" <<'PY'
import json
import sys

out_path, ds_uid = sys.argv[1], sys.argv[2]

def panel(pid, y, title, expr, unit, leg):
    return {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "gridPos": {"h": 10, "w": 24, "x": 0, "y": y},
        "datasource": {"type": "prometheus", "uid": ds_uid},
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line",
                    "fillOpacity": 8,
                    "lineInterpolation": "linear",
                    "lineWidth": 1,
                    "showPoints": "never",
                },
                "mappings": [],
                "unit": unit,
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "table", "placement": "right", "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": [
            {
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "editorMode": "code",
                "expr": expr,
                "legendFormat": leg,
                "range": True,
                "refId": "A",
            }
        ],
    }

dash = {
    "annotations": {"list": []},
    "editable": True,
    "fiscalYearStartMonth": 0,
    "graphTooltip": 0,
    "links": [],
    "liveNow": False,
    "panels": [
        panel(
            1,
            0,
            "GPU thermal (per host / GPU)",
            'gpu_temperature{parameter=~"^gpu[0-9]+$", hostname=~"$job_host_regex"}',
            "celsius",
            "{{hostname}} / {{parameter}}",
        ),
        panel(
            2,
            10,
            "GPU power usage (per host / GPU)",
            'gpu_power_usage_instant{parameter=~"^gpu[0-9]+$", hostname=~"$job_host_regex"}',
            "watt",
            "{{hostname}} / {{parameter}}",
        ),
    ],
    "refresh": "10s",
    "schemaVersion": 39,
    "style": "dark",
    "tags": ["bright", "gpu", "nvis-rack-status"],
    "templating": {
        "list": [
            {
                "current": {"selected": True, "text": ".*", "value": ".*"},
                "hide": 0,
                "label": "Job hosts (Prometheus regex)",
                "name": "job_host_regex",
                "options": [{"selected": True, "text": ".*", "value": ".*"}],
                "query": ".*",
                "skipUrlSync": False,
                "type": "textbox",
            }
        ]
    },
    "time": {"from": "now-6h", "to": "now"},
    "timezone": "utc",
    "title": "Bright GPU thermal and power",
    "uid": "bright-gpu-per-host",
    "version": 1,
    "weekStart": "",
}

with open(out_path, "w", encoding="utf-8") as f:
    json.dump(dash, f, indent=2)
PY
}

load_docker
echo "Using credentials from: $PROMETHEUS_RC"
echo "State dir: $STATE_DIR"
rm -rf "$STATE_DIR"
mkdir -p "$STATE_DIR/provisioning/datasources" \
  "$STATE_DIR/provisioning/dashboards/json"
chmod 700 "$STATE_DIR"

write_datasource_yaml "$STATE_DIR/provisioning/datasources/prometheus-bright.yaml"
write_dashboard_provider_yaml "$STATE_DIR/provisioning/dashboards/dashboards.yaml"
write_dashboard_json "$STATE_DIR/provisioning/dashboards/json/bright-gpu-per-host.json"

echo "Pulling $GRAFANA_IMAGE ..."
docker pull "$GRAFANA_IMAGE"

if docker ps -a --format '{{.Names}}' | grep -qx "$GRAFANA_CONTAINER"; then
  echo "Removing existing container $GRAFANA_CONTAINER"
  docker rm -f "$GRAFANA_CONTAINER" >/dev/null
fi

# Grafana admin (change via env for production)
GF_ADMIN_USER="${GF_SECURITY_ADMIN_USER:-admin}"
GF_ADMIN_PASS="${GF_SECURITY_ADMIN_PASSWORD:-admin}"

# Anonymous users: same organization as file-provisioned content (orgId 1 / Main Org.)
GF_AUTH_ANONYMOUS_ENABLED="${GF_AUTH_ANONYMOUS_ENABLED:-true}"
GF_AUTH_ANONYMOUS_ORG_NAME="${GF_AUTH_ANONYMOUS_ORG_NAME:-Main Org.}"
GF_AUTH_ANONYMOUS_ORG_ROLE="${GF_AUTH_ANONYMOUS_ORG_ROLE:-Viewer}"

echo "Starting $GRAFANA_CONTAINER (host network, Grafana UI port $GRAFANA_HTTP_PORT) ..."
docker run -d \
  --name "$GRAFANA_CONTAINER" \
  --restart unless-stopped \
  --network host \
  -e GF_SERVER_HTTP_PORT="$GRAFANA_HTTP_PORT" \
  -e GF_SECURITY_ADMIN_USER="$GF_ADMIN_USER" \
  -e GF_SECURITY_ADMIN_PASSWORD="$GF_ADMIN_PASS" \
  -e GF_USERS_ALLOW_SIGN_UP=false \
  -e GF_AUTH_ANONYMOUS_ENABLED="$GF_AUTH_ANONYMOUS_ENABLED" \
  -e GF_AUTH_ANONYMOUS_ORG_NAME="$GF_AUTH_ANONYMOUS_ORG_NAME" \
  -e GF_AUTH_ANONYMOUS_ORG_ROLE="$GF_AUTH_ANONYMOUS_ORG_ROLE" \
  -e GF_SECURITY_ALLOW_EMBEDDING=true \
  -v "$STATE_DIR/provisioning:/etc/grafana/provisioning:ro" \
  "$GRAFANA_IMAGE"

echo ""
echo "Grafana: http://127.0.0.1:${GRAFANA_HTTP_PORT}/"
echo "Anonymous: enabled=${GF_AUTH_ANONYMOUS_ENABLED} org=\"${GF_AUTH_ANONYMOUS_ORG_NAME}\" role=${GF_AUTH_ANONYMOUS_ORG_ROLE}"
echo "Login:   user=${GF_ADMIN_USER}  password=${GF_ADMIN_PASS}  (override with GF_SECURITY_ADMIN_*)"
echo "Datasource and dashboard are provisioned from: $STATE_DIR/provisioning"
echo "Dashboard folder: Bright -> \"Bright GPU thermal and power\""
echo ""
echo "Logs: docker logs -f $GRAFANA_CONTAINER"
