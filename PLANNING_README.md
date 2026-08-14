# NVIS Rack Status v3 — Planning & Deployment README

_ObsVM deployment planning document. Location: `/opt/observability/nvis-rack-status-v3`.
Target host: NscaleTX GB300 SuperPOD ObsVM (`network-server`, dual-homed 192.168.10.3 + 10.40.x fabric),
running alongside the existing 29-container observability stack._

---

## 1. Overview — What It Is

NVIS Rack Status (v3, app version 0.1.5) is a full-stack **"single pane of glass"** for
validating and operating NVIDIA GPU clusters (GB300 NVL72 / GBx00 SuperPOD-class). It is a
**Flask/Python backend fronted by a React 19 SPA** (NVIDIA Kaizen UI) that connects — locally
or over SSH — to a cluster head node (BCM or MAAS) and drives Slurm, `cmsh`, `pdsh`, and a
bundled library of site validation/diagnostic/benchmark bash scripts.

It is an **action-oriented operator control-plane and validation cockpit**, not a telemetry
pipeline. It does NOT speak BMC/Redfish/IPMI/SNMP directly and does NOT emit Prometheus
metrics. Instead it SSHes to the head node, runs CLI tools there, parses head-node log
directories into a per-rack PASS/FAIL state machine, and embeds Grafana panels via iframe/deep-link
URLs. It is therefore **complementary to** — not a duplicate of — the read-only VictoriaMetrics/
Grafana/Loki stack already on the ObsVM.

From the operator's perspective it does five things:
1. Shows real-time per-rack validation state and node health across the fleet.
2. Launches acceptance benchmarks and training jobs (NCCL, NeMo/Nemotron, Llama, MNUBERGEMM
   stress, nvloom, ClusterKit, LLMB NCCL microbenchmark) on single racks, in bulk, or cross-rack.
3. Performs hardware/network remediation (power cycle, GPU reset, NVSwitch/NVLink resets, IMEX,
   BMC recovery, service restarts, MAC changes) with a web terminal into nodes.
4. Analyzes and charts results (NCCL bandwidth, deviation vs fleet median, Nemotron step-time/
   TFLOPs, scaling).
5. Adds an AI assistant, a visual workflow engine, tooling installers, reports, and
   multi-operator rack-lock coordination.

> Note: several **Infrastructure**, **Observability**, and **Platform** pages are design
> previews on mock data pending backend wiring.

---

## 2. Features & Data

### Key features
- **Cluster overview dashboard** — cluster/project/customer identity, product line, HW vendor,
  stat tiles for rack/node/tray counts, validation done/failed/ongoing/pending, Stage-1 NCCL and
  Stage-2 NeMo progress, connected users, BCM/Slurm/NeMo-container/build versions, squeue summary.
- **Rack status table** — sortable/filterable; per rack shows validation state with per-stage
  NCCL/NeMo badges, last Slurm job, `sinfo` node states (idle/alloc/mix/drain/down/comp with
  counts), node/tray up-down status (UP/DN/INST/UNK), operator comments, and a rack-lock indicator.
- **Per-row rack actions** — open/live-tail log, parser charts, Grafana links, drain/undrain,
  cancel Slurm job, power up/down nodes, add/edit/delete comment.
- **Bulk "Select" mode** — fire an operation across many racks in parallel (NeMo, Llama, stress,
  nvloom, ClusterKit, LLMB NCCL, power on, GPU reset, hardware check, bug report, IMEX reset/check,
  restart services, NVSwitch fix/SDN/system/check, NVLMapper, drain/undrain).
- **Rack detail** — node/tray selector + four action tabs: **Validation** (Stage-1 NCCL, Stage-2
  NeMo, Llama, MNUBERGEMM, nvloom, ClusterKit, LLMB NCCL, parser results, cancel, drain/undrain),
  **Hardware** (power on, GPU reset, checker.sh, nvidia-bug-report, IMEX reset/check, BMC recovery
  via ipmitool on head node, MAC change), **Networking** (NVSwitch fix/SDN/system reset/audit
  stream/check, NVLMapper topology), **Services** (restart mount/munge/slurmd/imex/dcgm, live
  sysinfo stream, tail NeMo log).
