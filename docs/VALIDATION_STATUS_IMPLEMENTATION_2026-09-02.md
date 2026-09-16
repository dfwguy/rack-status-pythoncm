# Validation Status redesign — implementation note (2026-09-02)

## Outcome

`Analytics & Benchmarks > Post Run Node Report` is now **Validation Status**. The default view is
a condensed floor map that can display the Nscale 16K topology as 14 SU rows, 16 racks per SU,
and 18 nodes per rack (3 across by 6 down). It preserves the existing node investigation dialog,
deep-audit actions, operator marking, and hardware/network audit tables.

The implementation was derived from `/cluster/OVA/rackstatus/example.zip`:

- `daily-status-node-racks-2026-09-02-filtered.jpg` (3300x2550, 300 DPI)
- `slurm-state-node-racks-flashing.gif` (3300x2550)

The on-screen map uses compact HTML buttons for keyboard accessibility and native hover/focus
evidence. It renders one CSS gradient per node rather than one DOM element per check, keeping the
4,032-node view responsive. Zoom is 40–150%, with pan/overflow and a 62% all-SU default.

## Live data and refresh

No new switch, Slurm-controller, or filesystem scraper was added. The view composes two existing,
first-class Rack Status feeds:

1. `/api/post-run-node-report`: exact node allocations from durable validation activities plus
   scheduler accounting, current scheduler state/reason, deep audits, findings, and Job IDs.
2. `/api/bringup/fleet`: the existing read-only `/cluster/bringup` integration. It delegates
   verdicts to `status-check.sh` and metrics to `daily-report.sh`, preserving the bring-up system's
   authoritative pass/fail floors. Its backend cache is already 30 seconds.

The browser refreshes both feeds every 30 seconds and prevents overlapping refreshes. Failure of
the bring-up feed leaves those checks unknown and displays a warning; missing data is never green.

## Validation views

The default `All validations` combines every available dimension, including Slurm, Node Health,
DCGM (`dcgm`/`dcgmi-r4`), NCCL tests, NeMo 56B, Llama 3.1 405B, MNUberGEMM,
`llm-pretrain`, and `llm-pretrain-solo`. The last three checks are intentionally included.

The selector always contains All, Node Health, Slurm, DCGM, NCCL, NeMo 56B, Llama 3.1 405B, and
MNUberGEMM. Source names outside those classifiers become additional discovered selections rather
than being dropped. Each per-domain selection changes map status, tooltips, node detail, ETA, CSV,
PDF, and JPEG together.

Status rules:

- dark green: all selected evidence passed/completed, including Slurm `UP`/`IDLE`;
- red: failed, cancelled, blocked, down, drained, timed out, or other hard failure evidence;
- orange: warning, degraded, stale, incomplete, retry, pending/queued, or inconclusive evidence;
- yellow: explicit active execution only (`RUNNING`, `ONGOING`, `ALLOC`, `ALLOCATED`,
  `COMPLETING`, or `CONFIGURING`), with pulse;
- dark red: validation evidence exists but every validation check is NOT_RUN/unknown;
- gray: no evidence for the selection;
- checkerboard segments use the same contract: good dark green, hard failure red, warning orange,
  NOT_RUN light red, running yellow, and unknown gray.

Raw status matching is case/whitespace tolerant and strips the common Slurm `*` suffix. Unknown
provider-specific states remain gray with their raw value in the tooltip instead of being guessed.
Numeric/scientific values are pass evidence only for a metric-only bring-up cell whose explicit
status is absent; a numeric value never overrides an explicit failure.

Only the first 15 segments are drawn when more checks exist. Every check remains in node detail,
tooltips, CSV, and PDF. Running nodes pulse; `prefers-reduced-motion` replaces animation with a
static double outline.

## ETA methodology

SU and cluster ETAs use timestamped PASS completions already present in durable post-run evidence.
They require at least two pass transitions spanning one minute. Confidence is based on sample count
and observation span. The UI explicitly withholds an ETA when:

- failed, warning, or NOT_RUN nodes block an all-pass outcome;
- unknown/missing evidence withholds an ETA rather than implying future success;
- fewer than two usable transitions exist;
- the completion stream has stalled (no completion within the greater of 10 minutes or twice the
  observed median completion interval);
- the computed rate is zero or invalid.

Bring-up cells currently carry verdict and metric but no transition timestamp. Their current state
is displayed and exported, but they do not fabricate ETA samples.

## Exports

