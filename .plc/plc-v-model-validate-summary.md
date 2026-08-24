# PLC V-Model Validation Summary — NVIS Rack Status

| Item | Value |
| :---- | :---- |
| Date | Aug. 24, 2026 |
| Repo | `nvis/nvis-rack-status` |
| Branch / HEAD | `feat/auth-audit-logging` @ `72b01c5` |
| Comparison base | `origin/main` @ `06bd329` (merge-base `06bd329`) |
| Changed files | 121 committed on branch; 0 staged; 0 unstaged; 2 untracked (unrelated customer drafts) |
| Mode | Legacy single-repo (`.plc/briefs/`) — no `agentic_plc/workspace.json` |
| Waivers | None found — normal |
| nSpect | NSPECT-S57Q-EI19 |

**Source documents used**

| Artifact | Path | Status at previous run |
| :---- | :---- | :---- |
| SRD | `.plc/requirements/SWE-PLC-L1-002-BasicPLC-SRD-NVIS-RACK-STATUS.md` | **missing** |
| SADD | `.plc/design/SWE-PLC-L1-003-BasicPLC-SADD-NVIS-RACK-STATUS.md` | **missing** |
| SUTP | `.plc/test/SWE-PLC-L1-003-BasicPLC-SUTP-NVIS-RACK-STATUS.md` | **missing** |
| Feature brief | `.plc/briefs/auth-audit-logging-brief.md` | available |
| Threat model | `.plc/security/threat-model-NVIS-RACK-STATUS.md` | available |

> **Advisory only.** Nothing here asserts PLC compliance, release-gate clearance, or satisfaction of
> nSpect, Pulse, ScanSpect, SonarQube, Coverity, BlackDuck, OSRB, LaunchAPI or TAVA. "Current" below
> means *current for workflow reuse*, not formal PLC approval.

---

## Did the results change?

**Yes — two phases that could not run before now run, and they produced 30 findings.**

The August assessment was scoped to security only: `.plc/profile.json` recorded the SRD, SADD and
SUTP as `missing`, so `plc-requirements-validation` and `plc-design-validation` had no inputs.
Requirements validation would have halted (a missing SRD is fatal); design validation would have
returned `SKIP` with reason `design_artifact_not_provided`. The bidirectional SRD↔SADD consistency
check — the whole point of requirements validation — was structurally impossible.

| Phase | Previous run | This run | Findings |
| :---- | :---- | :---- | :----: |
| Requirements validation | `SKIP` — `srd_not_provided` | **WARN** | 21 |
| Design validation | `SKIP` — `design_artifact_not_provided` | **WARN** | 9 |
| Verification | PASS | **Reused** @ `72b01c5` | — |
| Security scan | PASS (Semgrep, dependency CVE, license, Pulse secrets, SecNemo TAVA) | **Reused** | — |
| Code implementation check | n/a | **PASS** | 0 material |
| Acceptance validation | not run | `SKIP` — deferred, see below | — |
| Traceability matrix | not run | `SKIP` — deferred, see below | — |

All 30 findings are **newly visible, not newly introduced**. Nothing regressed; the previous
assessment simply could not see this class of defect. That is the honest answer to "did the results
change": the security posture is unchanged and still clean, and two entire categories of finding
became observable for the first time.

---

## Findings by phase

### Requirements validation — WARN (21 findings: 2 Critical, 9 Warning, 10 Info)

Report: [`.plc/plc-requirements-validation-report.md`](plc-requirements-validation-report.md)

Template compliance is strong: **11 of 11** content areas present (9 required, 2 optional). Group A
findings are about completeness *within* areas, which is the expected shape for a document
reconstructed from a shipped product.

The two Critical findings share one cause: the SRD states two **refusal** requirements — refuse a
launch when the reviewed target no longer holds (REQ-6), refuse a multi-rack target that is not a
clean rack partition (REQ-7) — and the SADD designs neither. Both are implemented and both are
Safety-typed. A design document that omits the refusal path is describing the happy path of a safety
control.

| Group | Critical | Warning | Info |
| :---- | :----: | :----: | :----: |
| A — SRD template compliance | 0 | 3 | 3 |
| B — SRD → SADD coverage (47 requirements checked: 37 covered, 5 partial, 5 uncovered) | 2 | 3 | 5 |
| C — SADD → SRD traceability | 0 | 3 | 2 |

