# SADD Validation Report: NVIS Rack Status

**SADD source**: `.plc/design/SWE-PLC-L1-003-BasicPLC-SADD-NVIS-RACK-STATUS.md`
**Reference used**: standard PLC design guidelines (no materialized team template at `.plc/templates/sadd-template.md`)
**Codebase(s) reviewed**: `nvis/nvis-rack-status` @ `72b01c5`, branch `feat/auth-audit-logging` — backend (130 modules), frontend (`frontend/src`), `bin/`, CI config
**Date**: 2026-08-24
**Waivers**: none found — normal

> **Advisory only.** These findings improve quality and surface gaps early. They do **not** satisfy
> PLC requirements, assert compliance, or replace nSpect, Pulse, ScanSpect, SonarQube, Coverity,
> BlackDuck, OSRB, LaunchAPI or TAVA.

## Summary

- **Group A (guideline compliance): 8 areas checked — 3 covered, 5 partial, 0 missing.** No guideline
  area is absent. The gaps are depth, concentrated in Interface Architecture and Operational
  Considerations, which the severity rubric rates High.
- **Group B (design → code): 5 findings, 0 High.** The SADD is unusually accurate for a reconstructed
  document — 218 routes, 89 actions over 35 services, TLS ≥ 1.2 on both serving paths, owner-only
  audit permissions, no CORS anywhere, 16 vendored fonts and 2,294 tests all verified exactly as
  claimed. The findings are one incorrect invariant, one stale debt note, and three count drifts.
- **Group C (code → design): 4 findings, 1 High.** The one that matters: an **undocumented outbound
  data-egress path** that POSTs captured request *and response* bodies over **plain HTTP**.
- **Top risk:** the lake sink (C1). It is absent from the SADD's external interface table, absent
  from the SRD entirely, and unencrypted by default — while REQ-30 mandates TLS ≥ 1.2 for the
  inbound surface.
- **Second risk:** the SADD asserts the audit hook "runs last" and sees the fully decorated
  response (B1). It does not; `flask_compress` runs after it. The same claim is asserted in a code
  comment, so both would mislead a maintainer relying on the invariant.