- CSV schema: `rack-status-validation-v2`; formula-injection guarded; CR/LF/tab normalized. It
  includes selected mode, filters, SU/rack/node, overall and check status, source freshness,
  evidence timestamp, ETA/confidence, test name, metric/unit, Job ID, reason, and source.
- PDF: 11x8.5 landscape. Page 1 is the selected 3300x2550 map. A compact exception appendix
  retains SU/rack/node, status, ETA/confidence, check, Job ID, timestamp, metric, and failure reason.
  The CSV is the exhaustive all-check artifact; passing nodes remain represented on the map rather
  than producing thousands of redundant PDF rows.
- JPEG: deterministic 3300x2550 with JFIF density set to 300 DPI, suitable for an 11x8.5 landscape
  print. It includes title/time, selected mode, source freshness, ETA, legend, SU/rack labels, and
  the required dashed separators after physical racks 4 and 20 plus the solid 8/17 boundary.

Every export reflects the active mode, SU/rack filter, and node search.

## Validation performed

- Focused tests: `validationStatus`, `validationExport`, and `PostRunNodeReportView`.
- Full frontend suite: 452 tests passed on 2026-09-02 after the status-contract correction.
- TypeScript production build passed.
- ESLint: zero errors (57 existing repository warnings, including the established effect-load
  pattern also used on this view).
- Browser load with a production-scale 4,032-node fixture: 14 SUs, 224 racks, 4,032 accessible
  node buttons rendered. The map and 3300x2550 JPEG were visually inspected; labels and legend were
  not clipped. CSV/PDF/JPEG downloads completed in Chromium.

## Deployment

This change is frontend-only. A production deployment must keep `static/spa/index.html` and its
content-hashed `assets/` from the same build and use the documented in-place index update. No
backend or service restart is required. Run `bin/prod-drift.sh` and `bin/verify-deploy.sh` before
and after deployment respectively.

### Nscale TX live deployment record

Deployed to the Rack Status-only Compose service on port 5056 on 2026-09-02. The first candidate
was immediately rolled back when live browser QA proved that the backend `appserver` sentinel was
being grouped as a fifteenth SU. Physical topology grouping now accepts only `dhNN-letterNN` rack
identifiers; the regression is covered by `validationStatus.test.ts`.

The corrected deployment uses image
`sha256:861b45444da6683ae74f14301bcdbf6580564c150c498a7179a79f0fa60a7807` and retained the existing
`rack-status_rack-data` volume. The pre-deploy image remains tagged
`rack-status-5056-rollback:20260902T175529Z`; its container/image inspection, dirty-tree patch,
untracked-source archive, and a volume archive are under
`/tmp/rack-status-5056-rollback.CV20xf` on the deployment host.

Live evidence currently contains 3,978 physical compute nodes in 221 racks across all 14 SUs.
Three physical racks are absent from `/api/post-run-node-report`, while `/api/bringup/fleet`
currently returns no node rows. The UI does not invent those nodes or report them healthy.
Authenticated post-run, validation-activity, node-issue, deep-audit, and workflow APIs all passed
after deployment. Live NCCL CSV/PDF/JPEG exports passed; the JPEG is 3300x2550 at 300 DPI.

### Slurm status-contract correction

The 2026-09-02 follow-up corrected the Slurm `UP` alias and replaced scattered color decisions with
one normalization contract shared by the floor map, node detail, rollups, ETA, and exports. The
full frontend suite passed 452/452 tests. The Rack Status-only deployment is image
`sha256:f3a23d17cb59cf3d99fd5cc8cd4be1b3b4225abc1425812c6937dbdd5645bc8b`, container
`95846b5fb7709a19981f203d0e0d8a678989f3049d0f807096de1ff278d43f39`, with zero restarts and
the original `rack-status_rack-data` volume. Its immediate rollback tag is
`rack-status-slurm-rollback:20260902T192906Z`; fresh inspection/source/data-volume artifacts are in
`/tmp/rack-status-slurm-rollback.zYyDNf`.

At final live QA, the source and Slurm map matched exactly: 3,848 `up` nodes were green, 97 `down`
nodes red, and 33 `unknown` nodes gray. No node was allocated at that snapshot. All required and
discovered menu modes rendered 3,978 physical nodes across 221 racks and 14 SUs. Chromium reported
no console/network errors, the 30-second refresh advanced on schedule, node detail preserved raw
evidence and job history, and selection-aware Slurm CSV/PDF/JPEG exports passed. The live JPEG was
visually inspected at 3300x2550/300 DPI with no clipped rack labels or legend.

### Cursor zoom and indexed DCGM evidence

