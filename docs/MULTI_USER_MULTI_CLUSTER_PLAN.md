# Multi-user, multi-cluster: what blocks it and the order to fix it

**Status:** analysis complete, no code written. Investigated 2026-08-18 during the `app.py` route
refactor. Nothing here is urgent for a single-operator deployment; all of it is required before one
backend can serve several operators driving *different* clusters at once.

## The goal

One server, several operators. Each operator's requests carry the cluster **they** are working on, so
two people can drive Nscale TX and Boston from the same backend without interfering. Today the
product serves one cluster at a time per process.

## Why it does not work today

### 1. Selecting a cluster is a durable, global write

`catalog/context.set_active()` ends with `repo.set_meta("active", sel)` — it persists the selection to
the catalog store. So it is not just process-global, it survives a restart. If two operators share a
backend and one switches to Boston, the other's next request resolves Boston too, silently.

There is no per-user notion of "my cluster" anywhere in the backend.

### 2. The config ladder cannot be told which cluster

```python
def overlays_for_active() -> tuple[dict[str, str], dict[str, str]]:
```

It takes no arguments and caches a single `(high, low)` pair keyed only by the store generation. Every
`_eget` call therefore reads the one global selection. There is no way to ask "what is
`RACK_SSH_HOST` *for cluster X*" through the normal config path.

### 3. The hard blocker: the overlay is materialised into `os.environ`

`app._apply_catalog_active_to_environ()` writes the active cluster's overlay into the process
environment. Its own docstring explains why it has to: `nvis_id_templates`, `host_exec` SSH
forwarding, `native_status` and the workflow steps all read `os.environ` directly.

`os.environ` is process-wide. Two concurrent requests for different clusters therefore **cannot both
be correct** — one of them would SSH to the other's head node. This is why the problem cannot be
solved at the API layer alone, and it is the expensive part of the work.

### 4. There is no user identity to attach state to

The Flask session holds exactly one key:

```python
session["rack_web_auth"] = "1"
```

No user, no cluster. In token mode every operator shares one token, so the backend cannot currently
tell two operators apart. Per-user cluster state is therefore blocked on real authentication, which is
the work already queued after this refactor.

## What already works

Worth knowing, because it is more than nothing — the catalog layer is genuinely multi-cluster
capable, and the transport layer is close:

- `catalog.context.resolve(project_id, cluster_id)` accepts explicit ids. The resolution primitive
  exists and is used: `build_ai_context_block(cluster_id=...)`, `GET /api/catalog/tools?cluster=`,
  and the AI chat route already passes a per-request `context.cluster`.
- `host_exec` ControlMaster sockets are keyed per target, so connection multiplexing does not assume a
  single cluster.

So the gap is specifically the **config ladder** and the **environment materialisation**, not the
catalog model and not the transport.

## Order of work

Each step is useful on its own, and each is a prerequisite for the next.

1. **Real per-user identity.** Part of the authentication/authorization work. Token mode gives none,
   so nothing per-user can be stored until an operator is distinguishable. Blocks everything below.

2. **Cluster in the session.** Store the operator's selected project/cluster in their session and stop
   using `set_active()` as the per-operator switch. `set_active` becomes the *default* for new
   sessions rather than the global truth. Keep the store's `active` as that default.

3. **Thread the resolved cluster through config resolution.** Give `_eget` an explicit cluster, either
   as a parameter or via a `contextvars.ContextVar` set once per request. A ContextVar is the smaller
   change: `_eget`'s ~80 call sites keep their signature, and async/threaded workers inherit the
   value. Replace the single `_overlay_cache` with a per-cluster cache keyed by
   `(store generation, project, cluster)`.

4. **Remove the `os.environ` materialisation.** The expensive step, and the one that actually unlocks
   concurrency. Pass an explicit resolved context into the direct readers — `nvis_id_templates`,
   `host_exec` env forwarding, `native_status`, workflow steps — instead of publishing to the process
   environment. Until this is done, steps 2 and 3 make the *config* per-request while the *execution*
   stays global, which is worse than today because it looks correct.

5. **Per-request context object.** Once the above holds, a resolved `ClusterContext` (project,
   cluster, platform, overlays, naming) constructed once per request and passed down. This is the
   class-per-cluster idea, and it also fixes a real bug that exists today: a single request can
   straddle a cluster switch, so two `_eget` calls in one request can resolve against different
   clusters.

## Two hazards to design against

**Shared mutable overlays.** `overlays_for_active()` returns the cached dicts themselves. A caller
that mutates one corrupts the cache for every other caller. Whatever replaces it should hand out
copies, or immutable mappings. The same class of bug is already documented for the job registries
("mutate, never rebind") and it has bitten this codebase more than once.

**A half-migration is worse than none.** Per-request config with process-global execution produces a
UI that reports one cluster's data while commands run against another. If steps 3 and 4 cannot land
together, gate the whole thing behind a flag that keeps single-cluster behaviour as the default.

## Not required for this

Single-operator deployments (Mayo, Firmus, Boston, Nscale TX as they run today) are unaffected. The
current model — one active cluster per process, switched from the UI — is correct for them and should
keep working unchanged after any of the above.