- **Interactive web terminal** — xterm.js + flask-sock PTY into compute nodes and the head node.
- **Multirack operations** — select eligible racks, launch cross-rack NeMo validation or NCCL
  microbenchmark (with dry-run), durable job table, result charts, on-disk result browser.
- **Slurm controls** — sinfo, squeue, drain/undrain, scancel, power-cycle.
- **Analytics** — NCCL collective bandwidth (all-reduce/all-gather/all-to-all), per-rack deviation
  vs fleet median, all-reduce bus-BW size sweeps, Nemotron step-time/TFLOPs/CV, multirack
  throughput/scaling and time-to-train (Chart.js).
- **Tools / Operations & Scripts** — live-streaming installers (NeMo container, HPC-X, CUDA+NCCL,
  DCGM/DCGMI, nvloom, Exemplar/dgxc-benchmarking), Grafana launch, run-script-by-rack.
- **Visual Workflow engine** — Mermaid ↔ React Flow editor, YAML per-node config, AI generation,
  state-machine execution with timeline, per-step output, versioning, persistence.
- **AI assistant** — floating chat widget, SSE streaming, view/rack context aware; NVIDIA NIM /
  OpenAI-compatible / vLLM / Ollama / local Llama; demo mode.
- **Rack locks** — implicit locks during ops, explicit 30-min refreshable claims, "rack busy"
  confirm dialog, toolbar popover of who holds which racks.
- **Reports & Settings** — nvidia-bug-report Excel inventory + download; log dir, AI config with
  connection test, tabbed job-environment editor (masked/password fields), operator display name,
  encrypted NVFTP token vault + cache sync.
- **Auth / theming / help** — token or PAM login (or disabled), demo mode, dark/light theming,
  in-app Help, deep-linkable hash routes.

### Data shown
Per-rack validation state and per-stage (NCCL, NeMo) status; Slurm sinfo node states with counts
and squeue jobs; node/tray up-down (UP/DN/INST/UNK); cluster identity + inventory; software
versions (BCM/Slurm/NeMo/build); NCCL collective bandwidth + deviation-vs-median + bus-BW sweeps;
Nemotron step-time/TFLOPs/CV + multirack scaling/time-to-train; validation/job logs + live tail;
GPU thermal/power via embedded Grafana panels; hardware diagnostics (checker.sh, sysinfo:
GPU/driver/fabric manager); NVSwitch audit stream; multirack job records; bug-report Excel archive;
and preview/mock data on the Infrastructure/Observability/Platform pages.

### UI pages
Compute (Cluster overview, Racks, Rack detail, Multirack, Scheduler) · Infrastructure (Ethernet,
IB-NVLink, Storage — preview) · Observability (Metrics, Logs, Events — preview) · Tools (Operations
& Scripts, Workflows, Terminal) · Analytics & Benchmarks (Analytics, Reports) · Platform (Projects
& Clusters, Catalog, Tool Registry — preview; Settings) · Global (AI chat widget, Help, rack-lock
popover, login).

---

## 3. Architecture & Tech Stack

**Single-service, single-port containerized web app.**

- **Backend:** Python 3 / Flask 3 WSGI served by **Gunicorn — 1 worker on purpose**, gthread
  worker-class, 24 threads, 600s timeout. Single-process by design because it holds significant
  **in-RAM state** (rack-status cache, SSH reachability cache, async job/command contexts,
  active-user counts, PTY terminal sessions). Do NOT raise worker count. Key libs: flask-sock
  (WebSocket terminal), flask-compress+brotli, paramiko (SSH), tinydb (JSON doc store), openpyxl
  (Excel export), cryptography (token vault), pyyaml (workflows), requests (LLM/HTTP).
- **Frontend:** React 19 + TypeScript SPA, Vite 8, MUI 9 + @nvidia/foundations-react-core (Kaizen
  UI), react-router 7, xterm.js, chart.js/react-chartjs-2, reactflow+dagre+mermaid. Built to
  `static/spa` and served by Flask.
- **Packaging:** multi-stage Dockerfile (`node:22-slim` build → `ubuntu:22.04` runtime with only
  python3 + openssh-client). docker-compose with named volume `rack-data:/app/var` and the host SSH
  key bind-mounted read-only to `/run/secrets/ssh_key`.

