# DSX orchestrator support — v1 / v2 plan

**Date:** 2026-10-02 · **Status:** approved direction, pre-implementation
**Companion:** [RESEARCH.md](RESEARCH.md) (what NICo/NVCRE are, how to run them, API/CLI surfaces)

This plan records *where we are heading* and *why*, so the staged build is legible. It covers
adding **DSX** (NVIDIA Infra Controller — NICo) as a platform provider in Rack Status, in two
deliberate phases, plus the generic **Credential provider** the second phase introduces.

> **Scope reminder (from RESEARCH.md).** NICo is the *bare-metal lifecycle substrate* — it owns
> inventory / health / power / rack topology, **not** scheduling. So DSX enters on the
> **`cluster_manager` (PlatformProvider)** axis; the `scheduler` axis stays Slurm/K8s. NVCRE
> (cluster readiness / certification) is a **separate** workstream on the catalog/Tools + results
> pipeline, tracked in §4 — not part of v1/v2 here.

---

## The two phases at a glance

| | **v1 — `nicocli` over SSH** | **v2 — REST API direct** |
|---|---|---|
| Goal | Fastest path to a working DSX provider | First-class, binary-free integration |
| Transport | **Reuses the exec hop chain as-is** | **New `forward()` TCP-tunnel primitive** on the chain |
| Protocol | Exec `nicocli … -o json`, parse stdout | HTTP/JSON client against NICo REST (`/v2/org/{org}/nico/…`) |
| Auth | `nicocli` owns login + refresh on the exec host | **Generic Credential provider** → OIDC token acquire/refresh |
| New mechanisms | **None** — `nico.py` mirrors `maas.py` | `forward()`, `CredentialProvider`, `NICoRestClient` |
| Deploy prereq | `nicocli` installed + configured on the exec host | Network reachability + a service-account client |
| Contract risk | CLI `-o json` shape (version-coupled) | OpenAPI `/v2/` (declared, versioned) |

Both phases present the **same `NICoProvider` to the rest of Rack Status**; the transport/auth
mechanism behind it is an internal mode. Moving v1→v2 is a config switch plus swapping the
provider's data-access layer — not a rewrite of inventory/health/power mapping or any UI.

---

## Phase v1 — `nicocli` exec provider (fastest)

The `PlatformProvider` abstraction (`backend/platforms/base.py`) was built for exactly this: emit a
command, run it on a host over the transport, normalize the output. `MAASProvider`
(`backend/platforms/maas.py`) already proves the "parse JSON from a vendor CLI" shape. NICo v1 is a
near-mirror.

### Work items
1. **`backend/platforms/nico.py` — `NICoProvider(PlatformProvider)`**
   - Command builders emit `nicocli` invocations:
     - `compute_node_inventory()` ← `nicocli machine list -o json` (+ `rack list`, `tray list` for
       placement/health enrichment).
     - `list_racks_cmd()` ← `nicocli rack list -o json`.
     - `device_status_cmd()` ← machine status / `machine health-report`.
     - `device_power_cmd()` ← `nicocli machine power …` (or `rack power`).
     - `physical_rf0_cmd()` ← BMC IPs from machine metadata (`includeMetadata`).
   - `compute_node_inventory()` parses the JSON into our node record
     `{id, rack, status, ip, scheduler_state}`, enriched with NICo's richer fields (tray slot,
     aggregate health alerts). Map `MachineStatus` → our states (note: **no "drain"**; `Maintenance`
     is the closest — see RESEARCH.md §4).
2. **Registration & vocab**
   - Add `"dsx"` (provider value — name TBD, see open-Q) to `CLUSTER_MANAGERS`
     (`backend/catalog/models.py`). Flows automatically to `Cluster.validate()` and
     `GET /api/catalog/vocab`.
   - Add to `get_platform()` (`backend/platforms/__init__.py`). **Consider** converting its if/else
     to a dict registry so platform parity is test-guarded like schedulers already are
     (`test_cluster_scheduler_field.py`).
