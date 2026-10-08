# Rack Status — roadmap

**Living document.** Last updated **2026-10-08**. Horizons and statuses below are *inferred* from
open MRs, active branches, and the plan docs as of that date — **owners should correct priorities
and dates.** This is the one place to see every workstream at once; the per-feature detail lives in
the linked plan docs.

Status legend: **Shipped** (in `main` / a release) · **In flight** (active branch/MR) ·
**Spiked** (prototype or design done, not built) · **Planned** (written up, not started).
Current release line: **0.4.0** (cut 2026-10-01).

---

## Horizons at a glance

| Horizon | Headline items |
|---|---|
| **Now** (active) | DSX support v1 groundwork · pool/rackless cluster mode · identity & authz programme · nvrs CLI · deploy/TLS hardening |
| **Next** (1–2 cycles) | DSX v1 `nicocli` provider · multi-user / multi-cluster · PythonCM hardening · frontend token-expiry resilience |
| **Later** (envisioned) | DSX v2 REST + transport `forward()` + generic Credential provider · NVCRE readiness results · NVIS-P2P as a per-project capability · policy engine (authz Phase 3) |

---

## 1. Platform & scheduler providers
The two-axis provider model (`cluster_manager` × `scheduler`) and its growth. See
[EXECUTION_ARCHITECTURE.md](EXECUTION_ARCHITECTURE.md), [WORKFLOW_COMPOSITION.md](WORKFLOW_COMPOSITION.md).

- **Scheduler selection from the UI** — *Shipped.* Operators pick a cluster's scheduler in the
  editor (`feat/cluster-scheduler-selection`, merged).
- **PythonCM scheduler** — *In flight / stabilizing.* Marked live and selectable per workflow step
  (`docs/pythoncm-deployed`, `feat/pythoncm-scheduler`). Next: harden against the "head needs a
  helper script" prerequisite.
- **DSX (NICo) provider** — *Now (planning) → Next (v1).* v1 = `nicocli` exec provider mirroring
  MAAS; v2 = REST direct with a transport `forward()` tunnel + a generic composable
  `CredentialProvider`. Plan: [dsx/PLAN.md](dsx/PLAN.md) · research: [dsx/RESEARCH.md](dsx/RESEARCH.md)
  · `feat/dsx` (MR !62).
- **MAAS provider** — *Shipped* (reference for the API-CLI provider shape).

## 2. Clusters, placement & geometry
How a fleet is modeled when BCM placement is partial or absent. See
[RESOURCE_MODEL.md](RESOURCE_MODEL.md), [explanation/geometry-and-hostnames.md](explanation/geometry-and-hostnames.md).

- **Pool / rackless mode** — *In flight.* Flat node-pool view for clusters BCM never placed
  (Reno RTX6000). `pool-mode-rackless-clusters`; validation + NeMo-compat fixes
  (`fix/rackless-rtx-validation`, `fix/rtx-nemo-compatibility`) *Shipped*.
- **Multi-user / multi-cluster** — *Planned.* [multi-cluster/MULTI_USER_MULTI_CLUSTER_PLAN.md](multi-cluster/MULTI_USER_MULTI_CLUSTER_PLAN.md).
- **DSX placement mode** — *Later.* NICo returns authoritative rack/tray placement → likely a new
  `rack_mapping_mode` (see dsx/PLAN.md §v1.4).

## 3. Identity, authorization & audit
The auth programme. See [auth/AUTH_PROGRAMME_SCOPE.md](auth/AUTH_PROGRAMME_SCOPE.md),
[explanation/why-authorization-is-default-deny.md](explanation/why-authorization-is-default-deny.md).

- **OIDC provider + SRP login + tokens** — *In flight.* Inbound identity for Rack Status' own users
  (`backend/identity/`, `feat/auth-identity`).
- **Authorization (default-deny, scopes/roles)** — *In flight.* `backend/authz/`; resource model in
  [RESOURCE_MODEL.md](RESOURCE_MODEL.md).
- **Audit logging + reader** — *In flight.* [auth/AUDIT_PHASE1_VERIFICATION.md](auth/AUDIT_PHASE1_VERIFICATION.md).
- **Groups (Phase 2)** — *Planned.*
- **Policy engine (Phase 3)** — *Spiked.* Engine comparison + revocation-freshness design in
  [auth/PHASE3_POLICY_ENGINE_SPIKE.md](auth/PHASE3_POLICY_ENGINE_SPIKE.md).