### Where an endpoint lives: `backend/api/`

**A new endpoint is a new file in `backend/api/`. Nothing else is edited — not `app.py`, not the
package `__init__`.** The loader walks the directory at startup and registers what it finds.

```
backend/
  core.py            # imports neither Flask nor app — this is what breaks the import cycle
  api/
    __init__.py      # the loader; you should never need to touch it
    _common.py       # shared helpers that DO need Flask (underscore = skipped by the loader)
    terminal.py      # exposes install(app): WSGI middleware, below Flask's router
    help.py          # exposes bp: a normal Blueprint
    history.py
  app.py             # creates the app, calls api.register(app), holds what has not moved yet
```

A module qualifies by exposing `bp` (a Blueprint) or `install(app)`. Exposing neither raises at
startup rather than being skipped, and imports are deliberately not wrapped in `try` — a module
that cannot import must fail loudly, not vanish from the API.

URL prefixes: omit `url_prefix` and it comes from the filename (`rack_locks.py` → `/api/rack-locks`,
underscores becoming hyphens); declare one when the URL cannot be spelled as a module name;
`url_prefix=""` means the routes carry full paths, which is the mode used when migrating existing
routes so a move cannot change a URL by accident.

**Why this exists.** `app.py` reached 20,000 lines and still holds 135 of the 199 routes, because
every feature had to touch it. Parallel work collided by construction — which is how an entire feature (node issues, deep audits,
the post-run report) ended up living uncommitted on a customer deployment, discovered only because
something else broke. One file per domain removes the shared edit point; implicit registration
stops the registry from becoming the next one.

**Migration is in progress.** `terminal` (via `install`), `help`, `vault`, `history`, `deep_audits`, `node_issues`,
`workflows`, `nvftp_sync`, `cluster_health`, `ds_diag`, `openapi_spec`, `job_status`,
`fabric_probe`, `ai`, `sn_llama_logs`, `slurm_queries`
have moved; 135 routes are still in `app.py`. Configuration resolution is no longer the blocker: it now lives in
`config_env.py`, with the job registries in `job_registry.py`, host paths in `paths.py`, rack naming
in `naming.py`, the Slurm command runners in `slurm_query.py` and the build version in
`app_version.py`. What is left is coupled to the *launch* side of the async tools — the `_prepare_*`
builders and worker threads, which reach `_record_validation_activity`,
`_validation_node_eligibility` and `_rack_node_hostnames_from_inventory` — so that connected
component wants its own extraction next, the way `slurm_query.py` separated runners from routes.

`backend/tests/test_route_parity.py` snapshots all 199 URL paths and fails by name if a move
changes one; run it after every extraction.

**Notable backend components:** `app.py` (REST routes not yet migrated + settings registry);
`backend/api/` (per-domain endpoint modules, auto-discovered); `core.py` (Flask-free shared
helpers); `native_status.py` (primary rack/device status collector — merges `cmsh rack list` +
`ds`, `sinfo`, log-dir probes into in-memory state); `host_exec.py` + `transport/` (local-vs-SSH
routing, env-forwarding, sudo escalation, SSH ControlMaster multiplexing, jump-host hop, fanout);
`platforms/` (bcm.py / maas.py / base.py); `schedulers/` (slurm/local + runai/slinky/maas stubs);
`workflow_engine.py` (workflow state machine **and** the general long-job runner — see Reusable
components below); `validation_activities.py` (durable validation-family launch registry; it
migrates the legacy `multirack_jobs.json` on first read); `rack_locks.py` (fcntl advisory locks); `catalog/`
(TinyDB tool/platform/project catalog blueprint); `ai_chat.py`; `token_vault.py`;
`nvftp_cache_sync.py`; `rack_web/pam_*` (PAM login); `nvis_id_templates.py` (configurable
rack/CT/NVSwitch naming templates); and `bin/scripts/` (bundled operational + benchmark bash).

### First-class citizens — the parts built to scale

Rack Status is not a collection of scripts behind a web page. A small number of components own a
concern for the whole product and are meant to be built **on**, not beside. Each has been
reimplemented at least once by someone who read the file inventory above and could not tell that
the component *was* the intended mechanism — so they are named explicitly here, with the
prescriptive version in `.cursor/rules/core-designs.mdc`.

