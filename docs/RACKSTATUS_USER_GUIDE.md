# NVIS Rack Status

## Operator and administrator guide

**Release-candidate guide:** 2026-09-14

**Application:** `nvis-rack-status-v3`

**Default site URL:** `http://localhost:5056`
**Source of truth:** the deployed application and this repository

This guide describes the current Rack Status product: live rack state, validation,
deep hardware and network audits, post-run node history, observability, reports,
workflows, and platform administration. It is deliberately explicit about what a
green result proves, which operations can alter cluster state, and which data is
durable.

> **Operational rule:** missing, stale, skipped, unreachable, or unparseable data is
> never converted into a pass. A confirmed node failure and a launcher/transport
> failure are different outcomes.

> **Site rule:** when a real node issue is confirmed, record an exact scheduler reason
> and drain that node. Deep audits do this automatically only for their structured
> `fail` results. They do not drain on warnings or inconclusive infrastructure
> failures.

---

## 1. Product boundaries and architecture

Rack Status is a React single-page application (SPA) backed by Flask. The default
production container is `nvis-rack-status-v3`; port `5056` on the observability VM
maps to the service in the container. In the current Nscale deployment, the backend
uses PythonCM/BCM for physical inventory and control, live Slurm for scheduler state
and validation admission, approved scripts, and bounded node fanout. These are
separate authority domains.

Current facts:

- Native Python collectors provide canonical rack status. The canonical snapshot is
  not published until its required log scan has completed at least once.
- PythonCM/BCM is authoritative for physical inventory and device control. Slurm is
  authoritative for allocatability, current scheduler state, scheduler reason, and
  exact validation admission. The durable activity registry preserves launch state,
  cancellation receipts, and output attribution.
- Validation and audit histories are stored under the durable application data
  directory (`/app/var` in the container).
- Remote execution scripts are sourced from `bin/scripts/` in this repository and
  deployed to the configured remote base path on the head node.
- `RACK_SCHEDULER=pythoncm` remains the primary command-provider setting, while
  `RACK_NATIVE_SINFO_SOURCE=slurm` independently selects live Slurm availability.
  Reviewed Slurm/Pyxis recipes remain explicit feature-specific execution paths.
- Telemetry comes from VictoriaMetrics and Loki. Empty telemetry is displayed as
  empty or unavailable; the UI does not invent replacement data.

The application intentionally combines three data classes:

| Class | Examples | Persistence |
|---|---|---|
| Live control-plane state | active jobs, node availability, current comments, service probes | Refreshed; may become stale |
| Durable execution evidence | validation activities, deep-audit findings, post-run attribution | Stored under `/app/var` |
| Observability telemetry | GPU, host, cgroup, fabric, logs | Retained by VictoriaMetrics/Loki |

Do not interpret a service that is merely `active` as end-to-end telemetry health.
The exporter must listen, Alloy must forward with the correct node identity, and a
fresh series must arrive at the observability VM.

---

## 2. Access, authentication, and navigation

Open `http://localhost:5056`. Authenticate through the configured token or PAM
flow. The signed-in operator name is attached to locks and actions where supported.

All application routes below are hash routes; paste the complete URL into the same
browser session after login.

| Area | Route | Purpose |
|---|---|---|
| Compute overview | `#/compute/overview` | Inventory, validation summary, jobs, and cluster facts |
| Nodes | `#/compute/nodes` | Flat node inventory and exact one-node operations |
| Racks | `#/compute/racks` | Rack summary, floor/table views, worker health, rack actions |
| Rack detail | `#/compute/racks/:rackId` | Node selection, validation, hardware, network, and service actions |
| Multi-rack | `#/compute/multirack` | Exact-rack campaigns and pinned multi-rack presets |
| Scheduler | `#/compute/scheduler` | Live jobs, reservations/reasons, logs, and cancellation |
| Ethernet | `#/infrastructure/ethernet` | Ethernet telemetry and Grafana-linked coverage |
| IB & NVLink | `#/infrastructure/ib-nvlink` | IB/NVLink metrics and live fabric probe |
| Storage | `#/infrastructure/storage` | Storage telemetry and linked dashboards |
| Metrics | `#/observability/metrics` | Native metric charts and rack filters |
| Logs | `#/observability/logs` | Validation files and Loki-backed logs |
| Events | `#/observability/events` | Current unavailable-node reasons, locks, and commands |
| Operations | `#/tools/operations` | Workspace/install/bring-up operations |
| Workflows | `#/tools/workflows` | Workflow definitions, runs, diagrams, and results |
| Terminal launcher | `#/tools/terminal` | Terminal preview and launcher |
| Analytics overview | `#/analytics/overview` | Rack/node analysis and parser output |
| Validation Status | `#/analytics/post-run-nodes` | 14-SU floor map, current validation evidence, exports, and node history |
| Reports | `#/analytics/reports` | Generated reports, bug reports, and mapper artifacts |
| Projects | `#/platform/projects` | Project, cluster, and active-scope administration |
| Hardware catalog | `#/platform/catalog` | SKU, topology, geometry, and capability catalog |
| Tool catalog | `#/platform/tools` | Tool compatibility and execution documentation |
| API explorer | `#/platform/api` | Live OpenAPI route catalog and downloads |
| Settings | `#/settings` | General/runtime and AI settings |

