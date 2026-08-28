# Traceability Matrix

## Matrix

\<content authored by agent, review thoroughly and remove after human review\>

Agentic PLC version: v1.13.0; Skill: plc-traceability-matrix; LLM: unknown; Timestamp: 2026-08-28T21:11:19Z; Input ref: NVRACKSTAT-52

| Req ID | Req text | Repo | Revision | Design ref | Code | Test ID | Last verified | Status |
|---|---|---|---|---|---|---|---|---|
| FR-1 | The application shall apply a single logging configuration once during startup, such that every logger obtained via `logging.getLogger` routes to the configured sinks without changes at the call site. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-2 | The logging configuration shall support the sinks `stdout`, `file` (size-rotating) and `syslog` (network, UDP), individually selectable at deployment time, defaulting to `stdout` and `file` with `syslog` off. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-3 | The logging configuration shall support a configurable minimum level and a configurable record format of either human-readable text or single-line JSON. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-4 | The logging configuration shall apply a redaction filter that replaces the value of any configured sensitive key with a fixed placeholder before a record reaches any sink. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-5 | The application shall emit a structured audit event for every operator-initiated action, carrying: event id, UTC timestamp, principal, authentication method, service, action, action variant, resource, decision, result, source address, source-address provenance, user agent, request id, active cluster, and duration. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-6 | Audit emission shall occur at a single request-processing chokepoint that observes every `/api/*` request, such that adding a new endpoint requires no endpoint-specific audit code. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-7 | The audit sink shall append each event as one line to a JSONL file, never rewriting or truncating a previously written line, and shall by default bound total audit storage by size-capped rotation. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-8 | The audit event shall record the TCP peer address as `src_ip`; shall record a forwarded address chain only when the peer is a configured trusted proxy, resolved by scanning the chain right-to-left past known proxies; and shall label the derivation in `src_ip_trust` as `peer`, `proxy`, or `claimed-untrusted`. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-9 | The audit event shall record the principal from the authenticated session when one exists, and otherwise the literal `anonymous`; it shall not derive the principal from a client-supplied header. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-10 | The application shall record an audit event for both permitted and refused actions, with `result` distinguishing success, failure and error. | — | 434c93298219b1f9afdeb183188c6a94b6787238 | — | — | — | 434c932  2026-08-28 | GAP |
| FR-11 | Audit logging shall be enabled by default and shall produce a usable audit trail on a fresh deployment with no configuration supplied. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-12 | The application shall audit mutating actions and authentication events by default, and shall exclude read-only requests from the audit trail unless read auditing is explicitly enabled. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-13 | Every configuration knob introduced by this feature shall have a working default, such that no new environment variable is required for the feature to function. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| FR-14 | Each audited action shall be identified by an explicitly declared action name drawn from a validated vocabulary, independent of the name of the function implementing it, such that renaming or moving the implementation does not change the recorded action. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-1 | A failure to configure logging, to write an audit event, or to reach a log sink shall not fail, delay past its timeout, or alter the outcome of the operator request that triggered it. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-2 | The audit and logging path shall perform no configuration-ladder read, no filesystem stat, and no audit write while serving a static-asset request. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-3 | No value of a configured sensitive key, and no raw credential, shall appear in any log record, audit event, or audit file. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-4 | The audit file and any audit directory shall be created with owner-only permissions. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-5 | The trusted-proxy list and the sensitive-key list shall be deploy-time configuration that an authenticated dashboard operator cannot alter at runtime. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-6 | The feature shall not require edits to the existing 81 `logging.getLogger` call sites. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-7 | The product shall document that, for tunnelled access, `src_ip` records the tunnel endpoint rather than the operator's address, and that the operator's address exists only in the SSH server's authentication log. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-8 | Hardening beyond the default — external syslog, transport encryption, encryption at rest, remote retention — shall be opt-in, and the audit trail shall remain fully functional with all of it disabled. | — | 434c93298219b1f9afdeb183188c6a94b6787238 | — | — | — | 434c932  2026-08-28 | GAP |
| NFR-9 | The default configuration shall require no network egress, no external service, and no additional credential. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-10 | The audit sink shall serialize every event through a JSON encoder that escapes control characters, and shall never construct an audit record by string formatting or concatenation. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-11 | The audit event shall exclude query strings from `resource` by default, and the redaction filter shall redact configured sensitive key names appearing as query parameters. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-13 | The audit event shall not record an authorization decision that no policy engine made: until an engine is wired, `decision` shall read `not-evaluated` and the event shall carry `policy_engine` as `none`. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |
| NFR-14 | The trusted-proxy configuration shall reject an entry broader than a `/24` (IPv4) or `/64` (IPv6), and shall never accept a default route, reporting the refusal at startup. | — | d5e4f7cbd4afa813aa89ab975a6885dd13440d36 | — | — | — | d5e4f7c  2026-08-28 | GAP |

## Gaps

