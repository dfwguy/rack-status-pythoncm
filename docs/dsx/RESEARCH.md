# DSX orchestrator integration — research & plan

**Date:** 2026-10-02 · **Author:** rkoike + Claude · **Status:** research / pre-design
**Trigger:** weekly Rack Status meeting — new cluster deployments will move off BCM onto the
"DSX orchestrator". Felix (PM) sourced two open-source repos from the DSX team:

- `https://github.com/dsx-ai-factory/infra-controller` — NVIDIA Infra Controller (**NICo**)
- `https://github.com/NVIDIA/cluster-readiness-engine` — **NVCRE** / CRE

Both cloned to `../dsx-research/` (sibling of the repo, not tracked here).

---

## TL;DR — the headline that reshapes the framing

> **"DSX" is not one scheduler that replaces BCM.** Neither repo even contains the string
> "DSX" — these are the two OSS building blocks underneath the DSX product framing, and they
> live at **two different layers**:

1. **NICo (infra-controller)** is a **bare-metal lifecycle substrate that sits *below* BCM/Slurm,
   not beside them.** It does hardware discovery, inventory, firmware, power, health, DPU/network
   isolation, and hands hosts out as *Instances* via a REST API. It **explicitly does not schedule
   workloads and does not assemble clusters** — that's still Slurm/K8s's job
   (`docs/overview/scope-and-boundaries.md`).

2. **NVCRE (cluster-readiness-engine)** is **not a provider at all.** It's a Kubernetes operator
   that runs multi-node GPU **burn-in / acceptance certification** (NCCL, NeMo training, DCGM
   level-4) and **reports** bad nodes. It **never cordons/taints/modifies nodes**. It's a
   diagnostic/results producer — conceptually a sibling of our DCGM `mndiag`/`mnubergemm` work,
   not of BCM.

**Consequence for our provider model.** We have two orthogonal axes today
(`cluster_manager` = BCM/MAAS, and `scheduler` = slurm/pythoncm/…). The DSX work maps onto
**three** distinct integration points, not one:

| DSX piece | Rack Status first-class citizen it maps to | Axis |
|---|---|---|
| **NICo** inventory/health/power/rack-topology | **PlatformProvider** (`cluster_manager`) | replaces BCM as the inventory/health source |
| **NICo** instance allocation / host handout | the **scheduler** axis *may* stay Slurm/K8s on top | NICo is **not** a scheduler |
| **NVCRE** certification results | **catalog/Tools + results pipeline** (like DCGM diags) | neither manager nor scheduler |

So the meeting's "we'll use DSX instead of BCM" most precisely means: **NICo becomes the
`cluster_manager` (PlatformProvider) for new sites**, Slurm/K8s likely still schedules on top, and
NVCRE is an optional certification tool we can surface results from.

---

## 1. What each project is

### 1.1 NICo — NVIDIA Infra Controller (`infra-controller`)

- **Shape:** two halves. **NICo Core** = Rust microservices (~118 crates, edition 2024) on an
  in-datacenter K8s cluster, all intra-service mTLS/gRPC, Postgres-backed, `nico-api` the single
  source of truth. **NICo REST** = a Go REST/JSON layer (`rest-api/`, Go 1.26) — the
  **external integration surface**, served on **port 8388**, base path `/v2/org/{org}/nico/...`.
- **Not a CRD/operator model** for its state — orchestration lives in Postgres + Temporal.
- **Auth:** JWT Bearer; roles `PROVIDER_ADMIN` / `TENANT_ADMIN`; supports external OIDC issuers
  *or* a built-in Keycloak, plus **service-account** machine-to-machine tokens
  (`rest-api/openapi/auth.md`). → **service-account JWT is our integration path.**
- **Domain model** (hierarchy): **Site → Rack (→ NVLinkDomain / rack-group) → Tray → Machine
  (Host + DPUs) → Instance**. GPUs/CPUs are `MachineCapability` entries; switches & power-shelves
  are first-class inventory. Richer and more hardware-centric than Slurm's flat node list —
  includes physical slot placement, BMC IPs, and even **liquid-cooling leak status**.
- **`MachineStatus`:** `Initializing, Ready, Reset, Maintenance, InUse, Error, Decommissioning,
  Decommissioned, Unknown`. **No "drain"** — closest is `Maintenance` mode + the
  Decommissioning lifecycle.