The terminal session itself opens at pathname `/terminal`, outside the hash router.
The root route and an unknown hash route redirect to `#/compute/overview`.

### Common UI conventions

- **Refresh** asks the current view to reload; it does not repair a failed collector.
- **Initializing** means no complete canonical snapshot exists yet.
- **Fresh** means the snapshot age is within the configured threshold.
- **Stale** preserves the last good evidence and shows its age.
- **Failed** shows an explicit error; it does not replace data with zeros.
- Closing a progress/result dialog normally stops browser watching only. It does not
  cancel a scheduler task unless the dialog explicitly says **Cancel**.
- Destructive actions require confirmation. Read the resolved node list and reason
  before accepting.
- Rack and node comments are operational evidence. Use a concrete job/run identifier,
  subsystem, and failure signature.

---

## 3. Standard rack-validation workflow

Use this sequence for a normal rack campaign.

1. Open `#/compute/racks` and wait for the collector banner to say the snapshot is
   fresh. Check the native worker panel; required workers must have completed at
   least once.
2. Open the rack, for example `#/compute/racks/ink50-dh01-h01`.
3. Review the **Slurm Status** column for every node, including its full scheduler
   reason. This is intentionally independent of adjacent PythonCM device state.
   Exclude known-bad, drained, down, failing, maintenance, or unresolved nodes.
   Never infer health from a BMC ping alone.
4. Confirm the exact selected nodes. Rack detail selects its real inventory by
   default; site trays are non-contiguous, so do not synthesize numeric ranges.
5. Run **NVLink Preflight** in full mode. Quick mode is useful for triage but does
   not prove the data path.
6. Choose the pinned validation preset and storage mode. Review the generated plan,
   image/version checks, exact node count, and exclusions.
7. If using local storage, use the offered rsync stage so nodes do not cold-load the
   image at job start. Do not rsync when the selected storage mode is NFS.
8. Launch. Track it under Validation Activities, `#/compute/scheduler`, and
   `#/observability/logs`.
9. Open **Results** beside the launch action. Verify that expected ranks/nodes/tests
   reported; a successful scheduler exit without the expected result contract is
   incomplete, not pass.
10. Open `#/analytics/post-run-nodes`. Review the Validation Status floor map,
    attributed node findings, current scheduler reason, repeated-failure flags, and
    evidence provenance. Export the selected view when a handoff artifact is needed.
11. For a confirmed issue, preserve evidence, apply an exact scheduler reason, and drain
    only the implicated node. Do not mark every node in a failed collective as bad.

For multi-rack work, use `#/compute/multirack`; do not manually paste a shortened
hostlist into a single-rack form.

---

## 4. Compute views

### 4.1 Compute Overview

`#/compute/overview` presents inventory and state totals, validation-stage summaries,
live queued/running jobs, cluster facts, and configured project/customer metadata.
It is the fastest place to answer “is the cluster broadly ready?” It is not a
substitute for exact-node readiness in Rack Detail or Multi-rack.

Expected behavior:

- Counts reconcile to the active cluster scope.
- Validation summaries distinguish done, fail, ongoing, and pending.
- A completed log scan changes its test from pending to its parsed terminal result.
- Active scheduler tasks update independently of historical validation cards.

### 4.2 Racks

`#/compute/racks` supports rack filters, table and floor views, validation state,
comments, per-rack actions, and native collector details. A rack marked pending while
the log worker has never completed is expected during initialization. A rack that
remains pending after a completed scan is a parser/identity issue and must be
investigated.

The floor/table labels use memorable rack monikers such as `d01`, `e01`, `f01`, and
`g01`; raw telemetry labels such as `WH1D1` are normalized only for display.

### 4.3 Rack Detail

`#/compute/racks/:rackId` has four action tabs. Every action resolves the selected
nodes at launch time.

#### Validation tab

The node table includes live Slurm state and reason. Use its refresh control after a
drain or allocation changes. `UNKNOWN` means the node was not reported by Slurm or
the bounded query failed; it does not mean healthy. The query is scoped to the
displayed node names and does not read system journals.

| Action | Scope and result |
|---|---|
| NVLink Preflight | GPU enumeration, link/fabric/IMEX checks, P2P, and in full mode forced NVLink traffic plus RoCE control |
| NCCL 2.31.2 Validation | Direct-host CUDA 13.3+ validation with exact NCCL 2.31.2 and `/dev/infiniband` |
| Stage 1 NCCL Benchmark | Full LLMB NCCL suite and canonical one-rack result log |
| Stage 2 NeMo Training | NCCL sweep followed by Nemotron-H 56B FP8; Rack Status serializes the stages |
| NeMo 26.04.01 | Pinned NeMo validation with bounded steps |
| MNUberGEMM | Fleet-supported `26.03-v1.17` image; do not relabel it as an unavailable 26.04.1 image |
| GPT-OSS CPU | Per-node CPU microbenchmark; not a GPU training validation |
| Exemplar System Info | DGXC benchmarking `v26.06.01` system-info contract, once per node |
| Stress / nvloom / ClusterKit / LLMB NCCL | Specialized validation tools; inspect their plan and output contract |
| Llama 3.1 405B FP8 | Shown as multi-rack only; launch from Multi-rack when its exact contract is met |

