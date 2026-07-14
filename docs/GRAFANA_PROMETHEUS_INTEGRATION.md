# Grafana + Prometheus integration (BCM / Mission Control)

How Rack Status surfaces cluster metrics (GPU / NVLink / power / CPU / mem) through Grafana —
and why, on most BCM clusters, **you don't need to deploy anything**.

> TL;DR: **BCM 11 / NVIDIA Mission Control (NMC) head nodes already ship a full Prometheus +
> Grafana stack** with purpose-built GB200/DGX dashboards. Point Rack Status at that Grafana and
> use the **deep-link** feature (below). Only fall back to deploying your own Grafana when the
> cluster genuinely has none.

---

## 1. First: check whether the cluster already has Grafana

Bright Cluster Manager (11+) and NVIDIA Mission Control provision a Prometheus + Grafana stack on
the head node, typically served over HTTPS at **`https://<head>/grafana`** (port 443), with a
default `prometheus` datasource and an **"NMC Dashboards"** folder (SuperPOD overview, DGX GB200
DCGM, Rack Power Analysis, NVLink, Health Status, Slurm, Inventory, Node Exporter, …).

Discover it (adjust host/creds; a tunnel is fine — see the per-site doc):

```bash
GF="https://<head>/grafana"; AUTH="admin:<password>"

# Is Grafana up and are the creds right? (200 = yes)
curl -sk -u "$AUTH" -o /dev/null -w '%{http_code}\n' "$GF/api/org"

# What datasources exist? (look for a default prometheus)
curl -sk -u "$AUTH" "$GF/api/datasources" | python3 -m json.tool

# What dashboards exist? (grab uid + title)
curl -sk -u "$AUTH" "$GF/api/search?type=dash-db&limit=100" \
  | python3 -c "import sys,json;[print(d['uid'], '|', d.get('folderTitle','-'), '|', d['title']) for d in json.load(sys.stdin)]"
```

If that returns dashboards, **use them** — skip section 5 (deploying your own).

---

## 2. Two ways Rack Status uses Grafana

| Mode | What it does | Needs | When |
|---|---|---|---|
| **Deep-links** (recommended) | "Open in Grafana" opens a **full** dashboard in a new tab, scoped to a rack / node / time window | Just a browser-reachable Grafana URL | You have any Grafana (built-in NMC or your own) |
| **`/d-solo/` embeds** | Inline `<iframe>` panels inside the parser results dialog | `GF_SECURITY_ALLOW_EMBEDDING=true` + a dashboard whose panels match the embed config | You want inline panels and control the Grafana config |

Deep-links are the low-friction path: no embedding config, no CORS/X-Frame issues, no Prometheus
credentials stored in Rack Status. The operator is already logged into Grafana in the browser, so
the link just works.

---

## 3. Deep-links: configuration

All keys live under **Settings → External** (persisted as job-env overrides) and are read by
`GET /api/grafana/deeplinks`.

| Env var | Default | Meaning |
|---|---|---|
| `RACK_WEB_GRAFANA_URI` | *(empty)* | Grafana origin, no trailing slash (e.g. `https://head.example/grafana`). Empty → same host as the browser + `RACK_WEB_GRAFANA_PORT`. |
| `RACK_WEB_GRAFANA_PORT` | `3000` | Port used only when the URI is empty. |
| `RACK_WEB_GRAFANA_VAR_RACK` | `Rack` | Template-variable name for **rack** scoping. Sent as `var-<name>=<rack id>`. BCM/NMC dashboards use `Rack`. |
| `RACK_WEB_GRAFANA_VAR_HOST` | `hostname` | Template-variable name for **node** scoping. Sent as `var-<name>=<node>`. BCM/NMC dashboards use `hostname`. |
| `RACK_WEB_GRAFANA_TIMEZONE` | *(empty)* | `browser`, `utc`, or an IANA zone for the `timezone=` query. |
| `RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS` | *(empty)* | JSON list of dashboards to expose (see schema below). Empty → the built-in NMC catalog. |
| `RACK_WEB_GRAFANA_DEEPLINK_DISABLED` | *(empty)* | Truthy → hide all deep-link menus. |

### Dashboard catalog schema

`RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS` is a JSON array; each entry:

```json
[
  { "id": "dcgm",  "label": "GPU / DCGM",         "uid": "<grafana-uid>", "slug": "<dash-slug>", "scope": "host"    },
  { "id": "power", "label": "Rack power & thermal","uid": "<grafana-uid>", "slug": "<dash-slug>", "scope": "rack"    },
  { "id": "slurm", "label": "Slurm",              "uid": "<grafana-uid>", "slug": "<dash-slug>", "scope": "cluster" }
]
```

