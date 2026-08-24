# NVIS Rack Status

## PLC-L1: Basic PLC: Software Architecture and Design Document

## Documentation Control

| Item | Description |
| :---- | :---- |
| Title | NVIS Rack Status — Software Architecture and Design Document |
| Author(s) | Rafael M. Koike (NVIS OVA team), reconstructed with Agentic PLC |
| Revision | Aug. 24, 2026 |
| State | Development |
| Reviewed in | GitLab (`nvis/nvis-rack-status`) |
| nSpect | NSPECT-S57Q-EI19 |
| Input SRD | `.plc/requirements/SWE-PLC-L1-002-BasicPLC-SRD-NVIS-RACK-STATUS.md` |
| PLC-L1 SADD Template Revision | SWE-PLC-L1-001-BasicPLC-SADD-TMPL |

Revision History

| Date | Author | Summary of Change |
| :---- | :---- | :---- |
| Aug. 24, 2026 | Agentic PLC | Initial reconstruction from the shipped source tree (134 backend modules, 38 API modules, 218 routes), `.cursor/rules/core-designs.mdc`, `docs/EXECUTION_ARCHITECTURE.md`, `docs/PARSING_RESULTS_ARCHITECTURE.md` and the `auth-audit-logging` brief. Skill: plc-design-reconstruction; Timestamp: 2026-08-24T15:30Z; Input ref: NSPECT-S57Q-EI19. Human decision: pending; Reviewed by: pending |

---

# Introduction

## Purpose and Scope

This document describes the architecture and design of NVIS Rack Status as built. Rack Status is a
**standalone product** under NSPECT-S57Q-EI19 — a complete solution deployed at customer clusters so
NVIDIA NVIS and its partners can bring up NVIDIA clusters — so this is a product-level SADD.

It covers the Flask backend, the React single-page application, the execution transport, the
catalog, the results pipeline, the durable registries and the audit subsystem. **The frontend is in
scope**: it is part of the application rather than an external client, and has design of its own
(see *Frontend Design* under Static Design). It does not cover the cluster software being exercised
(BCM, Slurm, NeMo, DCGM) except at the interface.

The product is developed collaboratively by NVIS Solutions Architects and accepts contributions by
merge request. That has an architectural consequence rather than merely a procedural one: the
correct extension point must be obvious to a contributor arriving without context, which is the
reason the citizen model below is written down as a rule and enforced by tests.

The design is organised around a small number of **first-class citizens** — abstractions that own a
concern for the whole product. The governing rule is that a new capability extends the citizen that
owns its concern rather than adding a parallel structure beside it, because a second structure doing
the same job drifts from the first and the drift is discovered by a customer rather than by us.

## Assumptions

As stated in the SRD: a supported cluster manager and Slurm are present; the application reaches
the cluster over SSH or runs adjacent to it; a persistent data directory exists; operators are
trusted staff; telemetry, where present, comes from VictoriaMetrics and Loki.

## Constraints

Air-gapped operation with no runtime internet dependency; access over an SSH tunnel; one repository
serving several customers with no hard-coded site specifics; the deployed container's bind mounts
and named volume shadow build-time artifacts; head-node `sshd MaxSessions` bounds relayed fan-out;
Python 3.12 / React 19 / Kaizen UI.

## Dependencies

Internal: none outside this repository. External: BCM `cmsh`/CMDaemon, Slurm binaries,
VictoriaMetrics, Loki, Grafana, DCGM, NCCL, NeMo/DGXC recipe containers, OEM BMC/Redfish.

## Definitions, Acronyms, Abbreviations

As per the SRD. Design-specific additions:

| Term | Description |
| :---- | :---- |
| Citizen | First-class abstraction owning a concern product-wide |
| Chokepoint | The single point every instance of a class of operation must pass through |
| Overlay | A layer of configuration values with defined precedence |
| Hop | One SSH leg in a composed multi-leg connection |
| Fan-out | Executing the same work across N nodes with bounded concurrency |

## References

