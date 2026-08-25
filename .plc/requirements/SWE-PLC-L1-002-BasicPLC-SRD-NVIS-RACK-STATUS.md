# NVIS Rack Status

## PLC-L1: Basic PLC: Software Requirements Document

Documentation Control

| Item | Description |
| :---- | :---- |
| Title | NVIS Rack Status — Software Requirements Document |
| Author(s) | Rafael M. Koike (NVIS OVA team), reconstructed with Agentic PLC |
| Revision | Aug. 24, 2026 |
| State | Development |
| Reviewed in | GitLab (`nvis/nvis-rack-status`) |
| nSpect | NSPECT-S57Q-EI19 |
| PLC-L1 SRD Template Revision | SWE-PLC-L1-001-BasicPLC-SRD-TMPL, Mar. 09, 2026 |

Approvers

| Date | Name | Notes |
| :---- | :---- | :---- |
|  | Rafael M. Koike | Pending — reconstructed draft, not yet groomed |
|  | Keith Rosenberg | Pending — requested the requirements/design/test snapshot |

Reviewers

| Date | Name | Notes |
| :---- | :---- | :---- |
|  | Bryan Reese | NVIS OVA team lead |
|  |  |  |

Revision History

| Date | Author | Summary of Change |
| :---- | :---- | :---- |
| Aug. 24, 2026 | Agentic PLC | Initial reconstruction from the shipped product: 14 `.cursor/rules` files (1,761 lines), 34 `docs/*.md` (11,195 lines), the `auth-audit-logging` feature brief (39 FR/NFR, 55 AC), the README release-candidate capability inventory, and the source tree (134 backend modules, 218 routes, 2,294 tests). Skill: plc-requirements-reconstruction; Timestamp: 2026-08-24T15:00Z; Input ref: NSPECT-S57Q-EI19. Human decision: pending; Reviewed by: pending |

---

# Introduction

## Overview

**Why is the project being developed?**

NVIDIA AI-factory deployments are validated and handed over as bespoke projects rather than as
products. Each GB200/GB300 NVL72 cluster bring-up repeats the same work — inventory the racks,
prove the fabric, run NCCL and NeMo at scale, decide which nodes are genuinely faulty, and produce
evidence a customer will accept — and that work is largely carried in individuals' heads, shell
history, and spreadsheets. Rack Status exists to turn that runbook knowledge into tooling, so a
deployment is repeatable, observable, and fast to first token.

**What problem does it solve?**

Three problems, in order of the cost they impose:

1. **Acceptance evidence is not reproducible.** Without a durable record of what ran on which rack,
   when, and what came of it, a handover cannot be audited and a disputed result cannot be settled.
2. **A missing measurement is easily read as a pass.** Empty telemetry, an unparsed log, or a probe
   that never ran look identical to a healthy node unless the platform is explicit about the
   difference. Several sections of this product exist only to prevent that confusion.
3. **Node fault attribution is routinely wrong.** A failed multi-node job does not mean every
   allocated node is faulty, yet the default reaction is to drain all of them, which removes healthy
   capacity and hides the real fault.

**What are the main functions of the project?**

Inventory and status of racks, nodes and the scheduler; pinned validation recipes launched against
exact node selections (single-rack, multi-node, multi-rack); repeatable hardware and NVLink/network
deep audits; durable validation-activity history with advisory rack locks; result parsing into
metrics with generated CSV/JSON/PDF reports; post-run node attribution; observability integration
(VictoriaMetrics, Loki, Grafana); guarded Slurm and node-state operations; workflow authoring and
execution; multi-cluster and multi-platform support through catalog-driven Project/Cluster,
Platform and Tool records.

**Who are the primary stakeholders?**

| Stakeholder | Interest |
| :---- | :---- |
| NVIS OVA team (Observability, Validation, Automation) | Owns and operates the product |
| NVIDIA Solutions Architects on customer bring-ups | Primary daily users |
| Customer cluster operators | Secondary users; consume reports and node state |
| Customer acceptance reviewers | Consume validation evidence and generated reports |
| NVIDIA product teams (BCM/Mission Control, NeMo, DGXC) | Provide the platforms and recipes being exercised |

Deployments in service at the time of writing: **Mayo Clinic**, **Nscale TX** (GB300, including a
16K-node instance), **Firmus MEL01**, **Boston Markley BOS01**.

