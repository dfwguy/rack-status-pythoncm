# PLC Feature Brief — audit trail reader

**Status:** requirements draft, 2026-09-10. Not yet reviewed.
**Programme:** `docs/auth/AUTH_PROGRAMME_SCOPE.md` — the deferred work item from Phase 1, unblocked by Phase 3.
**Jira:** *(ticket to be raised)*.
**Directed by:** feature lead, 2026-09-10 — chosen as the next foundation after the Phase 3 close.

> Requirement numbering is per-brief. `audit FR-n` names requirements in the Phase 1 brief
> (`.plc/briefs/auth-audit-logging-brief.md`); `authz FR-n` names Phase 3's.

## Purpose

**The audit trail has no reader.** Phase 1 built a durable JSONL record to answer *"who did what, to
which rack, when, and what came of it"* — the question every handover asks — and the only way to
read it is to reach the host and `cat` a file. There is no API and no view: from the product's side
the trail is write-only.

This is a control that is already paid for and cannot currently be used. It is also invisible to the
people who built it: `EventsView`'s docstring still says rack-locks is *"the only source in the
estate that knows **who** did something"*, which stopped being true when Phase 1 landed.

**Why now.** [NVRACKSTAT-58](https://jirasw.nvidia.com/browse/NVRACKSTAT-58) deliberately gated this
on Phase 3: *"authentication alone is insufficient, because without authorization every operator
could read every other operator's actions."* Phase 3 closed on 2026-09-09, so the gate it was
waiting for exists.

## What already exists, so this is not a green field

| Piece | State |
|---|---|
| The record | `backend/audit/sink.py` — bounded rotating JSONL, owner-only `0600`, 17-field pinned schema (`audit.EVENT_FIELDS`) |
| The chokepoint | `audit/request_hook.py` — one event per audited request, resolved resource identifier, authorization verdict and engine |
| The vocabulary | 173 actions, generated to `docs/action-vocabulary.json`, so a filter list need not be hand-maintained |
| Authorization | default-deny with deny-override, and `declared_types()` for a new resource type |

## Measured facts this design must respect

Taken from a developer machine on 2026-09-10, not estimated:

| Fact | Value | Consequence |
|---|---|---|
| Event size | **490 bytes** | ~10,700 events per 5 MB file |
| Ceiling | 5 MB × (1 + 3 backups) = **20 MB** | **~42,800 events** is the whole readable history |
| Parse cost | **27 ms** per full file, ~110 ms for all four | fine on demand, far too slow to poll |
| Already accumulated | **15 MB across 4 files**, rotated three times since 2026-08-28 | the ceiling is reached in ordinary use, not hypothetically |

**Two structural properties of the file that a naive reader gets wrong:**

- **Newest-last, and rotation runs the other way.** Events append to `audit.jsonl`, so the newest is
  the *last* line; `audit.jsonl.1` is *older* than `audit.jsonl`. A newest-first reader therefore
  walks the current file backwards and then descends the numbered backups — the opposite of the
  order a directory listing suggests.
- **One line can represent many events.** The flood guard collapses repeated refusals into a
  `_summary_event` carrying `count` and `collapsed: true`. A reader that ignores those fields
  under-reports: it would show one refusal where forty-seven happened, in exactly the review where
  the count is the finding.

## Functional requirements

| ID | Requirement | Source / rationale |
|---|---|---|
| FR-1 | A read API shall return audit events newest-first, in pages of **50 by default and 500 at most**. | the question is almost always "what happened recently"; a named size is also what makes NFR-1 measurable |
| FR-1a | Pagination shall be **cursor-based, anchored on an event**, not offset-based. | **An offset is wrong here, and the failure is silent.** The trail is appended at the newest end, which is exactly where page 1 starts, so by the time a reader asks for page 2 the offset has shifted by however many events arrived — repeating some and skipping others. On a busy cluster the skipped ones are invisible, which in an audit reader is the worst possible defect. A cursor anchored on `event_id` (unique per event) is stable under concurrent append |
| FR-2 | The reader shall filter by principal, action, service, resource, decision, result and time range. | the fields an investigation starts from |
| FR-3 | The reader shall surface `count` and `collapsed` for a flood-summary event, and shall never present a collapsed event as a single occurrence. | see above — the alternative is under-reporting in a security review |
| FR-4 | The reader shall read across the current file and its rotated backups as one logical sequence. | 15 MB of the 20 MB ceiling is already in backups; a reader that sees only the live file misses most of the history |
| FR-5 | The scan shall stop after **20,000 events or 8 MB, whichever comes first**, and a truncated answer shall report `truncated: true`, `scanned_events` and `scanned_bytes` rather than returning a short page silently. | citizen 7: a probe that might not find its answer needs a read cap. A filter defeats early exit — `principal=alice` with three matches in 42,800 events forces a full scan. The figures are named so the cap is testable and so NFR-1 has a worst case, not just a best one |
| FR-6 | Reading the trail shall require an explicit authorization grant, under a new resource type. | reading it exposes every other operator's activity |
| FR-7 | Reading the trail shall append **exactly one event per request**, so N reads produce N events and never more. | A privileged read belongs in the record. "Not self-amplifying" was the first wording and is not a testable property — the measurable version is linear growth: the read's own event must not appear in its own response, and must not trigger further events |
| FR-8 | The action names shall be `audit.trail:list` and `audit.trail:read`. | **not** `audit:read`, which would sit beside the existing `audit.deep:read` — that is *NVSwitch hardware* auditing and shares nothing with this. Two unrelated meanings one character apart is the `/icons.svg` versus `/icons/` mistake again |
| FR-9 | A view under Observability shall present the trail with the filters of FR-2, and shall link an event to the operator and resource it names. | |
| FR-10 | The view shall state the retention bound by naming the **oldest event actually held**, not a computed date. | an empty result for last month must not read as "nothing happened last month". The oldest event held is a fact the reader has; a retention *period* would be a guess, since it depends on event rate |
| FR-11 | An **absent trail** shall be an empty result, not an error. | a deployment that has never emitted an audited event has no file. A reader that 500s there would make a fresh, healthy install look broken — and this is the state every first boot is in |
| FR-12 | A **rotation occurring during a read** shall not duplicate or skip an event. | rotation renames the file under the reader. An open descriptor survives the rename on POSIX, so the naive case is safe by accident; a reader that re-opens by *path* between pages would silently read the same events twice, or miss a window |

## Non-functional requirements

| ID | Requirement | Rationale |
|---|---|---|
| NFR-1 | An unfiltered first page of 50 shall cost **≤ 50 ms** server-side; a filtered request that exhausts the FR-5 cap shall cost **≤ 400 ms**. | achievable only by reading backwards with early exit; a parse-everything implementation costs ~110 ms before filtering. Both bounds are stated because the unfiltered case is the common one and the capped scan is the worst one, and quoting only the first would hide it |
| NFR-2 | Peak additional allocation shall stay **under 4 MB** regardless of trail size, and shall not grow with it. | citizen 7 — the results pipeline replaced ~16 full-file passes for this reason. "Bounded by the page size" was the first wording; without a figure it cannot fail a test |
| NFR-3 | A malformed line shall be skipped and counted, never fatal. | the trail is the thing you read *after* something went wrong, so it must tolerate a torn final line from a crash mid-write |
| NFR-4 | The reader shall not write to, truncate or rotate the trail. | identity-auth-traps §2: a read path that writes defeated its own cache and went unnoticed because every functional test passed |

## Acceptance criteria

| ID | Verifies | Criterion | Method |
|---|---|---|---|
| AC-1 | FR-1 | The first page is the most recent events, in descending time order, across a rotation boundary. | automated |
| AC-2 | FR-2 | Each filter narrows the result, and two filters compose as AND. | automated, parametrised |
| AC-3 | FR-3 | A collapsed event reports its `count`; the total occurrences reported for a burst equals what was emitted. | automated |
| AC-4 | FR-4 | Events written before a rotation are still readable after it. | automated |
| AC-5 | FR-5 | A filter matching nothing within the cap returns `truncated: true`, `scanned_events` and `scanned_bytes`, and stops at the named cap rather than reading on. | automated |
| AC-5a | FR-1a | With events appended between two page requests, the second page repeats no event and skips none. | automated — the defect an offset would cause, driven by appending mid-pagination |
| AC-6 | FR-6 | An operator without the grant is refused; the refusal does not reveal whether any event exists. | automated |
| AC-7 | FR-7 | N reads append exactly N events, and a read's own event is absent from its own response. | automated, parametrised over N |
| AC-8 | FR-8 | `audit.trail:*` is in the generated vocabulary and is distinct from `audit.deep:*`. | automated, against `docs/action-vocabulary.json` |
| AC-9 | NFR-1 | Measured first-page latency is within budget, recorded on the deployment's hardware. | measured, and recorded, or it cannot detect a regression |
| AC-10 | NFR-2 | Reading a 20 MB trail does not hold the file in memory. | automated — peak allocation bounded |
| AC-11 | NFR-3 | A truncated final line and a non-JSON line are both skipped, counted and reported. | automated |
| AC-12 | NFR-4 | The trail's mtime, size and inode are unchanged by a read. `atime` is excluded deliberately: a read legitimately updates it, so asserting on it would fail for the wrong reason. | automated — the §2 lesson, asserted rather than assumed |
| AC-14 | FR-11 | With no trail file present, the API returns an empty result and 200, not an error. | automated |
| AC-15 | FR-12 | A rotation triggered between two page requests neither duplicates nor drops an event. | automated — forced rotation mid-pagination |
| AC-13 | FR-9 | The view renders events and applies a filter. | automated render test |
| AC-13a | FR-10 | The view names the oldest event held, and an empty filtered result is distinguishable from an empty trail. | automated render test — the two must not look alike, or "nothing happened" and "we no longer hold it" become the same screen |

## Explicitly out of scope, and why

- **Tamper-evidence.** The trail is append-only with `0600` permissions, and has **no hash
  chaining** — an operator with host access can edit history undetectably. That is a real gap and a
  reader does not change it either way, but conflating "can be read" with "can be trusted against a
  privileged insider" would be the wrong claim to make. Worth its own ticket.
- **"Operators may read their own actions."** Deliberately not v1. It is a *condition* on the
  principal, and conditions are [NVRACKSTAT-61](https://jirasw.nvidia.com/browse/NVRACKSTAT-61).
  Until then, reading the trail is a privileged act granted by policy, which is the honest position:
  a half-built self-service view whose filter is presented as a security boundary is worse than not
  having one.
- **Shipping the trail off-box** (syslog, a lake sink, SIEM). The Phase 1 sink already supports
  network sinks; forwarding is a deployment choice, not this feature.

## Open question for the feature lead

**Is 20 MB of retention the right ceiling now that the trail is readable?** It is ~42,800 events, and
it is already full on a *developer* machine after two weeks. On a busy cluster with read auditing on,
a security review could find that last month is simply gone — and FR-10 makes the product say so
rather than imply nothing happened. Raising `NVIS_AUDIT_BACKUPS` is a one-setting change; the
question is what the retention commitment should be, which is a policy decision rather than a
technical one.

---

## Retention decision (feature lead, 2026-09-10): raise it

`NVIS_AUDIT_BACKUPS` **3 → 24**, so 25 files × 5 MiB = **125 MiB**, about **90 days** at the measured
1.15 MB/day (~2,460 events/day over 13 days). Three backups held roughly 17 days, so a review asking
about last month found it gone. A trail nobody can read has no reason to be long; one with a reader
does.

**This makes one design point load-bearing rather than an optimisation.** Six times the history means
a *filtered* query over "everything" hits the FR-5 cap far more often — `principal=alice` now has
~267,000 events to sift rather than ~42,800, and the cap stops at 20,000. So:

- **The time filter must be able to terminate the scan**, not merely exclude rows. Walking
  newest-first, a `since` bound is reached and the scan stops — which is what keeps a bounded query
  cheap over unbounded history.
- **The view therefore defaults to a time window (last 7 days), not "all time".** A default of
  everything would make the common case the expensive one and put `truncated: true` in front of an
  operator who never asked a hard question.

## Design

### D-1. Where it lives

| Piece | Module | Why there |
|---|---|---|
| The read path | `backend/audit/reader.py` | the audit package owns this concern; a sibling of `sink.py`, not a new top-level mechanism |
| The HTTP surface | `backend/api/audit_trail.py` | citizen 0 — one file per domain, auto-discovered, nothing else edited |
| The resource type | `authz/resources.py` `_RESOLVERS` | `declared_types()` then picks it up, so it joins the administrators policy automatically |
| The view | `frontend/src/views/AuditTrailView.tsx` | Observability, beside `EventsView` |

### D-2. Newest-first, backwards, with early exit

Files are enumerated `audit.jsonl`, `.1`, `.2` … `.24`; within a file, lines are read **from the end**
in bounded chunks (256 KiB) and yielded in reverse. The scan stops at the first of: page filled, the
`since` bound passed, or the FR-5 cap.

This is what buys NFR-1. A parse-everything reader costs ~110 ms at the old 20 MiB ceiling and would
cost ~700 ms at 125 MiB; reading backwards with early exit makes an unfiltered first page cost
roughly one chunk regardless of how much history exists.

### D-3. The cursor is an event id, and ordering is insertion order — not `ts`

**`ts` is wall-clock and must not be the sort key.** An NTP step can give a later-appended event an
earlier timestamp, so ordering by `ts` would reorder history and, worse, make a cursor skip events.
The sequence is therefore *reverse insertion order* — the order the lines physically appear — and the
cursor carries the last event's `event_id`. Resuming walks newest-first, discards until it sees the
anchor, then takes the next page.

Cost is O(pages × page) to reach page *k*, which is the price of being correct under concurrent
append and rotation. It is paid only by someone paging deep, and a time filter is the cheap way to
go far back.

**A cursor whose anchor has been rotated out is a real state**, not an error to hide: the response
reports `cursor_expired: true` and returns the newest page, because the alternative is an empty page
that reads as "no more events".

### D-4. Reading the trail is always audited — via the chokepoint, not a second call site

FR-7 wants the read recorded even though read auditing is off by default. The tempting fix is an
explicit `audit.emit()` in the handler, and it is the wrong one: `request_hook`'s docstring commits
to exactly two emit call sites and says *"a third would mean this chokepoint had failed"*.

The first draft of this design said `@action(get="audit.trail:list", always_audit=True)`. **That
cannot work, and the reason is worth recording because the next person will try it too:** the
decorator's signature is `action(name=None, *, variant=..., **per_method: str)`, so any unrecognised
keyword is swallowed as a *per-method* declaration — `always_audit=True` would register an action for
an HTTP method called `always_audit`, with a non-string value. Silently wrong rather than rejected.

So the rule is an explicit set in `audit/actions.py` instead:

```python
ALWAYS_AUDITED = frozenset({"audit.trail:list", "audit.trail:read"})
```

consulted by the hook alongside `NVIS_AUDIT_READS`. This is the better shape anyway, and not merely
the available one: *which reads are recorded regardless of configuration* is a security decision, and
a reader should be able to see the whole answer in one place rather than assemble it by grepping
decorators across 38 modules. It follows `COLLECTION_FALLBACK_ACTIONS`, which is an explicit
allowlist for the same reason — and, like that set, its **contents must be asserted**, since a set
that silently grows is how a read stops being recorded.

**Self-amplification is prevented by the architecture, and asserted anyway.** The event is emitted in
`after_request`, after the body is built, so a read's own event cannot appear in its own response.
Repetition is bounded by the existing flood guard, and the view **does not poll** — it loads on demand
with an explicit refresh, which NFR-1's 50 ms budget assumes rather than a background timer.

### D-5. A collapsed event is rendered as what it is

`count` and `collapsed` are carried through the API unchanged, and the view shows *"×47"* rather than
one row. A reader that dropped them would report one refusal where forty-seven happened — in exactly
the review where the count is the finding.

### D-6. What the reader must not do

No write, no truncate, no rotate, and no `remove()`-shaped "tidy up while reading". This is
`identity-auth-traps.mdc` §2 restated: `revocation_snapshot()` pruned expired rows on a *read* path,
TinyDB rewrote the file even when nothing matched, and the write invalidated the cache keyed on the
file's mtime — so the cache never hit once while every functional test passed. AC-12 asserts mtime,
size and inode are unchanged, and deliberately excludes `atime`, which a read legitimately updates.
