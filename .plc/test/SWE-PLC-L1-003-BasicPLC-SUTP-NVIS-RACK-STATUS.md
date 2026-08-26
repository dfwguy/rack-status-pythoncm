# NVIS Rack Status

## PLC-L1: Basic PLC: Software Unit Test Plan

Documentation Control

| Item | Description |
| :---- | :---- |
| Title | NVIS Rack Status — Software Unit Test Plan |
| Author(s) | Rafael M. Koike (NVIS OVA team), reconstructed with Agentic PLC |
| Revision | Aug. 24, 2026 |
| State | Development |
| Reviewed in | GitLab (`nvis/nvis-rack-status`) |
| nSpect | NSPECT-S57Q-EI19 |
| Input SRD | `.plc/requirements/SWE-PLC-L1-002-BasicPLC-SRD-NVIS-RACK-STATUS.md` |
| Input SADD | `.plc/design/SWE-PLC-L1-003-BasicPLC-SADD-NVIS-RACK-STATUS.md` |
| PLC-L1 SUTP Template Revision | SWE-PLC-L1-001-BasicPLC-SUTP-TMPL |

Revision History

| Date | Author | Summary of Change |
| :---- | :---- | :---- |
| Aug. 24, 2026 | Agentic PLC | Initial reconstruction from the shipped suite: 108 backend test files (2,294 tests), 52 frontend test files, measured coverage, and the `auth-audit-logging` test plan's 40 cases. Skill: plc-test-plan-reconstruction; Timestamp: 2026-08-24T16:00Z; Input ref: NSPECT-S57Q-EI19. Human decision: pending; Reviewed by: pending |
| Aug. 26, 2026 | Rafael M. Koike & Cursor Agent ("Ada") | Refreshed metrics for Phase 2 identity: 115 backend test files (2,503 tests, 164 of them covering `backend/identity/`), 65.7% coverage over 26,883 statements, and the runtime cost of 4096-bit SRP. Recorded that three cases are now measurement-only and skip under `CI`, and noted the two Phase 2 test techniques worth reusing — asserting a cache actually *hits* before asserting it cannot go stale, and running a sub-second boundary many times because that class of bug presents as flakiness rather than failure. |

---

# Introduction

## Test Strategy

The strategy is shaped by one fact about this product: it operates real clusters. A test that
reaches a head node can drain nodes, cancel jobs or power-cycle equipment, so **no automated test
may execute anything**. That is enforced structurally rather than by convention:

1. **One execution chokepoint.** Everything that leaves the process goes through
   `host_exec.run_on_host` / `popen_on_host`, so stubbing two functions neutralises the entire
   execution surface (SADD citizen 1).
2. **A hard floor beneath it.** `subprocess.run`/`Popen` are also stubbed in the route-walking
   suites, because `api/tools.py` still calls `subprocess` directly at eight sites. Two independent
   mechanisms must fail before anything executes.
3. **Demonstration mode** supplies synthetic data for cases that run the app out of process, where
   monkeypatching cannot cross the process boundary.
4. **Session isolation.** Catalog, activity ledger, activity archive, AI settings and audit paths
   are redirected to temporary directories before any test module imports the app, so no test can
   read or damage a developer's active project.

**One consequence of that safety is a real hole, and it belongs in the strategy rather than only in
the gap list.** `conftest.py` also sets `RACK_WEB_AUTH_DISABLED=1`, so **2,286 of 2,339 tests reach
every endpoint unauthenticated**; only 53 exercise the gate. The suite therefore verifies what the
endpoints do, and almost nothing about who may reach them. Coverage cannot see this — the lines
execute, the gate simply is not in the path. Closing it is not a test-writing exercise but a missing
facility (gap **G-10**, and `auth-identity-brief.md` FR-18b), and it is a hard prerequisite for
testing authorization at all: proving user A may do what user B may not needs two authenticated
principals, which a bypass cannot produce.

Beyond safety, the strategy has three deliberate emphases.

**Meta-tests defend architecture, not behaviour.** Several invariants in the SADD cannot be checked
by testing outputs, so they are asserted directly against the source: route parity across all 218
paths, no deferred `app` attribute lookups, no duplicate module-level assignment, no stdlib import
inside a function, and a coverage gate proving every mutating route declares an audit action. Each
exists because the corresponding mistake was made and cost time.

