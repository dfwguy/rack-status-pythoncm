# Progressive Dashboard Loading (A + C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After restart/refresh, Overview and Racks paint useful metadata immediately, never block the HTTP request on a full inventory collect, and show clear loading / failed / stale inventory states until the shared snapshot is ready.

**Architecture:** Keep the canonical SQLite `ClusterSnapshot` as the single inventory source for `/api/status` and `/api/cluster-overview`. Change refresh from **blocking-in-request** to **lease + background collector**: API handlers return current snapshot state in under ~1s (`initializing` / `stale` / `failed` / `fresh`). Overview always returns env/catalog metadata; inventory counts stay `null` until a payload exists. Frontend renders the page shell immediately and uses section-level skeletons + existing `ClusterSnapshotState` banners. Also remove the duplicate `squeue` pass in the collector hot path so warm refreshes stay ~one Slurm round-trip.

**Tech Stack:** Flask (`backend/app.py`), `backend/cluster_snapshot.py`, native status (`backend/native_status.py`), React 19 + KUI (`ClusterDashboardView`, `SingleRackView`, hooks), pytest + Vitest.

## Global Constraints

- Inventory counts must never show false zeros while `snapshot.state` is `initializing` or `failed` without a payload — use `null` / unavailable UI.
- Overview and Racks must continue to share the same `snapshot.id` when both have a payload.
- `/api/status?raw=1` remains a diagnostic bypass and must never become Overview’s inventory source.
- Prefer Kaizen UI (`Banner`, `Button`, `Text`, `Skeleton`) over new MUI widgets for loading/error affordances.
- Do not redesign the Overview card grid or Racks table beyond progressive loading states.
- Keep commits conventional (`feat`/`fix`/`test`) if the user asks to commit; do not commit unless asked.

---

## File Map

| File | Responsibility |
|------|----------------|
| `backend/cluster_snapshot.py` | Add non-blocking refresh: acquire lease, spawn collector thread, return immediately |
| `backend/app.py` | Wire `wait=False` for API paths; kick background refresh on first hit; overview metadata without blocking inventory; optional probe skip/cache while initializing |
| `backend/native_status.py` | Short-lived `squeue` cache (or equivalent) so collect does not pay for two full `squeue` calls |
| `backend/tests/test_cluster_snapshot.py` | Async / non-blocking lease + state transition tests |
| `backend/tests/test_app_api.py` | Cold Overview/Status return fast with metadata + `initializing` |
| `frontend/src/views/ClusterDashboardView.tsx` | Shell + metadata always; inventory cards skeleton until ready |
| `frontend/src/views/SingleRackView.tsx` | Shell + banner; table skeleton until ready |
| `frontend/src/components/ClusterSnapshotState.tsx` | Clearer initializing/failed copy (optional polish) |
| `frontend/src/views/ClusterDashboardView.test.tsx` | Progressive render tests |
| `frontend/src/views/SingleRackView.test.tsx` | Progressive render tests |

---

### Task 1: Non-blocking snapshot refresh in `ClusterSnapshotService`

**Files:**
- Modify: `backend/cluster_snapshot.py`
- Test: `backend/tests/test_cluster_snapshot.py`

**Interfaces:**
- Consumes: existing `SnapshotRepository.acquire_refresh_lease` / `commit_success` / `commit_failure`
- Produces: `ClusterSnapshotService.get_cluster_snapshot(..., wait: bool = True)` — when `wait=False` and lease acquired, start collector on a daemon thread and return the leased snapshot immediately (`initializing` or prior `stale`/`fresh` payload)

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_cluster_snapshot.py`:

```python
def test_wait_false_returns_initializing_without_blocking(tmp_path):
    started = threading.Event()
    finish = threading.Event()
    calls = 0

    def collect():
        nonlocal calls
        calls += 1
        started.set()
        assert finish.wait(5)
        return payload()

    snapshots = service(tmp_path, collect)
    first = snapshots.get_cluster_snapshot(
        "cluster-a", "/logs", max_age_seconds=60, wait=False
    )
    assert first.state == "initializing"
    assert first.payload is None
    assert started.wait(2)
    assert calls == 1

    finish.set()
    # Poll until background commit lands
    for _ in range(50):
        done = snapshots.get_cluster_snapshot(
            "cluster-a", "/logs", max_age_seconds=60, wait=False
        )
        if done.state == "fresh" and done.payload is not None:
            break
        Clock().advance(0)  # no-op if using real time; prefer joining via short sleep
        import time as _t
        _t.sleep(0.05)
    assert done.state == "fresh"
    assert done.payload["summary"]["done"] == 1