**Who is developing the project?**

NVIS OVA team, Rafael M. Koike as principal author, reporting to Bryan Reese; contributions from
Harish Lakshminarayanan (Nscale TX 16K port). Agentic PLC skills and AI pair-development are used
throughout, and their use is recorded per commit.

## Product scope and PLC level

**Rack Status is a standalone product**, not a component of a larger registration. It is a complete
solution deployed at a customer cluster to enable NVIDIA NVIS and its partners to bring up NVIDIA
clusters. This document is therefore a **product-level SRD** under NSPECT-S57Q-EI19, and the
accompanying SADD and SUTP are product-level documents. (Author decision, Aug. 24, 2026; supersedes
the open question carried in `.plc/profile.json`.)

Scope includes the React single-page application. The frontend is part of the application rather
than a separate client, so its design is in scope for the SADD and its tests are in scope for the
SUTP.

## Development and contribution model

Rack Status is a **collaborative SA-driven project**, and that shapes its requirements rather than
being merely process trivia. It originated with the Solutions Architects who operate it, and
continues to be developed by them: NVIS SAs contribute through merge requests, and the product is
explicitly not a one-way door handed over and frozen. New versions are released periodically.

Consequences that are requirements rather than preferences: a contributor who is not the author
must be able to add a tool, preset or site without changing code (REQ-21); the architecture must
make the correct extension point obvious, because contributors arrive without context; and new
contributions must carry their own tests (REQ-45).

There is no formal supported-version window, upgrade/rollback commitment, or customer-facing SLA at
this time. Release cadence is periodic but not contractually fixed.

## Assumptions

- Each managed cluster runs a supported cluster manager (**BCM/Bright** or **MAAS**) and **Slurm**
  as the workload scheduler; Slurm is the authority for job state, allocation, drain state and node
  reasons.
- The application reaches a cluster through a head node (or an adjacent host) over SSH, or runs
  directly on it; a persistent application data directory is available.
- Operators are trusted NVIDIA or customer staff with existing cluster credentials; the product
  constrains *what* they can do through it, and does not defend against a hostile administrator.
- Observability data, where present, is produced by VictoriaMetrics and Loki; the product reads it
  and does not own its retention.
- Validation recipes and container images are pinned by version and are supplied by the site.

## Constraints

- **Air-gapped and restricted sites are first-class.** No dependency may require public internet at
  runtime. Assets that would normally be fetched from a CDN are vendored (for example the Kaizen UI
  CSS and fonts), and images are distributable as an offline `docker save` bundle.
- **Operators reach the UI over an SSH tunnel.** This rules out ACME/public-CA certificate issuance
  and shapes the TLS approach (self-signed by default, internal CA as an opt-in upgrade).
- **One repository serves several customers.** No customer hostname, credential, absolute site path
  or campaign roster may be hard-coded; site specifics belong to configuration or the catalog.
- The deployed container is bind-mounted over by its host tree and `/app/var` is a named volume, so
  build-time generated files under those paths are invisible at runtime.
- The head node's `sshd MaxSessions` (OpenSSH default 10) bounds any fan-out that relays through it.
- Python 3.12 backend, React 19 + Vite frontend, NVIDIA Kaizen UI as the design system.

## Dependencies

| Team or Company | Deliverable |
| :---- | :---- |
| NVIDIA BCM / Mission Control | `cmsh`, CMDaemon, prejob healthchecks, NMC dashboards |
| SchedMD (Slurm) | `sbatch`, `srun`, `sinfo`, `squeue`, `sacct`, `scontrol`, `scancel` |
| NVIDIA NeMo / DGXC Benchmarking | Pinned training and benchmark recipes and containers |
| NVIDIA DCGM | `dcgmi`, `mndiag`, `mnubergemm` multi-node diagnostics |
| NVIDIA NCCL | `nccl-tests`, `ncclGetVersion` runtime gates |
| VictoriaMetrics / Grafana / Loki | Metrics store, dashboards, log store |
| Dell / OEM (site-dependent) | iDRAC Redfish, BMC recovery, out-of-band power control |
| Customer site operations | Head-node access, credentials, storage, network topology |

## Definitions, Acronyms, Abbreviations