A genuinely new capability that no citizen covers may need a new structure; that is how each of
these began. When it happens, it gets documented as a citizen so the next person inherits it.

| Concern | Citizen that owns it | Instead of |
|---|---|---|
| Adding an endpoint | **`backend/api/<domain>.py`** — a module exposing `bp` (or `install(app)`), discovered automatically; shared helpers in `core.py` | adding a route to `app.py`, which is the shared edit point that made parallel work collide |
| Running anything on a host | **connection/transport** — `host_exec.run_on_host()` / `popen_on_host()` / `connection()`, `transport.fanout()` for per-node work | a direct `ssh` call, which gets none of the env forwarding, hop chain, retry or ControlMaster behaviour |
| What the product can run | **catalog/Tools** — a Tool document; pins, images and step counts as `tool_param_defaults` on the Platform | constants in a new module, which no operator can edit and which ship one site's paths everywhere |
| Which site, which nodes | **Project/Cluster** record, resolved by `catalog/context.py` as the high overlay — including acceptance **campaigns** (pinned, versioned node rosters with an id) | env vars pointing at a file on one host |
| Long-running work | **workflow state machine** — uuid, run persisted under `var/` so it survives a restart, per-step output, bounded-concurrency fan-out, HTTP surface already built | another in-memory `_*_jobs` dict, which loses everything on deploy |
| Where a knob comes from | **configuration resolution** — settings registry + `_eget`/`_subprocess_env` + catalog overlays, with defined precedence | a bare `os.environ.get()`, which opts out of per-cluster overlays |
| Which cluster manager / queue | **platform + scheduler providers** (`platforms/`, `schedulers/`) | an `if` branch in a caller |
| Raw output → metrics | **results pipeline** — `nvis_logscan.py` single pass, `universal_parser.py`, tiered `nvis_nccl_ingest.py` | a multi-pass shell scan or a head/tail sample, which silently misses results mid-file |
| What ran, and who owns a rack | **durable run registry + locks** — `validation_activities.py`, `rack_locks.py` | in-process state that dies with the worker |
| Node and rack identity | **naming templates** — `nvis_id_templates.py` | deriving hostnames from a rack id, which yields targets that do not resolve |
| Reports and exports | **one dataset, many renderers** — `cluster_report.build_dataset()` → CSV / JSON / PDF | one generator per format, which drift on the first added column |
| Access | **auth** — PAM/token vault + `_require_dashboard_auth` and its bypass list | a second check invented per route |
| Shared frontend state | **contexts** — `ActiveClusterContext`, `RackCommandsStoreContext`, `ThemeContext` | local state that duplicates them, so two panes disagree |

**Planned:** *terminals integrated with AI* — the WebSocket PTY and `ai_chat.py` exist separately
today; the intent is one operator console where the terminal, the AI context and the product's
knowledge of the cluster are the same surface.

**Persistence — file-based only, NO external database.** All runtime state lives under `/app/var`,
persisted via the compose named volume `rack-data`. TinyDB at `var/store/catalog.json`
(platforms/tools/projects, atomic writes + flock). Plain JSON under `var/cache/`
(`validation_activities.json` — durable validation launches + adopted external logs, migrated
from legacy `multirack_jobs.json`; `rack_locks.json`, `job_env_overrides.json` — holds
Prometheus/sudo/NGC secrets, perms-sensitive, `ai_settings.json`, log-parse caches,
`cmsh_rack_list.txt`, `nvftp_sync_state.json`). Encrypted NVFTP token vault. Flask session
secret at `var/secrets/.rack_web_secret_key`. Workflows as YAML in `var/workflows/` with run
state JSON. `rack_comments.json` at repo root.

---

## 4. Data Sources

- **BCM head node over SSH** (PRIMARY; `RACK_SSH_HOST`, default remote base
  `/home/cmsupport/nvis-rack-status`): `cmsh -c "rack; list"` (inventory), `cmsh -c ds` (per-node
  device status), `cmsh` device foreach → `rf0` IP (hostname→BMC/rf0 IP map), `cmsh device power
  on/off`.
- **MAAS head node** (alternative, `RACK_PLATFORM=maas`): `maas <profile> machines read` / `tags
  read` / `machine power-on|off`.