3. **Connection wiring — reuse, don't invent**
   - Add a **`nico` `ConnectionRole`** (like the existing `bcm` role) pointing at the endpoint where
     `nicocli` runs, reached through the normal `Endpoint.via` hop chain. No new transport concept.
   - `nicocli` is configured in **service-account / `client_credentials` mode** on that host, so it
     re-mints tokens on demand and never hits a stale-refresh wall. Its secret lives in that host's
     `~/.nico/config.yaml` — **off our catalog**, mirroring "the SSH key is a path, not material."
4. **Placement / `rack_mapping_mode`** — NICo returns authoritative rack/tray placement, which is
   neither `direct` (hostname-parse) nor `pool` (placeless). Decide between: (a) a **new mode**
   (`provider`/`inventory`) where placement comes from the provider payload, or (b) feeding the
   existing `translate` grouping from the API instead of `scalable_units`. Prototype against real
   `nicocli` JSON before choosing.
5. **API + UI** (per "every feature = API + UI")
   - Frontend: add the provider value to the unions in `frontend/src/types/catalog.ts` &
     `api.ts`; add to `SetupWizard.tsx` manager RadioGroup + `wizardDraft.ts` defaults +
     `ProjectsView.tsx` editor; surface the `nico`-role endpoint fields with best-fit KUI inputs.
   - If a new placement mode: option in `ProjectsView` + a renderer (template `NodePoolView.tsx`) +
     branch in `SingleRackView.tsx`.
6. **Tests & fixtures** — capture real `nicocli … -o json` from the `make kind-reset` sandbox as
   fixtures; mirror `test_schedulers.py` / `test_catalog.py` / `test_cluster_rack_mapping_mode.py`;
   frontend `SetupWizard.test.tsx` / `wizardDraft.test.ts`.

### v1 exit criteria
A cluster with `cluster_manager = dsx` renders its NICo fleet (nodes, racks, status, health) in Rack
Status, with power actions working, scheduler still Slurm/K8s. Validated end-to-end against the
mock-Core kind sandbox.

### v1 prerequisites / risks
- **`nicocli` must be installed + logged-in (service-account) on the exec host** — a deployment
  prerequisite, echoing the pythoncm "the head needs a helper script" gotcha. Route to Felix.
- Confirm `nicocli` exposes `-o json` for machine / rack / **health-report** / power. Any REST-only
  datum falls back to a direct call (early taste of v2).

---

## Phase v2 — REST API direct (first-class)

v2 removes the `nicocli` binary dependency and talks to NICo REST directly. It introduces two
reusable primitives — both designed to be **generic**, not NICo-specific.

### v2.1 Transport `forward()` — reachability via composition
The transport layer composes N-hop **exec** today (`Connection.hop()` →
`HopExecutor` emits nested `ssh … bash`). v2 adds a **peer capability**: open a local TCP
port-forward *through the same composed hop chain* to a remote `host:port`
(`ssh -J h1,h2,… -L 127.0.0.1:<local>:<nico-host>:8388 -N`), reusing the existing per-leg
host/user/key and host-key policy (`backend/transport/ssh.py:49-104`). This is the clean way to
reach a NICo REST endpoint that sits behind the same bastions as the nodes — and it's a general
transport feature, not a DSX one. The direct case (NICo reachable from Rack Status) opens no tunnel.

> Design note: `forward()` is additive — it sits beside `.hop()`/`.run()`, does **not** change the
> `Executor` exec/filesystem/PTY contract, and does **not** try to make HTTP an `Executor` leaf
> (that was rejected in design — see RESEARCH.md §3 / transport deep-dive: the leaf contract is
> shell-shaped and HTTP satisfies ~30% of it and cannot compose).

### v2.2 Credential provider — a composable auth component (chosen over `nico_*` fields)
Rather than bolt vendor-specific `nico_client_id` / `nico_client_secret` fields onto
`ClusterConnection` (which bakes one orchestrator's name into the schema and forces a refactor when
the next orchestrator arrives), introduce a **generic `CredentialProvider`** that composes with
Transport the same way executors/decorators do.

