# PLC Test Plan — audit trail reader

**Status:** draft, 2026-09-10.
**Brief:** `.plc/briefs/auth-audit-reader-brief.md` — 17 requirements, 17 acceptance criteria.
**Approved:** feature lead, 2026-09-10 (requirements and design, with retention raised to 125 MiB).

## Purpose

Verify that the trail can be read completely, in the right order, without being altered — and that
the three failure modes which would be *silent* are caught: a page that skips events, a collapsed
event reported as one occurrence, and a read that writes.

## Entry criteria

Phase 3 delivered (the authorization gate this feature is scoped by), and the audit sink writing
events with the pinned `EVENT_FIELDS` schema.

## Exit criteria

Every AC cited by at least one case; NFR-1 measured and recorded in `docs/PERFORMANCE_BASELINE.md`;
the `ALWAYS_AUDITED` set's contents asserted.

## Environments and fixtures

| Fixture | Provides |
|---|---|
| `a_trail(tmp_path, n, rotated=0)` | a synthetic trail of *n* events across *rotated+1* files, with known ids in known order |
| `a_big_trail(tmp_path)` | 125 MiB across 25 files — the ceiling, for NFR-1 and NFR-2 |
| `reader_client` | an authenticated client holding `audit.trail:list`, via `identity.testing.enrol` + `attach_policy` |
| `no_grant_client` | authenticated, holding no audit grant — for AC-6 |

`a_big_trail` is generated, not committed: 125 MiB of fixture in git would be absurd, and the shape
matters more than the content.

## Test cases

| ID | Verifies | Case | Expected |
|---|---|---|---|
| TC-1 | AC-1 | Unfiltered first page over a trail spanning 3 rotated files | 50 events, reverse insertion order, crossing the file boundary without a gap |
| TC-2 | AC-1 | The order is **insertion order, not `ts`** — one event written with a deliberately skewed clock (`ts` earlier than its predecessor) | it still appears in the position it was written, not reordered |
| TC-3 | AC-2 | Each of principal, action, service, resource, decision, result, since, until | narrows the result; parametrised, one case per field |
| TC-4 | AC-2 | Two filters together | AND, not OR — asserted by a pair that would return more under OR |
| TC-5 | AC-3 | A burst of 47 identical refusals, collapsed by the flood guard | the response carries `count: 47` and `collapsed: true`; the summed occurrences equal 47, not 1 |
| TC-6 | AC-4 | Events written, rotation forced, then read | the pre-rotation events are still returned |
| TC-7 | AC-5 | `principal=nobody` over a trail larger than the cap | `truncated: true`, `scanned_events` at the 20,000 cap, `scanned_bytes` ≤ 8 MiB |
| TC-8 | AC-5 | A `since` bound within the trail | the scan **stops** at the bound — `scanned_events` far below the cap, proving the filter terminates rather than merely excludes |
| TC-9 | AC-5a | 10 events appended between requesting page 1 and page 2 | page 2 repeats none of page 1 and skips none — the defect an offset would cause |
| TC-10 | AC-5a | A cursor whose anchor event has been rotated away | `cursor_expired: true` and the newest page, not an empty page |
| TC-11 | AC-6 | `no_grant_client` reads the trail | 403, and the body is byte-identical to the refusal for a trail that does not exist |
| TC-12 | AC-7 | N reads, parametrised over N ∈ {1, 2, 5} | exactly N `audit.trail:list` events appended; a read's own event absent from its own response |
| TC-13 | AC-7 | `NVIS_AUDIT_READS=0` | the read is **still** recorded — the point of `ALWAYS_AUDITED` |
| TC-14 | AC-8 | The generated vocabulary | `audit.trail:list` and `audit.trail:read` present and distinct from `audit.deep:*` |
| TC-15 | AC-8 | `ALWAYS_AUDITED` contents | asserted exhaustively — a set that silently grows is how a read stops being recorded, and how an unrelated read starts being |
| TC-16 | AC-9 | Unfiltered first page over `a_big_trail` | ≤ 50 ms; recorded in `PERFORMANCE_BASELINE.md` |
| TC-17 | AC-9 | A filtered request exhausting the cap | ≤ 400 ms — the worst case, measured so it is not discovered later |
| TC-18 | AC-10 | Peak allocation while reading `a_big_trail`, via `tracemalloc` | < 4 MiB, and **not** proportional to trail size: asserted against two trail sizes so a linear implementation fails |
| TC-19 | AC-11 | A file whose final line is truncated mid-JSON (a crash mid-write) | skipped, counted in `skipped_lines`, the rest of the page returned |
| TC-20 | AC-11 | A line that is not JSON at all, and a JSON line that is not an object | both skipped and counted; neither raises |
| TC-21 | AC-12 | mtime, size and inode of every trail file, before and after a read | unchanged. `atime` deliberately not asserted — a read legitimately updates it |
| TC-22 | AC-14 | No trail file present | 200 with an empty list, not 404 and not 500 |
| TC-23 | AC-15 | Rotation forced between page 1 and page 2 | no duplicate, no drop |
| TC-24 | AC-13 | `AuditTrailView` renders events and applies a filter | automated render test |
| TC-25 | AC-13a | An empty *filtered* result versus an empty *trail* | visibly different — "no events match" must not look like "we no longer hold that period" |
| TC-26 | AC-13a | The view names the oldest event held | present, and taken from the response rather than computed in the browser |

## Coverage

| Requirement | Covered by |
|---|---|
| FR-1, FR-1a | TC-1, TC-2, TC-9, TC-10 |
| FR-2 | TC-3, TC-4, TC-8 |
| FR-3 | TC-5 |
| FR-4 | TC-6 |
| FR-5 | TC-7, TC-8 |
| FR-6 | TC-11 |
| FR-7 | TC-12, TC-13, TC-15 |
| FR-8 | TC-14 |
| FR-9, FR-10 | TC-24, TC-25, TC-26 |
| FR-11 | TC-22 |
| FR-12 | TC-23 |
| NFR-1 | TC-16, TC-17 |
| NFR-2 | TC-18 |
| NFR-3 | TC-19, TC-20 |
| NFR-4 | TC-21 |

## Layer coverage

| Layer | Exercised by |
|---|---|
| Unit | TC-1…TC-10, TC-19, TC-20 — `audit/reader.py` directly |
| Integration | TC-5, TC-6, TC-12, TC-13, TC-22, TC-23 — through the API and the real sink |
| Authorization | TC-11 |
| Performance | TC-16, TC-17, TC-18 |
| Frontend | TC-24, TC-25, TC-26 |
| Regression / build gate | TC-14, TC-15 |
| Security | TC-11, TC-21 — refusal parity, and the read-does-not-write property |
| Manual | *N/A* — every case is automatable; there is no hardware or cluster dependency |

## Known risks in this plan

- **TC-18 is the one that can pass for the wrong reason.** `tracemalloc` measures Python allocations,
  so a reader that let the OS page in a 125 MiB file via `mmap` could show low Python heap use while
  still being unbounded. Asserting against *two* trail sizes is the mitigation: the property is that
  peak does not grow with the file, not merely that one number is small.
- **TC-16's budget is machine-dependent.** Recorded on the measuring hardware, as AC-9 requires, or
  it detects nothing on a different box.
- **TC-5 depends on the flood guard's threshold and window.** Both are settings; the case must set
  them explicitly rather than inherit defaults, or it becomes a test of the configuration.