| Term or Abbreviation | Description |
| :---- | :---- |
| BCM | NVIDIA Base Command Manager (Bright Cluster Manager) |
| CT | Compute tray |
| Deep audit | Repeatable structured per-node hardware or fabric inspection |
| First-class citizen | An abstraction that owns a concern product-wide and is extended, not duplicated |
| IMEX | NVIDIA Internode Memory Exchange service, required for multi-node NVLink |
| MNNVL | Multi-node NVLink |
| MOP | Method of Procedure |
| NVL72 | 72-GPU NVLink-domain rack architecture (GB200/GB300) |
| Post-run attribution | Deciding which nodes a failed run actually implicates |
| Preset | A pinned, reviewed validation recipe with fixed version and topology parameters |
| Rack id | Cluster-manager grouping key (e.g. `p5-r01`); **not** a compute hostname |
| SU | Scalable Unit |
| Xid | NVIDIA driver-reported GPU error code |

## References

| SWE Number | Input Work Product | Revision | Location |
| :---- | :---- | :---- | :---- |
| — | Rack Status operator and administrator guide | Aug. 2026 | `docs/RACKSTATUS_USER_GUIDE.md` |
| — | First-class citizens of Rack Status | Aug. 2026 | `.cursor/rules/core-designs.mdc` |
| — | Execution architecture | Aug. 2026 | `docs/EXECUTION_ARCHITECTURE.md` |
| — | Parsing results architecture | Aug. 2026 | `docs/PARSING_RESULTS_ARCHITECTURE.md` |
| — | Auth, audit and logging feature brief | Aug. 2026 | `.plc/briefs/auth-audit-logging-brief.md` |
| — | Security assessment | Aug. 2026 | `docs/SECURITY_ASSESSMENT_2026-08.md` |
| — | Threat model (SecNemo TAVA) | Aug. 2026 | `.plc/security/threat-model-NVIS-RACK-STATUS.md` |
| — | Environment variable reference | Aug. 2026 | `docs/ENVIRONMENT.md` |
| — | Multi-user / multi-cluster plan | Aug. 2026 | `docs/MULTI_USER_MULTI_CLUSTER_PLAN.md` |
| — | Auth programme scope (Phases 0–3) | Aug. 2026 | `docs/AUTH_PROGRAMME_SCOPE.md` |

---

# Use Cases

The following table lists the use cases for this project.

| Use Case | Description |
| :---- | :---- |
| UC-1 Assess cluster readiness | An SA opens Cluster Overview to see identity, availability, validation summary, live Slurm state and active users before starting work. |
| UC-2 Inspect a rack | Select a rack to see per-node status, validation history, hardware, networking and service tabs. |
| UC-3 Validate one rack | Select exact nodes, choose a pinned preset, review the resolved target, launch, and watch live output. |
| UC-4 Validate several racks | Select racks, check eligibility, and launch a multi-rack campaign that fans out one job per rack where the recipe requires it. |
| UC-5 Run a repeatable deep audit | Run the hardware or NVLink/network deep audit and read structured per-node findings. |
| UC-6 Attribute a failed run | Use the Post Run Node Report to see run participation, attributed failures and repeat offenders, and decide which nodes are genuinely implicated. |
| UC-7 Drain or resume a node with evidence | Record the decisive signature, drain with a structured reason, and resume once verified clean. |
| UC-8 Produce acceptance evidence | Generate and download a cluster report (CSV/JSON/PDF) and retrieve it later from the durable archive. |
| UC-9 Investigate telemetry | View native metrics charts, open Grafana deep links, and read logs and events. |
| UC-10 Operate the scheduler | Review `squeue` and recent accounting, inspect job detail and output, and cancel a job under guard. |
| UC-11 Recover hardware out of band | Perform BMC recovery, power actions, MAC change or NVSwitch fixes against a confirmed node list. |
| UC-12 Author and run a workflow | Compose a workflow in the designer and execute it with per-step output and cancellation. |
| UC-13 Administer the catalog | Manage Platform, Tool and Project/Cluster records, and switch the active cluster. |
| UC-14 Operate several clusters from one instance | Switch active project/cluster so one running instance serves multiple sites. |
| UC-15 Demonstrate or develop without a cluster | Run in demo mode with synthetic data and no cluster mutation. |
| UC-16 Audit operator activity | Review the durable audit trail of who did what, when, from where, and with what result. |

---

# Requirements