The tab also exposes durable Validation Activities, parsed results, explicit job
cancellation, and drain/undrain controls. Cancellation changes workload state; it
does not automatically declare node failure.

#### Hardware tab

| Action | Effect and safety |
|---|---|
| Hardware Deep Audit | PythonCM/direct fanout; structured checks; automatically drains only confirmed failures |
| Hardware Check | Runs approved `checker.sh` preset/custom checks |
| Exemplar System Info | Read-only DGXC recipe inventory and validation |
| System/Bug Report | Collects evidence into the report inventory |
| IMEX Check | Read-only IMEX status check |
| IMEX Reset | Restarts/repairs IMEX state; disruptive |
| GPU Reset | Quiesces the execution agent, resets GPUs, and restarts required services; disruptive |
| Power action | Uses the configured BCM device power workflow; disruptive |
| BMC Recovery | Cold BMC recovery through the approved IPMI path; disruptive |
| MAC Change | Exact one-node operation; can auto-swap when exactly one current owner exists |

Run evidence collection before reset/power operations whenever possible.

#### Networking tab

| Action | Effect and safety |
|---|---|
| NVLink/Network Deep Audit | Forced NVLink path plus IB rate/state/counter-delta audit; confirmed failures can drain |
| NVSwitch Audit | Live check-only NVSwitch audit |
| NVSwitch Check | Targeted switch health check |
| NVSwitch Fix (NMX-C) | Applies approved switch configuration; mutating |
| NVSwitch SDN Reset | Resets SDN state; disruptive |
| NVSwitch System Reset | Resets switch system state; disruptive |
| NVLMapper | Creates/collects topology mapping output |

#### Services tab

| Action | Purpose |
|---|---|
| Restart Services | Runs mount recovery and restarts authentication, execution-agent, NVIDIA IMEX, and DCGM services as configured |
| System Info | Streams node system inventory |
| Tail NeMo Log | Follows the latest relevant NeMo log |

Service restart is not a health proof. Re-run the affected validation and confirm
fresh telemetry after recovery.

### 4.4 Nodes

`#/compute/nodes` is the all-cluster inventory. Filter by node, row, or rack. Select
exactly one node to expose the same Validation, Hardware, Networking, and Services
actions plus its bring-up results. This is the safest UI for a one-node canary.

### 4.5 Scheduler

`#/compute/scheduler` shows live jobs, state, elapsed time, allocation/reason, and
available logs. Use **Cancel** only for the intended job after verifying its Job ID.
Cancellation is not evidence that a node failed.

---

## 5. Pinned validation contracts

Pinned presets prevent accidental “almost the same” recipes.

| Preset ID | Allowed scope | Fixed contract |
|---|---|---|
| `nccl-2.31.2` | node, rack, multi-rack | Direct host; CUDA >=13.3; NCCL exactly 2.31.2; IB devices required |
| `llama31-405b-fp8` | multi-rack | Exactly 256 nodes / 1,024 GB300 GPUs; pinned image and exact-node hash; reviewed MNNVL/NVLS/userbuffer contract; allocation preflight |
| `gpt-oss-cpu` | per node | CPU-only validation; at most 256 per-node jobs; TensorRT-LLM 1.1.0rc5 image |
| `exemplar-system-info` | node, rack, multi-rack | DGXC benchmarking `v26.06.01`; once per node; IB required |
| `nemo-26.04.01` | rack, multi-rack | Pinned NeMo image; 30-step acceptance target; full single-rack NVLink path or reviewed multi-rack hybrid transport |
| `mnubergemm-26.03-v1.17` | node, rack, multi-rack | Per-rack fanout, pinned image, positive-work and all-rank qualification contract |

### Storage and image staging

The plan supports NFS or node-local `/tmp/containers` storage. Local mode can stage
with rsync to avoid a cold image load. Preparation validates image existence, size,
digest/version where defined, exact live scheduler eligibility, and IB requirements.

Safety semantics:

- A plan is generated before launch. Review it.
- Pinned image/version/step values cannot be replaced by arbitrary UI flags.
- Do not add extra NCCL or topology flags merely to improve a number; that changes
  the recipe and invalidates comparison.
- A node that is down/drained/failing/maintenance is ineligible even if its BMC
  answers.
- An allocated node is eligible only under the explicit yieldable bring-up exception.
- Llama cannot launch until the exact 256-node manifest and fixed recipe validate.

---

## 6. Multi-rack validation

Open `#/compute/multirack`.

1. Filter by row, text, validation state, and **eligible only** as needed.
2. Select the intended racks. Read any warning about selected racks hidden by the
   current filter.