- **Slurm CLI on head node** (behind `module load slurm` unless skipped): sinfo / squeue / sacct /
  scontrol / scancel.
- **pdsh fanout** to compute nodes for service restarts, hw checks, bug-report collection.
- **Bundled bash scripts** under `bin/scripts` run locally or over SSH: `rack_status.sh
  --probe-json`, `bmc-recovery.sh`, `gpu_reset.sh`, `nvswitch_*.sh`, `imex_*.sh`, `sysinfo.sh`, plus
  nccl/nvloom/clusterkit/nemo/llmb launchers.
- **Head-node log directories** (validation NeMo log dirs, e.g.
  `/home/cmsupport/aihpc_val-main/validation/nemo`) parsed for PASS/FAIL/progress heuristics, cached
  in `var/cache`.
- **Grafana / Bright-Prometheus** — NOT queried for data; the app only builds Grafana solo-panel
  **embed iframe URLs** (`RACK_WEB_GRAFANA_URI`, default port 3000, dash uid `bright-gpu-per-host`)
  and PromQL host-regex for GPU thermal/power charts + deeplinks.
- **LLM inference endpoint** (optional): default NVIDIA inference API; also OpenAI-compatible /
  vLLM / Ollama / local Llama.
- **NVFTP** (optional): outbound SFTP mirror of `var/cache` to a project view.
- **Interactive SSH/PTY sessions** to head node and, via `ssh -J` jump, to compute nodes.

> **IPMI/Redfish/BMC are reached INDIRECTLY** — BMC IPs come from `cmsh rf0`; power/BMC recovery is
> done through `cmsh` and `bmc-recovery.sh` on the head node. `ipmitool` appears only as textual AI
> guidance, not as a direct protocol client in the Python backend.

---

## 5. How It Aids NscaleTX Validation & Handover

This tool is the **NVIS-side execution cockpit** that actually runs the VR200/GBx00 FastTrack MOP
and produces the evidence trail. It is complementary to the observability stack: the 29-container
stack is a **read-only telemetry pipeline** (VictoriaMetrics/Grafana/Loki + DCGM/UFM/BCM/eth/BMS
exporters); rack-status is an **action-oriented, per-rack validation-state + remediation console**.
Critically it is a Grafana **consumer** (deep-links/iframe), so it stays standalone and reuses the
existing nvis Grafana (`:3001`, 45 dashboards) rather than feeding VictoriaMetrics.

### Distinctive value (what NO exporter produces)
1. **Per-rack validation state machine** — turns raw NCCL/NeMo Slurm job logs into per-rack
   PASS/FAIL/ongoing/pending (stage1 = NCCL busbw vs fleet-median deviation; stage2 = NeMo/Nemotron
   step-time). This adjudicated PASS/FAIL is not in VictoriaMetrics (the NCCL/LLMB exporters are
   metric-oriented and currently DARK).
2. **Job launch** — NCCL via llmb-run, MNUBERGEMM burn-in, ClusterKit bw/latency heatmaps, nvlmapper
   NVLink down-lane, nvidia-bug-report.sh pdsh fan-out, GPU reset, power-cycle, Slurm drain/undrain
   for node quarantine.
3. **Multi-operator rack locks** — advisory locks (implicit active-op + explicit 30-min claims)
   prevent NVIS/Dell engineers stepping on the same rack during a chaotic multi-vendor bring-up.
4. **Workflow engine + AI assistant** — codify the MOP as runnable procedures.

### Concrete validation use-cases mapped to the current bring-up state
- **Bring-up tracking (useful NOW):** with 282 SU5 nodes DOWN, `cmsh rack;list` + `ds` and
  sinfo/scontrol render a per-rack UP/DOWN/INSTALLING table across the 288-blade SU5 — directly
  answers "which racks/nodes are up," the same signal the stack's node-level panels are DARK for.
  Its NCCL/NeMo validation views light up as nodes come UP.
- **Phase-1 single-rack validation:** `run-validation-async` launches NCCL collectives + NeMo per
  rack; the parser rolls up rack-by-rack "validated / not validated."
- **Phase-1 stress + thermal/power watch:** MNUBERGEMM burn-in with Xid/throttle monitoring; GPU
  thermal/power watched via Grafana deep-links scoped to rack/node/time-window (defers to existing
  DCGM dashboards).