| Req ID | Status | Missing / stale columns | Suggested next step |
|---|---|---|---|
| FR-1 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-2 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-3 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-4 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-5 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-6 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-7 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-8 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-9 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-10 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-11 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-12 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-13 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| FR-14 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-1 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-2 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-3 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-4 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-5 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-6 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-7 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-8 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-9 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-10 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-11 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-13 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |
| NFR-14 | GAP | Code, Design ref, Test ID | fill the missing traceability pointer and rerun verification evidence |

## Coverage Summary

| Type | Covered | Partial | Gap |
|---|---:|---:|---:|
| Functional | 0 | 0 | 14 |
| KPI | 0 | 0 | 1 |
| Security | 0 | 0 | 6 |
| Other Non-Functional | 0 | 0 | 6 |

## Sources Used

- Feature brief: .plc/briefs/auth-audit-logging-brief.md
- Evidence log: plc-evidence.jsonl

## Document Change Log

| Timestamp | Section changed | Change summary | Provenance | Human decision | Reviewed by | Acceptance reference |
|---|---|---|---|---|---|---|
| 2026-08-28T21:11:19Z | Matrix | Initial agent-authored report | Agentic PLC version: v1.13.0; Skill: plc-traceability-matrix; LLM: unknown; Timestamp: 2026-08-28T21:11:19Z; Input ref: NVRACKSTAT-52 | pending | \<dev\> | none |

## Disclaimer

This matrix is **advisory only**. It surfaces traceability gaps between
requirements, design, code, tests, and recorded evidence so reviewers can
spot missing or stale links early. It does NOT satisfy PLC requirements,
replace formal audit-trail attestation, or replace deterministic release
validation (qualified scanners, evidence generation, policy enforcement
via LaunchAPI, TAVA, nSpect, ScanSpect, SonarQube, Coverity, BlackDuck,
OSRB). A `COVERED` row is "current for workflow reuse" — not release-gate
clearance. Your work must still go through the governed PLC release
process.

---

## Companion matrix — built from the test plan, the resolved tests and the evidence log

The generated table above marks every requirement `GAP` on *Code, Design ref, Test ID*. That is
a tooling artefact, not the state of the work: `plc_traceability_matrix.py` accepts no
`--test-plan` argument and does not scan source, so it has no pointer to resolve for any row.
The pointers exist, and this section resolves them.

How each column was obtained, so a reader can re-derive it:

- **Design / component** — the *Component* cell of each TC row in the test plan.
- **Test cases** — the requirement's own *Verifies* field in the test plan, reversed.
- **Test nodes / outcome** — pytest node ids resolved by two exact strategies (the
  `TestTC<N>` class convention the implementation used, and the plan's cited function names
  where they still resolve), then **executed in this session**. No pointer was keyword-matched.
- **Evidence rows** — rows in `plc-evidence.jsonl` naming that requirement or one of its ACs.

| Requirement | Design / component | Test cases | Test nodes | Executed outcome | Evidence rows | Unresolved TC |
|---|---|---|---|---|---|---|
| **FR-1** | logging_config.py | TC-1, TC-36 | 4 | 4 passed | 2 | — |
| **FR-2** | logging_config.py | TC-2, TC-35 | 5 | 5 passed | 2 | — |
| **FR-2a** | logging_config.py | TC-2, TC-21 | 6 | 6 passed | 1 | — |
| **FR-3** | logging_config.py | TC-3 | 2 | 4 passed | 2 | — |
| **FR-4** | logging_config.py | TC-4 | 4 | 4 passed | 1 | — |
| **FR-5** | audit/__init__.py | TC-6 | 3 | 3 passed | 1 | — |
| **FR-6** | app.py | TC-22, TC-37 | 7 | 7 passed | 1 | — |
| **FR-7** | audit/sink.py | TC-8, TC-9 | 3 | 3 passed | 7 | — |
| **FR-7a** | audit/sink.py | TC-11 | 2 | 2 passed | 1 | — |
| **FR-7b** | audit/sink.py | TC-38 | 6 | 6 passed | 2 | — |
| **FR-7c** | audit/sink.py, logging_config.py | TC-10 | 3 | 3 passed | 1 | — |
| **FR-7d** | audit/sink.py, logging_config.py | TC-10 | 3 | 3 passed | 1 | — |
| **FR-8** | api/_common.py, core.py | TC-19 | 8 | 8 passed | 2 | — |
| **FR-9** | — | TC-26 | 1 | 1 passed | 1 | — |
| **FR-10** | — | TC-24, TC-31 | 4 | 4 passed | 6 | — |
| **FR-10a** | audit/__init__.py, audit/sink.py | TC-13, TC-14 | 4 | 4 passed | 3 | — |
| **FR-10b** | api/terminal.py | TC-25, TC-31 | 4 | 5 passed | 1 | — |
| **FR-11** | — | TC-23 | 4 | 4 passed | 3 | — |
| **FR-11a** | audit/sink.py, logging_config.py | TC-10, TC-35, TC-40 | 4 | 4 passed | 2 | TC-40 |
| **FR-12** | audit/actions.py | TC-18, TC-23 | 5 | 5 passed | 2 | — |
| **FR-13** | audit/sink.py, logging_config.py | TC-5 | 5 | 5 passed | 1 | — |
| **FR-14** | audit/actions.py | TC-15 | 6 | 9 passed | 5 | — |
| **FR-14a** | audit/actions.py | TC-16 | 5 | 11 passed | 1 | — |
| **FR-14b** | audit/actions.py | TC-18 | 1 | 1 passed | 2 | — |
| **FR-14c** | audit/actions.py | TC-17 | 6 | 6 passed | 2 | — |
| **NFR-1** | audit/sink.py, logging_config.py | TC-28 | 1 | 1 passed | 4 | — |
| **NFR-2** | — | TC-29 | 3 | 3 passed | 2 | — |
| **NFR-2a** | — | TC-34 | 1 | 1 passed | 1 | — |
| **NFR-3** | logging_config.py | TC-4 | 4 | 4 passed | 1 | — |
| **NFR-4** | audit/sink.py | TC-12 | 1 | 1 passed | 1 | — |
| **NFR-5** | — | TC-30 | 1 | 1 passed | 1 | — |
| **NFR-6** | — | TC-39 | 0 | not executed | 2 | TC-39 |
| **NFR-7** | — | TC-21 | 2 | 2 passed | 1 | — |
| **NFR-8** | — | TC-31 | 2 | 2 passed | 1 | — |
| **NFR-9** | audit/sink.py, logging_config.py | TC-5 | 5 | 5 passed | 1 | — |
| **NFR-10** | audit/sink.py | TC-7, TC-32 | 4 | 4 passed | 2 | — |
| **NFR-11** | — | TC-33 | 1 | 1 passed | 1 | — |
| **NFR-13** | — | TC-27 | 2 | 2 passed | 1 | — |
| **NFR-14** | — | TC-20 | 5 | 12 passed | 1 | — |

