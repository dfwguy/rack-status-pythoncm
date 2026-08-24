# PLC Requirements Validation Report — NVIS Rack Status

| Item | Value |
| :---- | :---- |
| Date | Aug. 24, 2026 |
| Repo | `nvis/nvis-rack-status` @ `72b01c5` (branch `feat/auth-audit-logging`) |
| SRD | `.plc/requirements/SWE-PLC-L1-002-BasicPLC-SRD-NVIS-RACK-STATUS.md` (47 requirements, 16 use cases) |
| SADD | `.plc/design/SWE-PLC-L1-003-BasicPLC-SADD-NVIS-RACK-STATUS.md` (13 citizens, 8 design alternatives) |
| Reference used | Standard PLC SRD sections (`srd-sections.md`) — no materialized team template at `.plc/templates/srd-template.md` |
| Project type | General application (not GPU Driver), so Test Automatability is Warning-severity rather than Critical |
| Waivers | None found (`.plc/waivers.toml`, `agentic_plc/waivers.toml` absent) — normal |
| nSpect | NSPECT-S57Q-EI19 |

> **Advisory only.** These findings improve quality and surface gaps early. They do **not** satisfy
> PLC requirements, constitute PLC compliance, or replace nSpect, Pulse, ScanSpect, SonarQube,
> Coverity, BlackDuck, OSRB, LaunchAPI or TAVA.

---

## Summary

| Group | Critical | Warning | Info | Total |
| :---- | :----: | :----: | :----: | :----: |
| A — SRD template compliance | 0 | 3 | 3 | 6 |
| B — SRD → SADD coverage | 2 | 3 | 5 | 10 |
| C — SADD → SRD traceability | 0 | 3 | 2 | 5 |
| **Total** | **2** | **9** | **10** | **21** |

**What changed since the previous assessment.** The August security-only assessment recorded the
SRD, SADD and SUTP as `missing`, so requirements validation and design validation were both `SKIP`
— no findings were possible in either group, and the bidirectional consistency check could not run
at all. Both phases now execute. All 21 findings below are newly visible, not newly introduced.

**Template compliance is strong.** All 9 required content areas are present, and all 2 optional
areas are present — 11 of 11. The findings in Group A are about *completeness within* areas, not
missing areas. That is the expected shape for a document reconstructed from a shipped product: the
content existed before the document did.

**The two Critical findings share one cause,** and it is worth stating plainly: the SRD contains two
*refusal* requirements — a launch must be refused when the reviewed target no longer holds (REQ-6),
and a multi-rack target must be refused when its rack grouping is not a clean partition (REQ-7) —
and the SADD describes neither mechanism. Both are implemented, and both are Safety-typed. A design
document that omits the refusal path is describing the happy path of a safety control.

---

## Group A — SRD Template Compliance

### A-1 (Warning) Requirement type "Legal and Standards" is neither used nor marked N/A

Area 10 lists 13 PLC requirement types. The SRD's Type column uses Functional, Non-Functional,
Interface, Safety, KPI, Platform, Security, Telemetry, Backward Compatibility and Test
Automatability — 11 of 13 accounted for. **Legal and Standards** appears nowhere and is not declared
N/A.

This is not a paperwork gap. The product has real obligations in this category that are currently
unrequirmented: every source file must carry the NVIDIA SPDX proprietary header (enforced across the
tree for the v3→main merge), dependency **license** scanning runs in CI (`pip-licenses.json`,
`dependency-frontend-npm-package-lock-json.json` are committed scan outputs), and the vendored
Kaizen UI CSS and fonts are redistributed NVIDIA-brand assets with their own terms.

**Remediation:** add a requirement of type *Legal and Standards* covering the SPDX header obligation
and third-party license scanning, or state explicitly why the type is N/A for this product.

### A-2 (Warning) Requirement type "Virtualization" is neither used nor marked N/A