3. Inspect the exact-node campaign banner. A reviewed manifest or live Slurm
   snapshot provides exactly 16 eligible nodes per rack by default. The campaign ID
   is sent with the launch and stale IDs are rejected. Single-rack validation still
   uses the complete 18-node rack; Llama readiness requires exactly 256 nodes.
4. Run the preset dry-run/readiness check.
5. Review resolved nodes, exclusions, storage, container version, and dependent
   stages.
6. Launch NCCL, NeMo, or a pinned preset. Track jobs and logs in the same view and
   in Scheduler.

One rack can remain multi-rack eligible when it has 16 idle nodes even if two other
trays are unavailable. `idle*` is Slurm IDLE plus NOT_RESPONDING and is not eligible.
Missing live inventory, a rack outside the campaign, or an ineligible selected node
stops preparation before scheduler submission.

Deep-audit workflows can also accept multiple rack targets. The resolver preserves
an exact explicit node target only when one rack is selected. For multiple racks it
expands full live inventory for each selected rack; it must not reuse the first
rack's hostlist.

---

## 7. Repeatable deep audits

The Hardware and Network audit workflows are first-class repeatable validations.
They can be launched from Rack Detail or Workflows, and their durable results appear
beside the launch button, in Post-run Nodes, and through `/api/deep-audits`.

### 7.1 Hardware Deep Audit

The workflow `hardware-deep-audit` resolves PythonCM rack inventory and uses bounded direct fanout.
It checks:

- expected GPU enumeration (site default: four GPUs per GB300 compute tray);
- GPU temperature against the configured maximum (default 85 C) and active hardware
  thermal slowdown;
- uncorrectable ECC and pending GPU recovery action;
- fabric registration/completion on every GPU;
- current-boot NVIDIA Xid;
- fatal/uncorrected PCIe, DPC, EDAC, and MCE evidence;
- the configured execution agent, NVIDIA IMEX, and CMDaemon when installed;
- active `md0` and a mounted `/raid`.

Optional unavailable queries are warnings. A structured failing check exits as a
node failure with its exact reason.

### 7.2 NVLink/Network Deep Audit

The workflow `nvlink-network-deep-audit` checks:

- GPU count and active NVLink count;
- completed/successful fabric registration and active IMEX;
- expected physical IB ports (site default: four), `Active/LinkUp`, and expected
  800 Gb/s rate;
- `link_downed`, symbol, receive-error, and transmit-discard counters before and
  after traffic; only a new delta fails the run;
- `mlx5_bond_0` state, or an explicit warning when that interface is absent;
- full deployed NVLink preflight, or a local nvbandwidth fallback that forces a
  four-GPU peer-copy data path through the active scheduler.

Old cumulative IB counters alone do not fail a node. A new counter delta under the
test traffic does.

### 7.3 Result and drain semantics

| Result | Meaning | Automatic drain |
|---|---|---|
| `pass` | All required checks ran and met their contract | No |
| `warning` | Required proof passed; optional evidence was unavailable or noteworthy | No |
| `inconclusive` | Launcher, scheduler, privilege, transport, or result-contract failure prevented proof | No |
| `fail` | Structured node-local check proved a fault | Yes, when policy enables it |

The generated reason is:

```text
Drained for <category> deep audit - for validation <run-id-prefix> due to <exact reason>
```

Never convert `execve`, SSH, scheduler launch, permission, timeout, or result-parser
failure into a hardware drain. Repair the infrastructure and rerun.

### 7.4 Safe test recipe

1. Choose one healthy, idle SU7 node in `#/compute/nodes`.
2. Run Hardware Deep Audit and open **Results**. Confirm one terminal node result and
   the expected GPU count.
3. Run NVLink/Network Deep Audit. Confirm forced traffic actually executed, expected
   ports are active, and before/after counters are present.
4. Expand to one complete SU7 rack from Rack Detail.
5. Use Workflows to select two racks only after both single-rack runs resolve the
   right inventory.
6. Inspect `#/analytics/post-run-nodes` and `#/analytics/reports` for the saved data.

Release-candidate QA examples from SU7 showed a full 18-node hardware run and a full
18-node network run with terminal results for every node. Those runs are evidence of
the workflow path, not a permanent promise that future hardware is healthy.

---

## 8. Validation Status and post-run evidence

Open `#/analytics/post-run-nodes`. The default **Validation map** is the current
14-SU floor view; Hardware audits and Network audits retain the deeper report
tables. This is a native, durable reliability view—not a live import of a Google
punch card.

### Floor map controls and status

- Choose All validations or a single evidence domain such as Slurm, Node Health,
  DCGM/r1/r3, NCCL, NeMo 56B, Llama, or MNUberGEMM. New source names appear as
  discovered domains rather than disappearing.
- Filter by SU, rack, or node text. Every filter also scopes exports.
- A rack tile summarizes up to 18 physical nodes. Click it to open the highest-
  priority non-pass node, then inspect every current and historical evidence row.
- Green means all selected current evidence passed. Red means an explicit hard
  failure. Yellow/orange means active, warning, incomplete, or NOT_RUN. Gray means
  missing/unknown. Missing evidence is never green.
- The view refreshes every 30 seconds and prevents overlapping browser requests.
  The post-run response uses a compact current-state projection; node detail is
  fetched lazily so superseded history remains available without bloating every
  refresh.
