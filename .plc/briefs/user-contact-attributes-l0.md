# PLC L0 — user-contact-attributes

> **Advisory:** This document is AI-authored and does not replace the official PLC
> process, the nSpect submission, or any MVSB artifact. The developer and Security
> PIC remain responsible for official PLC compliance.
>
> **Security scans and mitigations are not waived by PLC L0.** Teams remain responsible
> for mitigating security findings in accordance with the applicable Release Type and SLA
> before releasing code.

| Pillar | Work Product | Content |
|---|---|---|
| Planning | Project Plan | Additive contact attributes on the identity user record, so the product can address a user out of band (notifications) later. Lands on `feat/auth-identity` alongside Phase 2 identity work. Release target: *[not specified]*. |
| Requirements | Requirements | See **Requirements** below (5 bullets). |
| Design | Design Document | See **Design** below. |
| Coding | Static Code Analysis | Semgrep **PASS** — `.plc/security/semgrep.json`, 11 files in scope, 0 high / 0 medium / 0 low. Dependency scan **WARN** (`no manifest or lockfile in scope`) — expected, this change adds no dependency; it is a standing repo condition, not a finding against this work. Secret scan **SKIP** — the Pulse scanner image could not be pulled in this session; the authoritative scan is Pulse via nSpect/ScanSpect. |
| Verification | QA Test | **PASS.** `python -m pytest backend/tests -q` → 3344 passed, 16 skipped (randomised order). `npm test -- --run` (frontend) → 446 passed, 58 files. New coverage: 48 cases in `backend/tests/test_identity_contact_attributes.py` (classes `TestR1…`–`TestR5…`, one per requirement) and 13 in `frontend/src/views/UsersView.test.tsx` (`describe("contact attributes")`, `describe("editing a profile")`). `bin/check-ci-lint.sh` PASS (ruff + pylint); `tsc -b --noEmit` clean; `bin/check-tracked-tree.sh` PASS. |

## Requirements

- **R1 — Stored, exposed, and backward compatible.** A user record carries an optional
  `email` and optional `phone_number`, each paired with an `email_verified` /
  `phone_number_verified` boolean. All four are persisted in the identity store and
  returned by the admin API, which continues to expose no credential material. An email is
  validated pragmatically (exactly one `@`, non-empty local part, dotted domain, no
  whitespace, ≤254 characters per RFC 5321) and stored lowercased; a phone number is
  punctuation-stripped and length-capped. Users already in a deployed store have none of
  these fields and must load unchanged, with **no store migration**.

- **R2 — Required at the boundary, permitted in the model.** Creating a user through the
  admin API without an email is refused, so every account created from now on can be
  notified. The record type itself still permits an empty email, because the first-boot
  bootstrap admin and every pre-existing account legitimately have none — making it
  mandatory in the type would invalidate them.

- **R3 — An email identifies at most one user.** Email is unique across users,
  case-insensitively, and the check happens inside the same lock as the insert or update, so
  two concurrent creates cannot both win. **An absent email is exempt from uniqueness** —
  many users may have none without colliding with one another, which is the distinction a
  naive uniqueness check gets wrong.

- **R4 — A verified flag never outlives the value it describes.** Changing an address
  clears that address's verified flag, enforced inside `User.edited()` rather than at each
  call site, so no caller can retain a stale assertion. This is a security property, not
  tidiness: OpenID Connect Core §5.1 defines `email_verified` as the provider asserting the
  address was verified, and relying parties use it for **account linking** — a
  `verified: true` inherited by a newly-set address is an account-takeover primitive on any
  RP that links by verified email. A flag becomes true only through a distinct,
  audited admin action with its own action-vocabulary entry, never as a side effect of a
  generic profile edit, so the trail records "this admin asserted this address" rather than
  burying it among display-name changes.

- **R5 — Manageable from the Users UI.** The Users view collects an email (and optionally a
  phone number) when creating a user, edits both afterwards, displays the email and its
  verified state in the table, and flags accounts that have no email so the gap is visible
  rather than silent.

## Design

Add `email`, `email_verified`, `phone_number` and `phone_number_verified` to the frozen
`User` dataclass in `backend/identity/models.py`, defaulting to `""` / `False`, with
validation and normalisation in `__post_init__` beside the existing `canonical_login` and
`_clean_display_name` calls. Extend `edited()` to accept all four, and have it clear a
verified flag whenever the corresponding address changes (R4) — enforced in the type for the
same reason the type refuses to move a login: a caller cannot get it wrong. `to_dict` and
`public_dict` carry the new fields; `from_dict` already filters to declared dataclass fields,
so rows written before this change simply take the defaults and **no migration runs**.

