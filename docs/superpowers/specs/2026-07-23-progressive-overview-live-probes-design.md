# Progressive Overview Live Probes Design

**Date:** 2026-07-23
**Status:** Approved

## Goal

Render Cluster Overview identity and inventory within one second, regardless of
SSH, BCM, or Slurm probe latency. Load live cluster details independently with
clear loading, stale, failed, and retry states.

## Root Cause

`/api/cluster-overview` currently returns one JSON document only after it runs
the live Slurm version, BCM version, and queue probes. A persisted inventory
snapshot therefore does not make the route fast. BCM probe contention with the
background inventory collector can delay the response for 60–80 seconds, while
the frontend displays a full-page skeleton.

## Architecture

### Fast overview

`GET /api/cluster-overview` must perform no remote execution. It returns:

- active cluster/catalog identity and dashboard metadata;
- the canonical inventory snapshot and nullable inventory counts;
- configured version values when already supplied by environment/catalog;
- no live BCM, Slurm, or queue probe results.

The route must return in under one second in a regression test even when every
live probe is blocked.

### Live details endpoint

`GET /api/cluster-overview/live` owns remote probes and returns:

- BCM version result;
- Slurm version result;
- queue result;
- per-section state: `initializing`, `fresh`, `stale`, or `failed`;
- last-success, last-attempt, age, and sanitized error metadata.

The endpoint returns cached results immediately. An expired or absent entry
acquires a process-safe lease and refreshes in a daemon thread. Concurrent
requests must not start duplicate refreshes.

### Cache policy

Store live details in the existing SQLite snapshot database or a sibling SQLite
repository so Gunicorn workers share values and leases.

- BCM version TTL: 3600 seconds.
- Slurm version TTL: 3600 seconds.
- Queue TTL: 15 seconds.
- Preserve last-good values as `stale` when refresh fails.
- With no prior success, expose `failed`, never invented values.
- A refresh failure in one section must not discard successful sections.

### Frontend

Cluster Overview renders the page shell before either request finishes:

- use active-cluster context for immediately available identity;
- show section-shaped KUI skeletons for missing base data;
- fill identity and inventory from `/api/cluster-overview`;
- fetch `/api/cluster-overview/live` independently;
- show loading only for live sections actually refreshing;
- show stale values with a warning;
- show failed/unavailable state with retry without hiding inventory.

The existing two-row statistics grid remains unchanged.

## Error Handling

- Main Overview failure must not hide the page shell.
- Live endpoint failure must not replace successful inventory.
- Errors returned to the browser must be sanitized and bounded.
- Cluster-unreachable state returns cached stale details immediately and does
  not start remote probes until reachability recovers.

## Testing

Backend tests cover:

- fresh inventory plus blocked probes still yields a sub-second main response;
- cold and cached live endpoint responses return immediately;
- cache TTLs and lease contention;
- independent section success/failure and stale preservation;
- unreachable clusters do not start probes.

Frontend tests cover:

- page shell before base data;
- inventory visible while live details load;
- live success, stale, failure, retry, and unreachable states;
- no false zero inventory values;
- hard refresh does not return to an empty full-page wait.

## Out of Scope

- Changing inventory collector cadence or BCM command implementations.
- Streaming individual rack rows.
- Removing the existing `/api/slurm-squeue` operational endpoint.