def test_wait_false_with_stale_payload_returns_stale_immediately(tmp_path):
    clock = Clock()
    good = service(tmp_path, lambda: payload(), clock=clock)
    fresh = good.get_cluster_snapshot("cluster-a", "/logs", max_age_seconds=1)
    assert fresh.state == "fresh"

    clock.advance(10)

    def boom():
        raise RuntimeError("refresh failed")

    failing = ClusterSnapshotService(
        good.repository,
        collector=boom,
        clock=clock,
        lease_seconds=30,
    )
    # Force refresh path with wait=False should still return prior payload quickly
    out = failing.get_cluster_snapshot(
        "cluster-a", "/logs", max_age_seconds=1, force_refresh=True, wait=False
    )
    assert out.payload is not None
    assert out.state in ("fresh", "stale", "initializing")
```

Adjust the second test once implementation is clear: after async failure, a follow-up `wait=False` read should show `stale` + `last_error`. Prefer one precise test for “cold wait=False” and one for “contention returns existing lease without second collector”.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/tests/test_cluster_snapshot.py::test_wait_false_returns_initializing_without_blocking -v`

Expected: FAIL (`wait` unexpected keyword or blocking forever / not initializing)

- [ ] **Step 3: Implement non-blocking refresh**

In `ClusterSnapshotService.get_cluster_snapshot`, add `wait: bool = True`.

When lease is acquired and `wait is False`:

```python
def _run_refresh(key: str) -> None:
    try:
        result = self.collector()
        # same validation as blocking path
        self.repository.commit_success(key, self.owner, result)
    except Exception as error:
        self.repository.commit_failure(key, self.owner, error)

threading.Thread(
    target=_run_refresh,
    args=(leased.snapshot_key,),
    name=f"cluster-snapshot-refresh-{leased.snapshot_key[:8]}",
    daemon=True,
).start()
return leased
```

Keep the existing blocking path when `wait=True` (background loop / tests that need deterministic completion).

Important: if lease was **not** acquired, return `leased` immediately (already the case) — callers poll.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest backend/tests/test_cluster_snapshot.py -v`

Expected: PASS (including new async tests; update any tests that assumed blocking cold start if needed)

- [ ] **Step 5: Commit (only if user asked)**

```bash
git add backend/cluster_snapshot.py backend/tests/test_cluster_snapshot.py
git commit -m "$(cat <<'EOF'
feat: allow non-blocking cluster snapshot refresh

EOF
)"
```

---

### Task 2: Wire APIs to non-blocking refresh + fast Overview shell

**Files:**
- Modify: `backend/app.py` (`get_cluster_snapshot`, `api_status`, `api_cluster_overview`, `build_cluster_overview`, `_rack_status_background_loop`)
- Test: `backend/tests/test_app_api.py`

**Interfaces:**
- Consumes: `ClusterSnapshotService.get_cluster_snapshot(..., wait=False)`
- Produces: `/api/cluster-overview` and `/api/status` return within ~1s on cold start with `snapshot.state == "initializing"`; overview still includes cluster_name / customer / product_line / etc.

- [ ] **Step 1: Write the failing API tests**

In `TestSharedClusterSnapshotAPI` (or a new class), add:

```python
def test_cold_overview_returns_metadata_while_initializing(self, client, monkeypatch):
    gate = threading.Event()

    def slow_collect(log_dir, include_raw=False):
        gate.wait(10)
        return {
            "ok": True,
            "returncode": 0,
            "updated_at": "2026-07-22T15:00:00+00:00",
            "rows": [{"rack": "p5-r01", "nemo": "done", "last_job": "-", "sinfo": "-", "ct_down_unk": "-"}],
            "physical_rf0": {"n1": "10.0.0.1"},
            "summary": {"done": 1, "fail": 0, "ongoing": 0, "pending": 0},
        }

    # Patch the collector used by get_cluster_snapshot / run_rack_status as the suite already does
    ...
    t0 = time.monotonic()
    res = client.get("/api/cluster-overview")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0
    body = res.get_json()
    assert body["ok"] is True
    assert body["snapshot"]["state"] == "initializing"
    assert body["rack_count"] is None
    assert body["tray_hosts"] is None
    assert body["cluster_name"]  # non-empty metadata
    gate.set()