- **Concept:** a `CredentialProvider` resolves a *stored secret reference* (a mounted path, like
  `RACK_SSH_KEY` — never inline material) into a *live auth artifact* for a connection.
  - `kind = ssh_key` → yields a private-key path for `ssh -i` (today's SSH behavior, now named).
  - `kind = oidc` → yields a **bearer token**, acquired + cached + **refreshed ahead of expiry**
    from a remote IdP's token endpoint; force-refresh on `401`. Primary grant `client_credentials`
    (service account), secondary `password`; `authorization_code` is interactive-only, out of scope.
- **Why composable:** transport already proved composition (hops, env-forward, sudo decorators).
  A credential is the auth analog — it attaches to an endpoint/role and is resolved lazily at
  connection time, cached, and invalidated on config change (mirroring `invalidate_connection()`).
  A future orchestrator with a different auth scheme adds a `kind`, not a new column.
- **Catalog shape:** `ConnectionRole`/`Endpoint` gains a `credential` reference (id → a
  `Credential` entry carrying `kind` + a secret *path/ref* + non-secret params like `token_url`,
  `client_id`, `scopes`). Validation lives next to the existing endpoint/cycle checks.
- **Net-new vs identity/:** `backend/identity/` is an **inbound** OIDC *provider* (mints tokens for
  our users); it has no outbound client. The `oidc` credential's acquire/refresh loop is net-new
  (its JWT format helpers may be reusable for decoding, not for the flow).

### v2.3 `NICoRestClient` + provider mode
- `NICoRestClient` holds the base URL (direct, or the `127.0.0.1:<local>` from v2.1), asks the
  `oidc` `CredentialProvider` for a bearer, sets `Authorization: Bearer`, returns parsed JSON for
  `/machine`, `/rack`, `/machine/{id}/health-report`, power, stats.
- `NICoProvider` gains an internal **data-access mode** (`cli` | `rest`). The inventory/health/power
  → our-model mapping is written **once** and shared; only the fetch differs. So v1→v2 is a mode
  flip + the client, not a reimplementation, and a site can pin either mode.

### v2 exit criteria
A `dsx` cluster works with **no `nicocli` on any host** — REST direct, tokens via the `oidc`
Credential provider, reachable through `forward()` when behind bastions. SSH connections
transparently migrated to the `ssh_key` Credential kind with no behavior change.

---

## 4. NVCRE (cluster readiness) — separate workstream (noted, not scheduled here)
Certification results are **not** a provider. They belong on the catalog/Tools + results pipeline,
beside our DCGM diagnostics: a reader that pulls `nvcrectl certification report --results-file`
JSON and/or scrapes `nvcre_*` Prometheus metrics, mapping `nodeResults[]`/`failedNodes[]` onto our
node-health model; optionally a Tool launch to kick off a run. Read-only (NVCRE never mutates node
state), so no ownership conflict. Plan separately after v1.

---

## 5. Sequencing
1. **Sandbox + fixtures** — `make kind-reset`; capture real `nicocli … -o json` for machine/rack/
   health/power; confirm command coverage (resolves v1 open items).
2. **v1 provider** — `nico.py`, vocab, `nico` role, placement-mode decision, wizard/UI, tests.
3. **v1 hardening** — real-site prereq doc (`nicocli` install/login), fallback notes.
4. **v2.1 `forward()`** — transport port-forward primitive + tests (general feature; useful beyond DSX).
5. **v2.2 Credential provider** — generalize SSH key → `ssh_key` kind; add `oidc` kind + refresh loop.
6. **v2.3 REST client + mode flip** — `NICoRestClient`, provider `rest` mode, migrate a site.
7. **NVCRE reader** — separate workstream (§4).

## 6. Open questions (some for Felix / DSX team — see RESEARCH.md §4 too)
- **Provider value name:** `dsx` vs `nico` vs site-specific? (Product rename pending — avoid
  baking a soon-stale name; this is *why* we chose a generic Credential provider over `nico_*`.)
- Does `nicocli` cover machine/rack/health/power with stable `-o json`? (gates v1 §1)
- Is NICo REST directly reachable from Rack Status, or behind the node bastions? (gates whether
  v2.1 `forward()` is required day one)
- One NICo per site/cluster vs. central REST fanning in multiple sites via Site Agents? (maps a
  `Cluster` record to a NICo `siteId`/org)
- Service-account (`client_credentials`) available for us on target sites? (preferred for both phases)