## 4. Clients & UX

- **nvrs (Rust CLI)** — *In flight.* Hybrid command model over the Rack Status API, zero backend
  changes; prebuilt binaries shipped like the desktop installers (`cli`). Phase 0 done.
- **Frontend token-expiry resilience** — *Next.* Route every request through `apiFetch` so sessions
  survive token expiry (`fix/apifetch-everywhere`).
- **Progressive cluster status loading** — *Shipped* (`felixm/NVRACKSTAT-32`).
- **KUI-first UI** — *Ongoing.* MUI→KUI migration; see [UX_FRAMEWORK.md](UX_FRAMEWORK.md).

## 5. Deployment & hardening

- **HTTPS-only container + single config/ dir** — *Shipped* (`fix/https-only-container`); TLS under
  gunicorn (`refactor/api-module-migration`).
- **SSH host-key policy** — *In flight.* Opt-in `RACK_SSH_KNOWN_HOSTS`, honored on all paths
  (`fix/ssh-host-key-paths`).
- **Secret hygiene** — *Shipped.* Removed the shipped NVSwitch admin password
  (`fix/nvswitch-hardcoded-credential`).
- **Nscale NC (air-gapped x86_64)** — *In flight.* Offline image, `--no-build`, render-site-config
  contract. See clusters/ + deploy notes.
- **Consolidate a cluster's on-host paths under one home** — *Planned.* A cluster's scripts
  (`RACK_REMOTE_BASE_PATH`), logs (`connection.log_dir`), readable roots
  (`RACK_STATUS_ALLOWED_PREFIXES`), bug-report drop (`RACK_BUG_REPORT_REMOTE_DIR`) and build
  workspace (`NVIS_WORKSPACE_DIR`) are six independent settings with six independent defaults, today
  spread across three `/home` users on NscaleTX-NC (`rkoike`, `cmods`, `cmsupport`) — and the
  workspace, unset, falls through to a ghost `/home/cmsupport/workspace` that silently breaks the
  host-NCCL sweep. Model a single per-cluster **cluster home** (e.g. `/home/cmods`) that the others
  derive from by default, each still overridable, and surface the resolved set (exists? writable?) on
  one screen as the single source of truth. Design:
  [explanation/the-cluster-home-directory.md](explanation/the-cluster-home-directory.md). First
  NscaleTX-NC step: set `NVIS_WORKSPACE_DIR=/home/cmods/workspace` and pull the scattered paths onto
  `/home/cmods`.
- **Site validation campaigns** — *Ongoing.* mel01 (`mel01-validation`), Nscale 16K harvest
  (`harvest/nscale-16k`).

## 6. Integrations, observability & results

- **Grafana / Prometheus embeds** — *In flight.* [integrations/GRAFANA_PROMETHEUS_INTEGRATION.md](integrations/GRAFANA_PROMETHEUS_INTEGRATION.md).
- **Results / parsing pipeline** — *Ongoing.* [PARSING_RESULTS_ARCHITECTURE.md](PARSING_RESULTS_ARCHITECTURE.md).
- **NVCRE (cluster readiness) results reader** — *Later.* Surface certification pass/fail + failed
  nodes beside DCGM diagnostics; read-only. Separate workstream, see dsx/PLAN.md §4.
- **Multirack validation** — *Ongoing.* [clusters/MULTIRACK_VALIDATION_LEDGER.md](clusters/MULTIRACK_VALIDATION_LEDGER.md),
  [networking/](networking/) (NCCL/HCOLL/NVLS).

## 7. Bringup automation
- **Bringup automation integration** — *Planned/In flight.* [bringup/BRINGUP_AUTOMATION_INTEGRATION.md](bringup/BRINGUP_AUTOMATION_INTEGRATION.md).

## 8. NVIS-P2P
- **Dragonfly P2P console as a per-project capability** — *Later.* Merging INTO Rack Status (one
  instance per cluster, wizard flag, deploy-or-connect). Tracked in project memory; plan doc TBD.

---

## How to keep this current
- One line per workstream; link the plan doc, don't duplicate it here.
- Update the **Horizons** table when something moves Now→Next→Later or ships.
- When a feature lands in a release, flip it to **Shipped** and note the version.
- New feature → add a row here *and* a `docs/<feature>/` folder (see [README.md](README.md)).