`.cursor/rules/core-designs.mdc`; `docs/EXECUTION_ARCHITECTURE.md`;
`docs/PARSING_RESULTS_ARCHITECTURE.md`; `docs/RACKSTATUS_USER_GUIDE.md`;
`.plc/briefs/auth-audit-logging-brief.md`; `docs/AUTH_PROGRAMME_SCOPE.md`;
`docs/MULTI_USER_MULTI_CLUSTER_PLAN.md`.

---

# Architectural Details

**High-level description**

```
                    Browser (React 19 SPA, Kaizen UI, Vite bundle)
                                     │  HTTPS (TLS >= 1.2), SSH tunnel in practice
                    ┌────────────────▼─────────────────────────────────┐
                    │ Flask application (backend/app.py)               │
                    │  composition root: ~970 lines, 3 routes          │
                    │  auth gate · middleware pairs · SPA catch-alls   │
                    └──┬────────────────┬───────────────┬──────────────┘
                       │                │               │
      ┌────────────────▼───┐   ┌────────▼────────┐   ┌──▼─────────────────┐
      │ backend/api/       │   │ audit/          │   │ catalog/           │
      │ 38 modules,        │   │ chokepoint,     │   │ Platform · Tool ·  │
      │ 218 routes,        │   │ actions, sink   │   │ Project/Cluster    │
      │ auto-discovered    │   └─────────────────┘   │ (TinyDB + overlays)│
      └────────┬───────────┘                         └────────────────────┘
               │ imports (one direction only)
      ┌────────▼──────────────────────────────────────────────────────────┐
      │ mechanism modules                                                  │
      │  host_exec + transport/   rack_status_engine   results_ingest      │
      │  workflow_engine          validation_activities  rack_locks        │
      │  paths · naming · node_targets · config_env · slurm_query · ...    │
      └────────┬──────────────────────────────┬────────────────────────────┘
               │ platforms/ · schedulers/      │ nvis_logscan · universal_parser
      ┌────────▼───────────┐         ┌─────────▼──────────────┐
      │ cluster head node  │         │ VictoriaMetrics · Loki │
      │ cmsh · Slurm · pdsh│         │ Grafana                │
      │ bin/scripts/*      │         └────────────────────────┘
      └────────────────────┘
```

**Static and dynamic aspects**

Statically the dependency graph is one-directional: `app.py → api/<domain>.py → core.py`. `app.py`
is a composition root that owns the Flask object, startup bootstraps, the auth gate, two middleware
pairs and the SPA catch-alls; it holds three routes. Adding an endpoint means adding a file to
`backend/api/`, which the loader discovers at startup — nothing else is edited. An `api/` module
never imports `app`.

Dynamically the product is a request/response API over a set of long-running background
operations. Anything that outlives a request is owned by the workflow/state-machine citizen: it
returns an identifier immediately, runs on a background thread, persists under `var/`, and exposes
progress and per-step output. Fleet-wide reads are served from a cross-process leased snapshot
rather than re-scanned per request.

**Key assumptions and limitations**

- The head node is a bottleneck by construction when hops are relayed through it; its
  `MaxSessions` (default 10) bounds concurrency. A direct node route exists as an opt-in where the
  network permits it.
- The catalog is a document store, not a relational one; cross-record integrity is enforced in
  code, not by the store.
- The product is single-tenant per running instance in terms of identity: an operator label is
  self-reported until the identity phase lands, so today's activity ledger is advisory rather than
  authenticated. This is the explicit motivation for the auth programme.

---

# Design Details

## Design Alternatives