The following table lists the requirements for this project. IDs are stable; each row is traceable
to shipped code, a shipped document, or a recorded decision.

| ID | Title | Description | Type |
| :---- | :---- | :---- | :---- |
| REQ-1 | Canonical rack and node status | The product shall present rack and node status built by native collectors, and shall not publish a canonical snapshot until its required log scan has completed at least once. | Functional |
| REQ-2 | Slurm as scheduler authority | The product shall treat Slurm as authoritative for job state, allocation, drain state and node reasons, and shall not substitute its own inference. | Functional |
| REQ-3 | Live cluster-manager inventory | The product shall obtain rack and device inventory from the configured cluster manager (BCM `cmsh` or MAAS). | Functional |
| REQ-4 | Exact-node selection | The product shall support validation against an exact node selection, several nodes within a rack, and several racks. | Functional |
| REQ-5 | Pinned validation presets | The product shall launch validation only from presets that pin recipe version, container image and topology parameters. | Functional |
| REQ-6 | Launch review gate | The product shall resolve and display the exact target of a launch for review, and shall refuse the launch if the reviewed target no longer holds. | Functional, Safety |
| REQ-7 | Per-rack fan-out for NVLink-domain tests | For recipes confined to one NVLink domain, the product shall submit one job per rack and shall refuse a target whose rack grouping is not a clean partition of the selection. | Functional, Safety |
| REQ-8 | Repeatable deep audits | The product shall provide repeatable hardware and NVLink/network deep audits producing structured per-node results. | Functional |
| REQ-9 | Durable validation activity registry | The product shall record every validation launch durably, with a canonical `activity_id`, task list, phases and adopted external logs, surviving process and container restarts. | Functional |
| REQ-10 | Acceptance completion from the archive | The product shall compute acceptance completion from the append-preserving activity archive and result history, never from the capped live cache alone. | Functional, Safety |
| REQ-11 | Advisory rack locks | The product shall claim an advisory lock for exclusive rack operations, with TTL and heartbeat release, so two operators cannot act on one rack simultaneously. | Functional, Safety |
| REQ-12 | Result parsing to metrics | The product shall parse tool output into metrics in a single bounded pass, preferring structured tool output over scraping text. | Functional |
| REQ-13 | Distinguish absent from zero | The product shall represent a missing measurement, unavailable telemetry or unparsed result as absent or unavailable, and shall never present it as a passing or zero value. | Functional, Safety |
| REQ-14 | Generated report archive | The product shall generate cluster reports as CSV, JSON and PDF renderings of one dataset, and shall retain completed reports durably for later retrieval. | Functional |
| REQ-15 | Post-run node attribution | The product shall attribute run participation and failures per node, surface repeat offenders, and shall not mark every node of a failed multi-node job as failed. | Functional, Safety |
| REQ-16 | Evidence-first node state change | The product shall require an explicit node list and shall support recording a decisive reason before draining, resuming or resetting node state. | Safety |
| REQ-17 | Guarded workload cancellation | The product shall guard job cancellation behind explicit confirmation of the exact job, and shall support disabling cancellation entirely by configuration. | Safety |
| REQ-18 | Observability integration | The product shall read metrics from VictoriaMetrics and logs from Loki, and shall offer Grafana deep links and optional embeds. | Interface |
| REQ-19 | Workflow authoring and execution | The product shall allow authoring workflows whose source of truth is Mermaid text, and shall execute them with per-step output, progress and cancellation. | Functional |
| REQ-20 | Long-running work is durable and observable | Long-running operations shall return an identifier immediately, run in the background, persist their state across restarts, and expose progress and per-step output. | Non-Functional |
| REQ-21 | Catalog-driven multi-generation support | The product shall represent hardware Platforms, launchable Tools and customer Project/Cluster records as catalog documents, so a new tool, preset or site is a data change rather than a code change. | Functional |
| REQ-22 | Multi-cluster from one instance | One running instance shall serve several projects and clusters, with the active cluster's identity, naming and connection taking precedence over process environment. | Functional |
| REQ-23 | Single execution chokepoint | Every command the product runs on a host shall pass through the transport layer, with environment forwarding, multi-hop composition, bounded-concurrency fan-out and retry on transient SSH failure. | Non-Functional |
| REQ-24 | Bounded fan-out | Fan-out concurrency shall respect the target's session limits so that a large sweep does not silently lose nodes. | Non-Functional, KPI |
| REQ-25 | Rack id is not a hostname | The product shall derive real compute hostnames from the cluster's naming convention and shall not construct targets from a rack id. | Functional, Safety |
| REQ-26 | Configuration precedence | Configuration shall resolve in a defined order — active cluster overlay, job overrides, process environment, platform defaults — through a single accessor set. | Non-Functional |
| REQ-27 | Authenticated access | The product shall require authentication for operator access, supporting PAM locally and a shared token for container deployments, with one enforcement gate. | Security |
| REQ-28 | WebSocket terminal is gated at the transport | The interactive terminal shall be authenticated and origin-checked in WSGI middleware, above the router, and shall validate its target before any session is created. | Security |
| REQ-29 | No shell metacharacter reaches a command | Node targets and script arguments shall be validated against an allowlist or inventory, and quoted, so an authenticated operator cannot reach arbitrary remote execution. | Security |
| REQ-30 | Transport confidentiality | The product shall be servable over TLS 1.2 or higher, with certificate generation tooling that works offline, and shall emit hardening headers and a content security policy. | Security |
| REQ-31 | Durable audit trail | Every mutating operation shall produce one structured audit event recording timestamp, principal, authentication method, service, action, resource, source address and provenance, result, decision and latency. | Security, Telemetry |
| REQ-32 | Audit retention independent of stdout | Audit retention shall not depend on stdout being collected, and shall survive a container recreate. | Security, Telemetry |
| REQ-33 | Bounded audit budget | The audit trail shall be bounded by a retention target in days and a hard byte ceiling, retaining whichever binds first and recording which one is binding. | Non-Functional |
| REQ-34 | Audit under load | A refusal loop shall not evict the history it should be recorded in; repeated identical refusals shall collapse into a counted summary after a threshold. | Non-Functional |
| REQ-35 | Audit overhead budget | The audit path shall add no more than 5 ms at the 95th percentile to an audited request. | KPI |
| REQ-36 | Stated source-address provenance | The product shall record whether a source address was observed directly, supplied by a trusted proxy, or claimed but untrusted, and shall not trust a forwarded header by default. | Security |
| REQ-37 | Air-gapped operation | The product shall operate with no runtime dependency on the public internet, and shall be distributable as an offline image bundle. | Platform |
| REQ-38 | Supported platforms | The product shall support BCM and MAAS as cluster managers and Slurm as scheduler, selected by configuration rather than by code branching. | Platform |
| REQ-39 | Demonstration mode | The product shall provide a demonstration mode that serves synthetic data and performs no cluster mutation. | Functional, Test Automatability |
| REQ-40 | Deployment verification | A deployment shall be verifiable by an automated check that fails unless the container runs, every backend module imports, no module is staler than the tree, and every asset the served page references resolves. | Non-Functional |
| REQ-41 | Backward-compatible identifiers | Asynchronous APIs shall continue to return `job_id` as the canonical `activity_id`, and superseded endpoints shall remain available as compatibility aliases. | Backward Compatibility |
| REQ-42 | Committed tree stands alone | The committed tree shall import and pass its tests without build artifacts or untracked files present. | Test Automatability |
| REQ-43 | Read-only operating posture | A deployment shall be configurable as an inventory and reporting surface only, with workload cancellation, privileged script execution and cluster-manager mutation disabled. | Security, Platform |
| REQ-44 | Accessibility and design system | Operator surfaces shall be built on the NVIDIA Kaizen UI design system. | Non-Functional |
| REQ-45 | New code carries its own tests | Every source file shall be covered by automated tests to at least 50% of its statements and branches, enforced in CI. Files below that threshold at the time the gate was introduced are recorded as a finite, named exception list that may only shrink. | Test Automatability |
| REQ-46 | Contribution without code change | Adding a validation tool, preset, hardware platform or customer site shall be achievable by editing catalog data and configuration, without modifying application code. | Functional, Non-Functional |
| REQ-47 | Verification evidence is attributable | Verification evidence that is not an automated test — inspection and demonstration results — shall be committed to the repository and signed off by a named approver from the authorised set. | Non-Functional |
| REQ-48 | Provenance export to a data lake | The product shall be able to export validation-run provenance — the parsed result, the raw log and the inputs that defined the run — to an external data lake, so a published measurement stays traceable to exactly what produced it. | Functional, Telemetry |
| REQ-49 | Egress is opt-in, minimised and encrypted | Any outbound export of operator activity shall be disabled by default; shall never include credential-bearing request bodies; shall redact secrets by key name and by value in whatever it does include; and shall support TLS 1.2 or higher with certificate verification that cannot be disabled. | Security, Telemetry |
| REQ-50 | Egress never degrades the operator surface | Outbound export shall not block a response, shall be bounded in memory, and shall count what it discards rather than dropping silently. | Non-Functional, KPI |