- Completion predictions use only real timestamped pass transitions. Blocked,
  stalled, missing, or under-sampled data produces an explicit withheld ETA.

Use **CSV** for exhaustive machine-readable evidence. It includes source freshness,
job IDs, timestamps, reasons, metrics, and provenance and protects against formula
injection. **JPEG** produces a 3,300 x 2,550, 300-DPI floor map. **PDF** produces an
11 x 8.5 landscape map plus an exception appendix. CSV/PDF/JPEG always represent the
currently selected validation domain and filters.

DCGM results come from the bounded, atomically published validation index. The
current attempt determines color; `.prevN` and superseded attempts remain history.
Missing r1/r3/r4 evidence is NOT_RUN. OVA single-node health results come from the
read-only health-check mirror. Neither path scans the head-node journal.

### Joined post-run evidence

The report joins:

- validation activity history;
- exact scheduler targets and task history while available;
- structured Hardware/Network Deep Audit findings;
- operator findings and current scheduler state/comments;
- durable attribution snapshots that survive normal scheduler-history retention.

### Attribution rules

A failed multi-node collective does **not** make every allocated node a failed node.
A failure is attributed only when evidence identifies the node, the job was a
single-node allocation, or a structured node finding names it. Rack/control-plane
failures remain run-level failures unless node evidence exists.

The node tile shows runs, attributed failures, current scheduler state/comment, trend,
and a repeat flag. The trend compares the most recent five attributed results with
the preceding five:

- up arrow: failure rate is worsening;
- down arrow: improving;
- flat: no material change;
- new: insufficient prior comparison.

A repeat flag requires at least three attributed failures across at least three
distinct validation runs. Click a node to see all jobs/runs, non-implicated failed
jobs, exact findings, output, current comment, and latest hardware/network result.

The page includes separate **Node hardware status report** and **Node network status
report** sections. From the detail dialog an operator can launch a targeted audit.

### Operator findings and scheduler comments

Use **Mark issue** only with evidence. Enter a subsystem-specific reason and job/run
identifier. Optional drain applies only to that exact node and requires confirmation.
The report preserves who marked it and why. Recheck current scheduler state after the
action; never assume a browser success toast proves the controller accepted it.

The legacy Sheets punch-card integration can seed historical context where enabled,
but the native report, structured validations, and scheduler evidence are the operating
source of truth.

---

## 9. Analytics and generated reports

### 9.1 Analytics Overview

`#/analytics/overview` offers Rack Level and Node Level analysis. Analysis is
asynchronous and saves the latest result. Rack analysis includes NCCL deviation from
fleet median, NeMo/Nemotron timing and throughput, variability, multi-rack scaling,
nodes/GPUs/log coverage, and time-to-target where supported. Node analysis provides
test completion and heat-map-style comparisons.

Check the analysis scope and sample count before comparing racks. A highlighted
deviation is a lead for investigation, not proof of hardware failure.

### 9.2 Reports

`#/analytics/reports` lists generated analytics reports as soon as generation
finishes. Each row includes creation time, report/scope metadata, and size, with
download and confirmed delete actions. The same view includes NVIDIA bug-report and
NVLMapper artifacts.

Generated report files are stored in the durable report store under `/app/var` with
a bounded retention (current default: 40 generated reports). Container restart or
recreate must preserve the named data volume. Files from an older host-only report
directory do not appear until explicitly migrated into the active report volume.

Test:

1. Generate a small scoped report from Analytics Overview.
2. Wait for its background job to complete.
3. Open Reports without a full page reload.
4. Download the file and verify it is non-empty.
5. Restart only in a controlled maintenance test, then confirm the row remains.

---

## 10. Observability and infrastructure

### 10.1 Metrics

`#/observability/metrics` reads VictoriaMetrics through the backend and supports
15-minute, 1-hour, 6-hour, and 24-hour windows. Rack filters display site monikers
(`d01`, `e01`, `f01`, `g01`, and so on) even when the raw telemetry label is a BCM
identifier such as `WH1D1`.

The view must include D/E/F/G racks when those identities are ingesting. `g01`,
`g02`, and `g03` are independent labels; one cannot stand in for the others. An
empty chart is not healthy zero and is not backfilled from another source.

### 10.2 Node telemetry chain

Compute-node telemetry normally includes:

- Alloy forwarding;
- node_exporter;
- cgroup exporter;
- DCGM exporter;
- correct node, rack, and SU identity labels.

At this site the intended observability sinks are VictoriaMetrics at
`10.40.112.3:8429` and Loki at `10.40.112.3:3100`. Verify from deployed configuration
before changing a node.

End-to-end verification requires all of the following:

1. Services are active on the node.
2. Expected local listeners exist.
3. Alloy's persistent service environment and config identify the actual node—not a
   cloned image source node.
4. Its write-ahead log does not replay stale data under the wrong identity.
5. VictoriaMetrics returns a fresh series for that exact node/rack/SU.
6. Loki returns a recent log stream where configured.