```

Mirror for `/api/status`: fast `initializing`, empty/placeholder rows OK, `snapshot.state == "initializing"`.

- [ ] **Step 2: Run the new API test — expect fail**

Run: `python -m pytest backend/tests/test_app_api.py -k cold_overview_returns_metadata -v`

Expected: FAIL (request blocks until collector finishes, or elapsed >= 2s)

- [ ] **Step 3: Change `get_cluster_snapshot` default for request paths**

```python
def get_cluster_snapshot(
    log_dir: Path,
    *,
    force_refresh: bool = False,
    wait: bool = True,
) -> cluster_snapshot.ClusterSnapshot:
    ...
    return service.get_cluster_snapshot(
        _cluster_snapshot_identity(),
        log_root,
        max_age_seconds=_rack_status_background_interval_sec(),
        force_refresh=force_refresh,
        wait=wait,
    )
```

In `api_status` (non-raw) and `api_cluster_overview`:

```python
snapshot = get_cluster_snapshot(log_dir, force_refresh=force_refresh, wait=False)
```

Background loop keeps `wait=True` (or `force_refresh=True, wait=True`) so the daemon still owns durable refreshes.

Also: on first API hit, ensure `_ensure_rack_status_background_thread()` runs **before** returning, and consider kicking an immediate refresh (existing loop sleeps `interval` first — either remove the initial sleep or call `get_cluster_snapshot(..., force_refresh=True, wait=False)` once at thread start).

- [ ] **Step 4: Keep Overview metadata fast while inventory initializes**

In `build_cluster_overview`, when `current.state == "initializing"` and `current.payload is None`:

- Keep returning env/catalog identity fields (already local).
- Skip live head probes that can take tens of seconds (`_cluster_probe_slurm_version`, `_cluster_probe_bcm_cmsh_version`, `_cluster_squeue_overview_block`) **or** serve last-known probe cache / `"—"`. Preferred: skip probes while `initializing` without payload; run probes once inventory is `fresh`/`stale` (or when payload exists).

Do **not** block Overview on `get_physical_rf0_map()` outside the snapshot collector.

- [ ] **Step 5: Re-run API tests**

Run: `python -m pytest backend/tests/test_app_api.py -k "SharedClusterSnapshot or cold_overview" -v`

Expected: PASS; shared snapshot id tests still pass once collector completes

---

### Task 3: Remove double `squeue` from canonical collect path

**Files:**
- Modify: `backend/app.py` (`_collect_canonical_rack_status` / `_merge_live_native_workers_into_status`) and/or `backend/native_status.py` (`load_squeue_jobs_map`)
- Test: `backend/tests/test_cluster_snapshot.py` or a small native_status unit test

**Why:** Cold/warm collect currently builds the table, then merges live workers which rebuilds the table and runs `squeue` again (~60s timeout each → ~2 min when Slurm is slow).

- [ ] **Step 1: Write a failing test that counts `squeue` invocations**

Prefer monkeypatching `native_status.load_squeue_jobs_map` (or `host_exec.run_slurm_on_host`) during one `_collect_canonical_rack_status` / `build_native_rack_status` + merge cycle and assert it is called once.

- [ ] **Step 2: Implement the smallest fix**

Preferred fix (minimal behavior change):

```python
def _collect_canonical_rack_status(log_dir: Path) -> dict:
    payload = run_rack_status(log_dir, include_raw=False)
    # run_rack_status / build_native_rack_status already produced rows + native_workers.
    # Do not call _merge_live_native_workers_into_status here — that re-runs squeue.
    return payload
```

If live merge is still required for `/api/status?raw=1` or non-snapshot paths, leave it there only.

Alternative: cache `load_squeue_jobs_map` for 5–10s under a lock — useful if merge must stay.

- [ ] **Step 3: Verify tests pass**

Run: `python -m pytest backend/tests/ -k "squeue or SharedClusterSnapshot or cluster_snapshot" -v`

---

### Task 4: Overview UI — shell first, inventory section loading/failed

**Files:**
- Modify: `frontend/src/views/ClusterDashboardView.tsx`
- Modify: `frontend/src/components/ClusterSnapshotState.tsx` (copy polish only if needed)
- Test: `frontend/src/views/ClusterDashboardView.test.tsx`

**Interfaces:**
- Consumes: `ClusterOverviewResponse` with `snapshot` + nullable inventory fields
- Produces: Page header + identity metadata visible during `initializing`; StatTiles show skeletons/`—` until `fresh|stale`; failed banner + retry

- [ ] **Step 1: Write/adjust Vitest coverage**

```tsx
it("shows cluster identity while inventory is initializing", () => {
  renderDashboard({
    snapshot: { state: "initializing", id: null, /* ... */ },
    cluster_name: "boston",
    customer_name: "Acme",
    rack_count: null,
    tray_hosts: null,
    validation_summary: null,
  });
  expect(screen.getByText(/boston/i)).toBeInTheDocument();
  expect(screen.getByText(/Loading cluster inventory/i)).toBeInTheDocument();
  // Stat numbers must not show literal 0 from null inventory
  expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
});