| Decision | Chosen | Alternative rejected | Why |
| :---- | :---- | :---- | :---- |
| Endpoint organisation | 38 auto-discovered `api/` modules | Keep routes in `app.py` | `app.py` reached 20,000 lines and 135 routes; every feature touched it, so parallel work collided by construction and a whole feature was once lost uncommitted on a customer deployment |
| Long-running work | One durable workflow/state-machine runner | Per-feature in-memory job dicts | Four such dicts existed; each loses state on deploy, and a 2,000-node sweep whose progress nobody can see is indistinguishable from a hang |
| Tools and presets | Catalog documents with platform defaults | Frozen registry in Python | A code constant cannot be edited by an operator, ships one site's absolute paths everywhere, and needs a deploy to add a test |
| Result parsing | Single bounded pass, structured output preferred | Multi-pass shell scan with head/tail sampling | Logs reach hundreds of MB; sampling silently missed results in the middle of files, and the scanner replaced ~16 full-file passes |
| Report formats | One dataset, several renderers | A generator per format | Parallel generators drift on the first added column, and a report that contradicts itself is worse than a missing one |
| Policy engine (planned) | Interface plus RBAC model now, engine later | Adopt AWS Cedar immediately | Cedar has no NVIDIA-maintained Python SDK; the viable binding needs a vendored wheel for air-gapped sites, which is a conscious dependency decision rather than a detail |
| Audit event emission | One `after_request` chokepoint | Per-handler emit calls | Provenance you have to remember to add is provenance you will forget to add |
| TLS certificates | Self-signed by default, internal CA opt-in | ACME/Certbot with dynamic DNS | Operators reach the UI through an SSH tunnel and air-gapped sites cannot reach an ACME server |

## Static Design

### Component inventory

| Citizen | Modules | Owns |
| :---- | :---- | :---- |
| 0. API surface | `backend/api/` (38) | Where an endpoint lives; auto-discovery by `bp` or `install(app)` |
| 1. Connection / transport | `host_exec.py`, `transport/` | Every command that leaves the process: local/SSH executors, multi-hop chains, fan-out, retries, sudo, ControlMaster |
| 2. Catalog / Tools | `catalog/` (`models`, `store`, `context`, `api`) | What the product can run: Platform, Tool, Project/Cluster documents |
| 3. Project / Cluster | `catalog/models.py`, `catalog/context.py` | Which site, which nodes, which customer; high and low configuration overlays |
| 4. Workflow / state machine | `workflow_engine.py` | How long work runs: identifiers, background execution, persistence, fan-out steps |
| 5. Configuration resolution | `config_env.py` + settings registry | Where a knob comes from, and in what precedence |
| 6. Platform / scheduler providers | `platforms/`, `schedulers/` | Which cluster manager, which queue |
| 7. Results pipeline | `nvis_logscan.py`, `universal_parser.py`, `results_ingest.py` | Raw tool output becoming metrics |
| 8. Durable registry and locks | `validation_activities.py`, `rack_locks.py` | The system of record and exclusive access |
| 9. Naming | `nvis_id_templates.py`, `naming.py`, `node_targets.py` | Rack id versus real hostname |
| 10. Artifacts and reports | `cluster_report.py`, `cluster_report_pdf.py`, `node_report.py` | One dataset, many renderers |
| 11. Auth | `web_auth.py`, `token_vault.py`, `rack_web/pam_*` | One gate |
| 12. Frontend contexts | `ActiveClusterContext`, `RackCommandsStoreContext`, `ThemeContext` | Shared SPA state |
| (new) Audit | `audit/` (`__init__`, `actions`, `sink`, `request_hook`) | The durable trail of who did what |

### Frontend Design

The SPA is React 19 + TypeScript, built by Vite, and is served as a hashed bundle from
`static/spa/` by the same Flask process that serves the API — one origin, which is why no CORS
headers are emitted anywhere. Inventory: 30 views, 121 components, 37 library modules, 13 service
clients, 7 context pairs.

**Shared state has an owner.** Each cross-view concern is a context, split deliberately into a
`…Context.ts` (the contract) and a `…Provider.tsx` (the implementation) so a consumer imports the
contract without pulling in the provider's dependencies:

| Context | Owns |
| :---- | :---- |
| `ActiveClusterContext` | Active project/cluster and the switcher; the client-side mirror of catalog citizen 3 |
| `RackCommandsStoreContext` | Centralised async command tracking across views |
| `RackCommandContext` | The in-flight command for a single rack surface |
| `RackLocksContext` | Advisory lock state, mirroring citizen 8 |
| `AuthContext` | Session state and the gate's view of the operator |
| `ThemeContext` | Dark/light mode, and the Kaizen↔MUI bridge |
| `AIChatContext`, `HelpPanelContext` | Assistant and contextual help surfaces |