- `uid` (**required**) — the Grafana dashboard UID (from `/api/search`).
- `slug` — the URL slug (Grafana slugifies the title, e.g. `02-dgx-gb200-dcgm-dashboard`). If wrong, Grafana still redirects.
- `scope` — controls which variables get injected:
  - `host` → `var-<VAR_HOST>=<node>` **and** `var-<VAR_RACK>=<rack>` (per-node dashboards like DCGM/NVLink).
  - `rack` → `var-<VAR_RACK>=<rack>` only.
  - `cluster` → no scoping vars (cluster-wide dashboards like Slurm / overview).
- Invalid JSON, a non-list, or entries without `uid` → silently falls back to the built-in catalog.

### Built-in NMC catalog (used when the JSON is empty)

`GPU / DCGM` (host) · `Rack power & thermal` (rack) · `NVLink` (host) · `Health status` (rack) ·
`Slurm` (cluster) · `SuperPOD overview` (cluster). The **UIDs default to the stock NMC dashboards**;
if your Grafana uses different UIDs, override with the JSON above (find them via `/api/search`).

### URL shape produced

```
<base>/d/<uid>/<slug>?orgId=1&var-Rack=<rack>&var-hostname=<node>&from=<ms>&to=<ms>&timezone=<tz>
```

`from`/`to` are epoch-ms — for parser results they're the job's time window; for the rack table
they default to `now-1h`.

---

## 4. Where deep-links appear in the UI

- **Rack table** — a gauge icon in each rack's action cell → menu of dashboards scoped to that rack (live window).
- **Parser results → NEMO/Grafana tab** — an "Open in Grafana (full dashboards)" card scoped to the rack (or the single node, if the job ran on one host) and the **job's time window**. Renders even when iframe embedding is off.

---

## 5. Fallback: cluster has *no* Grafana

Only if section 1 found nothing. `bin/scripts/launch-grafana-bright-prometheus.sh` starts Grafana
in Docker with a provisioned Prometheus datasource + a GPU thermal/power dashboard, pointed at the
BCM Prometheus endpoint. This path **requires Prometheus basic-auth credentials** (`.prometheusrc`
or `RACK_PROMETHEUS_BASIC_AUTH_USER` / `_PASSWORD`) — ask the BCM admin for them. Prefer the
built-in NMC Grafana whenever it exists; it's richer and needs no creds in Rack Status.

---

## 6. Finding the right variable values (best-effort scoping)

Rack Status sends the **Rack Status rack id** (e.g. `bos01-b05`) as `var-<VAR_RACK>`. That only
pre-selects the dashboard if it matches a real Prometheus label value. To confirm:

1. Open the DCGM dashboard, open the **Rack** dropdown, note the exact values (e.g. `bos01-b05` vs `b05`).
2. If they differ, either adjust the Rack Status rack id or (future) add a rack-token transform.
3. `hostname` values come from `sinfo -N` node names (e.g. `bos01-b05-11-s1-dgx-05-c01`).

If a link opens the dashboard but doesn't pre-filter, the variable value didn't match — pick from
the dropdown; the dashboard itself is still correct.

---

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No gauge icon / no deep-link card | `RACK_WEB_GRAFANA_DEEPLINK_DISABLED` set, or `/api/grafana/deeplinks` returned no dashboards. |
| Link opens Grafana login | Not logged into that Grafana in the browser — log in once; the session cookie carries over. |
| Dashboard opens but not filtered | `var-Rack` / `var-hostname` value doesn't match a Prometheus label — see section 6. |
| 404 on the dashboard | Wrong `uid` — re-check via `/api/search`. |
| Embeds blank (not deep-links) | `/d-solo/` needs `GF_SECURITY_ALLOW_EMBEDDING=true` and a matching dashboard; mixed content (HTTPS page vs HTTP Grafana) also blanks iframes. Deep-links avoid all of this. |

---

## See also

- Per-site deployment specifics (URLs, tunnels, exact UIDs, credentials) live in that customer's
  own docs folder, **not** here — see e.g. `BOSTON/docs/GRAFANA_BOSTON.md`.
- `docs/ENVIRONMENT.md` — full env-var reference.
- `frontend/src/lib/grafanaLinks.ts` / `useGrafanaDeeplinks.ts` — deep-link URL builder + config hook.
- `frontend/src/components/GrafanaLinksMenu.tsx` — the rack-table menu.
- `bin/scripts/launch-grafana-bright-prometheus.sh` — self-hosted Grafana fallback.