---

# Test Automatability

## Security implications for test automatability

The product's automated tests must never reach a real cluster. Two independent mechanisms enforce
this: the execution layer is stubbed at its single chokepoint (`host_exec.run_on_host` /
`popen_on_host`), and `subprocess.run`/`Popen` are stubbed as a hard floor for the code paths that
still bypass the chokepoint. Demonstration mode supplies synthetic data for tests that run the
application out of process.

Credential material is never required by the test suite: catalog, ledger, audit and AI settings are
isolated to temporary directories per session, and no test may read a developer's active project.

## Security Best Practices

- Secrets are not committed; a repository check fails the build if vault material appears in git.
- Dependency and license scans run against the backend and frontend manifests.
- Static analysis (ruff, pylint) and a secrets scan run in CI; the enforced rule set is declared in
  the repository rather than inherited from a tool's defaults.
- Refusals are audited, and audit failure must never turn a refusal into a server error.

## Test Automatability Requirements Checklist

| Item | Status |
| :---- | :---- |
| Automated unit and integration tests exist and gate merges | Yes — 2,294 backend tests plus frontend Vitest, run in CI |
| The product can run without hardware | Yes — REQ-39 demonstration mode |
| Execution is stubbable at one point | Yes — REQ-23 single chokepoint |
| Test evidence is machine-readable | Yes — JUnit XML and coverage reports published per pipeline |
| Deployment is verifiable automatically | Yes — REQ-40 |
| Committed tree is verifiable independently of the working tree | Yes — REQ-42 |