**Guards are validated by mutation, not by passing.** A test that has only ever been seen green
proves nothing. Where an invariant matters, the guard was verified by reintroducing the defect and
observing the specific failure — the duplicate module-level assignment, the inverted `after_request`
registration order, and the tracked-tree import check were each confirmed this way. Two of them
were *found to be toothless* by this method and rewritten.

**The committed tree is tested, not the working tree.** A developer's tree contains untracked files
and build artifacts CI never has; three separate CI failures came from that gap in one day. A
dedicated gate exports only tracked files and runs the suite there.

**Coverage is a gate, not a report (REQ-45).** Every file must reach 50% of statements and branches.
Because the product predates the rule, the 13 files below that line when the gate was introduced are
recorded as a named exception list which **may only shrink** — a file may be removed from it by being
covered, never added. A new file therefore cannot be merged below 50%, which is the intent: every new
feature carries its own tests. Enforced by `bin/check-coverage-floor.py` in the `python-test` job.

**The frontend is in scope.** The SPA is part of the application (SADD, *Frontend Design*), so its
tests gate merges alongside the backend's. Vitest with Testing Library covers views, components and
contexts; 52 test files at the time of writing. The frontend has no equivalent per-file floor yet —
see gap G-8.

## Test Environment

| Environment | Purpose | Availability |
| :---- | :---- | :---- |
| Local pytest (Python 3.12) | Every backend case | Developer workstation |
| Local Vitest + Testing Library | Frontend components and views | Developer workstation |
| GitLab CI `python-test` (`python:3.12-slim`) | Gating backend run, JUnit + coverage artifacts | Every push and MR |
| GitLab CI `frontend-test` (`node:22-slim`) | Gating frontend run | Every push and MR |
| GitLab CI `python-lint` (ruff 0.16.0 pinned, pylint 4.0.7) | Static analysis and secret scan | Every push and MR |
| gunicorn (production server) | Cases that must exercise the forked-worker model | Local; skipped where unavailable |
| Docker + Compose with the `rack-data` named volume | Container-recreate demonstration | Local, on demand |
| Tracked-files-only export | Proving the commit stands alone | Local pre-push and CI |

Notably absent, and deliberately: no test environment has cluster access. Cluster-dependent
behaviour is verified by stubbing the chokepoint, and end-to-end cluster behaviour is verified by
operators against real deployments, recorded as evidence rather than as automated tests.

## Test Metrics

| Metric | Value (Aug. 24, 2026) |
| :---- | :---- |
| Backend test files | 115 |
| Backend tests | 2,503 passing, 17 skipped (of which 164 cover `backend/identity/`) |
| Frontend test files | 52 |
| Frontend coverage | 50.61% (reported by the `frontend-test` job; no per-file floor — gap G-8) |
| Backend statement+branch coverage | 65.7% in a working tree (26,883 statements, `.coveragerc` scope, tests excluded). The clean-checkout figure runs ~0.3 pt lower — see §3 of `local-vs-ci-tree.mdc` and confirm any per-file number against a tracked-only run |
| Files below 50% coverage | 14, all on the gate's exception list |
| Coverage floor | 50% per file, enforced (REQ-45) by `bin/check-coverage-floor.py` |
| Suite runtime | ~130 s local, ~220 s with coverage instrumentation, ~240 s in CI. Phase 2 added ~15 s: 4096-bit modular exponentiation in the SRP cases, plus ~2 s of Miller-Rabin asserting the group's modulus is a safe prime |
| Static analysis | ruff and pylint clean on `backend/` excluding tests |

Coverage is measured over `backend/` with the tests themselves omitted, because including them
inflates the figure towards meaninglessness — test files execute by definition. `.coveragerc` is the
authoritative scope, and the CI job now uses it.

Reconstructing this plan found that it did not: the `python-test` job passed
`--cov=ai_chat --cov=workflow_engine`, so the reported figure described 2 modules out of 136 and the
badge did not describe the project. Fixed in the same change that added the floor, because a gate
reading a two-module report would have had almost nothing to check.

---

# Unit Test Cases and Report

## Unit Test Cases

Test cases are grouped by the requirement they verify. Full per-case detail for the audit subsystem
lives in `.plc/briefs/auth-audit-logging-test-plan.md` (40 cases, TC-1..TC-40).