- **Cross-rack scale validation:** aggregate NCCL/Nemotron scaling across rack groups.
- **Phase-2 diagnostics & remediation:** ClusterKit heatmaps, nvlmapper down-lane mapping,
  bug-report fan-out, GPU reset, power-cycle, drain/undrain to quarantine failing nodes — the
  read-only stack cannot drive any of these.
- **Multi-operator coordination:** advisory rack locks during power/reset/validation ops.

### Handover value (Dell/Microsoft day-2)
During bring-up it is the execution cockpit that produces per-rack PASS/FAIL, NCCL/NeMo parser
artifacts, Excel exports, and nvidia-bug-report bundles that populate the Dell "Hardware Good"
hand-over report and the NScale consolidated test-plan. For day-2 it can persist as an operations
console the customer inherits: its workflow engine codifies the MOP (GPU health check, re-validation,
RMA re-test) as runnable workflows; its drain/power-cycle/GPU-reset/bug-report actions are standard
day-2 remediation; its Grafana deep-link config + operator playbooks transfer directly. The heavy
day-2 health/availability telemetry itself remains the observability stack — rack-status is the
remediation/validation front-end that sits on top of it, not a replacement.

### Honest overlaps & gaps vs the existing stack
**Overlaps (handled cleanly, no duplication):**
- _GPU thermal/power_ — rack-status defers to existing Grafana/DCGM via deep-links rather than
  re-plotting. No duplicated panels.
- _Node up/down + BCM inventory_ — both read `cmsh`; the bcm exporter already feeds VM and the
  invapp exporter gives 516-device reachability. Rack-status presents the same node-up signal at a
  different altitude (per-rack operational table **with actions**), not as competing metrics.

**Gaps it does NOT cover (do not overclaim):**
- _Redfish/BMC power/thermal/PSU/SEL_ — rack-status does not poll Redfish directly; that is the
  (creds-blocked) redfish-bmc exporter's job.
- _BMS/CDU coolant/water in-out (16 CDUs)_ — entirely outside rack-status (BMS exporter).
- _L1 cabling golden-P2P compare (CVT) + deep firmware/FRU inventory_ — it flags NCCL bandwidth
  outliers and runs nvlmapper/bug-report, but does NOT do CVT golden compare or capture per-node
  BMC/NIC/GPU firmware inventory. Avoid claiming hardware-readiness beyond validation-log +
  cmsh/sinfo-derived state.
- _Dell/Microsoft acceptance report_ — the tool does not generate `result_schema.json` /
  Microsoft-acceptance traceability. Its outputs are **inputs** to that report, not the report.
- _No metrics contribution_ — it adds nothing to VictoriaMetrics/Loki; validation progress is only
  visible inside its own UI unless a converter/exporter is added (see Follow-ups).

---

## 6. Deployment Plan

### Deployment context (important)
A **near-identical sibling of this same tool is ALREADY running** on the ObsVM: container
`nvis-rack-status` (image `nvis-rack-status-main-rack-status:latest`, from
`/opt/observability/nvis-rack-status-main`), demo/local mode, host port **5055**. **v3 is a SECOND
copy deployed alongside it.** Compose namespaces resources by project directory
(`nvis-rack-status-v3`), so container name, image, and volume are all distinct — no clash.

### Method
**Docker Compose** (INSTALL.md Option B), consistent with the existing 29-container stack and the
sibling deploy. `docker-compose.yml` defines one service `rack-status` with `build: .`, so
`docker compose up -d --build` builds the multi-stage image from source and runs it.

### Files created for this deploy (NOT committed upstream; v3-local)
| File | Purpose |
|---|---|
| `.env` | Compose interpolation values (RACK_WEB_PORT, RACK_SSH_KEY_PATH, RACK_SSH_HOST, RACK_WEB_AUTH_TOKEN). |
| `docker-compose.override.yml` | Runtime flags for a self-contained demo/local start + distinct `container_name`. Auto-merged; does NOT edit the committed compose. |
| `deploy-secrets/placeholder_ssh_key` (0600) | Satisfies the always-present `:ro` key bind-mount. Demo mode does not use SSH. |