The canonical identity repair is maintained outside this repository at
`/opt/observability/nscaletx/node-agents/repair-alloy-identity.sh`. Use its audited
canary/rollout process; do not hand-edit hundreds of nodes. Preserve a stale cloned
WAL as evidence before clearing it from active replay.

### 10.3 Logs

`#/observability/logs` lists validation/job log files from the configured rack log
directory and Loki-backed panels. It supports preview, filtering, tailing, opening,
and download. Log-file timestamps and Loki timestamps can differ; compare in UTC.

### 10.4 Events

`#/observability/events` shows current scheduler-unavailable reasons and active rack
locks/commands. It is a current-state view, not a durable incident timeline. Use the
post-run ledger and logs for history. Alerts are not a complete alert-management
surface; use the linked Grafana alerting system where configured.

### 10.5 Infrastructure views

`#/infrastructure/ethernet`, `#/infrastructure/ib-nvlink`, and
`#/infrastructure/storage` render native metric panels and links to configured
Grafana dashboards. IB & NVLink also offers a live fabric probe that distinguishes
compute rails from storage/non-compute paths and reports profile discrepancies.
Missing scraper coverage remains visibly missing.

---

## 11. Operations, workflows, terminal, and platform administration

### 11.1 Operations

`#/tools/operations` contains approved operational installers and setup helpers,
including LLMB NCCL planning, NeMo container download/import, HPC-X, CUDA/NCCL,
DCGM, nvloom, DGXC benchmarking `v26.06.01`, and Grafana/Bright Prometheus setup.

These actions can install software or change shared workspaces. Use status/dry-run
where available, review live output, and coordinate ownership before execution.

### 11.2 Workflows

`#/tools/workflows` supports list/create/edit, diagram views, automated/manual/
decision steps, resolve/fanout/scheduler/connection/evaluation steps, run output,
cancel, and manual advance. Shipped definitions include:

- `fasttrack-validation`
- `gpu-health-check`
- `hardware-deep-audit`
- `nccl-allreduce`
- `nvlink-network-deep-audit`
- `sysinfo-precheck`

Workflow edits and deletion are administrative mutations. Test a changed resolver
with one healthy node, then one rack, then multiple racks. A fanout step must consume
the resolver's `nodelist`, not a literal rack string or stale hostlist.

### 11.3 Terminal

`#/tools/terminal` previews the feature and opens `/terminal`, which provides live
head-node and compute-node shell sessions. Terminal access bypasses many form-level
guardrails. Apply normal production command-review practice.

### 11.4 Projects and active cluster

`#/platform/projects` manages projects, clusters, naming, geometry, platform, and
connection details. Changing the active cluster resizes the entire application's
scope; confirm it before any rack action.

### 11.5 Hardware catalog and tool catalog

`#/platform/catalog` manages hardware SKUs, topology, geometry, and capabilities.
`#/platform/tools` documents tool compatibility, UI surfaces, and execution modes.
Catalog changes affect planning and display and should be reviewed like code.

### 11.6 API explorer

`#/platform/api` is generated from the live backend route table. Filter routes or
download OpenAPI JSON/YAML. If this page and a static document disagree, confirm the
deployed version and treat the live registered route table as authoritative.

Important durable feature endpoints include:

| Endpoint | Purpose |
|---|---|
| `/api/status` | Canonical rack-status snapshot |
| `/api/scheduler-node-status?nodes=...` | Bounded live Slurm state and reason for explicit nodes |
| `/api/multirack-campaign` | Current exact eligible-node campaign and campaign ID |
| `/api/bringup/fleet` | Cached authoritative bring-up verdict/metric summary |
| `/api/validation-activities` | Durable validation parent/task ledger |
| `/api/deep-audits` | Deep-audit runs |
| `/api/deep-audits/<run_id>` | One audit and node findings |
| `/api/post-run-node-report?view=map` | Compact current Validation Status projection |
| `/api/post-run-node-report?node=<exact-node>` | Full current and historical evidence for one node |
| `/api/post-run-node-report/mark` | Operator finding/drain request |
| `/api/node-issues` | Node issue/trend data |
| `/api/reports/generated` | Generated report inventory |

Use an authenticated browser session or approved API token. Never paste credentials
into a ticket, guide, shell history, or screenshot.

### 11.7 Settings and assistance

`#/settings` includes searchable General/runtime settings and AI configuration.
Runtime overrides are durable and can supersede environment defaults. Record the
reason for a production override. AI configuration includes endpoint/model/key,
custom rules, and a connection test; secrets remain server-side.

The help/AI surface is page-aware when configured. It can explain visible data but
does not replace result evidence or authorize cluster mutations.

---

## 12. Safety and failure semantics

### Read-only or bounded evidence collection

Examples: refresh, status, parser results, logs, system info, IMEX check, NVSwitch
audit, fabric probe, analytics, report download.

Routine 30-second browser refresh must not invoke `sudo`, `docker ps`, `docker
exec`, or `journalctl` on the app server or head node. It reads Rack Status APIs,
which serve bounded/cached evidence. Service monitoring belongs to NHC/Alloy/AIC.
Node-local diagnostic scripts that explicitly inspect a service or kernel log are
operator-launched diagnostics and are not background refresh behavior.