### Design validation — WARN (9 findings: 1 High, 4 Medium, 4 Low)

Report: [`.plc/plc-design-validation-report.md`](plc-design-validation-report.md)

Guideline compliance: 8 areas checked, **3 covered, 5 partial, 0 missing**. Depth gaps concentrate in
Interface Architecture and Operational Considerations, both High-severity per the rubric.

The SADD proved unusually accurate against the code. Verified exactly as claimed: 218 routes, 89
actions over 35 services, `core.py` importing neither Flask nor `app`, zero CORS headers, audit sink
`0o600`/`0o700`, a TLS 1.2 floor applied through one shared policy on **both** the gunicorn and
Werkzeug paths, 16 vendored fonts, 2,294 tests, and the full frontend inventory.

The one High finding is Group C: an **undocumented outbound egress path** (`lake_sink.py`) that
captures request and response bodies and POSTs them over **plain HTTP** to a default of
`http://host.docker.internal:9410`. It is disabled by default, which limits present exposure, but it
appears in neither the SADD's interface table nor the SRD — and its plaintext default sits oddly
beside REQ-30's TLS mandate for the inbound surface.

### Code implementation check — PASS

The 121 changed files on this branch are traceable to requirements. The three added today map
directly: `bin/check-coverage-floor.py` and `backend/tests/test_coverage_floor_gate.py` to REQ-45,
`backend/requirements-dev.txt` to REQ-42's spirit (reproducing CI's tree), and the
`docs/AUDIT_PHASE1_VERIFICATION.md` sign-off table to REQ-47. No untraced behaviour found. One
advisory note: REQ-45 and REQ-47 were added to the SRD today and are not yet in the SADD — recorded
as findings B-4 and B-5 in the requirements report rather than duplicated here.

### Verification — Reused @ `72b01c5`

2,294 passed, 17 skipped locally; 2,310 passed, 6 skipped in CI (the difference is 16 bash-4 tests
that skip on macOS and the CI-gated latency case). Pipeline `64316235` is green across all five
jobs. Backend coverage 65.0% from a clean checkout, with the new per-file floor (REQ-45) passing at
14 known exceptions. Current for workflow reuse.

### Security scan — Reused

The August scan set remains the current evidence: Semgrep SAST, backend and frontend dependency CVE
scans, `pip-licenses`, Pulse secret scan, and the SecNemo Agent-TAVA threat model (T-1..T-64), all
under `.plc/security/`. No source change in this session touched a scanned surface in a way that
invalidates them. Rerunning is a network/auth-dependent operation and was not repeated.

### Acceptance validation and traceability matrix — SKIP (deferred)

Both were resolvable — `.plc/briefs/auth-audit-logging-brief.md` carries 55 acceptance criteria and
`.plc/briefs/auth-audit-logging-test-plan.md` carries the TC-1..TC-40 coverage tables — and both were
deliberately deferred rather than attempted, because they would validate the **auth-audit-logging
feature**, whereas this run's question was whether the **new product-level documents** change the
assessment. Running them is the natural next step and needs no new inputs.

Reason code: `deferred_scope_by_operator`.

---

## Top risks

1. ~~**Two Safety requirements have no designed refusal path** (REQ-6, REQ-7)~~ — **RESOLVED
   2026-08-24, same session.** Both are now in the SADD's *Launch safety* subsection, and both
   controls proved stronger than the report assumed: REQ-6 is a `prepare`→`launch` split with a
   SHA-256 `plan_hash` that is **re-resolved** and compared in constant time, and REQ-7's partition
   invariant is checked twice — in Python on the app host and again in bash on the head node, both
   failing closed.

   **But closing it opened a real one.** `plan_hash` is enforced in exactly **one launch endpoint
   out of thirteen**. The legacy per-tool launches (`run-nccl-fullrack-async`,
   `run-nemo-xrack-async`, `run-llmb-nccl-async`, `run-mnubergemm-stress-async`, `run-nvloom-async`,
   `run-clusterkit-async`, the llama/exemplar family) resolve their target at launch and rely on a
   client-side confirmation dialog, which detects nothing. So REQ-6 holds on the preset path and
   **fails on the legacy paths** — a code gap, now finding B-1a, and the highest-value remaining
   item.