### Exact steps executed
1. `cd /opt/observability/nvis-rack-status-v3`
2. Created `deploy-secrets/placeholder_ssh_key` (chmod 600) — the compose key mount is
   unconditional even in demo mode, so the file must exist and be readable.
3. Created `.env` with `RACK_WEB_PORT=5056`, absolute
   `RACK_SSH_KEY_PATH=/opt/observability/nvis-rack-status-v3/deploy-secrets/placeholder_ssh_key`
   (compose does NOT expand `~`), `RACK_SSH_HOST=127.0.0.1`, `RACK_WEB_AUTH_TOKEN=`.
4. Created `docker-compose.override.yml` forcing `RACK_STATUS_DEMO=1`, `RACK_WEB_AUTH_DISABLED=1`,
   `RACK_EXEC_MODE=local`, `RACK_STATUS_LOG_DIR=/app/var/logs`, `container_name: nvis-rack-status-v3`,
   `restart: unless-stopped`.
5. `docker compose -p nvis-rack-status-v3 up -d --build`
6. Verify: `docker compose -p nvis-rack-status-v3 ps` and `curl -sf http://localhost:5056/api/status`;
   open `http://<host>:5056/spa/`.

### Port map
The container exposes **exactly one port: 5000** (gunicorn binds `0.0.0.0:5000`; serves SPA, legacy
HTML, JSON API, and WebSocket terminals multiplexed). Published to host via `RACK_WEB_PORT`.

| Host port | Status | Notes |
|---|---|---|
| **5056** | **chosen — FREE** | v3 published port. Reads as the "v3 twin" adjacent to the sibling on 5055. |
| 5055 | OCCUPIED | Running `nvis-rack-status-main` sibling. |
| 5050 | OCCUPIED | `build-docker.sh` dev default. |
| 5000 | free today | compose default; less explicit, avoided. |
| 3000/3001/3100/8012/8090/8428/8429/9812 + others | OCCUPIED by core stack | infra-engine/Grafana/Loki/invapp/lldpq/VM/exporters — v3 touches NONE of these. |

Outbound (only when switched to ssh mode later): 22 (SSH to head node + SFTP to NVFTP), 3001
(Grafana embed — must point at `:3001`, NOT the default `:3000` which clashes with nvis-infra-engine),
443 (LLM/NVFTP tokens).

### Config vars set for the demo start
| Var | Value | Why |
|---|---|---|
| `RACK_WEB_PORT` | `5056` | Free host port; avoids 505x collisions. |
| `RACK_SSH_KEY_PATH` | absolute placeholder path | Mandatory `:ro` mount; `~` not expanded by compose. |
| `RACK_SSH_HOST` | `127.0.0.1` | Placeholder to silence compose interpolation warning. |
| `RACK_WEB_AUTH_TOKEN` | empty | Auth disabled for demo. |
| `RACK_STATUS_DEMO` | `1` | Synthetic data, no cluster access. |
| `RACK_EXEC_MODE` | `local` | Overrides rack-config.env's shipped `ssh`. |
| `RACK_WEB_AUTH_DISABLED` | `1` | Simplest minimal start. |
| `RACK_STATUS_LOG_DIR` | `/app/var/logs` | Avoids a 400 on `/api/status` from the non-existent head-node log path. |

---

## 7. "Configure Later" Checklist (intentionally unconfigured now)

Everything below is deliberately left as safe placeholders so the tool starts with synthetic data.
Configure when moving to real cluster data:

- [ ] **Switch to real cluster:** empty/delete `docker-compose.override.yml`; set `RACK_EXEC_MODE=ssh`,
      `RACK_PLATFORM=bcm` in `rack-config.env`.
- [ ] **Head node:** `RACK_SSH_HOST=192.168.10.2` (nscale-hn1), `RACK_SSH_USER`, `RACK_SSH_PORT`,
      `RACK_REMOTE_BASE_PATH` (where nvis-rack-status scripts + logs live on the head node).
- [ ] **Real SSH key:** replace `deploy-secrets/placeholder_ssh_key` with a private key whose public
      half is in the head node's `authorized_keys`; point `RACK_SSH_KEY_PATH` at it.