The design rule is that a view consumes these rather than keeping local state that duplicates them.
Local command tracking beside `RackCommandsStoreContext` is how two panes come to disagree about
what is running, which is a bug class rather than a style preference.

**Design system.** NVIDIA Kaizen UI (`@nvidia/foundations-react-core`) is the primary component
library; MUI is retained only for complex existing widgets with no clean Kaizen equivalent (the rack
table, the sidebar drawer, the app-bar shell). The two coexist through a bridge: the Kaizen
`ThemeProvider` is the outer wrapper and the MUI theme is synchronised from it in
`context/ThemeContext.tsx`, so one mode toggle drives both. Icons are Lucide React exclusively.

**Kaizen assets are vendored, and that is an air-gap requirement, not a convenience.** The npm
package does not ship the CSS, and the CDN is unreachable from a customer cluster behind an SSH
tunnel, so `base.css`, `components.css` and 16 `woff2` fonts live in `src/styles/kaizen/` and are
imported by `main.tsx` before the app's own styles. The `@font-face` rules are rewritten to
relative paths on every version bump; missing that step leaves the CSS looking correct while fonts
silently fall back.

**The workflow designer keeps one source of truth.** Mermaid source text is authoritative; the
React Flow canvas reads from it and writes back to it, via a parser that infers each node's type
from the Mermaid shape (automated = rectangle, manual/event = stadium, decision = diamond). Two
consequences are load-bearing: the inferred `stepType` must survive a save or nodes lose their
appearance on reload, and deleting an edge must re-insert any node that was defined inline on that
edge as a standalone declaration, or the node disappears with the edge.

**Failure presentation.** The SPA renders absent data as absent. Where a dependency cannot be shown
at all — a Grafana embed blocked by mixed content when the dashboard is HTTP and the SPA is HTTPS —
it renders an explanatory banner rather than an empty frame, because a blank iframe is
indistinguishable from a broken deployment.

**Build coupling.** `static/spa/` is a gitignored build artifact. `index.html` and its hashed
`assets/` must always come from the same build: a pinned `index.html` from another build 404s every
chunk and renders a black screen that reads as "slow". This is why deployment verification checks
that every asset the served page references actually resolves, and why the backend answers a missing
bundle with an explicit 503 page rather than a 500.

### Configuration Data

Configuration resolves through one accessor set (`_eget`, `_eget_int`, `_subprocess_env`) with a
defined precedence: **active cluster high overlay → job env overrides → process environment →
platform low overlay**. The high overlay carries cluster identity, naming and connection; the low
overlay carries platform geometry defaults and per-tool parameter defaults. Overlays are also
materialised into `os.environ` so that modules reading the environment directly stay consistent.

A bare `os.environ.get` in new code silently opts out of the overlay, which is how a setting comes
to work on one cluster and not another; the accessors exist to prevent that. Settings are declared
in a registry so the defaults are testable, and non-prefixed knobs are forwarded across SSH by an
explicit allowlist. Full reference: `docs/ENVIRONMENT.md`.

### External Interface and Specification

