# Progressive Overview Live Probes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Cluster Overview render identity and inventory immediately while BCM, Slurm, and queue details load and fail independently.

**Architecture:** Remove all remote execution from `/api/cluster-overview`. Add a shared SQLite-backed live-probe service with per-section TTLs, leases, stale preservation, and a non-blocking `/api/cluster-overview/live` endpoint. Fetch that endpoint independently in React and render KUI section-level loading, stale, failed, and retry states.

**Tech Stack:** Flask, Python `sqlite3`/threads, React 19, TypeScript, NVIDIA Kaizen UI, pytest, Vitest.

## Global Constraints

- `/api/cluster-overview` must perform no SSH, BCM, Slurm, or queue execution.
- A blocked live probe must not delay the main Overview response.
- BCM and Slurm version TTLs are 3600 seconds; queue TTL is 15 seconds.
- Cache and leases must be shared across Gunicorn workers.
- Preserve last-good live values as stale on failure.
- Inventory and live-detail failures must not hide each other.
- The Overview shell must render before either request completes.
- Use KUI loading/status components; do not redesign the existing two-row statistics grid.
- Do not change inventory collector cadence or BCM command implementations.
- Do not commit unless the user explicitly requests it.

---

### Task 1: Shared live-probe repository and service

**Files:**
- Create: `backend/cluster_overview_live.py`
- Create: `backend/tests/test_cluster_overview_live.py`

**Interfaces:**
- Produces `LiveProbeSection` with `state`, `value`, `last_attempt_at`, `last_success_at`, `age_seconds`, and `last_error`.
- Produces `ClusterOverviewLiveService.get_snapshot(cluster_id, force_refresh=False) -> dict`.
- Constructor accepts section collectors, per-section TTLs, SQLite path, clock, and lease duration.

- [ ] **Step 1: Write failing repository/service tests**

Cover cold non-blocking return, success transition, per-section TTL, contention, stale preservation, independent section failure, and expired lease recovery. Use `threading.Event` gates with bounded cleanup.

```python
def test_cold_request_returns_initializing_and_collects_in_background(tmp_path):
    started = threading.Event()
    release = threading.Event()
    service = make_service(
        tmp_path,
        bcm=lambda: gated_value(started, release, "BCM 11"),
        slurm=lambda: "slurm 25.05",
        queue=lambda: {"ok": True, "stdout": ""},
    )
    snapshot = service.get_snapshot("cluster-a")
    assert snapshot["sections"]["bcm"]["state"] == "initializing"
    assert started.wait(1)
    release.set()
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest backend/tests/test_cluster_overview_live.py -v`

Expected: collection error because `cluster_overview_live` does not exist.

- [ ] **Step 3: Implement the SQLite state machine**

Use one row per `(cluster_id, section)` with:

```sql
PRIMARY KEY (cluster_id, section)
state, value_json, last_attempt_at, last_success_at, last_error,
lease_owner, lease_expires_at
```

Acquire leases with `BEGIN IMMEDIATE`. Return current rows immediately and start one daemon refresh per expired/missing section. Validate collectors section-by-section; commit failure as `stale` when `value_json` exists and `failed` otherwise. Sanitize errors using a bounded helper.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest backend/tests/test_cluster_overview_live.py -v`

Expected: all tests pass.

---

### Task 2: Fast Overview route and independent live endpoint

**Files:**
- Modify: `backend/app.py`
- Modify: `backend/tests/test_app_api.py`

**Interfaces:**
- Consumes `ClusterOverviewLiveService`.
- Produces `GET /api/cluster-overview/live?refresh=1`.
- Changes `build_cluster_overview` to local/snapshot-only data.

- [ ] **Step 1: Write failing route tests**

Add tests that replace all three live probes with gated/blocking functions and assert:

```python
started = time.monotonic()
body = client.get("/api/cluster-overview").get_json()
assert time.monotonic() - started < 1.0
assert body["cluster_name"]
assert body["snapshot"]
assert probe_calls == {"bcm": 0, "slurm": 0, "queue": 0}
```

Add `/api/cluster-overview/live` tests for immediate `initializing`, eventual fresh values, `refresh=1`, cluster unreachable without collector calls, and sanitized failure metadata.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest backend/tests/test_app_api.py -k "overview_live or overview_never_runs_live_probes" -v`

Expected: main route invokes probes and live route is 404.

- [ ] **Step 3: Remove live probes from `build_cluster_overview`**

Keep configured environment/catalog version values, but never call:

```python
_cluster_probe_slurm_version()
_cluster_probe_bcm_cmsh_version()
_cluster_squeue_overview_block()
```

Return a neutral queue placeholder from the base response only for backward compatibility, marked unavailable with `"live details load separately"`.

- [ ] **Step 4: Add live service factory and endpoint**

Build collectors around the existing probe functions. Use the snapshot DB directory for a sibling `cluster_overview_live.sqlite3`, keyed by `_cluster_snapshot_identity()`. When unreachable, return cached state without starting refreshes. `refresh=1` expires all section TTLs but remains non-blocking.

- [ ] **Step 5: Verify GREEN and existing API compatibility**

Run:

```bash
python -m pytest backend/tests/test_cluster_overview_live.py backend/tests/test_app_api.py -v
```

Expected: all tests pass.

---

### Task 3: Frontend live-detail types and hook

**Files:**
- Modify: `frontend/src/types/api.ts`
- Create: `frontend/src/hooks/useClusterOverviewLive.ts`
- Create: `frontend/src/hooks/useClusterOverviewLive.test.ts`

**Interfaces:**
- Produces `ClusterOverviewLiveResponse` and `LiveProbeSection<T>`.
- Produces hook state `{data, loading, error, retry}`.

- [ ] **Step 1: Write failing hook tests**

Test immediate loading, successful response, failed HTTP response, retry adding `refresh=1`, and polling every 3 seconds while any section is initializing (30 seconds otherwise).

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/hooks/useClusterOverviewLive.test.ts`

Expected: module-not-found failure.

- [ ] **Step 3: Implement types and hook**

Use `fetch("/api/cluster-overview/live?...")` with `cache: "no-store"`, preserve last successful data on request errors, and clear timers on unmount.

- [ ] **Step 4: Verify GREEN**

Run: `cd frontend && npm test -- --run src/hooks/useClusterOverviewLive.test.ts`

Expected: all tests pass.

---

### Task 4: Progressive Overview rendering

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/views/ClusterDashboardView.tsx`
- Modify: `frontend/src/views/ClusterDashboardView.test.tsx`
- Modify: `frontend/src/components/ClusterSqueueOverview.tsx` if its input contract requires explicit loading/error support

**Interfaces:**
- `ClusterDashboardView` receives live data/loading/error/retry separately from base Overview.
- Existing inventory props and two-row stat grid remain unchanged.

- [ ] **Step 1: Write failing view tests**

Cover:

- page header/shell renders with `data=null` and loading base data;
- identity and inventory remain visible while live details initialize;
- environment tiles show KUI Skeleton only for initializing sections;
- stale values remain visible with a warning;
- failed live details show unavailable + retry without hiding inventory;
- queue loading/failure cannot replace Overview content.

- [ ] **Step 2: Verify RED**

Run: `cd frontend && npm test -- --run src/views/ClusterDashboardView.test.tsx`

Expected: full-page skeleton hides shell and no independent live states exist.

- [ ] **Step 3: Implement progressive rendering**

Render `PageHeader` and stable card structure unconditionally. Use active cluster/context values passed through `App` where available; otherwise show section-shaped KUI skeletons. Replace base-response version/queue values with live-section values only when available. Add retry at the affected live-details section; do not reuse inventory retry for probe failures.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
cd frontend
npm test -- --run src/hooks/useClusterOverviewLive.test.ts src/views/ClusterDashboardView.test.tsx
./node_modules/.bin/tsc -b --noEmit
```

Expected: tests and typecheck pass.

---

### Task 5: Full verification and live timing

- [ ] **Step 1: Run backend suites**

```bash
python -m pytest backend/tests/test_cluster_overview_live.py backend/tests/test_cluster_snapshot.py backend/tests/test_app_api.py -v
.venv/bin/ruff check backend/cluster_overview_live.py backend/app.py backend/tests/test_cluster_overview_live.py backend/tests/test_app_api.py
```

- [ ] **Step 2: Run frontend suites**

```bash
cd frontend
npm test -- --run src/hooks/useClusterOverviewLive.test.ts src/views/ClusterDashboardView.test.tsx src/views/SingleRackView.test.tsx
./node_modules/.bin/tsc -b --noEmit
```

- [ ] **Step 3: Rebuild through the supported launcher and time**

Use `./start.sh` (not direct Python/npm build) in the appropriate configured environment. Verify:

- hard refresh paints page shell immediately;
- `/api/cluster-overview` completes under one second even while BCM probes run;
- live sections transition independently;
- stale/failure states preserve inventory.

