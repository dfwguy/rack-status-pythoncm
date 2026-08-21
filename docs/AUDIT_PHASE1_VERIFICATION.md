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
| **TC-40** | **demonstration** | FR-11a, AC-10f | **not yet run — checklist below** |

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

**Not yet run.** Requires Docker, which was not running on the workstation when work item 7 was
implemented. The layer keeps an automated floor regardless: TC-35 covers the same requirements
against gunicorn, including the stdout-disabled case, which is why this was deliberately kept
separate.

`--force-recreate` is essential. A bare `docker compose up -d` reports the service up to date and
does nothing, so the case would pass without exercising anything — the container id must change.

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

| Step | Expected | Observed | |
|---|---|---|---|
| container id before | recorded | | |
| event id written | present in `/app/var/logs/audit/audit.jsonl` | | |
| container id after | **differs** from before | | |
| event id after recreate | still present | | |

Operator: ______________________  Date: ______________

Passing this proves the `rack-data` named volume held the history rather than the container. That
is the whole point of FR-11a: the documented deploy recreates the container and discards stdout, so
an audit trail that lives only in stdout does not survive the deploy that produced it.