| Interface | Direction | Specification |
| :---- | :---- | :---- |
| HTTP/JSON API, 218 routes | inbound | Browsable API reference generated from the URL map; `job_id` retained as the canonical `activity_id` for compatibility |
| WebSocket `/ws/terminal` | inbound | WSGI middleware above the router; authenticated and origin-checked before upgrade |
| SSH to head node / nodes | outbound | Composed hops with ControlMaster multiplexing; env forwarding by allowlist |
| Cluster manager | outbound | `cmsh` (BCM) or `maas-cli` (MAAS) behind a provider interface; an optional read-only `cmsh` allowlist wrapper constrains it to four inventory queries |
| Slurm | outbound | Absolute binary paths, individually configurable |
| VictoriaMetrics | outbound | Read-only proxy; PromQL selectors built from an allowlisted label pattern |
| Grafana | outbound | Deep links by dashboard uid/slug with template variables; optional `/d-solo/` embeds |
| Container images | outbound | Version-pinned per platform `tool_param_defaults` |
| **Data lake (`nvis-lakewriter`)** | **outbound** | `POST /v1/artifacts` with a JSON envelope: `run_id`, `cluster`, `job_id`, `harness`, `captured_at`, `resolve_context`, and `artifacts[]` of `{artifact_path, encoding: utf8\|base64, content, sha256, size_bytes}`. Digest is computed over raw bytes pre-encoding so the writer can verify either encoding. Carries run provenance (parsed result, raw log, defining inputs) and — only when opted in — a per-request record. See *Egress design* below |

### Egress design (`lake_sink.py`) — REQ-48, REQ-49, REQ-50

This is the product's only outbound data path, which makes its design a security design. Its purpose
is provenance: a busbw or power figure published six months ago must remain traceable to the exact
log and inputs that produced it, which SQLite results and head-node log files could not guarantee.

Four controls, each answering a specific way the path was found to be wrong on 2026-08-24:

- **Opt-in.** `RACK_LAKE_CAPTURE` defaults to `off`. Request capture previously defaulted to
  `actions`, so a site that had never heard of the lake still posted ~100 mutating endpoints to
  `host.docker.internal:9410`; the only thing making that safe was that nothing usually listens
  there, which is an accident of the environment rather than a control.
- **Credential paths are refused, not filtered.** `NEVER_CAPTURE` is checked before a body is read
  and cannot be widened by `RACK_LAKE_CAPTURE=all`. `POST /api/login` carries the password in its
  body; `PUT /api/vault/nvftp-token` carries a token. This mirrors the audit trail's choice to drop
  query strings rather than filter them — a credential is invisible to key-name redaction as often
  as not, so the reliable control is not to take the data. Enforced twice, at the capture predicate
  and again in `scrub_record`, so a second caller building its own record cannot bypass the first.
- **Redaction by key and by value** on whatever is captured, reusing `logging_config`'s env-sourced
  secret list rather than growing a second one — a parallel list would drift, and silently.
- **Verified TLS with no escape hatch.** `RACK_LAKE_CA_BUNDLE` supports a self-signed or internal-CA
  lake; there is deliberately no skip-verify switch, because an unverified TLS connection to a
  provenance store authenticates nothing while looking secure in review. Switching to `https://`
  without a trusted certificate fails rather than downgrading, and an unreadable bundle path raises
  rather than falling back to system trust.

**Availability.** Request capture enqueues onto a bounded queue drained by a daemon thread. It runs
inside `after_request`, so a synchronous POST would place the lake's availability directly in the
operator's latency path — up to `RACK_LAKE_TIMEOUT` (default 20 s) per captured request across ~100
endpoints, which would present as a dashboard that simply feels broken. A full queue drops and
**counts**, surfaced by `GET /api/lake/health`, because the product's own absent-versus-zero rule
applies to its own telemetry. `push_run` stays synchronous for the results-ingest path, which needs
the honest status: a silent provenance failure is the exact class of bug this platform exists to
catch.

This is an interface to a system outside this repository, owned by a colleague, so the payload schema
above is the contract as this side understands it; the writer owns its own validation.

### Dependencies (design-level)

`api/` depends on mechanism modules and on `core.py`; mechanism modules depend on `transport/` and
on each other only through the module that owns the shared concern. `core.py` imports neither Flask
nor `app` (verified: zero matches), which is what keeps the dependency one-directional.

**One module still `import app`s inside a function body** to dodge a cycle —
`agent/registrations.py` — and that is recorded debt, not a pattern to copy. An earlier revision of
this section said "two modules" and the governing rule named `workflow_engine.py` and `bcm_query.py`;
both are now clean, so the debt was largely paid without the record being updated. Corrected
2026-08-24 from a word-boundary search across all 130 non-test backend modules.