- **Scope caveat:** deployment infrastructure (the OVA's `docker-compose.override.yml`, the head
  node's `bin/scripts` copy) is not in this repo, so claims about deployment topology are capped at
  Medium confidence.

## Group A — Design Guidelines Compliance

### A1. Proposed Design (and alternatives considered)
- **Status**: Covered
- **Severity**: —
- **Confidence**: High
- **Evidence**: *Design Alternatives* gives 8 decisions in Chosen / Rejected / Why form, each with
  the concrete failure that motivated it (`app.py` at 20,000 lines and 135 routes; four in-memory
  job dicts; head/tail sampling missing results mid-file). The citizen inventory names 13 owners.
- **Remediation**: none. This is the document's strongest section.

### A2. Interface Architecture
- **Status**: Partial
- **Severity**: High (Interface per the rubric)
- **Confidence**: High
- **Evidence**: *External Interface and Specification* lists 8 interfaces with type, direction and a
  one-line spec. The guideline asks for seven things per interface; the table supplies roughly two.
  Absent for every interface: data transfer content/format/sequence, data formats on both sending
  and receiving sides (item names, codes, abbreviations), interface procedures, interface equipment,
  and data conversion requirements. Operational and security implications are given for some rows
  (`/ws/terminal` origin-checking) and not others.
  The table also **omits an interface that exists** — see C1.
- **Remediation**: expand the highest-risk rows rather than all eight. The HTTP/JSON API already has
  a generated reference (`openapi.py`) that can be cited for formats instead of restated. `/ws/terminal`
  and the lake sink need genuine specs: message framing and lifecycle for the former, payload
  schema, destination, transport security and retention for the latter.

### A3. System KPIs and Metrics
- **Status**: Partial
- **Severity**: Medium
- **Confidence**: High
- **Evidence**: The SRD declares two KPI-typed requirements. REQ-35 (5 ms p95 audit overhead) has a
  design mechanism, though stated only in passing as `catalog/context (generation-cached)` in the
  control flow. REQ-24 (bounded fan-out respecting session limits) has no instrumentation design at
  all. There is no section describing how any KPI is measured in the field — only how it was measured
  once in a test.
- **Remediation**: add a short KPI section: for each KPI requirement, the metric, where it is
  emitted, and how an operator observes it in a live deployment. The audit event already carries a
  latency field, which makes REQ-35 field-observable; say so.

### A4. Security Design
- **Status**: Partial
- **Severity**: Medium
- **Confidence**: High
- **Evidence**: The section is substantive — a 9-control table (authentication, terminal, command
  injection, transport, browser, source provenance, audit, read-only posture, planned identity) plus
  a reference to the threat model at T-1..T-64. Two guideline elements are absent: no security
  standard is referenced (NIST, OWASP or equivalent), and no control cites the SRD requirement or
  threat ID it satisfies, so the mapping between REQ-27..36 / T-1..T-64 and these nine controls must
  be inferred by the reader.
- **Remediation**: add a REQ/T-ID column to the control table. The mapping already exists in the
  authors' heads and in the feature brief; writing it down is what makes the section auditable.

### A5. Configure Software to Have Secure Settings by Default
- **Status**: Partial
- **Severity**: Medium
- **Confidence**: High
- **Evidence**: The product has real secure-by-default decisions, and the SADD mentions several in
  passing rather than collecting them: TLS self-signed by default; the container binds
  `127.0.0.1` by default (`docker-compose.yml`); the session cookie's `Secure` flag is set
  automatically when TLS is configured; the trusted-proxy set defaults to **empty** so a forwarded
  header is never trusted unless configured; CSP defaults to `enforce`; demo mode performs no
  mutation. A reader cannot currently find these in one place, and "what is safe out of the box" is
  precisely the question an install-time reviewer asks.
- **Remediation**: one table listing each security-relevant default, its value, and the risk it
  reduces. All the content exists in `docs/ENVIRONMENT.md`; this is a collection exercise.

### A6. Debugging and Troubleshooting
- **Status**: Partial
- **Severity**: Medium
- **Confidence**: High
- **Evidence**: *Logging and Debugging* covers sink selection, level, format and redaction well.
  The guideline also asks for error messages and their definitions, and how to troubleshoot each.
  *Error Handling* gives the conventions (failures as data; JSON errors with the one deliberate HTML
  503 exception) but defines no specific error and gives no troubleshooting procedure. The product
  has a rich set of diagnosable failures documented elsewhere — stale ControlMaster, `Prolog error`
  drains, mismatched SPA bundle — and none reaches this document.
- **Remediation**: a table of the top recurring failure signatures, what each means, and the first
  diagnostic step. `.cursor/rules/*.mdc` already holds the content.

### A7. Logging and Instrumentation
- **Status**: Covered
- **Severity**: —
- **Confidence**: High
- **Evidence**: Central configuration, sink selection, format, redaction, pytest-capture
  preservation, and — the part that matters — independent rotation budgets so application log volume
  cannot evict audit evidence. The audit subsystem is described separately with its vocabulary,
  chokepoint and sink.
- **Remediation**: none.

### A8. Operational Considerations
- **Status**: Partial
- **Severity**: High (Operational per the rubric)
- **Confidence**: High
- **Evidence**: Four of the eight sub-items are addressed, mostly implicitly: **Error Handling**
  (its own section), **Recoverability** (runs persist under `var/` and survive restart),
  **Capacity** (head-node `MaxSessions` bounds fan-out), **Data Retention** (audit retention target
  and byte ceiling). Four are absent: **Availability** (no statement of expected uptime or
  degradation behaviour when the head node is unreachable), **Failure Contingencies** (no stated
  behaviour when Slurm, the cluster manager, or VictoriaMetrics is down — though the code degrades
  gracefully), **Performance and Timing** (the SRD records deliberately that no targets exist beyond
  REQ-35, which is a decision the SADD should restate rather than omit), and **Adaptability**.
- **Remediation**: an Operational Considerations section with a subsection per sub-item, stating
  "none committed" where that is the honest answer. The absence of a performance target is
  information; an absent section is not.

## Group B — Design-to-Code Inconsistency

### B1. The audit hook does not run last, so it does not observe the fully decorated response
- **SDD claim**: *Control Flow* — `after_request (reverse registration order): security headers ──►
  no-store ──► audit emit (runs last, sees final response)`. `backend/app.py:688-692` asserts the
  same thing in a comment: "the first registered runs last — which is what lets the audit hook
  observe the fully decorated response".
- **Code reality**: the audit hook is **not** the first registered. `Compress(app)` runs at
  `backend/app.py:206`, ~490 lines before `_audit_request_hook.install(app)` at
  `backend/app.py:697`. Resolving the live app confirms the execution order is
  `_lake_capture_request → _security_headers → _api_json_no_store → _audit_emit →
  flask_compress.after_request`. A third-party handler runs after the audit hook and rewrites the
  body encoding, `Content-Encoding`, `Content-Length` and `Vary`.
- **Severity**: Medium
- **Confidence**: High
- **Remediation**: fix the doc and the comment, not the code — the order is harmless today because
  the audit event records status code, principal, action and latency, none of which compression
  alters. State the true invariant: the audit hook runs last **among the application's own**
  handlers, and its latency figure excludes compression. Correct the claim rather than leaving an
  invariant that a future change could rely on and be wrong about.

### B2. The "two modules import app in a function body" debt note is stale in both count and identity
- **SDD claim**: *Dependencies (design-level)* — "Two modules still `import app` inside function
  bodies to dodge a cycle; that is recorded debt". `.cursor/rules/core-designs.mdc` names them:
  `workflow_engine.py` and `bcm_query.py`.
- **Code reality**: **one** module does it, and it is neither of those. A word-boundary search across
  all 130 non-test backend modules returns exactly one hit:
  `backend/agent/registrations.py:53: import app as _app_mod`. `backend/workflow_engine.py` and
  `backend/bcm_query.py` are both clean.
- **Severity**: Medium
- **Confidence**: High
- **Remediation**: the debt was largely paid and the record was not updated, while a new instance
  appeared in a module the docs never mentioned. Update both the SADD and `core-designs.mdc` to name
  `agent/registrations.py`. Worth noting the meta-test `test_no_deferred_app_lookups.py` catches
  deferred *attribute* lookups but not this import form, which is why the drift went unnoticed.

### B3. The `api/` module count includes files the loader does not register
- **SDD claim**: "`backend/api/` (38)", "38 auto-discovered `api/` modules", "38 modules, 218 routes".
- **Code reality**: `backend/api/` holds 38 `.py` files, but the loader registers **36** — it logs
  `api: registered 36 module(s)` at startup. The other two are `__init__.py` (the loader itself) and
  `_common.py` (underscore-prefixed, deliberately skipped so it can hold Flask-dependent shared
  helpers). Route count is exactly right: 218.
- **Severity**: Low
- **Confidence**: High
- **Remediation**: say "36 registered domain modules (38 files, including the loader and
  `_common.py`)". The distinction is the design's own rule about underscore-prefixed modules, so
  getting it right reinforces the point.