- [ ] **Auth:** set a real `RACK_WEB_AUTH_TOKEN` and remove `RACK_WEB_AUTH_DISABLED=1`
      (the app binds `0.0.0.0` on both 192.168.10.3 and the 10.40 fabric — do this before exposing
      on a routable net). Optionally set a fixed `RACK_WEB_SECRET_KEY` for stable sessions.
- [ ] **Naming templates (REQUIRED for NscaleTX):** the default cmsh/sinfo parser regex is Bright
      `p<N>-r<N>-ct<N>`, which will NOT match NscaleTX `ink50-dh01-h01-NN`. Set
      `NVIS_RACK_ID` / `NVIS_CT_ID` / `NVIS_NVSW_ID` templates (Settings or
      `job_env_overrides.json`) or the rack/tray + validation tables stay EMPTY.
- [ ] **Grafana reuse:** set `RACK_WEB_GRAFANA_URI` to the nvis Grafana `:3001` (default embed port
      3000 clashes with nvis-infra-engine). Override `RACK_WEB_GRAFANA_DEEPLINK_DASHBOARDS` with the
      nvis dashboard UIDs (built-in catalog assumes stock NMC UIDs).
- [ ] **Validation config:** `NEMO_SQSH_SRC` (NeMo `.sqsh` on head node),
      `RACK_SLURM_SKIP_MODULE_LOAD=1` (already set), `RACK_VALIDATION_CT_RANGE`, `VALIDATION_TEST`
      (e.g. `nccl_allreduce,llm`).
- [ ] **Optional AI assistant:** `RACK_AI_API_KEY` / `RACK_AI_ENDPOINT` / `RACK_AI_MODEL`.
- [ ] **Optional NVFTP:** token vault + cache sync (SFTP host + tokens API).

---

## 8. Follow-ups & Risks

### Risks
- **DUPLICATE DEPLOYMENT:** a sibling instance of this exact tool is already live
  (`nvis-rack-status`, host 5055, demo). Confirm running v3 as a SECOND copy is intended and not a
  redundant/competing deploy. (Both coexist cleanly via compose project namespacing.)
- **505x port family:** 5050 and 5055 are occupied. v3 uses 5056; do NOT run `build-docker.sh` dev
  (hardcodes 5050 + container name `rack-status` + `docker rm -f`) — it could disrupt other
  instances. Stick to compose.
- **Tilde not expanded:** compose takes `~/.ssh/id_rsa` literally. Always use an absolute
  `RACK_SSH_KEY_PATH` (done).
- **Mandatory key mount:** the `:ro` key mount is present even in demo — `up` fails if the file is
  missing (placeholder created).
- **Build needs network:** `build: .` runs apt-get + pip + npm ci + npm run build (may pull Node 22).
  Needs registry/proxy access on the ObsVM. If air-gapped, use `build-docker.sh save/load`. (The
  sibling image built OK at 258MB, so the build works here.)
- **Default config is SSH, not demo:** `rack-config.env` ships `RACK_EXEC_MODE=ssh` with the host
  commented out; without forcing demo/local you get an empty "no racks" state. The override forces
  demo/local.
- **Binds all interfaces + no auth in demo:** with `RACK_WEB_AUTH_DISABLED=1` there is no login and
  the UI is reachable on both the mgmt IP and the 10.40 fabric. Restrict access / enable auth before
  a routable exposure.
- **Single gunicorn worker by design** (in-process cache/SSH/terminal state) — not a scaling knob;
  do not raise workers.

### Follow-ups (high-value integrations, not built)
- **result_schema.json converter:** a small converter from rack-status parser results / per-rack
  states → the NScale `result_schema.json` (per-test results with Microsoft-acceptance traceability,
  `test_id ^(GPU|CH|ST|NET|UFM)-[0-9]{2}$`, owner Dell/NVIS) would close the acceptance-reporting
  loop. The parser output maps cleanly onto that schema.
- **Optional metrics exporter:** surface per-rack validation PASS/FAIL as `nscale_*` metrics into
  VictoriaMetrics for a validation-progress Grafana panel (currently validation progress is only
  visible inside the tool's own UI).
- **Naming-template wiring:** set the NscaleTX `ink50-dh01-h01-NN` templates as soon as ssh mode is
  enabled, else the core tables are empty.