### Integration Validation Plan

Deployment is verified by an automated gate (`bin/verify-deploy.sh`) that fails unless the container
is running rather than crash-looping, every backend module imports, no module in the container is
staler than the tree, and every asset the served page references resolves. A separate gate
(`bin/check-tracked-tree.sh`) proves the committed tree imports and passes its tests using only
files git tracks, which is what catches an import committed before the module it names, or a test
that depends on an unbuilt frontend.

## Dynamic Design

### Functionality and Behavior

A request arrives, passes the auth gate, is routed to an `api/` module, which resolves
configuration through the overlays, resolves targets through the naming citizen, and executes
through the transport citizen. Anything long-running is handed to the workflow runner and answered
immediately with an identifier. Every mutating request emits exactly one audit event on the way out.

### Control Flow

```
request ──► auth gate ──► before_request: audit timer, lake capture
        ──► api/<domain> handler
              ├─ config_env overlays ──► catalog/context (generation-cached)
              ├─ naming/node_targets ──► real hostnames
              ├─ rack_locks.claim    ──► advisory exclusivity
              ├─ host_exec/transport ──► head node or per-node fan-out
              └─ validation_activities.record ──► durable registry
        ──► after_request (Flask runs these in REVERSE registration order):
              lake capture ──► security headers ──► no-store ──► audit emit ──► response compression
```

Registration order of the `after_request` handlers is load-bearing: Flask runs them in reverse
order of registration, so the audit hook is registered early in order to run late — after the
security-headers and no-store hooks have decorated the response.

It is **not** last, and it is worth being precise because the natural assumption is wrong.
`flask_compress` is registered before the audit hook and therefore runs after it, rewriting the body
encoding, `Content-Encoding`, `Content-Length` and `Vary`. Nothing the audit event records is
affected (status, principal, action, resource), but the latency it measures excludes compression.
The accurate invariant is: **last among the application's own handlers**. The lake capture at the
head of the chain reads the response body before the security headers exist on it.

### Data Flow

```
tool on node ──► log file on shared/remote FS
   ──► nvis_logscan.py (single pass, bounded memory, NDJSON)
   ──► universal_parser.py / nvis_nccl_ingest tiers (structured JSON → experiment dir → scrape)
   ──► results_ingest.py predicates: is this a real measurement or parser.sh's "---" placeholder?
   ──► results_history (SQLite) + validation_activities archive
   ──► rack_status_engine dataset ──► /api/status, /api/cluster-overview, exports, reports
```

Three data classes are deliberately combined and separately labelled: live control-plane state
(refreshable, may be stale), durable execution evidence (under `/app/var`), and observability
telemetry (retained externally). Empty telemetry is rendered empty, never as a substituted value.

### Error Handling

- A tool or upstream failure is reported as data, not raised: a non-zero return code from a
  fan-out is an outcome the operator needs to see, not a server error.
- The dominant convention is a JSON body carrying an HTTP error status; the one deliberate
  exception is the SPA shell, which answers a missing bundle with a human-readable HTML page and
  503, because its audience is a person in a browser.
- Audit and logging failures degrade silently rather than converting a refusal into a 500.
- Any probe that might not find its answer is read-capped, so a file without the answer costs no
  more than one with it.

### Logging and Debugging

Central configuration (`logging_config.py`) selects sinks (stdout, file, syslog), level and format
(text or JSON), applies a redaction filter, and preserves pytest's capture handler. Application log
and audit trail have independent rotation budgets so log volume cannot evict evidence.

The audit subsystem is separate: `audit/actions.py` declares a stable action vocabulary by
decorator (89 actions over 35 services, covering 103 mutating routes, with a startup validation gate
and a snapshot test); `audit/request_hook.py` is the single emission chokepoint;
`audit/sink.py` writes an append-only JSONL trail with owner-only permissions, rotation sized by a
retention target and a byte ceiling, and a flood guard that collapses repeated refusals after a
threshold.

### State Machine

