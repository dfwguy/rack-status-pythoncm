# Bringing NVIS-P2P into Rack Status as a per-project capability

*Explanation — why the Dragonfly peer-to-peer artifact console becomes a capability of a
Project/Cluster rather than a separate app, and how it lands on the first-class citizens instead of
beside them.*

## What NVIS-P2P is

NVIS-P2P is a [Dragonfly](https://d7y.io/)-based peer-to-peer distribution system for keeping a
validated container-image catalog (`.sqsh` files) synchronized across a large GPU fleet — up to
4032 nodes (14 SUs × 24 racks × 12 nodes). An app server runs the Dragonfly control plane (Manager,
scheduler, Redis, two seed peers, an S3-compatible `dfstore` object store) plus an operator console
and an operations API. Compute nodes run only three client binaries and two systemd units, with
back-to-source disabled so origin traffic funnels through the seeds instead of being multiplied
across thousands of nodes. The operator surface is the **Artifact Fleet** console.

It arrived as a standalone React + Vite + Kaizen SPA (`console/`) backed by a 820-line Python
stdlib operations server (`services/console-api/server.py`) and the Dragonfly Manager. That console
was deliberately built to match Rack Status: **identical** React 19, react-router 7, Vite 8, Kaizen
`@nvidia/foundations-react-core` 1.0.0, MUI 9, and lucide-react versions. Merging it is therefore a
re-homing, not a rewrite.

The old fork (`upstream/`), the deployment copy tied to one operator's home directory, and every
literal secret in the imported configs are **not** brought across. This is a fresh start.

## The shape of the decision

Two facts collide, and resolving the collision is the whole design:

- **P2P is single-tenant.** One P2P instance serves exactly one cluster. It lives on that
  customer's app server, next to that customer's compute fabric.
- **Rack Status is multi-tenant.** One running instance serves several projects and clusters, and
  the operator switches between them in the UI. This is the Project/Cluster first-class citizen.

So P2P cannot be "a module wired to a backend." It is a **capability that a Project/Cluster either
has or does not**, and when it has it, there is a dedicated P2P instance for that cluster that the
Rack Status backend addresses on the operator's behalf, scoped by the active project — exactly the
way scheduler and validation work is already scoped today.

This is why standalone operation was dropped. An earlier draft kept P2P runnable on its own; once
P2P became a per-project capability keyed off the Project/Cluster record and the active-cluster
overlay, a standalone shell would have had to re-implement project selection, auth, and the
gateway — rebuilding the citizens beside themselves, which is the one thing the architecture rules
forbid.

## How it maps onto the first-class citizens

Nothing here is a new mechanism. Each requirement extends a citizen that already owns the concern
(`core-designs.mdc`).

| Requirement | Citizen it extends |
|---|---|
| Every P2P call is scoped to one cluster | **Project/Cluster (3)** + config overlays (5). The active-cluster high overlay already writes `NVIS_PROJECT_ID`/`NVIS_PLATFORM_ID` and scopes scheduler calls; the P2P gateway resolves *that cluster's* P2P endpoints the same way. |
| One P2P instance per cluster; 2 projects → 2 instances | **Project/Cluster (3).** The instance's connection facts (Manager URL, ops URL, object-store URL, internal topology) live on the Cluster record's **high overlay**. Manager credentials live in the **token vault (11)**, never in a config file — which also closes the release audit's "literal secrets" blocker. |
| Enable/disable P2P per project | A feature field on the Project/Cluster record, surfaced as a step in `SetupWizard` and editable from the project editor. Off means the P2P nav group and gateway are inert for that cluster. |
| Enabling provisions an instance; if present, connect | A **`workflow_engine` (4)** reconcile run: deploy the control plane if absent, connect if present. Idempotent, durable across restart, progress visible. Runs over the cluster's connection (1). |
| Check for an existing P2P instance on create/edit | A **P2P preflight** modeled on `startup_gate` / `config_preflight` (13): report every fault at once (Manager unreachable, object store down, seeds unregistered, catalog missing, version drift) each with the action that fixes it — the same discipline as the certificate and identity-store checks at launch. |
| One login, no browser→Manager | **Auth (11).** The browser has a single Rack Status session. The gateway proxies the active cluster's Dragonfly Manager server-side, checks its `root` role once, and maps it to the `p2p:manage` authz action. No `jwt` cookie in the browser, no direct Manager calls. |
| Fleet fan-out across compute | **Connection/transport (1).** `transport.fanout` over the multi-hop chain, with the `MaxSessions` ceiling, retries, and multiplexing — not the private 32-worker SSH pool the stdlib server used. |
| Long-running operations (deploy/persist/sync/verify/…) | **Workflow engine (4).** Durable runs with progress and idempotency, not the `Popen` + global `MUTATION_LOCK` + JSON/log files the stdlib server used. |
| Per-cluster image catalog | A per-project release manifest (reviewed), in that project's layer of the deploy/script tree — distinct from the Tool/Platform catalog (2), which answers a different question. |

### Two "clusters," two layers

The word *cluster* means two different things and must not be conflated:

- The **Rack Status active cluster** (Project/Cluster) selects **which P2P instance** the operator
  is acting on.
- Inside that instance, Dragonfly has its own **scheduler-clusters and seed-peers** — an internal
  topology the P2P "Clusters" surface administers.

The active-cluster context picks the instance; the P2P Clusters view manages Dragonfly's internals
*within* it.

## Provisioning is a workflow, not a script

Because enabling the flag *fully provisions* the instance, the reproducible control-plane deployment
that the 2026-09-17 release gap audit flagged as missing (its P0 items on reproducible deploy,
service lifecycle, and safe orchestration) is built as a **`workflow_engine` reconcile run** rather
than as manual steps. That is the correct home for it regardless, and it inherits durability,
resumability, and visible progress for free.

`reconcile-p2p-instance` (triggered by the wizard toggle, for the target cluster):

1. **Preflight** the app server over the cluster connection: container runtime and compose,
   PostgreSQL, disk headroom, and on compute — architecture, page size, writable `/opt/containers`,
   non-interactive SSH and the required sudo, and the canonical origin path.
2. **Render** per-cluster config from templates with **no literal secrets**; credentials are
   generated and stored in the token vault, injected at deploy time.
3. **Materialize** the control plane (PostgreSQL, Redis, Manager, scheduler, seeds, object store)
   via compose on the app server; install client binaries and systemd units across compute via
   `transport.fanout`.
4. **Bootstrap** the Manager database, scheduler-cluster, seed-cluster, and seed-peer lifecycle —
   the exact bootstrap the audit found absent.
5. **Register** the instance endpoints onto the Cluster overlay; store Manager credentials in the
   vault.
6. **Verify** — the preflight again, now as acceptance.

The run is a reconcile: re-running detects existing healthy components and connects rather than
recreating, with a per-cluster idempotency key so a second wizard submission joins the run already
in flight. Disabling the flag runs the teardown/uninstall counterpart — the uninstall path the
audit also found missing.

## Where the code lands

Work lands on branch **`v3.1`**. Clean copy — no history imported from the separate repo.

- **Frontend** → `frontend/src/features/p2p/` (views, hooks, services). Folded into the single SPA,
  lazy-loaded per `initialBundle.test.ts`. Plugs into the documented extension points: a `grp-p2p`
  group in `Sidebar.tsx`, rows in `routes.ts`, and `VIEW_TITLES` + lazy imports + `<Route>`s in
  `App.tsx` under `#/p2p/*`. The imported console's own `AppThemeProvider`, `SessionProvider`,
  `RequireSession`, `AppShell`, and `apiFetch` are dropped — the host supplies all five.
- **Backend** → `backend/api/p2p_*.py` gateway blueprints (auto-discovered, behind the auth gate,
  with `@action("p2p:…")` audit vocabulary and throttling), plus supporting modules for
  fleet-coverage parsing, settings render/apply, and a Manager client. The stdlib `console-api` is
  retired.
- **Deploy assets** → `deploy/p2p/` (Dragonfly configs, compose files, systemd units, pinned
  binaries, catalog/inventory) consumed by the reconcile workflow.

## Sequencing

The read, UI, and preflight paths can be developed against a hand-deployed instance in parallel with
the installer, so nothing is blocked on the provisioning workflow landing.

1. **Import + frontend re-home** into `features/p2p/`, rendering under the Rack Status shell with the
   host theme, auth, and `apiFetch`, behind the per-project feature flag.
2. **Read gateway + per-cluster resolution:** `p2p_*.py` read paths resolve the active cluster's
   endpoints from the Cluster overlay; `root` maps to `p2p:manage`; Manager credentials come from the
   vault.
3. **P2P preflight** wired into the wizard and project editor.
4. **Operations on the engine:** move mutating operations onto `workflow_engine` + `transport.fanout`;
   retire `console-api`.
5. **Provisioning + teardown workflows:** the wizard toggle drives them. This is where the reproducible
   installer is built.

## What this deliberately does not do

- It does not keep a standalone P2P app or a second frontend build.
- It does not let the browser talk to Dragonfly Manager, or carry a Manager `jwt` cookie.
- It does not add a job dict, a private SSH pool, or a bespoke lock — those are the engine and the
  transport citizen.
- It does not import the Dragonfly fork, the operator-home deployment copy, or any literal secret.