The 2026-09-02 DCGM follow-up reads only the existing bounded collector index at
`/cluster/OVA/logs/nscaletx-16k-validation/validated-racks.json`; it never scans the 421 GB log
tree, journalctl, switches, or remote hosts. `lake.validation_attempts[].evidence[]` supplies the
node, test, explicit verdict, detail, job, artifact path, and mapping method. The index does not
contain an artifact completion timestamp, so the application labels `collected_at` as observation
time and does not invent an evidence timestamp.

The DCGM batch is stored atomically in the existing `results_history` SQLite database on the
`rack-status_rack-data` volume. Schema v2 adds a durable ingestion checkpoint. A content
fingerprint skips unchanged DCGM evidence; deterministic artifact/job/test/verdict IDs deduplicate
reprocessing; any malformed batch rolls back before the checkpoint advances. Canonical current
state is the newest job sequence for each node and r1/r3/r4 test. Superseded and `.prevN` attempts
remain in node detail/export but cannot inflate current repeat-failure flags. An absent required
r1/r3/r4 result is explicitly NOT_RUN, never pass. INCOMPLETE remains warning.

At deployment the index contained 5,430 DCGM records: 4,974 PASS, 455 FAIL, and one INCOMPLETE.
The current projection contains 4,982 node/test results: r1 2,688 PASS/5 FAIL and r3 2,284 PASS/5
FAIL; r4 has no indexed evidence. The next scheduled collector run left the DCGM checkpoint and
5,430-row count unchanged, proving the idempotent skip path.

The map viewport now owns a non-passive wheel listener, so wheel/trackpad zoom is captured only
while the pointer is over the map. Zoom remains clamped to the slider's 40–150% range and adjusts
scroll offsets to preserve the logical map point under the cursor. The existing accessible slider
and keyboard path remain available; no zoom animation is introduced for reduced-motion users.

To keep the 30-second view responsive, the SPA uses `?view=map`. That backward-compatible response
removes the duplicate rack tree and superseded history, retaining only the current/latest event per
node/test. The original API remains unchanged for other clients; clicking a node lazily requests
the existing `?node=<exact-node>` response with complete history. Live compact payload is 11.0 MB
uncompressed but 730,827 bytes over the wire with zstd: 1.18 s forced-cold and 0.21 s cached on
localhost. Exact-node history is 1,846 bytes compressed and 1.06 s.

Final gates: backend 1,513 passed/1 skipped; frontend 459/459; production TypeScript/Vite build and
deployment verification passed. Live Chromium rendered 3,978 nodes, 221 racks, and all 14 SUs with
no console errors. DCGM aggregate states were 8 attention (the 10 current failures span 8 nodes),
2,685 warning, and 1,285 NOT_RUN. A visible mouse-wheel gesture changed 62% to 115%; the 30-second
map request repeated successfully. DCGM CSV/PDF/JPEG exports succeeded: CSV v2 contains exactly
11,934 active rows (r1/r3/r4 for 3,978 nodes) with provenance; JPEG is 3300x2550 at 300 DPI; PDF is
11x8.5 landscape. The deployed image is
`sha256:37889b90e8b7d8a59500ea7de81513b87adf3365f91bca3fc51554b3011b91b4`, container
`00458f770eb3dc195dcbe9d3e2e175b5ce644e672a30e065165f8ef9bf793891`, zero restarts. Rollback image
`rack-status-dcgm-rollback:20260902T195902Z` points to
`sha256:f3a23d17cb59cf3d99fd5cc8cd4be1b3b4225abc1425812c6937dbdd5645bc8b`; inspection, source patch,
and persistent-volume snapshot are under `/tmp/rack-status-dcgm-rollback.20260902T195902Z`.

Final topology QA also corrected a frontend-only ordering bug that had assigned lexical rack rows
sequentially. The explicit production contract is now `dh02-a` through `dh02-k` = SU1 through
SU11, followed by `dh01-a`, `dh01-b`, and `dh01-c` = SU12, SU13, and SU14. Unknown rows are labeled
unmapped instead of being silently assigned an SU. Chromium verified that exact 14-row order, and
the final All-view CSV starts with SU1/`ink50-dh02-a01`; the exported JPEG visibly preserves all 14
rows in the same order. Wheel zoom now snaps to the range control's 5% steps so the controlled
slider, percentage label, and rendered map remain identical; live QA moved 62% to 115%, retained
cursor anchoring, and observed both the initial and scheduled 30-second compact requests return
HTTP 200. Final All-view exports were 11,514,274-byte CSV, 3,882,000-byte 167-page PDF, and
2,831,203-byte 3300x2550/300-DPI JPEG.