**39 requirements**, each with a design pointer and at least one test case. 137 test-node references across the matrix (requirements share cases, so this exceeds the 99 distinct nodes executed); every executed node passed.

- Requirements with **no evidence row**: 0
- Requirements resolving to **no executed test**: 1 — NFR-6

### Unresolved test cases

Two remain, and both are unresolved **by design** — they verify by a method that is not a
pytest run, so their evidence is a committed checklist rather than a node id:

| TC | Method | Evidence |
|---|---|---|
| TC-39 | inspection — the feature diff carries no hunk touching a pre-existing `logging.getLogger` call site (NFR-6) | `docs/AUDIT_PHASE1_VERIFICATION.md` |
| TC-40 | demonstration — audit history survived a container recreate on the `rack-data` named volume (FR-11a / AC-10f) | `docs/AUDIT_PHASE1_VERIFICATION.md` |

NFR-6 therefore shows **no executed test** above and that is correct, not a gap: an
inspection has no test to execute.

**Closed on 2026-08-28**, and worth recording because the plan claimed no gaps existed while
these two had no implementation at all:

- **TC-31** (NFR-8 / AC-26) — the end-to-end case. Now `backend/tests/test_audit_e2e.py`: a real gunicorn child with token auth on, driven with a permitted action, an auth refusal and an unauthenticated `/ws/terminal` upgrade, all asserted present in the trail while every audit hardening option stays at its default. **This was the sole finding in the acceptance report, and it now passes.**
- **TC-25** (FR-10b / AC-14d) — the terminal refusal hook. Now `TestTC25TerminalRefusalsAreAudited`, parametrized over all three reasons `_refuse_reason` can return, plus a case proving the three do not collapse into one flood-guard entry.

Both were checked by **mutation** rather than by passing alone: deleting the
`_audit_terminal_refusal` call in `api/terminal.py` makes the end-to-end case fail. A refusal test that still passes with the audit call removed is worse than no test, because it reports coverage that does not exist.

### Pointer drift found while building this — now fixed

**25 of the 54 pytest node ids the test plan cited resolved to nothing**: 7 named three files
that were never created (`test_audit_e2e.py`, `test_audit_injection.py`,
`test_audit_settings.py`) and 18 named functions renamed when the tests were reorganised into
`TestTC<N>...` classes. The coverage was intact throughout, which is exactly what made it
invisible — every test passed, and only a reader who tried to *follow* a citation would find it
led nowhere.

All 103 citations were regenerated from the tree on 2026-08-28 and now collect cleanly (119
tests, zero unresolvable). The `TestTC<N>` class convention is the durable anchor and is what
this matrix resolves against: the TC id lives in the class name, so renaming a method cannot
break the mapping and the column can be re-derived from source at any time.

One correction was drafted and **withdrawn**, and it is the reason only exact-match strategies
feed this matrix: a keyword search for TC-25's subject matter found
`test_principal_never_from_header`, which is real, passing, and verifies a *different*
requirement (FR-9/AC-13, TC-26). Recording it would have put a false pointer into the audit
trail — a plausible-looking citation is worse than an absent one, because nobody re-checks it.

One correction was drafted and **withdrawn** during this run, and it is worth recording why: a
keyword search for TC-25's subject matter found `test_principal_never_from_header`, which is real,
passing, and verifies a *different* requirement (FR-9/AC-13, TC-26). Recording it would have put a
false pointer into the audit trail. Only exact-match strategies feed this matrix.