Uniqueness (R3) goes into `IdentityStore.create_user` and `update_user` in
`backend/identity/store.py`, inside the existing module `_lock` next to the duplicate-login
check and for the identical race reason, comparing case-folded and skipping the empty value.
The create-time email requirement (R2) goes at the `POST /api/identity/users` boundary in
`backend/api/identity_admin.py`, deliberately not in the model. `PATCH /users/<login>`
accepts both addresses; a new `PUT /users/<login>/email-verified` carries the admin assertion
as its own audited action, resolving through the existing `user` resource resolver on
`<login>`, which adds one entry to the action vocabulary and its snapshot.

Frontend: extend `IdentityUser` in `frontend/src/types/identity.ts`, the create and update
functions in `frontend/src/services/identityService.ts`, and the create/edit dialogs and
table in `frontend/src/views/UsersView.tsx`.

The field names are the OpenID Connect Core §5.1 standard claim names, paired with the
standard `email` and `phone` scopes, because this product is now an OIDC Provider — so a
later `/userinfo` can return them with no mapping layer.

Out of scope, recorded so it is a decision rather than an omission: `/userinfo` is **not**
extended to return these claims in this change (that would pull in the `email` and `phone`
OIDC scopes and consent handling); there is no self-service or mail-based verification flow,
so admin assertion is the only verification the product can honestly make today; and unlike a
login, an address is **not** retired when a user is deleted, because an address may
legitimately be reassigned or the person may return.

SRP, token minting, the authorization gate, and the audit sink are untouched. The audit sink
was checked and records no request bodies, so an address supplied in a create or update body
never reaches the durable trail and no redaction rule is needed.

## Notes from implementation

**R5 needed more than it looked.** `UsersView` had **no edit path at all** — the row menu offered
only Reset password, Enable/Disable and Delete, and `updateDisplayName` in the service was already
dead code before this change. That made the requirement's "edits both afterwards" clause unmet, and
not merely as a convenience: the bootstrap admin has no address, so with create-only collection the
product's primary account could never be notified. An *Edit profile…* dialog and a separate *Mark
email verified* action were added, the latter kept out of the dialog so the audited assertion stays
its own act (R4).

The edit path sends **only the fields that moved**. Resending an unchanged address would be a write
that clears a standing verified mark for nothing, since the server clears the flag whenever an
address is set.

**Evidence-ingestion gap.** `plc-evidence-ingestion` could not record the test run: its `--ref` is
validated against `^(FR|NFR|AC)-\d+$`, while the L0 brief template carries plain requirement bullets
with no ID scheme (the `R1`–`R5` labels here were added for traceability). Relabelling them `FR-*`
would collide with the L1 `FR-*` namespace already in use by `auth-identity-brief.md`, so the row was
left unrecorded rather than filed under a misleading id. Security-scan evidence **was** recorded —
`plc_security_scan.py` appends its own ref-less rows to `plc-evidence.jsonl`, and did so for this run.

Two smaller tooling mismatches, noted so the next run does not rediscover them: the L0 skills
document `"pillar":"l0"` for the telemetry preflight but the installed `aplc` CLI (v1.13.0) rejects
it and accepts `validation`; and the orchestrator names evidence types `security-scan` and `qa-test`,
neither of which exists in the ingest CLI's type list.

## Backward-compatibility impact

One consumer-facing contract changed, deliberately (R2), and it is worth stating plainly because it
is the only part of this work that can break a caller:

**`POST /api/identity/users` now requires an `email` field** and answers `400` without one. Any
existing client that creates users must add it. In-repo callers were updated: the `_create` helper in
`test_identity_admin_api.py`, the subject-creation call in `test_oidc_provider.py`, and the
chosen-password create. A duplicate address answers **`409 Conflict`** — from both the create and the
update route, which is a new status on the update path.

Nothing else moves. Stored records need no migration (R1): `User.from_dict` filters to declared
fields, so a row written before this change takes the defaults. `GET` responses gain four fields,
which is additive. `updateDisplayName` in the frontend service is kept as a thin wrapper over the new
`updateProfile`, so its callers are untouched.

## Document Change Log

<content authored by agent, review thoroughly and remove after human review>
Agentic PLC version: v1.13.0; Skill: plc-l0-authoring; LLM: unknown; Timestamp: 2026-09-04T14:25:26Z; Input ref: none-provided

| Date | Author | Change | Human decision | Reviewed by |
|---|---|---|---|---|
| 2026-09-04 | Agentic PLC / plc-l0-authoring | Initial L0 brief authored | pending | rkoike |
| 2026-09-04 | Agentic PLC / plc-l0-authoring | Added `email_verified` / `phone_number_verified` (R1, R4) and the distinct audited admin assertion action, per developer decision | pending | rkoike |
| 2026-09-04 | Agentic PLC / plc-v-model-l0 | Implementation, verification and scan results recorded; R5 edit path added (see Notes) | pending | rkoike |