| Req | Verified by | Layer | Notes |
| :---- | :---- | :---- | :---- |
| REQ-1 canonical status | `test_native_status_summary.py`, `test_cluster_snapshot.py`, `test_native_rack_inventory_cache.py` | Unit, Integration | Includes the case that a failed refresh keeps the last good inventory rather than publishing empty |
| REQ-2 Slurm authority | `test_slurm_command_routing.py`, `test_slurm_launcher_init.py`, `test_app_api.py` | Unit | |
| REQ-3 cluster-manager inventory | `test_extract_rack_ids*`, `test_bcm_query.py` | Unit | |
| REQ-4 exact-node selection | `test_nodelist*`, `test_multirack_campaign.py`, `test_nodelist_metrics_and_naming.py` | Unit | Boundary: zero-padding preserved, expansion bounded |
| REQ-5, REQ-6 pinned presets, launch gate | `test_validation_presets.py`, `test_launch_env_nemo_options.py` | Unit, Integration | |
| REQ-7 per-rack fan-out | `test_multirack_campaign.py`, `test_multirack_nemo_target.py` | Integration | Refusal when rack grouping is not a clean partition |
| REQ-8 deep audits | `test_deep_audit_workflows.py`, `test_nvlink_preflight.py`, `test_nvlink_preflight_api.py` | Integration | |
| REQ-9, REQ-10 durable registry, archive completion | `test_validation_activities.py`, `test_results_history_completion.py` | Integration | Completion must survive a later failed retry |
| REQ-11 rack locks | `test_rack_locks.py` | Unit | TTL and heartbeat release |
| REQ-12, REQ-13 parsing, absent-not-zero | `test_analytics_metricless_rows.py`, `test_parser_results*`, `test_nccl_ingest.py`, `test_nvis_logscan.py` | Unit | The metricless-rows case is the direct "absent is not a pass" guard |
| REQ-14 report renderers | `test_cluster_report*.py`, `ReportsView.test.tsx` | Unit, Integration | |
| REQ-15 attribution | `test_node_report_job.py`, `test_post_run_api.py` | Integration | A failed multi-node job does not implicate every node |
| REQ-16, REQ-17 node-state and cancel guards | `test_slurm_command_routing.py`, `test_schedulers.py`, `test_reset_job_progress.py` | Unit | **Partial — see gap G-7.** Binary routing and scheduler behaviour are covered, and the smoke harness reaches `slurm_admin.py` (65.2%), but the drain/resume/cancel *guards* are not directly asserted |
| REQ-18 observability | `test_metrics_query*.py`, `test_grafana*.py`, `test_nodelist_metrics_and_naming.py` | Unit | PromQL label allowlist refusal cases |
| REQ-19, REQ-20 workflows, durable long work | `test_workflow_composition.py`, `test_workflow_engine.py`, `test_workflow_eval.py` | Unit, Integration | |
| REQ-21, REQ-22 catalog, multi-cluster | `test_catalog_api.py`, `test_config_precedence.py` | Unit, Integration | |
| REQ-23, REQ-24 transport chokepoint, bounded fan-out | `test_transport_*.py`, `test_interactive_ssh_argv.py`, `test_pdsh_job_runner.py` | Unit | N-hop argv composition; the pdsh contract is parametrized over both endpoints |
| REQ-25 rack id is not a hostname | `test_nodelist_metrics_and_naming.py`, `test_naming*.py` | Unit | Real Boston hostname shape asserted |
| REQ-26 configuration precedence | `test_config_precedence.py`, `test_config_materialize_hook.py`, `test_job_env_missing_file.py` | Unit | |
| REQ-27, REQ-28 auth gate, terminal | `test_web_auth_gate.py`, `test_ws_terminal_auth.py`, `test_wsgi_auth_hook.py` | Unit, Integration | Gate must not lock everyone out; terminal refused above the router |
| REQ-29 no metacharacter reaches a command | `test_nvlink_preflight_api.py`, `test_wsgi_auth_hook.py` | Unit | Hostname predicate plus 400 on injection attempts |
| REQ-30 TLS and headers | `test_https_serving.py`, `test_tls_min_version.py`, `test_tls_cert_gen.py`, `test_security_headers.py` | Integration | Real TLS handshake against a generated CA; TLS 1.1 refused |
| REQ-31, REQ-32, REQ-33, REQ-34, REQ-35, REQ-36 audit subsystem | `test_audit_event.py`, `test_audit_chokepoint.py`, `test_audit_actions.py`, `test_action_coverage.py`, `test_audit_platform.py`, `test_client_addr.py`, `test_logging_config.py` | Unit, Integration, Performance, Compatibility | TC-1..TC-40; includes the p95 latency budget and the gunicorn worker model |
| REQ-37 air-gapped | frontend build check for external requests; `test_tls_cert_gen.py` | Integration | Vendored Kaizen assets |
| REQ-38 platforms | `test_platforms*.py`, `test_schedulers*.py` | Unit | |
| REQ-39 demo mode | `test_demo_data.py` and the demo path in most API tests | Unit | |
| REQ-40 deployment verification | `bin/verify-deploy.sh` | System | Executed by `build-docker.sh` and on demand remotely |
| REQ-41 backward-compatible ids | `test_validation_activities.py`, `test_route_parity.py` | Unit | `job_id` == `activity_id`; compat aliases retained |
| REQ-42 committed tree stands alone | `bin/check-tracked-tree.sh` (CI, `python-test`) | System | Verified by mutation against the pre-fix commit |
| REQ-43 read-only posture | Configuration-level; **no automated case** | — | See gap G-3 |
| REQ-44 design system | Frontend component tests | Unit | |
| REQ-45 new code carries its own tests | `test_coverage_floor_gate.py`, `bin/check-coverage-floor.py` (CI, `python-test`) | Unit, System | 24 cases drive the gate's *refusal* paths — a gate that stops failing is silent |
| REQ-46 contribution without code change | `test_catalog_*.py`, `test_action_coverage.py` | Unit | Catalog documents add tools/platforms/sites; the action gate proves a new endpoint is registered without editing a central list |
| REQ-47 evidence is attributable | `docs/AUDIT_PHASE1_VERIFICATION.md` | Inspection | **Partial — see gap G-9.** The record exists; the named sign-off is not yet captured in it |