Same area. **Virtualization** is absent and not declared N/A, while the product's primary field
deployment shape is virtual: Nscale TX runs Rack Status as a container on its **own OVA** (Open
Virtual Appliance), every deployment is Dockerised, and the air-gapped distribution path is a
`docker save` image bundle. Demonstration mode (REQ-39) is arguably a simulation requirement in this
category too.

**Remediation:** add a *Virtualization* requirement for the container/OVA deployment shape, or mark
the type N/A with a reason. Note this interacts with REQ-37 (air-gapped) and REQ-40 (deployment
verification), which describe consequences of the virtualized shape without requiring the shape.

### A-3 (Warning) Requirements carry no per-requirement verification criteria or use-case linkage

Area 9 notes the enhanced requirements format (QA Applicable, Use Case Scenario, Verification
Criteria, Tools Required) is mandatory for GPU Driver projects. This is not one, so the enhanced
format is **not required** — but the SRD also has 16 use cases and 47 requirements with no mapping
between them, and no per-requirement verification statement.

Materially mitigated: the SUTP maps every REQ to named test files, so verification *is* traceable —
just in a different document. The residual risk is that a reader of the SRD alone cannot tell how a
requirement will be proven, and that UC→REQ coverage is unverifiable in either direction (no way to
show UC-11 is fully requirmented).

**Remediation:** the cheap version is a UC column on the requirements table. The SUTP already
supplies verification, so cross-reference it rather than duplicating it.

### A-4 (Info) Approval chain present but entirely unsigned

Area 1 is satisfied structurally — Approvers, Reviewers and Revision History tables all exist. Every
entry is "Pending", no dates are filled, and the Reviewers table carries one empty row. State is
"Development", which is honest for a reconstructed draft.

**Remediation:** none required while the document is a draft. Before this is offered as PLC audit
evidence it needs real approver dates, and the empty reviewer row should be removed or filled.
Note this is the same gap as SUTP gap G-9 (REQ-47 sign-off pending) in a different document — one
remediation pass could close both.

### A-5 (Info) Reference table carries no SWE numbers or revisions

Area 7 is satisfied — 10 references with titles and locations. The "SWE Number" column is `—` for
every row and revisions are month-granular ("Aug. 2026").

**Remediation:** populate SWE numbers for referenced PLC work products once they exist. Repository
documents legitimately have no SWE number; a git ref would be more precise than a month.

### A-6 (Info) Use cases identify actors informally