---

## Appendix: Reconstruction provenance and open questions

This document was reconstructed from the shipped product rather than authored ahead of it. Every
requirement above is traceable to code, a shipped document, or a recorded decision; nothing was
invented to fill a template section.

**Deliberately excluded as parked, per the README capability inventory:** the PythonCM
cluster-manager provider and the Clush transport. Both are named in the source and both are
explicitly not release-candidate execution paths.

**Resolved by the author, Aug. 24, 2026:**

| # | Question | Answer |
| :---- | :---- | :---- |
| 1 | Standalone product or component? | **Standalone product.** A complete solution deployed at customer clusters so NVIS and partners can bring up NVIDIA clusters. These are product-level documents. |
| 2 | Is the frontend in scope? | **Yes** — the SPA is part of the application, not a separate client. Its design is a SADD section and its tests are in the SUTP. |
| 3 | Release and support posture? | Periodic releases; **collaborative SA-driven development** with contributions by merge request; explicitly not a one-way door. No formal supported-version window or upgrade/rollback commitment. Recorded above under *Development and contribution model*. |
| 4 | Committed performance targets? | **None beyond REQ-35's 5 ms audit budget.** No SLAs exist for page load, fleet-scan duration or live-state staleness, and none are planned yet; they may be added incrementally. Recorded as a deliberate absence rather than an oversight. |
| 5 | Coverage threshold as a gate? | **Yes** — 50% per file, enforced in CI, so every new feature carries its own tests. Now REQ-45. |
| 6 | Who signs off non-test evidence? | A named approver from **Rafael M. Koike, Giovanni Mascari, or Felix Melligan**. Now REQ-47. |

**Remaining open questions — lower priority, still unanswered:**

1. **User scale as a requirement.** `docs/MULTI_USER_MULTI_CLUSTER_PLAN.md` sizes for 20
   simultaneous operators. Should that become a stated requirement, and does the 16K-node Nscale
   deployment raise it?
2. **External accessibility standard.** Kaizen UI is mandated internally; is there a WCAG level or
   equivalent the operator surfaces must meet for customer delivery?
3. **Data-handling classification.** Validation logs and reports contain customer cluster topology
   and hostnames. Is there a required classification, retention limit or export restriction beyond
   the current bounded audit budget?