Validation activities move through recorded phases and are reconciled against Slurm and the result
log until terminal. Two stores back them, and the distinction is a correctness requirement rather
than an optimisation: `var/cache/validation_activities.json` is a capped live working set, while
`var/history/validation_activities.sqlite3` is the append-preserving archive. Acceptance completion
is computed from the archive, because pruning, dismissal, cache replacement or a later failed retry
must not erase an earlier successful run. The live cap never evicts open work.

Workflow runs have their own state: queued → running per step → terminal, persisted under `var/` so
a container restart does not lose them.

### Security Design

| Control | Design |
| :---- | :---- |
| Authentication | One gate (`_require_dashboard_auth`) with an explicit bypass list; PAM locally, shared token in containers |
| Terminal | Authenticated and origin-checked in WSGI middleware above the router; target validated before session creation |
| Command injection | Node targets validated against inventory or a strict RFC 1123 predicate; arguments quoted; script allowlists |
| Transport | TLS ≥ 1.2 enforced in a shared policy applied to both the dev server and gunicorn; compression and client renegotiation disabled |
| Browser | CSP, HSTS (only over TLS), `X-Content-Type-Options`, `Referrer-Policy`, frame-ancestors; no CORS headers, because the SPA and API are one origin |
| Source provenance | Client address resolved right-to-left across a configured trusted-proxy set and labelled peer / proxy / claimed-untrusted; overly broad CIDRs refused |
| Audit | One event per mutating request, bounded and rotated independently, surviving container recreate on a named volume |
| Read-only posture | A deployment can disable cancellation, privileged script execution and cluster-manager mutation by configuration; an allowlist wrapper can constrain `cmsh` to inventory queries |
| Planned | Local identity store with SRP, OIDC federation, JWT id/access tokens, RBAC with default-deny policy evaluation (Phases 2–3) |

Threat model: `.plc/security/threat-model-NVIS-RACK-STATUS.md` (T-1..T-64).

### Test Automation

2,294 backend tests (pytest) plus frontend Vitest, gating merges in CI. The execution layer is
stubbable at one chokepoint, which is what makes route-level testing safe; `subprocess` is stubbed
as a hard floor for the code paths that still bypass it. Demonstration mode serves synthetic data
for out-of-process cases. Meta-tests enforce architectural invariants: route parity across the URL
map, no deferred `app` attribute lookups, no duplicate module-level assignment, no stdlib import
inside a function, and full action-declaration coverage for mutating routes.

---

## Appendix: Reconstruction provenance and open questions

Reconstructed from the shipped tree; every component named above exists in the source. Parked paths
(PythonCM provider, Clush transport) are excluded deliberately, per the README capability inventory.

**Resolved by the author, Aug. 24, 2026:**

| # | Question | Answer |
| :---- | :---- | :---- |
| 1 | Product or component SADD? | **Product-level.** Rack Status is a standalone product deployed at customer clusters. |
| 2 | Is the frontend in scope? | **Yes.** Now a section under Static Design, covering the contexts, the Kaizen/MUI bridge, the vendored assets, the workflow designer's single source of truth and the build coupling. |

**Remaining open questions:**

1. **Identity design commitment.** Phases 2–3 are scoped in `docs/AUTH_PROGRAMME_SCOPE.md` but not
   designed. Should this SADD describe the planned identity and policy architecture as design, or
   stay strictly as-built and let the phase briefs carry it until they land?
2. **The two remaining `import app` cycles.** Recorded as debt in `core-designs.mdc`. Is unwinding
   them a committed design intent to state here, or accepted permanently?
3. **`pythoncm` migration.** `docs/MULTI_USER_MULTI_CLUSTER_PLAN.md` names moving read traffic from
   `cmsh` to `pythoncm` to reduce head-node load, while the README lists PythonCM as parked. Which
   is current — is the migration intended, and if so is it a design commitment?
4. **Frontend performance and accessibility design.** With the frontend now in scope, the absence of
   committed page-load targets (SRD resolved question 4) and of an external accessibility standard
   becomes a design gap rather than only a requirements gap.