### Workload-changing

Examples: launch/cancel validation, container staging, workflow execution. Verify
scope, Job ID, dependencies, and queue order.

### Node-state-changing

Examples: drain/undrain, GPU/IMEX/service reset, power/BMC recovery, MAC change,
NVSwitch fixes/resets. Preserve evidence first and confirm the exact node list.

### What does not prove a node fault

- A peer rank printing a collective-wide “test failure.”
- A job with `NODE_FAIL` after a controller/CMDaemon mass-down.
- A launcher `execve()` error.
- SSH timeout, missing sudo, inaccessible script, or invalid scheduler option.
- Old non-incrementing IB counters.
- A missing telemetry series.
- A service that is active but not ingesting.

### What can prove a node fault

- Structured deep-audit failure tied to the node.
- First fatal node-local Xid/ECC/PCIe/DPC/storage/fabric signature.
- New IB counter delta during controlled traffic.
- Exact single-node reproduction.
- A workload log that identifies the initiating endpoint, corroborated by node
  evidence.

Recommended comment pattern:

```text
Drained for <concise subsystem fault> - for Job <job-id> due to <first decisive signature>
```

---

## 13. Troubleshooting

### Every rack says NCCL/NeMo pending

1. Open Racks and inspect native worker status.
2. Confirm the log worker has `finished_once=true` and the canonical snapshot is
   newer than its refresh threshold.
3. Confirm the configured log directory is reachable and contains terminal logs.
4. Compare `/api/status` with the UI. A partial worker snapshot must not overwrite a
   complete canonical snapshot.
5. If old content persists, verify the deployed backend and SPA match the source.

### Post-run Nodes says API error

1. Open `#/platform/api` and confirm the post-run, node-issue, and deep-audit routes
   are registered.
2. Check the backend log for import or platform-provider errors.
3. Confirm the required backend module exists in the container, not only in the
   host tree.
4. If live scheduler reconciliation fails, the UI should retain durable data and show a
   warning; fix configured provider execution rather than erasing the
   history store.
5. Run `bin/verify-deploy.sh` after the approved deployment.

### Generated Reports is empty after an update

Confirm `/api/reports/generated` is registered, the report module exists in the
container, and `/app/var/reports` is on the durable volume. Host-only legacy reports
require an explicit, checksum-verified migration.

### D/E/F/G racks or `g01`–`g03` metrics are missing

1. Query VictoriaMetrics for raw node/rack labels; do not rely on the filter list.
2. Verify every affected node's Alloy service environment and configuration identity.
3. Verify node_exporter, cgroup exporter, and DCGM listeners.
4. Check Alloy forwarding errors and the intended sinks.
5. Confirm a fresh exact-node series at the observability VM.
6. If nodes were cloned/reimaged, quarantine the stale WAL and restart through the
   canonical identity repair.

### A deep audit shows `execve(): 0`

This is a launcher defect, not node hardware. Optional scheduler arguments must be valid
tokens such as `--kill-on-bad-exit=0` and `--immediate=60`. Mark the run inconclusive,
fix/deploy the workflow engine, invalidate the bad durable run with an audit trail,
and rerun. Do not drain its nodes.

### A multi-rack audit runs on the wrong nodes

Inspect the resolve-step output. One-rack exact-node execution may preserve its
explicit target; a multi-rack run must expand each selected rack and pass the
combined `nodelist` to fanout. Cancel before further steps if resolution is wrong.

### A source change is not visible

- In SSH mode, editing `bin/scripts/` on the observability VM does not update the
  head-node copy. Set an explicit non-root `RACK_SSH_USER` and approved
  `RACK_KNOWN_HOSTS_PATH`; use `bin/deploy-scripts.sh --dry-run <target>`, review, then the
  approved deployment.
- A Docker restart does not apply a newly added bind mount. Recreate through Compose
  when the deployment change requires it.
- Build the frontend before expecting source changes in the SPA.
- Run `bin/verify-deploy.sh`; “container is up” is insufficient.

### The SPA is blank or an API route disappeared

Run the deployment verifier. It checks backend imports, missing/stale modules,
durable routes, built asset consistency, HTTP assets, and crash-loop signatures.

### A result is empty

Treat it as incomplete. Check expected node/rank/test counts, log discovery,
permissions, parser version, and timestamps. Never translate empty into zero/pass.

---

## 14. Release and deployment verification

This section is for maintainers. Follow the site deployment MOP and
`docs/NSCALE_TX_DEPLOYMENT.md`; do not improvise on production.

### Source and persistent state

- Repository source: `/opt/observability/nvis-rack-status-v3`
- Container application root: `/app`
- Durable application data: `/app/var`
- Remote scripts: configured `RACK_REMOTE_BASE_PATH/bin/scripts/`
- Site validation logs: configured cluster log path (currently under
  `/cluster/benchmark/harish/rack_status/logs`)

Never delete the named volume as a routine redeploy step. Back up durable JSON and
report artifacts before schema or migration work.

### Remote script synchronization

Preview first:

```bash
cd /opt/observability/nvis-rack-status-v3
RACK_SSH_USER=<non-root-user> RACK_KNOWN_HOSTS_PATH=<pinned-file> \
  bin/deploy-scripts.sh --dry-run <approved-ssh-target>
```

The normal sync is checksum-based and does not delete head-only additions. Do not use
`--delete` without an explicit reviewed migration plan.

### Test and verification commands

```bash
cd /opt/observability/nvis-rack-status-v3
python -m pytest backend/tests/ -v

cd frontend
npm test
npm run build

cd ..
NVIS_VERIFY_PORT=5056 bin/verify-deploy.sh
```

`bin/verify-deploy.sh` verifies more than HTTP health: it checks container state,
backend imports, host/container module parity, critical durable routes, mounted SPA
asset parity, referenced asset responses, and recent import/boot errors.

### Post-deploy browser smoke test

In one authenticated session, open each route in the navigation table. At minimum:

1. Racks loads a fresh/native snapshot and nonzero inventory.
2. One Rack Detail page resolves the correct real node list.
3. Multi-rack dry-run resolves every selected rack.
4. Post-run Nodes loads without API error and a node detail opens.
5. Reports loads `/api/reports/generated`.
6. Metrics lists D/E/F/G monikers and a known exact-node series is fresh.
7. API Explorer contains deep-audit, post-run, node-issue, and generated-report APIs.
8. A one-node read-only System Info completes.
9. A healthy one-node audit returns a terminal structured result.

Do not use a full-rack mutating operation as a deployment smoke test.

---

## 15. Operator handoff checklist

For every significant validation or repair, record:

- UTC start/end time;
- exact route/action and active cluster;
- rack(s) and resolved node list;
- scheduler task ID(s), workflow run ID, and validation preset;
- image path/digest/version and storage/staging mode;
- expected versus received node/rank/test counts;
- first decisive error, implicated node, and corroborating evidence;
- cancellation, drain, reset, or recovery action and operator;
- exact scheduler reason/comment;
- rerun result and report link/path;
- whether fresh telemetry resumed.

This information makes the Post-run Node Report useful over days rather than only
for the current incident.

---

## 16. Current limitations and compatibility

- Clush support is parked.
- Slurm-only `.sbatch` workflows require the explicit Slurm fallback until they have
  a validated direct-execution script form.
- Events is a current-state view, not a retained alert timeline.
- Deep audits prove only the checks that actually executed; optional unavailable
  queries remain warnings.
- Observability coverage depends on installed exporters and correct identity; the UI
  does not fabricate missing rack data.
- A historical Sheets punch card may be used as optional baseline context, but the
  native Post-run Node Report is the current product surface and source of ongoing
  run attribution.
- Pinned presets intentionally reject unsupported image/version/scale changes.
- Validation Status refreshes every 30 seconds, while the expensive bring-up fleet
  summary has an independent five-minute cache and at most 16 verdict workers by
  default. Multiple open browsers must share one refresh, not launch one scan each.
- Slurm state/reason is scheduler truth; a conflicting PythonCM `up` value proves
  physical visibility, not allocatability.

---

## 17. Screenshot checklist for a release packet

Screenshots age quickly, so capture them from the deployed release rather than
embedding stale cluster state in this guide. A useful packet contains:

1. `#/compute/racks` with snapshot freshness and native workers visible.
2. Rack Detail Validation tab with selected-node count, live Slurm Status/reason,
   and Results buttons.
3. Hardware Deep Audit terminal result card.
4. NVLink/Network Deep Audit terminal result card with forced traffic evidence.
5. `#/analytics/post-run-nodes` showing all 14 SU rows, the status legend, source
   freshness, one selected validation domain, export controls, and an expanded
   failure node with Job ID/reason/provenance.
6. `#/analytics/reports` showing a generated report.
7. `#/observability/metrics` showing D/E/F/G rack monikers.
8. `#/platform/api` filtered to `deep-audits` and `post-run`.

Redact tokens, keys, usernames where required, BMC credentials, and terminal command
history before sharing screenshots.

---

## 18. Glossary

| Term | Meaning |
|---|---|
| Canonical snapshot | Last complete rack-status snapshot eligible for publication |
| Deep audit | Repeatable structured hardware or network validation with per-node results |
| Inconclusive | Test infrastructure prevented a valid health determination |
| IMEX | NVIDIA inter-node fabric service/state used by the platform |
| NVLS | NVLink SHARP transport behavior; fixed by the applicable pinned recipe |
| Post-run attribution | Evidence-based link between a validation outcome and an implicated node |
| Repeat flag | At least three attributed failures in three distinct runs |
| SU | Site super-unit grouping used in node/rack identity |
| Structured failure | Machine-readable node-local failed check, eligible for policy-driven drain |
| Validation activity | Durable record of a launched validation and its results/log identity |

For environment-variable details use `docs/ENVIRONMENT.md`. For production deployment
landmines and recovery use `docs/NSCALE_TX_DEPLOYMENT.md`. For parser internals use
`docs/PARSING_RESULTS_ARCHITECTURE.md`.