2. ~~**The lake egress path ships credentials in plaintext, and it is ON by default.**~~
   **RESOLVED 2026-08-24, same session.** Revised upward first: it was **not** disabled by default
   (no deployment file sets `RACK_LAKE_DISABLED`) and capture covered **102 mutating endpoints**
   including `POST /api/login`, whose body is `{"password": "..."}`, and
   `PUT /api/vault/nvftp-token` — with no redaction, because `lake_sink` builds its own payload and
   bypasses the logging system's redaction filter.

   Then closed on all four axes rather than just the transport the question asked about: credential
   paths refused before the body is read, redaction by key name and by value, verified TLS with no
   skip-verify switch, and capture now opt-in. The synchronous 20 s POST inside `after_request` —
   an availability problem found alongside — became a bounded, counting queue. REQ-48/49/50 added,
   SADD *Egress design* written, `ENVIRONMENT.md` §16b written, 30 tests.
3. **Three shipped components have no requirement authorising them**: the AI assistant, the
   interactive terminal (constrained by REQ-28/29 but never required), and the lake capture path.
   An unrequirmented feature is also an unthreat-modelled one.
4. **`slurm_admin.py` is thin in both design and test** — no citizen row, no dedicated test module
   (SUTP gap G-7). It owns drain, resume and cancel: the operations that remove customer capacity.
5. ~~**Two documented invariants are false or stale**~~ — **RESOLVED 2026-08-24, same session.** The
   audit hook does not run last (`flask_compress` follows it); corrected in the SADD, the `app.py`
   comment and the test docstring that had all three inherited the claim, and now pinned by
   `test_the_chain_order_is_what_the_docs_claim`, which was mutation-checked to confirm it fails on a
   real regression. The "two modules import `app` in a function body" note named two modules that
   are both now clean; the SADD and `core-designs.mdc` now name `agent/registrations.py`, the one
   that is not, and record why the meta-test missed it.

## Next actions

| # | Action | Addresses | Effort |
| :----: | :---- | :---- | :---- |
| ~~1~~ | ~~Decide the lake sink's fate — specify it as an interface or remove it~~ — **done**: specified, secured, opt-in, requirmented (REQ-48/49/50) | Design C1, Requirements C-3 | Small + a scope decision |
| ~~2~~ | ~~Design the two refusal paths in the SADD's Dynamic Design~~ — **done** | Requirements B-1, B-2 | Small — rationale exists in `.cursor/rules/` |
| **2a** | **Extend the `plan_hash` binding to the twelve legacy launch endpoints**, or route them through prepare/launch. Opened by closing B-1 | Requirements B-1a | Medium — one shared helper, thirteen call sites |
| ~~3~~ | ~~Correct the audit-ordering claim in the SADD and the false comment at `app.py`~~ — **done**, plus a mutation-checked guard test | Design B1 | Trivial |
| ~~4~~ | ~~Update the stale `import app` debt note~~ — **done** in the SADD and `core-designs.mdc` | Design B2 | Trivial |
| 5 | Requirement the interactive terminal and the AI assistant, or scope them out | Requirements C-1, C-2 | Small |
| 6 | Add Legal-and-Standards and Virtualization requirements, or mark N/A | Requirements A-1, A-2 | Small |
| 7 | Add an Operational Considerations section, stating "none committed" where true | Design A8 | Medium |
| 8 | Run acceptance validation and the traceability matrix for the auth feature | the two SKIPs | Medium |

## Phase freshness

| Phase | Ran this invocation | Commit | Freshness |
| :---- | :---- | :---- | :---- |
| requirements_validation | yes | `72b01c5` | current for workflow reuse |
| design_validation | yes | `72b01c5` | current for workflow reuse |
| code_implementation_check | yes | `72b01c5` | current for workflow reuse |
| verification | no — reused | `72b01c5` | current for workflow reuse |
| security_scan | no — reused | Aug. 2026 scan set | current with caveat (not re-executed) |
| acceptance_validation | no — skipped | — | not started |
| traceability_matrix | no — skipped | — | not started |

"Current" means the workflow can reuse the prior result without redoing the validation. It is not
formal PLC approval, release-gate clearance, scanner satisfaction, or team sign-off.

## Disclaimer

Advisory developer feedback from `plc-v-model-validate`, coordinating
`plc-requirements-validation` and `plc-design-validation`. It does not replace nSpect, Pulse,
ScanSpect, SonarQube, Coverity, BlackDuck, Anchore, OSRB, LaunchAPI or TAVA, and does not assert PLC
compliance or release-gate satisfaction.