### Architectural invariant cases (no single requirement; they defend the SADD)

| Case | Asserts |
| :---- | :---- |
| `test_route_parity.py` | All 218 URL paths unchanged by refactoring |
| `test_no_deferred_app_lookups.py` | No name resolved off `app` at call time |
| `test_no_duplicate_module_assignments.py` | No module-level name written twice unread; verified by mutation |
| `test_no_function_level_stdlib_imports.py` | No stdlib import inside a function; verified by mutation |
| `test_action_coverage.py` | Every mutating route declares an audit action (105 pairs, 0 undeclared) |
| `test_endpoint_smoke.py` | 122 read endpoints and 99 write endpoints answer without an unhandled exception |
| `test_audit_docs.py` | Documented settings match the registry |

## Test Report

Latest gating run (commit `0bea87c`, CI pipeline on `feat/auth-audit-logging`):

| Suite | Result |
| :---- | :---- |
| `python-test` | 2,286 passed, 6 skipped |
| `frontend-test` | passing |
| `python-lint` (ruff + pylint + secret scan) | passing |
| `check-tracked-tree` | 134 modules imported, 218 routes |

Local full run: 2,503 passed, 17 skipped, ~220 s with coverage instrumentation. The count differs
between environments because measurement cases skip when `CI` is set — a p95 or a timing ratio on a
shared runner is not a measurement — and the local tracked-tree export omits `frontend/`. Three cases
are in that category now: TC-34's audit latency, the revocation check's 5 ms budget (NFR-3), and the
SRP unknown-login cost ratio that keeps FR-4b from becoming a timing oracle.

**Reproducing the gates locally.** Test tooling is pinned in `backend/requirements-dev.txt` so a
contributor runs the same versions CI does; previously CI installed `pytest`/`pytest-cov` unpinned
and tracked latest, which made "green locally" and "green in CI" different claims:

```bash
pip install -r backend/requirements.txt -r backend/requirements-dev.txt
python -m pytest backend/tests/ -q --cov --cov-config=.coveragerc --cov-report=json:coverage.json
python bin/check-coverage-floor.py coverage.json     # REQ-45
bash bin/check-tracked-tree.sh --with-tests          # the committed-tree gate
```

