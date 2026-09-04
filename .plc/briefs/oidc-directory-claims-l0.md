# PLC L0 — oidc-directory-claims

> **Advisory:** This document is AI-authored and does not replace the official PLC
> process, the nSpect submission, or any MVSB artifact. The developer and Security
> PIC remain responsible for official PLC compliance.
>
> **Security scans and mitigations are not waived by PLC L0.** Teams remain responsible
> for mitigating security findings in accordance with the applicable Release Type and SLA
> before releasing code.

| Pillar | Work Product | Content |
|---|---|---|
| Planning | Project Plan | Complete the identity store as an OIDC-conformant directory: add the remaining useful OpenID Connect Core §5.1 profile fields, and release claims according to granted scope so a standard client can actually consume them. Follows `user-contact-attributes-l0.md` on `feat/auth-identity`. Release target: *[not specified]*. |
| Requirements | Requirements | See **Requirements** below (5 bullets). |
| Design | Design Document | See **Design** below. |
| Coding | Static Code Analysis | Semgrep **WARN** — `.plc/security/semgrep.json`, 13 files in scope, 1 high. **Triaged as pre-existing and accepted:** `unverified-jwt-decode` at `oidc_provider.py:292` (`_peek`), from commit `66a6311d` on 2026-09-01, surfaced only because this change brings that file into the changed-file scan scope for the first time. The call reads back a token `mint` produced three lines earlier to record its `jti` for revocation; it authenticates nothing, and its docstring says so. Not suppressed — recorded here so the triage is reviewable. Dependency scan WARN (no manifest in scope; no dependency added). Secret scan SKIP (Pulse image unavailable locally). |
| Verification | QA Test | **PASS.** `python -m pytest backend/tests -q` → 3394 passed, 16 skipped. `npm test -- --run` → 449 passed, 58 files. New coverage: 33 cases in `backend/tests/test_identity_profile_fields.py` (R1, R2), 4 classes in `test_oidc_provider.py` (`TestScopeIsGrantedAtAuthorization`, `TestUserinfoReleasesOnlyWhatWasGranted`, `TestARefreshCannotWidenScope`, `TestATokenWithNoGrantReleasesOnlyTheSubject`), 2 in `test_oidc_discovery.py`, 3 in `test_tokens_oidc_mode.py`, and 3 UI cases in `UsersView.test.tsx`. `bin/check-ci-lint.sh` PASS; `tsc -b --noEmit` clean; **`npm run lint` 0 errors** (the gate `tsc` alone does not cover). |

## Requirements

- **R1 — Four more standard profile fields.** A user record carries `given_name`,
  `family_name`, `zoneinfo` and `locale`, all optional, using the OpenID Connect Core §5.1
  claim names. `zoneinfo` must be an IANA time-zone name that this process can actually
  resolve; `locale` must be a well-formed RFC 5646 language tag, stored in the conventional
  casing (`en-US`, `zh-Hant-TW`) so two spellings of one locale do not read as two values.
  Records written before these fields existed load unchanged, with no migration.

- **R2 — `zoneinfo` validation degrades rather than refuses.** The set of resolvable zones
  comes from the running process's time-zone database and is **not** the same everywhere:
  macOS carries 113 legacy aliases (`America/Buenos_Aires`, `America/Indianapolis`) that
  `python:3.12-slim` does not, and slim reports a `localtime` entry that is not a zone. So a
  zone is accepted when it resolves here, and if the database is entirely absent the field
  falls back to a shape check instead of refusing every write. This is the opposite of the
  fail-closed rule that governs security controls, and deliberately so: a display preference
  that cannot be saved because the OS lacks a package is a worse outcome than an unverified
  zone string.

- **R3 — Claims are released by granted scope, not unconditionally.** `/userinfo` returns
  `sub` always; the `profile`, `email` and `phone` scopes each release exactly the claims
  §5.4 assigns them. `updated_at` is converted to a NumericDate, because §5.1 defines it as
  seconds since the epoch while the record stores an ISO-8601 string — returning the stored
  form under that name would be well-formed and wrong.

- **R4 — A refresh cannot widen scope.** The granted scope is recorded on the authorization
  code, carried in the access **and** refresh tokens, and re-issued on refresh as the
  intersection of what was originally granted with anything the client now asks for. RFC 6749
  §6 requires this, and without it any client holding a refresh token could promote an
  `openid` session into `openid profile email` and read attributes it was never granted.

- **R5 — Discovery tells the truth.** `scopes_supported` lists exactly the scopes that now
  gate something, and `claims_supported` lists the claims that can actually be returned. The
  existing code comment states the rule this preserves — *"A scope that appears in a token and
  gates nothing must not be advertised as though it did"* — so the list grows only because the
  gating became real in R3.

## Design