- **Health model:** `MachineHealth` = `successes[]` + `alerts[]` (each alert has `inAlertSince`,
  `message`, `tenantMessage`, `classifications` e.g. `PreventAllocations`); probes include
  Temperature/Voltage/FanSpeed/PowerSupply/BgpStats/**Dcgm** tests/DPU disk. Health is aggregated
  (`source: aggregate-host-health`).

**Read endpoints we'd consume** (all `rest-api/openapi/spec.yaml`): `GET /site`, `GET /machine`
(rich filters: siteId/status/instanceTypeId/hwSkuDeviceType/`includeMetadata`…), `GET /machine/{id}`,
`/machine/{id}/health-report`, `/machine/{id}/power`, `GET /rack`, `/rack/{id}/health-report`,
`GET /tray`, `GET /domain/nvlink`, `GET /machine/gpu/stats` + instance-type stats (fleet rollups),
`/machine/label/...` (labels carry `RackIdentifier`, `ServerName`), `GET /instance`. Health probe
`GET /healthz`. There's also a **gRPC** `Forge` service (heavier, mTLS) and an **MCP** server
(`rest-api/mcp/`, `rest-api/TOOLS.md`) — **we integrate via REST, not gRPC.**

### 1.2 NVCRE — Cluster Readiness Engine (`cluster-readiness-engine`)

- **Shape:** Go 1.27, Kubebuilder v4 / controller-runtime **operator + CLI** (`nvcrectl`, also
  `kubectl-nvcre`). CRD hierarchy `Certification → Workflow → Job → (Kubeflow TrainJob)`, plus
  `WorkloadRun`, `GoodputMeasurement`, `BandwidthMeasurement`, `LogProfile`
  (group `nvcre.nvidia.com/v1alpha1`).
- **What it runs:** catalog categories — `communication/*` (NCCL all-reduce/all-gather/alltoall/
  loopback), `diagnostics/dcgm-level4` (wraps DCGM `diag --run 4`), `training/*` (NeMo nemotron).
  Topology-aware grouping by clique/rack/T1-switch; adaptive bisection fault isolation.
- **Never modifies nodes** (ADR-061) — it emits failed-node signals; remediation is the
  platform's job (NVSentinel/BCM/us). Reads external health via **CEL expressions** over node
  conditions/labels/taints.
- **Result surfaces for us:**
  - **JSON report** (`certification report --results-file`): `result ∈ PASSED|INCOMPLETE|FAILED|
    RUNNING`, `nodeResults[] {name,group,rack,status}` (per-node pass/fail), `categories[]` with
    bandwidth/goodput/`diagnose{}`, `failedNodes[]`.
    (`certification report` exits non-zero until complete — poll status.)
  - **CRD status** `Certification.status.categoryStatuses[].failedNodesRef` (ConfigMaps).
  - **Prometheus `/metrics`:** per-node `nvcre_topology_validated_nodes{node,domain}` /
    `_failed_nodes`, `nvcre_job_status`, NCCL bandwidth & goodput gauges — directly dashboard-able.
- **Overlap with our DCGM work:** additive, not a replacement. It orchestrates single-node DCGM
  L4 as one category and adds the **multi-node** NCCL/training burn-in we don't have. Doesn't
  implement/reference `mndiag`/`mnubergemm` by name.

---

## 2. How to run them in containers for testing

Both assume Kubernetes; **neither needs real GPUs** for wiring/integration work (mocks exist).

### 2.1 NICo REST on kind — **best sandbox for our REST integration** (no hardware)
```bash
cd ../dsx-research/infra-controller/rest-api
make kind-reset        # kind cluster + images + Postgres/Temporal/Keycloak/cert-manager
                       # + Helm umbrella + site-agent + a MOCK NICo Core
make kind-status       # check
make kind-verify
# API:       http://localhost:8388   (GET /healthz)
# Keycloak:  http://localhost:8082   Temporal UI: :8233   Adminer: :8081
```
Test identity: `admin@example.com` / `adminpassword` (PROVIDER_ADMIN+TENANT_ADMIN), org `test-org`
(token + curl recipe in `rest-api/README.md`). This exposes the **exact REST endpoints** we'd
consume, backed by a mock Core — so we can build and test a NICo provider with zero hardware.
Unit tests: `cd rest-api && make test` (manages a Postgres test container itself).
Full Core+REST locally: DevSpace (`dev/deployment/devspace/`), uses `machine-a-tron` mock hosts.

### 2.2 NVCRE on Kind + KWOK — full cert flow, fake GPUs
```bash
cd ../dsx-research/cluster-readiness-engine
make setup-test-uat    # Kind cluster for UAT
make test-uat          # Tilt deploys CRE + Kubeflow Trainer, runs the e2e suite
make build-nvcrectl    # build the CLI
# offline, no cluster needed:
nvcrectl certification render --category communication/nccl-all-reduce --dry-run
```
KWOK fakes `nvidia.com/gpu.product` nodes and auto-completes pods, so the whole
`Certification → Workflow → Job → TrainJob → Pod` path runs **without real GPUs**. Real runs need
GPU Operator + Kubeflow Trainer + (default) Prometheus Operator CRDs.

---

## 3. How Rack Status adds support — integration plan

Grounding in our existing abstraction (`backend/platforms/base.py`, `backend/schedulers/base.py`,
`backend/catalog/models.py`, branch `pool-mode-rackless-clusters`). Two orthogonal axes already
exist: `Cluster.cluster_manager` (PlatformProvider) and `Cluster.scheduler` (Scheduler), each
selected per-cluster and materialized into env by `catalog/context.py:high_overlay()`.

### 3.1 The one hard design decision (build ON, not BESIDE)

Our `PlatformProvider` contract is **shell-command-shaped**: `list_racks_cmd() -> list[str]`,
`device_status_cmd()`, `device_power_cmd()` — strings run over an SSH `Connection` via host-exec
(cmsh for BCM, maas CLI for MAAS). **NICo is an HTTP+JWT REST API, not a CLI over SSH.** Forcing
NICo through `*_cmd()` string builders would be building *beside* the abstraction (shelling out to
`curl`, or worse). The honest options:

- **(A) Generalize the provider transport.** Treat "emit a shell command" as *one* transport and
  add an HTTP/REST transport as a peer, so a provider returns normalized records regardless of how
  it fetched them. MAAS already hints at this — it overrides `compute_node_inventory()` to parse
  JSON rather than leaning on the Slurm-CLI builders. NICo would be the first provider that is
  *purely* API-driven (inventory **and** power via REST), which is the forcing function to make the
  transport a real seam rather than an implicit SSH assumption.
- **(B) Narrow fit via `compute_node_inventory()` only.** Implement just the inventory override
  with an HTTP client, and leave `*_cmd()` methods raising "unsupported". Smaller diff, but power
  control / rack enumeration / service restart wouldn't work, and we'd be special-casing NICo at
  every call site — classic build-beside debt.

**Recommendation: (A).** This is exactly the "build-cost is not a tradeoff" case — NICo is the
second API-first manager (MAAS was the first crack), and new deployments are moving *to* it, so the
transport seam pays off structurally. Design doc needed before code (per `core-designs.mdc`: new
mechanism = design first).

### 3.2 Placement / `rack_mapping_mode`

`RACK_MAPPING_MODES = ("direct","translate","pool")` (`catalog/models.py:64`). NICo gives **explicit
placement** (Site→Rack→Tray, `placementInRack`, `RackIdentifier` labels) — so it's neither
hostname-parse (`direct`) nor SU-grouping (`translate`) nor placeless (`pool`). It's
**"placement comes authoritatively from the provider API."** Likely a **new mode** (e.g.
`provider`/`inventory`) added to the tuple + `native_status.py` handling + the two frontend unions,
*or* folded into the provider so the inventory records simply carry rack/tray and we reuse
`translate`-style grouping fed by the API instead of `scalable_units`. Decide in the design doc.
(The recent `pool` work is the template for *how* to thread a new mode through
backend + `/api/catalog/vocab` + `ProjectsView` + a dedicated renderer.)

### 3.3 Scheduler axis

NICo is **not** a scheduler, so most likely new DSX sites keep `scheduler = slurm` (or a future
`k8s`) on top of a NICo `cluster_manager`. **No new scheduler is strictly required for NICo.** If
DSX sites turn out to run K8s-native scheduling, that's a *separate* scheduler backend
(start from the `schedulers/stubs.py` author-ahead pattern → `_BUILDERS` dict in `factory.py`),
tracked independently from the NICo provider work.

### 3.4 NVCRE as a certification Tool (separate workstream)

NVCRE is **not** a provider — it belongs with the **catalog/Tools + results pipeline** first-class
citizens, next to our DCGM diagnostics. Integration = (a) a results reader that pulls
`certification report --results-file` JSON and/or scrapes the `nvcre_*` Prometheus metrics, mapping
`nodeResults[]`/`failedNodes[]` onto our node-health model; optionally (b) a Tool launch to kick off
a certification. This is lower priority than the provider and can land after. Its failed-node
signal is *read-only* — no node-state ownership conflict with us or BCM/NVSentinel.

### 3.5 Wiring checklist (per "every feature = API + UI")

**Backend**
- `backend/platforms/nico.py` — new `PlatformProvider` (HTTP/REST transport per 3.1).
- Register in `backend/platforms/__init__.py:get_platform()` (today an if/else — consider moving to
  a dict registry like schedulers already have, so platform parity is test-guarded too).
- Add `"nico"` (or agreed name) to `CLUSTER_MANAGERS` in `catalog/models.py` → flows to
  `Cluster.validate()` and `GET /api/catalog/vocab` automatically.
- Connection model: NICo needs **base URL + org + service-account JWT** — extend
  `ClusterConnection`/`Endpoint`/`ConnectionRole`; secret handling for the token.
- Inventory flow branches at `native_status.py` (`build_node_inventory`,
  `node_inventory_snapshot`) and `rack_status_engine.py` — the non-Slurm branch already routes
  through `compute_node_inventory()`, so NICo node discovery lands there.
- New `rack_mapping_mode` value (3.2) if we go that route.

**Frontend**
- Add the new manager value to the unions in `frontend/src/types/catalog.ts` &
  `frontend/src/types/api.ts`.
- `SetupWizard.tsx` manager RadioGroup + `wizardDraft.ts` defaults + `ProjectsView.tsx` editor;
  surface the URL/org/token connection fields using best-fit KUI inputs.
- If new placement mode: option in `ProjectsView` + a renderer (template:
  `NodePoolView.tsx`) + branch in `SingleRackView.tsx`.

**Tests to mirror:** `test_schedulers.py`, `test_cluster_scheduler_field.py` (parity guard),
`test_catalog.py`, `test_cluster_rack_mapping_mode.py`; frontend `SetupWizard.test.tsx`,
`wizardDraft.test.ts`. Add a NICo-provider test with a mocked REST client (point it at the
`make kind-reset` sandbox for an integration smoke test).

---

## 4. Open questions for Felix / the DSX team

1. **Product framing:** is "DSX orchestrator" = NICo + NVCRE + something closed-source, or exactly
   these two? (Neither repo mentions "DSX".)
2. **Scheduler on top:** for the new no-BCM deployments, what schedules workloads — Slurm on
   NICo-provisioned hosts, K8s, or a DSX-specific control plane?
3. **Do we read NICo REST or Core gRPC?** REST (service-account JWT) is far easier; confirm the
   sites expose REST and will issue us a service account + org scope.
4. **Site topology:** one NICo per site/cluster, or one central REST fanning in multiple sites via
   Site Agents? Affects how a `Cluster` record maps to a NICo `siteId`.
5. **NVCRE adoption:** will new sites actually run certifications we should surface, or is that out
   of scope for v1?
6. **Naming:** given the pending product rename, what do we call the provider value
   (`nico` vs `dsx` vs site-specific)?

---

## 5. Pointers

- NICo: `rest-api/openapi/spec.yaml` (164 paths), `rest-api/openapi/auth.md`,
  `docs/overview/scope-and-boundaries.md`, `rest-api/README.md` (kind targets).
- NVCRE: `README.md`, `docs/concepts/catalog.md`, `pkg/report/report.go`,
  `docs/operations/metrics.md`, `test/uat/`.
- Rack Status: `backend/platforms/base.py`, `backend/schedulers/base.py`,
  `backend/catalog/models.py`, `backend/native_status.py`, `backend/catalog/context.py`,
  `frontend/src/components/setup/SetupWizard.tsx`.
