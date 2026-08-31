# Phase 1 audit/logging — inspection and demonstration evidence

Companion to `.plc/briefs/auth-audit-logging-test-plan.md`. Five of the seven work-item-7 cases are
automated and their evidence is the test run; the two that are not — TC-39 (inspection) and TC-40
(demonstration) — record their result here, so `plc-evidence-ingestion` has something durable to
point at rather than a paragraph in a merge-request description that disappears when the MR merges.

| Case | Method | Requirement | Status |
|---|---|---|---|
| TC-34 | test | NFR-2a, AC-17a | automated — `test_audit_event.py::TestLatencyBudget` |
| TC-35 | test | FR-2, FR-11a, AC-3, AC-10f | automated — `test_audit_platform.py` |
| TC-36 | test | FR-1 | automated — `test_logging_config.py::test_dictconfig_preserves_caplog_capture` |
| TC-37 | test | FR-6 | automated — `test_security_headers.py::TestTC37HookOrdering` |
| TC-38 | analysis | FR-7b, AC-10c, AC-10g | automated — `test_audit_event.py::TestRetentionBudget` |
| **TC-39** | **inspection** | NFR-6, AC-20 | **PASS — recorded below** |
| **TC-40** | **demonstration** | FR-11a, AC-10f | **PASS — recorded below** |

---

## TC-39 — no existing `getLogger` call site was modified (NFR-6, AC-20)

**Result: PASS.** Zero pre-existing call sites changed.

Performed 2026-08-21 by Rafael M. Koike with Cursor Agent ("Ada"), against
`e7d4e80` (`git merge-base origin/main HEAD`).

The plan's command as written also reports **new** files, because the merge-base predates the whole
`api/` migration, so its output includes 29 added files and ~16,700 insertions that have nothing to
do with this feature. AC-20's claim is narrower — no *existing* call site modified — so the
inspection adds `--diff-filter=M` to restrict it to files that existed at the base:

```bash
BASE=$(git merge-base origin/main HEAD)
git diff "$BASE"...HEAD -G 'getLogger' --diff-filter=M --stat -- \
    'backend/**/*.py' ':!backend/audit/**' ':!backend/logging_config.py'
```

Two pre-existing files come back, carrying three `getLogger` lines between them. Both were examined
line by line, and neither is a modification of an existing call site:

- **`backend/api/terminal.py`** — shows a `-`/`+` pair for
  `_logger = logging.getLogger("rack_web.api.terminal")`. The two lines are **byte-identical**: the
  logger sat at line 46 at the base and sits at line 52 now, displaced by the
  `from api import _common` import added above it for the terminal refusal hook. A positional move,
  not an edit. Verified by comparing `git show "$BASE:backend/api/terminal.py"` against the file.
- **`backend/api/_common.py`** — shows a single added `_logger = logging.getLogger(__name__)`.
  `git show "$BASE:backend/api/_common.py" | rg getLogger` returns **nothing**, so the file had no
  logger at the base commit. This is a new call site in an old file, which AC-20 does not forbid;
  what it forbids is disturbing the 81 that were already there.

The exclusions are load-bearing and were kept: the feature's own modules (`backend/audit/**`,
`backend/logging_config.py`) legitimately call `getLogger` — the sink fans out to a dedicated
`audit` logger — so an unexcluded pathspec always reports hunks and the case would fail for the
wrong reason.

---

## TC-40 — audit history survives a container recreate (FR-11a, AC-10f)

**Result: PASS.** Performed 2026-08-22 by Rafael M. Koike with Cursor Agent ("Ada"), on the macOS
workstation against Docker 29.7.2 / Compose v5.4.0, image built from `e000da6` plus the working
tree.

| Step | Expected | Observed |
|---|---|---|
| container id before | recorded | `ef3693ea312f` |
| event written | present in `/app/var/logs/audit/audit.jsonl` | `event_id 367da7ed-3331-4705-b838-614c2e330ea7`, 1 line |
| container id after | **differs** from before | `71f758789132` — changed |
| event id after recreate | still present | found, 1 occurrence, 1 line |

`--force-recreate` is essential and was used. A bare `docker compose up -d` reports the service up
to date and does nothing, so the case would pass without exercising anything — the changed container
id above is what makes the result mean something.

The run used a temporary, gitignored `docker-compose.override.yml` supplying `RACK_STATUS_DEMO=1`,
`RACK_WEB_AUTH_DISABLED=1` and `RACK_UPTIME_DRY_RUN=1`, because no head node is reachable from a
workstation and `rack-config.env` defaults to token auth with no token set. None of those touch the
mechanism under test: the audited event is emitted by the `after_request` chokepoint regardless of
what the handler returns, and the property being demonstrated is that the `rack-data` **named
volume** — not the container — held the line across the recreate. The override and the driver script
were removed afterwards; both were test scaffolding, not deployment config.

Two things worth knowing for anyone repeating this. `RACK_WEB_PORT` must be set to something other
than 5000, because `:5000` on macOS is the AirPlay receiver and answers 403. And the first build
failed with `DeadlineExceeded: context deadline exceeded` loading metadata for `ubuntu:22.04` and
`node:22-slim` — a slow-network stall rather than a real error; pre-pulling the two base images made
the build succeed unchanged.

```bash
# 1. build and start, recording the starting container id
bin/write-build-info.sh && docker compose build && docker compose up -d
docker compose ps -q rack-status                     # -> container id BEFORE

# 2. drive the canonical audited endpoint and note the event id
curl -sk -X POST http://127.0.0.1:5050/api/rack-uptime \
     -H 'Content-Type: application/json' -d '{"rack":"p5-r01"}'
docker compose exec rack-status tail -1 /app/var/logs/audit/audit.jsonl   # -> event_id

# 3. recreate the container, which is what the documented deploy does
docker compose up -d --force-recreate
docker compose ps -q rack-status                     # -> container id AFTER (must differ)

# 4. the same event id must still be readable
docker compose exec rack-status grep '<event_id>' /app/var/logs/audit/audit.jsonl
```

Passing this proves the `rack-data` named volume held the history rather than the container. That
is the whole point of FR-11a: the documented deploy recreates the container and discards stdout, so
an audit trail that lives only in stdout does not survive the deploy that produced it.

---

## Sign-off

REQ-47 requires evidence that is not an automated test to carry a named approver. A committed record
establishes that an inspection or demonstration happened; it does not establish that anyone other
than its author accepted the result, and inspection is exactly the method where that distinction
carries the weight. Authorised approvers: **Rafael M. Koike, Giovanni Mascari, or Felix Melligan**.

| Case | Method | Result | Recorded by | Recorded | Approved by | Approved |
| :---- | :---- | :---- | :---- | :---- | :---- | :---- |
| TC-39 | Inspection | PASS | Cursor Agent ("Ada") | 2026-08-24 | **Rafael M. Koike** | 2026-08-31 |
| TC-40 | Demonstration | PASS (`ef3693ea312f` → `71f758789132`) | Cursor Agent ("Ada") | 2026-08-24 | **Rafael M. Koike** | 2026-08-31 |

The single `Date` column became two. It sat between *Recorded by* and *Approved by* and belonged to
neither: recording and approval happened a week apart, so one date could only have been right about
one of them. Whichever way it were read, the other fact would have been quietly wrong — and the
point of this table is that "somebody other than the author accepted this" is separable from "this
was written down".

Approvers: replace `_pending_` with your name and the date to accept a recorded result. Leaving it
pending is also information — it says the evidence exists but has not been reviewed.
