---
diataxis: reference
title: ClusterKit Mac Integration, 2026-10-30
---

# ClusterKit Mac Integration, 2026-10-30

## Source and Scope

The received archive was `greenlight-clusterkit-complete-20261030.tar.gz`, with
SHA-256 `b9c592de3bc34dc3bcb23917ee9589e7e57266b8025686dda253e278d47c3043`.
Its internal `SHA256SUMS` verified successfully. The cumulative patch applied
cleanly to `feature/clusterkit-ui` at
`8de202c48f21bc3ff0c5c5380ae7ede4f9421179`.

Commit `51d07f3` imports exactly the 40 changed paths, including 14 new files.
All 1,594 tracked/changed files and links in the full source snapshot matched
the integrated tree, including executable bits. Three transfer-only files were
not imported: `HANDOFF_APP_SERVER.md`, `requirements.offline.lock` and
`SOURCE_REVISION.json`. Site profiles and operational launcher/fence references
remain evidence, not generic defaults or installation commands.

The import includes canonical read-only rack inventory, rack-scoped selection,
bounded whole-rack requests, opt-in logical tiles without invented physical rows,
display-name compatibility with stable catalog IDs, quieter routine notes and
per-attempt failure explanations. Static Slurm/rank scripts, the compute execution
module and the scheduler providers are unchanged by the October 30 patch.

The only additional generated-code correction is the action snapshot entry for
`GET /api/clusterkit/racks`, regenerated through `bin/gen-action-snapshot.py`.
The transferred action vocabulary already included that endpoint.

## Mac Verification

| Check | Result |
|---|---|
| ClusterKit backend, API, inventory, whole-rack, failure, rename and cold-compute tests | 246 passed; 186 subtests passed |
| API/inventory/service tests with external process and connection audit guard | 178 passed; 29 subtests passed |
| Shared snapshot, authorization, routes, scheduler, script-tree and import compatibility | 738 passed |
| Action coverage and audit declarations after snapshot regeneration | 41 passed |
| Full frontend suite | 1,005 tests passed across 106 files |
| Focused ClusterKit, floor and topology suite | 89 passed across 7 files |
| Backend and new backend-test Ruff | Passed |
| Frontend ESLint | Zero errors; 66 existing warnings outside this feature |
| TypeScript, SPA and tracker production builds | Passed through `start.sh` |
| Generated action vocabulary/snapshot, Settings reference and TOML example | Current |
| Documentation structure and warning-as-error HTML build | Passed |
| Tracked-source-only import check without built frontend assets | 217 modules imported; 309 routes registered |

The test sets overlap; their counts are not a unique combined total. This is a
focused Mac integration verification, not a claim that every backend test or the
Linux deployment suite was rerun.

Browser checks use a separate local TLS/SRP instance, synthetic rack/success
fixtures and replayed recorded failure responses. Every ClusterKit browser API
request is intercepted locally, including confirmation: no scheduler work is
submitted. The checks cover an 18-node/153-pair whole-rack preview, recovery UUID
before mocked confirmation, recorded UCX failure explanation and node diagnostics.
Desktop 1440x1000 and mobile 390x844 are checked in both light and dark themes.
Mobile checks use the existing collapsed navigation rail. The expanded shared
sidebar is not a mobile-layout qualification.
The existing horizontally scrollable result tables remain dense on narrow
screens; the planned results/navigation work still includes mobile table polish.

## Existing Live Evidence and Remaining Work

The AppServer handoff reports a successful Q4 18-node control, job 226760, with
153/153 pairs for bandwidth and latency in both modes. Those are retained
AppServer observations, not jobs run by the Mac. A 64-node software bound is not
live qualification of every size; administrator policy and approved recipes
still determine availability.

The Q1 communication issue remains unresolved. Local preflight PASS does not
prove inter-node communication; missing measurements are not confirmed failed
links. A UCX reporting node is not a diagnosed faulty GPU/NIC, peer, cable or
switch. Failed attempts remain failures even if a smaller diagnostic test passes.
Functional PASS remains separate from performance UNVERIFIED.

The October 30 call's combined bandwidth/latency rows, result sorting/pagination,
drill-down navigation, acceptance policies and historical comparisons are further
work, not features supplied by this integration commit.

The [deployment plan](../how-to/clusterkit-deployment-plan.md) remains separately
gated. No production or AppServer pilot deployment, restart, configuration,
permissions, tunnel, node state or cluster job was changed by this Mac integration.