### B4. `app.py` holds four route decorators, not three
- **SDD claim**: "composition root: ~970 lines, 3 routes"; and "It is now ~2,000 lines and 3 routes"
  in `core-designs.mdc`.
- **Code reality**: 968 lines (accurate) and **four** `@app.route` decorators — `/` (886), `/spa/`
  (895), `/spa/<path:path>` (896) and `/<path:path>` (902). Six rules resolve to the app object
  directly.
- **Severity**: Low
- **Confidence**: High
- **Remediation**: say "four SPA catch-all routes". The spirit of the claim — that `app.py` holds
  no domain routes — is correct and worth keeping; the number is just wrong.

### B5. "134 backend modules" depends on an unstated counting rule
- **SDD claim**: "reconstruction from the shipped source tree (134 backend modules, 38 API modules,
  218 routes)".
- **Code reality**: 130 `.py` files under `backend/` excluding tests and `__init__.py`; the
  tracked-tree gate reports "134 modules imported", which counts package `__init__.py` files. Both
  numbers are defensible; the document does not say which it means.
- **Severity**: Low
- **Confidence**: High
- **Remediation**: state the rule once ("134 importable modules, per `bin/check-tracked-tree.sh`").

### Claims verified accurate

Recorded because a validation report that lists only failures misrepresents the document: 218 routes;
89 distinct actions over 35 services across 105 endpoint/method pairs; `core.py` imports neither
Flask nor `app` (zero matches); no `api/` module imports `app`; no CORS header emitted anywhere (the
single grep hit is a comment explaining why not); audit sink permissions `0o600` file / `0o700`
directory; TLS floor 1.2 applied through the *same* `tls_server` policy on both the gunicorn
`ssl_context` hook and the Werkzeug dev path; 16 vendored `woff2` fonts; 2,294 backend tests; 30
views / 121 components / 37 lib modules / 13 service clients / 7 context pairs.

## Group C — Code-to-Design Inconsistency

### C1. An outbound data-egress path captures request and response bodies and ships them over plain HTTP
- **Code behavior**: `backend/lake_sink.py` POSTs to `RACK_LAKE_URL`, defaulting to
  **`http://host.docker.internal:9410`** — plain HTTP (`lake_sink.py:57`, `_post` at `:113-119`, via
  `urllib.request`). It is fed by a `before_request`/`after_request` pair in `app.py`:
  `_lake_capture_start` (`:820-822`) caches up to `_LAKE_BODY_MAX` bytes of the **request** body, and
  `_lake_capture_request` (`:827-861`) captures the **response** body, the full query string, the
  request body, HTTP status and duration into a `rackstatus_request` record. The audit subsystem's
  client-address provenance work (REQ-36) means source addresses are in scope for this path too.