`backend/identity/models.py` gains the four fields on `User` with `""` defaults, normalised in
`__post_init__` beside the existing contact attributes, and accepted by `edited()`.
`canonical_zoneinfo()` checks resolvability through `zoneinfo.ZoneInfo(key)` and falls back to an
`Area/Location` shape check when the database is missing (R2); `canonical_locale()` validates the
RFC 5646 subtag shape and rewrites it to conventional casing. `from_dict` already filters to
declared fields, so older rows need no migration.

Scope becomes real in four places. `backend/api/oidc_provider.py` `_authorize` parses the `scope`
parameter, keeps only supported values, requires `openid`, and records the result on the code entry
(the entry already carries a `scope` field that was being handed a constant). `identity/tokens.py`
`mint()` takes an explicit `scope` argument instead of hardcoding `"openid"`, and `scope` joins the
reserved claim names so it cannot also arrive through `claims` and disagree with itself. The refresh
grant reads the scope from the presented refresh token and intersects it with any requested scope
(R4). `_userinfo` builds its response from a scope-to-claims map (R3).

`backend/identity/oidc.py` extends `scopes_supported` and `claims_supported` (R5). The admin API
(`backend/api/identity_admin.py`) accepts the four fields on create and update, and the frontend
carries them in `types/identity.ts`, `services/identityService.ts` and the `UsersView` dialogs.

**Compatibility choice, recorded because it is a judgement rather than a rule.** `/authorize`
currently discards `scope` and always grants `openid`, while `/userinfo` returns `name` and
`preferred_username` unconditionally — both of which §5.4 assigns to `profile`. Gating them without
further thought would silently stop returning them to every existing caller. So a request that omits
`scope` altogether is granted `openid profile`, which reproduces today's response exactly; a request
that names its scopes gets precisely those. A client asking for an unsupported scope has it ignored
rather than being refused, per RFC 6749 §3.3.

Not in scope: `address`, `gender`, `birthdate`, `picture`, `website` and `nickname` — an internal
cluster-operations tool has no use for them and each is PII the product would then own. `groups` is
deliberately **not** exposed as a claim here: it is not an OIDC standard claim, and a federated IdP
may send its own `groups`, so the name needs deciding before authorization reads it.

## Notes from implementation

**Two existing tests asserted the behaviour this change reverses**, and both were rewritten to
state the rule rather than the then-current state.
`test_oidc_discovery.py::test_only_the_openid_scope_is_advertised` pinned the literal list
`["openid"]`; it is now `test_every_advertised_scope_gates_something`, which asserts each advertised
scope resolves in `SCOPE_CLAIMS` — the constraint is unchanged, and adding a scope to the document
without wiring it now fails, which pinning a literal list could not catch.
`test_tokens_oidc_mode.py::test_scope_is_on_access_tokens_only` asserted `scope` was absent from
refresh tokens; it now asserts presence on access **and** refresh and absence from the **ID** token,
because these tokens are self-contained and a refresh that cannot recall its grant can only guess or
widen.

**Two defaults that look inconsistent and are not.** `mint()` defaults `scope` to `openid`, the
narrowest, because it is called from paths with no authorization grant at all — the SRP login among
them. `parse_scope("")` returns `openid profile`, which is the compatibility shim for `/authorize`
requests predating scope handling. `TestATokenWithNoGrantReleasesOnlyTheSubject` pins both so
neither is later "corrected" into the other. Consequence: a first-party SRP session token reads only
`sub` from `/userinfo`. There is no in-repo consumer of that endpoint, so nothing regresses.

**`scope` became a reserved claim name.** It was previously settable through `mint(claims=...)`
because it was not in the reserved list. With `/userinfo` releasing attributes from it, two ways to
set one security-relevant value is one too many, so the explicit parameter is now the only way.

## Mistakes made

Recorded because they cost real time and both were avoidable.

**I destroyed uncommitted work with `git checkout`.** After a mutation test on
`backend/identity/models.py`, I restored it with `git checkout <file>` — which restores from HEAD,
not from the pre-mutation state, silently discarding every field added in this change. All six edits
had to be reapplied. A mutation test must save and restore its own copy of the file; `git checkout`
is not an undo for uncommitted work.

**A `StrReplace` matched shell-prefixed indentation** and left `tokens.py` with an
`IndentationError` that surfaced as a confusing failure in `identity/revocation.py` — the *importing*
line, not the broken one. Compile-check the file you edited before believing a traceback that names
a different one.

## Document Change Log

<content authored by agent, review thoroughly and remove after human review>
Agentic PLC version: v1.13.0; Skill: plc-l0-authoring; LLM: unknown; Timestamp: 2026-09-04T18:30:00Z; Input ref: none-provided

| Date | Author | Change | Human decision | Reviewed by |
|---|---|---|---|---|
| 2026-09-04 | Agentic PLC / plc-l0-authoring | Initial L0 brief authored | pending | rkoike |