Run the gate this way to *check* a change, but measure any number you intend to write down — a new
exception baseline, a figure in this plan — in a tracked-files-only tree, per the note above.

Verification evidence that is not a pytest case is recorded in
`docs/AUDIT_PHASE1_VERIFICATION.md`: the `getLogger` diff inspection (TC-39) and the container
recreate demonstration (TC-40, container `ef3693ea312f` → `71f758789132` with the audit event
surviving).

**Such evidence requires a named approver (REQ-47).** A committed record establishes that the
inspection or demonstration happened, but not that anyone other than its author accepted the result,
and inspection is precisely the method where that distinction matters. Sign-off is by one of
**Rafael M. Koike, Giovanni Mascari, or Felix Melligan**, recorded in the evidence document itself alongside the
result. Approver surnames remain to be filled in — see open question 3.

## Function List

Coverage by area, and the honest gaps. Fourteen files remain below 50%; they are listed because a
test plan that reports only the total is not a test plan. Those fourteen are also the *entire*
exception list in `bin/check-coverage-floor.py`, so the table below and the gate cannot drift apart.

**Figures are from a tracked-files-only tree, which is what CI measures.** That distinction is not
pedantry: `universal_parser.py` reads 52.8% in a working tree and 48.1% from a clean checkout,
because a test picks up untracked sample data. Seeding the gate from a local run is exactly what
made its first CI push fail. Thirteen files differ between the two trees by up to 3.0 points; only
that one crosses the floor. See `.cursor/rules/local-vs-ci-tree.mdc` §3.

| Area | Coverage | Assessment |
| :---- | :---- | :---- |
| `audit/` (all modules) | 74–95% | Newest code, built test-first against a 40-case plan |
| `transport/` | 75–97% | The chokepoint is well covered; N-hop composition included |
| `log_time_window.py` | 86.0% | Raised from 12.8%; decides the Grafana window an operator reads results in |
| `stream_exec.py` | 80.4% | Raised from 39.3% |
| `analytics_cache.py` | 78.8% | Raised from 26.2% |
| `comments_store.py` | 74.7% | Raised from 18.0% |
| `validation_activities.py` | 76.4% | The system of record |
| `workflow_engine.py` | 66.3% | The long-work runner |
| `api/tools.py` | 48.3% | 855 statements; also the module bypassing the execution chokepoint |
| `api/analytics.py`, `api/fabric_ops.py`, `api/diagnostics.py`, `api/bringup.py`, `metrics_query.py` | 45–50% | Within a few points of the line |
| `api/rack_power.py` | 26.7% | **Gap G-1** — privileged: power, BMC recovery, MAC change |
| `api/parser_results.py` | 19.3% | **Gap G-2** |
| `rack_status_engine.py` | 25.5% | **Gap G-2** — needs a synthetic fleet fixture |
| `universal_parser.py` | 48.1% | 986 statements, the NCCL/NeMo result scrapers. Reads 52.8% locally — see the note above |
| `api/exports.py` | 22.3% | **Gap G-2** |
| `api/terminal.py` | 28.5% | **Gap G-4** — needs WebSocket upgrade simulation |
| `api/bug_report.py`, `api/logs.py`, `agent/registrations.py` | 28–37% | **Gap G-2** |

### Declared gaps

