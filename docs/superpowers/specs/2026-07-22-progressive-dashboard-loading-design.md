# Progressive Dashboard Loading (A + C) — Design

**Date:** 2026-07-22  
**Status:** Approved direction (A + C); implementation plan in `docs/superpowers/plans/2026-07-22-progressive-dashboard-loading.md`

## Problem

After restart/refresh, Overview and Racks wait on a **blocking** canonical inventory collect inside the first `/api/cluster-overview` or `/api/status` call. That collect can take on the order of minutes (especially duplicated Slurm `squeue` work), so the UI stays blank and feels broken.

## Goals

1. **C — Non-blocking snapshot:** HTTP handlers return current snapshot state quickly (`initializing` / `stale` / `failed` / `fresh`); collection runs under the existing SQLite lease in a background thread.
2. **A — Shell first:** Once the first JSON arrives, show cluster identity/metadata immediately; inventory sections show loading or failed, never false zeros.
3. Keep Overview and Racks on the **same** shared snapshot payload and `snapshot.id`.

## Non-goals

- Progressive per-rack streaming as collectors finish (option B)
- Changing catalog/multi-cluster model
- Tuning individual cmsh/RF0 timeouts as the primary fix

## UX

| Snapshot state | Overview | Racks |
|----------------|----------|-------|
| Request in flight, no JSON yet | Full-page skeleton | Full-page skeleton |
| `initializing` | Identity + “Loading cluster inventory…”; stat skeletons/`—` | Banner + table skeleton |
| `failed` (no payload) | Identity + error banner + Retry; inventory unavailable | Same |
| `stale` | Identity + stale warning; last-good counts | Table with stale warning |
| `fresh` | Normal | Normal |

## Backend shape

- `ClusterSnapshotService.get_cluster_snapshot(..., wait: bool = True)`
  - `wait=False`: acquire lease → start daemon collector → return leased row immediately
  - `wait=True`: current blocking behavior (background loop / deterministic tests)
- API routes use `wait=False`
- Overview metadata from env/catalog always; skip slow live probes while cold-initializing
- Canonical collect must not double-call `squeue` via live-table merge

## Frontend shape

- Hooks keep fast poll (~3s) while `initializing`
- Views stop treating “no inventory yet” as “no page”
- Reuse `ClusterSnapshotState` for banners

## Success metrics

- First useful Overview paint ≤ ~2s
- No false zero inventory during `initializing`/`failed`
- Same `snapshot.id` on Overview and Racks after success