Area 8 is satisfied: 16 use cases with descriptions that name their actor in prose ("An SA opens
Cluster Overview…"). There is no separate actor list and no user-story form. Semantic matching
accepts this, and the SRD's stakeholder table covers actor identification.

**Remediation:** optional. If UC→REQ mapping is added per A-3, an actor column costs nothing.

---

## Group B — SRD → SADD Coverage

Checked all 47 requirements. **37 are well covered**, 5 partially, 5 not covered.

### B-1 (Critical) REQ-6 launch review gate has no design

> REQ-6: *The product shall resolve and display the exact target of a launch for review, and shall
> refuse the launch if the reviewed target no longer holds.* Type: Functional, Safety.

The SADD describes target resolution (citizen 9, naming/node_targets) and shows
`naming/node_targets ──► real hostnames` in the control flow. It does not describe the **review
gate** — the display-for-review step, the binding of a reviewed target to a subsequent launch, or
the refusal when the two disagree. This is the mechanism that stops a stale browser tab from
launching against last week's node set, and it is the whole point of the requirement.

**Remediation:** add a Dynamic Design subsection for the launch path: how a resolved target is
presented, what identifies it, and where the comparison-and-refusal happens
(`validation_launch.py`). The SRD's citizen-3 concept of a pinned, versioned campaign roster is the
natural place to anchor it.

### B-2 (Critical) REQ-7 per-rack fan-out refusal has no design

> REQ-7: *For recipes confined to one NVLink domain, the product shall submit one job per rack and
> shall refuse a target whose rack grouping is not a clean partition of the selection.* Type:
> Functional, Safety.

The SADD describes fan-out generically (citizen 1: "per-node work with bounded concurrency") but not
**per-rack job splitting**, not the NVLink-domain reason it exists, and not the fail-closed
partition check. The consequence of getting this wrong is documented and severe: a single Slurm
allocation spanning two physical racks joins two separate NVLink domains and the job **hangs**.

**Remediation:** add a design subsection covering the multirack dispatcher
(`bin/scripts/run-validation-preset.sh`, `--group-nodelist` per rack), the partition invariant (a
node in two groups or outside the selection is a refusal, not a warning), and the allocation
ceiling. `.cursor/rules/dcgm-mndiag-mnubergemm.mdc` already holds the rationale.

### B-3 (Warning) REQ-16 evidence-first node state change has no design coverage

> REQ-16: *…shall require an explicit node list and shall support recording a decisive reason before
> draining, resuming or resetting node state.* Type: Safety.

The SADD's Security Design table covers *disabling* cluster-manager mutation under read-only
posture, but never designs the guarded mutation path itself. `slurm_admin.py` — which owns drain,
resume and cancel — appears in no citizen row and nowhere in the document.

**Remediation:** either add `slurm_admin` to the citizen inventory with its guard design, or state
which citizen owns it. Note the SUTP independently flags this module as gap G-7 (no dedicated test
module, guards not directly asserted) — the same code is thin in both design and test coverage,
which is a meaningful correlation for a Safety requirement.

### B-4 (Warning) REQ-45 coverage gate has no design coverage

> REQ-45: *Every source file shall be covered by automated tests to at least 50%… enforced in CI.*

The SADD's Test Automation section describes the suite, the stub chokepoint and the meta-tests, but
not the coverage floor, its shrinking exception list, or where it runs. The requirement was added
today and the design was not updated with it.

**Remediation:** one paragraph in Test Automation naming `bin/check-coverage-floor.py`, the
exception-list ratchet, and the constraint that baselines must be measured in a tracked-files-only
tree.

### B-5 (Warning) REQ-47 attributable evidence has no design coverage

> REQ-47: *Verification evidence that is not an automated test… shall be committed to the repository
> and signed off by a named approver from the authorised set.*

Not designed anywhere. Also added today.

**Remediation:** state where such evidence lives (`docs/AUDIT_PHASE1_VERIFICATION.md` is the
existing instance) and what a sign-off record contains. Small, but leaving it undesigned is how the
sign-off table stays perpetually pending.

### B-6..B-10 (Info) Partial coverage

| Req | Covered | Not covered |
| :---- | :---- | :---- |
| REQ-1 canonical status | `rack_status_engine` citizen, leased snapshot, data flow | The **gating condition** — that no canonical snapshot publishes until its log scan has completed at least once. This is the "absent is not zero" invariant at fleet level |
| REQ-8 deep audits | Tools are catalog documents; execution via transport | Deep audits are not named as a design element; their structured-result shape is undescribed |
| REQ-14 report archive | Citizen 10, one dataset / many renderers | Durable retention and later retrieval — `report_store.py` is absent from the citizen inventory |
| REQ-15 post-run attribution | `node_report.py` in citizen 10 | The attribution *logic* — how participation and blame are decided, and specifically how the "not every node of a failed job is faulty" rule is realised |
| REQ-17 guarded cancellation | Read-only posture can disable it | The confirmation design for the enabled case (which job, confirmed how) |

---

## Group C — SADD → SRD Traceability

### C-1 (Warning) The AI assistant and contextual help have no SRD requirement

The SADD names `AIChatContext` and `HelpPanelContext` in the frontend context table, and the
codebase carries `ai_chat.py` plus `catalog/context.build_ai_context_block()` which injects an
authoritative platform/cluster/customer envelope into the assistant's system prompt.
`core-designs.mdc` names "Terminals integrated with AI" as a *planned citizen*.

No SRD requirement authorises any of it. For a feature that receives cluster identity and customer
context — and is planned to converge with an interactive shell — that is the traceability gap most
worth closing, because an unrequirmented feature is also an unthreat-modelled and untested one.

**Remediation:** add an SRD requirement for the assistant (what it may read, what it must never do),
or record it as deliberately out of scope pending the planned-citizen work.

### C-2 (Warning) The interactive terminal is constrained by requirements but never required by one

REQ-28 says the WebSocket terminal *shall be* authenticated and origin-checked. REQ-29 constrains
its arguments. Nothing says the product shall **provide** an interactive terminal at all — the
capability exists only as the object of two security constraints, and appears in the SADD's external
interface table and as WSGI middleware.

This inverts the intended direction: a capability should be required, then constrained.

**Remediation:** add a functional requirement for the interactive terminal (purpose, scope of
access, session lifecycle), so REQ-28 and REQ-29 constrain something the SRD asked for. The SUTP's
gap G-4 (terminal at 28.5%, needs WebSocket upgrade simulation) makes this the third document to
independently flag this component as thin.

### C-3 (Warning) The "lake capture" data path has no requirement

The SADD's control flow shows `before_request: audit timer, lake capture`, and `lake_sink.py` exists
(66.9% covered). No SRD requirement describes a data-lake sink, what it captures, where it sends it,
or its retention. Given it sits in the request path and captures request data — including source
addresses, per REQ-36's provenance labels — this is a data-handling surface with no stated
requirement.

**Remediation:** add a Telemetry-type requirement, or remove the path if it is vestigial. This also
bears directly on SRD open question 3 (data-handling classification), which asks what may leave the
site.

### C-4 (Info) The generated API reference has no requirement

`openapi.py` and the browsable API reference appear in the SADD's external interface table. No
requirement asks for a published API surface. Low risk, but it is an externally visible interface.

### C-5 (Info) Bug-report collection is implied by a use case, not required

UC-11 covers out-of-band hardware recovery, and `api/bug_report.py` fans out
`nvidia-bug-report.sh` via pdsh. No requirement covers diagnostic bundle collection specifically.

---

## Top risks

1. **Two Safety requirements have no designed refusal path** (B-1, B-2). Both are implemented; the
   design document describes the success path only. For an acceptance-evidence product, an
   undesigned refusal is the gap a reviewer will find.
2. **`slurm_admin.py` is thin in design and in test** (B-3, and SUTP G-7 independently). It owns
   drain, resume and cancel — the operations that remove capacity from a customer cluster.
3. **Three components run in production with no requirement authorising them** (C-1, C-2, C-3): the
   AI assistant, the interactive terminal, and the lake capture path. Each handles cluster or
   request data. Unrequirmented features are also unthreat-modelled features.
4. **Two of the 13 PLC requirement types are unaddressed** (A-1, A-2), and both are genuinely
   applicable — licensing/SPDX obligations and the container/OVA deployment shape.

## Next actions

| Priority | Action | Effort |
| :---- | :---- | :---- |
| 1 | Design the two refusal paths (B-1, B-2) in the SADD's Dynamic Design | Small — rationale already exists in `.cursor/rules/` |
| 2 | Add an SRD requirement for the interactive terminal (C-2) so REQ-28/29 constrain a required capability | Small |
| 3 | Requirement the AI assistant and the lake capture path, or scope them out (C-1, C-3) | Small, but needs a scope decision |
| 4 | Add Legal-and-Standards and Virtualization requirements, or mark N/A (A-1, A-2) | Small |
| 5 | Give `slurm_admin` a design home and a test module (B-3 + SUTP G-7) | Medium |
| 6 | Add REQ-45 and REQ-47 to the SADD (B-4, B-5) | Small |
| 7 | Add a UC column to the requirements table (A-3) | Small |

---

## Disclaimer

This report is advisory developer feedback produced by `plc-requirements-validation`. It does not
assert PLC compliance, release-gate clearance, or satisfaction of any authoritative gate. All
findings are recommendations for the document owner, who decides what to change.