- **SDD gap**: the *External Interface and Specification* table lists 8 interfaces and this is not
  one of them. The *Control Flow* diagram mentions "lake capture" in `before_request` but never as
  an outbound interface, and the *Data Flow* section does not include it. The SRD has no requirement
  for it either (recorded as C-3 in the requirements validation report).
- **Severity**: High
- **Confidence**: High
- **Remediation**: this needs a decision before documentation. If the path is live, it is an
  outbound interface carrying request/response payloads and must appear in the interface table with
  payload schema, destination, transport security and retention — and its plaintext default sits
  oddly beside REQ-30's TLS ≥ 1.2 mandate for the inbound surface and directly engages SRD open
  question 3 (data-handling classification: what may leave a customer site). If it is vestigial,
  remove it. Note it is disabled by default (`is_disabled()` is checked first), which lowers
  present exposure but not the documentation gap.

### C2. Response compression is configured with a deliberately narrowed mimetype list, and the SADD never mentions compression
- **Code behavior**: `Compress(app)` at `backend/app.py:206`, with `COMPRESS_MIMETYPES` set
  explicitly (`:192`), `COMPRESS_LEVEL = 6`, `COMPRESS_BR_LEVEL = 5`, `COMPRESS_MIN_SIZE = 1024`.
  The comment at `:187-190` records why the list is narrowed: Flask-Compress **buffers the entire
  response** to compress it, so leaving `text/plain` in the list makes a streamed log tail arrive
  only at the end — and `X-Accel-Buffering` does not help because it only affects nginx.
- **SDD gap**: compression appears nowhere in the SADD. It affects an externally visible interface
  property (`Content-Encoding`, `Vary`), it is the reason the audit hook is not actually last (B1),
  and it has a real failure mode that a maintainer widening the mimetype list would reintroduce —
  a "Connecting…" hang in the log viewer.
- **Severity**: Medium
- **Confidence**: High
- **Remediation**: add it to the interface/operational description, including the
  streaming-versus-compression constraint. This is exactly the class of hard-won operational
  knowledge the SADD's own citizen model exists to preserve.

### C3. The `after_request` chain has five handlers; the SADD's control flow shows three
- **Code behavior**: the live chain is `_lake_capture_request → _security_headers →
  _api_json_no_store → _audit_emit → flask_compress.after_request`. The lake capture runs **first**,
  before security headers are applied.
- **SDD gap**: the *Control Flow* block lists only `security headers ──► no-store ──► audit emit`.
  A reader would not know two other handlers participate, nor that the response body is read for
  capture before the security headers exist on it.
- **Severity**: Medium
- **Confidence**: High
- **Remediation**: show the full chain. Combined with B1, the fix is one corrected diagram.

### C4. A background cache-sync thread starts at import and is not in the design
- **Code behavior**: `backend/nvftp_cache_sync.py` starts a daemon thread during application import
  — visible on every startup as `NVFTP cache sync thread started (interval 7200s, enabled=False)`.
- **SDD gap**: no citizen row, no mention in the dynamic design. The SADD states that anything
  outliving a request is owned by the workflow citizen, which this thread is not — so it is both an
  undocumented component and an apparent exception to a stated architectural rule.
- **Severity**: Low
- **Confidence**: High
- **Remediation**: name it in the component inventory, and either justify it as an exception to the
  workflow-citizen rule or note it as debt. It is disabled by default, so the risk is
  documentation, not behaviour.

## Scope and Caveats

- **Not reviewed:** the OVA deployment's `docker-compose.override.yml` (~50 bind mounts, not in
  git), the head node's deployed `bin/scripts` copy, and the Grafana/VictoriaMetrics/Loki
  deployments. Any Group B claim about deployment topology is therefore capped at Medium confidence;
  none of the findings above depend on those.
- **Frontend depth:** the frontend inventory claims were verified by counting (30 views, 121
  components, 7 context pairs, 16 fonts). The behavioural claims about the workflow designer's
  Mermaid round-trip were read but not exercised, so they are unverified rather than confirmed.
- **Counting drift is expected and mostly benign.** The SADD was written against a tree that has
  since advanced by several commits, including ones from this session. B3–B5 are reported for
  accuracy, not as defects.
- **`.plc/tmp/` scratch:** none created; the SADD was read from its repository path.