it("shows failed inventory banner with retry", () => {
  const onRetry = vi.fn();
  renderDashboard({ snapshot: { state: "failed", last_error: "boom", ... }, ... }, { onRetry });
  expect(screen.getByText(/unavailable/i)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /retry/i }));
  expect(onRetry).toHaveBeenCalled();
});
```

- [ ] **Step 2: Change loading gate**

Replace full-page `if (loading && !data) return <LoadingSkeleton />` with:

- Full-page skeleton **only** when `loading && !data` (backend unreachable / first byte not yet arrived).
- Once `data` exists (including `initializing`), render shell:
  - `PageHeader`, identity fields, `ClusterSnapshotState`
  - Inventory `StatTile` grid: if `!inventoryReady`, show KUI/`Skeleton` placeholders or `"—"` labels — never coerce `null` → `0`
  - Sections that need inventory (validation breakdown) stay hidden or skeletoned

Keep existing 3s poll while `initializing` in `useClusterOverview`.

- [ ] **Step 3: Run frontend tests**

Run: `cd frontend && npm test -- --run src/views/ClusterDashboardView.test.tsx`

Expected: PASS

---

### Task 5: Racks UI — same progressive pattern

**Files:**
- Modify: `frontend/src/views/SingleRackView.tsx`
- Test: `frontend/src/views/SingleRackView.test.tsx`

- [ ] **Step 1: Add tests for initializing / failed**

Assert:

- `initializing` → banner “Loading cluster inventory…”, no false empty-success table claim
- `failed` → error banner + retry
- `fresh|stale` → table / summary visible (RackTable may stay mocked)

- [ ] **Step 2: Render shell + banner before table**

When `data` exists:

- Always show `ClusterSnapshotState`
- Show `RackTable` / floor / summary only when `snapshot.state` is `fresh` or `stale`
- While `initializing`, show a compact skeleton panel where the table will be (not a blank page)

Keep `useRackStatus` 3s poll for `initializing`.

- [ ] **Step 3: Run tests**

Run: `cd frontend && npm test -- --run src/views/SingleRackView.test.tsx`

---

### Task 6: End-to-end verification

- [ ] **Step 1: Backend suite for touched areas**

Run:

```bash
python -m pytest backend/tests/test_cluster_snapshot.py backend/tests/test_app_api.py -v
```

Expected: PASS

- [ ] **Step 2: Frontend suite for touched views**

Run:

```bash
cd frontend && npm test -- --run src/views/ClusterDashboardView.test.tsx src/views/SingleRackView.test.tsx
```

Expected: PASS

- [ ] **Step 3: Manual cold-start check (SSH/demo as available)**

1. Stop the app; delete or bump snapshot DB generation if needed (`var/cache/cluster_snapshots.sqlite3`).
2. Start with `BUILD_FRONTEND=0 ./start.sh` (or current SSH env).
3. Open Overview: within ~1–2s see cluster identity + “Loading cluster inventory…”.
4. Confirm inventory cards are skeletons/`—`, not zeros.
5. Within collector time, cards populate; Racks table appears with same snapshot once fresh.
6. Simulate collector failure (optional monkeypatch / disconnect): failed banner + Retry works.

Success criteria:

| Metric | Target |
|--------|--------|
| First Overview paint with metadata | ≤ 2s after request |
| False zero inventory counts while initializing | Never |
| Shared snapshot id Overview ↔ Racks after success | Same |
| Warm refresh without double squeue | One squeue per collect |

---

## Out of Scope (explicit)

- Streaming per-rack progressive row growth (option B)
- Changing snapshot keying / multi-project catalog model
- Reworking RF0 / cmsh timeouts themselves (non-blocking refresh makes them background-only)
- Docker/Gunicorn worker count changes

## Risk Notes

- **Daemon thread + process exit:** refresh threads are daemon; acceptable for Gunicorn workers; lease expiry recovers stuck `initializing`.
- **Multiple workers:** SQLite lease already serializes; `wait=False` must not start a second collector when lease held.
- **Stale-while-revalidate:** with prior payload, prefer returning `stale`/`fresh` payload immediately while a background refresh runs (`wait=False` + `force_refresh` or age expiry).