| Gap | Description | Risk | Intent |
| :---- | :---- | :---- | :---- |
| G-1 | `api/rack_power.py` at 26.7% covers the most destructive surface in the product (power, BMC, MAC change). The pdsh runner beneath it is now covered by 15 parametrized cases, but the handlers are not. | High | Endpoint-specific payload fixtures |
| G-2 | Six `api/*` modules and `rack_status_engine.py` between 19% and 37%; they need per-endpoint payloads and a synthetic fleet fixture rather than generic route walking. | Medium | Incremental |
| G-3 | REQ-43 (read-only posture) is achieved by configuration and has no automated case asserting that cancellation, privileged execution and cluster-manager mutation are all actually refused when disabled. | Medium | Add a posture test |
| G-4 | `api/terminal.py` at 28.5%; the refusal paths are tested but the established-session paths are not. | Medium | WebSocket simulation |
| G-5 | `backend/tests` is not linted by CI (99 findings, 63 auto-fixable), so a new test file's own quality is unchecked. | Low | Enable after working the backlog |
| G-6 | Eight `subprocess` call sites in `api/tools.py` bypass the execution chokepoint, so test safety there rests on a second stub rather than on the architecture. | Medium | Move them behind `host_exec` |
| G-7 | `slurm_admin.py` has no dedicated test module, so REQ-16 and REQ-17 rest on incidental coverage. It reaches 65.2% through the endpoint smoke harness and the routing tests, which is above the floor but exercises the *paths* rather than asserting the *guards*: refusal without an explicit node list, refusal when cancellation is disabled, and the reason string reaching `scontrol`. Coverage percentage is not the same as intent being pinned. | **Medium** | Add a `test_slurm_admin.py` with those three guard cases. Lowered from High after measurement: the earlier draft cited 21.7%, a stale figure predating the smoke harness |
| G-9 | REQ-47 requires a named approver for evidence that is not an automated test, but `docs/AUDIT_PHASE1_VERIFICATION.md` records TC-39 and TC-40 without one. The requirement exists ahead of its own evidence. | **Medium** | Add an approver line (Rafael M. Koike, Giovanni Mascari, or Felix Melligan) to that document, and a template line to any future inspection or demonstration record |
| G-10 | **The authentication gate is bypassed by 2,286 of 2,339 tests.** `conftest.py` sets `RACK_WEB_AUTH_DISABLED=1` suite-wide, so only the 53 cases in `test_web_auth_gate.py`, `test_ws_terminal_auth.py` and `test_wsgi_auth_hook.py` ever exercise `_require_dashboard_auth`. Every other test reaches every endpoint unauthenticated. That is the product's single largest untested surface, and this plan did not declare it until 2026-08-25. Worse, the 53 that do test it authenticate by the shared-token mode that Phase 2 removes, so they verify a path with no future. | **High** | Not a test-writing task — it needs a **test credential facility** so tests authenticate as real users (`.plc/briefs/auth-identity-brief.md` FR-18b, gating Phase 3). Recorded here because a coverage percentage cannot see this: the lines execute, the gate simply is not in the path |
| G-8 | The frontend has no per-file coverage floor. The backend gate (REQ-45) covers `backend/` only; Vitest runs and must pass, and the job already reports a total (50.61%), but nothing fails when a new component merges untested. Now a real gap rather than a scoping choice, since the frontend is in scope for this plan. | **Medium** | The measurement already exists, so the work is the gate, not the instrumentation: point `bin/check-coverage-floor.py` at Vitest's JSON report, or set a `vitest --coverage` threshold, with the same shrinking-exception discipline |

---

## Appendix: Reconstruction provenance and open questions

Reconstructed from the shipped suite; every named test file exists. Coverage figures are from a real
measured run, not estimated.

**Resolved by the author, Aug. 24, 2026:**

| # | Question | Answer |
| :---- | :---- | :---- |
| 1 | Coverage target as a requirement? | **Yes, an enforced gate at 50% per file** (REQ-45), implemented as `bin/check-coverage-floor.py` in the `python-test` job with a shrinking exception list. Measuring it also corrected two figures in this plan and downgraded gap G-7. |
| 2 | Is the frontend in scope? | **Yes** — part of the application, so its tests gate merges. It has no per-file floor yet, now recorded as gap G-8 rather than left implicit. |
| 3 | Sign-off for non-test evidence? | **A named approver is required**: Rafael M. Koike, Giovanni Mascari, or Felix Melligan (REQ-47). The committed record alone is not sufficient. |

**Remaining open questions:**

1. **Frontend test *policy*.** Scope is settled, but 52 test files exist without a stated rule on
   what must be tested — component behaviour, view integration, accessibility. Should this plan
   prescribe one, and should G-8's floor be statements or something view-aware?
2. **Release qualification.** `docs/RELEASE_QA_2026-08-13.md` exists as a point-in-time exercise.
   Should a standing release-qualification checklist be part of this plan, distinct from the
   per-commit gates? Relevant now that releases are periodic and contribution-driven.
3. **Do the eight PLC test layers need explicit N/A statements?** This plan covers unit,
   integration, performance, compatibility, regression and system. Stress/soak/endurance and formal
   security testing are not represented; the product's own validation recipes are arguably the
   stress layer, but that is a judgement I should not make.
