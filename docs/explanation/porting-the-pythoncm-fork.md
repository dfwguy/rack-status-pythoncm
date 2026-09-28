---
diataxis: explanation
description: What Greenlight took from the PythonCM fork, what it deliberately left behind, what it reshaped on the way in, and the defects the port uncovered on both sides.
---

# Porting the PythonCM fork

A fork of Rack Status was built to run one large BCM cluster that has no Slurm. It worked,
it was in production, and it contained real operational knowledge. This page records what
came back into the product, what did not, and why — because the difference between those two
lists is almost entirely judgement, and a judgement nobody wrote down gets re-litigated by
whoever reads the fork next.

It is written for that reader: someone comparing the fork against `main` and asking "why is
this missing?" Most of the time the answer is deliberate, and most of the time the reason is
that the fork solved the problem for *one site* and the product has to solve it for several.

## The shape of the difference

The fork is a downstream copy that grew features against a live cluster under time pressure.
That produces good code with a predictable bias: the site's facts end up **in the code**,
because that is the fastest correct thing to do when the code serves one site. Some examples
that recur below — a fourteen-entry table mapping datacentre rows to Scalable Units, image
paths rooted at `/cluster/containers`, an acceptance roster named by two environment
variables, a rack id computed from coordinates through a hard-coded hall layout.

None of that is wrong in the fork. All of it is wrong here, because one Greenlight instance
serves several customers' clusters at once and switches between them at runtime. So the
recurring move in this port is **not** "copy the file": it is "find the site fact, move it to
the record that already owns site facts, and keep the mechanism".

That is why the diff looks smaller than the fork and the catalog looks bigger.

## What came across

Fifteen commits, in dependency order. The scheduler work is the spine; everything else hangs
off it.

| Area | What landed |
|---|---|
| **Dependencies** | Shipped-advisory clearance, plus a rule recording that `npm audit` audits the install tree rather than the bundle |
| **Storage** | `SqliteDocRepo` behind the existing `Repo` protocol, with the validation-activity archive moved onto it |
| **Results substrate** | Checkpointed idempotent ingest, a `source` filter, `latest_runs_by_rack`, and an `authoritative` filter in the post-run report |
| **Scheduler surface** | `scheduler.*` actions and provider-neutral URLs; the Slurm-named ones retired |
| **Scheduler backend** | `backend/schedulers/pythoncm.py` plus its head-side helper, `launch_mode` / `execution_user` / `node_results` on the base class, and `GET /api/scheduler-probe` |
| **Script deployment** | Sending the shipped script layers to the head from the setup wizard, over the existing transport |
| **Acceptance campaigns** | A pinned node roster on the `Cluster` record, versioned by content digest |
| **Scalable Units** | `/api/status` carries the SU grouping; the floor view draws it |
| **Evidence** | Collector-snapshot ingest, health-check markers, and a current-state overlay over monotonic acceptance |
| **Hardening** | The bundle refuses to ship a symlink; opt-in host-key verification |
| **Docs** | The site's release and hybrid-architecture audits, as `NSCALE_16K_*` |

## What was reshaped on the way in

These are the cases where the mechanism was right and its inputs were not. Each row is a
place where reading the fork and the product side by side will show different code doing the
same job.

| The fork | Here | Why |
|---|---|---|
| SU map: a 14-entry row-to-SU table in TypeScript | `Cluster.scalable_units`, sent on `/api/status` | Correct for one floor, silently wrong on every other, and invisible to the cluster switcher |
| Campaign: `RACK_MULTIRACK_CAMPAIGN_ID` + a path to a host list | `Cluster.campaigns`, with a content digest as the version | A path on one host is not portable, not editable from the UI, and cannot be reproduced on a second deployment |
| Rack identity from `su` + `rack_number` via a hall layout | Explicit `rack`, or coordinates resolved through that cluster's SU map | Keeps the floor plan in the one record that owns it |
| Container images rooted at `/cluster/containers` | A configurable root; the basename and SHA256 stay code-owned | A mount point is a site fact; an image digest is a security control |
| Script paths off `RACK_REMOTE_BASE_PATH` | `paths._script` / `script_tree` | Two resolvers that agree today diverge the first time one learns something |
| Settings via `os.environ` | `_eget` and the config ladder | A bare read opts out of the per-cluster overlay, so the feature works on one cluster and not another |
| Source names `nscale16k_*` | `external-evidence:*` | Nothing about the mechanism is site-specific |
| Default scheduler flipped to `pythoncm` | Stays `slurm`; opt-in per cluster | Defaulting to a native scheduler on a Slurm cluster yields a queue view containing only our own work, which looks plausible and is wrong |

## What was left behind, and what would change that

### The broker

The largest single omission. The fork runs a long-lived daemon on the BCM head holding one
warm `pythoncm` client behind a Unix socket, because constructing a client costs roughly ten
CPU-seconds on a large controller.

The cost is real and we did not dispute it. We declined the daemon: it is a second service to
keep alive on air-gapped appliances, a second transport beside `Connection`, unauthenticated
apart from filesystem permissions, and the fork ships a systemd timer whose only job is to
restart it when it wedges. Instead the *polling* path — the only place the cost compounds —
answers from the durable task files the work itself writes, and consults CMDaemon on a slow
cadence to catch a job that died.

Full reasoning, including the conditions that would reverse it:
[Why there is no PythonCM broker](why-no-pythoncm-broker.md). The likeliest trigger is
inventory polling moving to `pythoncm`, where no durable-file equivalent exists.

### The watchdog's acting half

The fork's watchdog exists solely to restart the broker. With no broker there is nothing to
restart, and porting it would have meant porting the remedy for a problem we chose not to
create. The *observation* half survives as `backend/scheduler_watch.py`, which records when
the active cluster's scheduler stopped answering. It is not called a watchdog, because one
that cannot act is a monitor.

### The ops daemons

An hourly production audit, a validation-evidence refresh timer, and a post-run refresh cron.
All three are bound to one appliance's paths, container names and rack count — the audit
asserts 224 racks. The useful parts of what they check are already covered by
`bin/verify-deploy.sh` and the in-process background threads.

### The touchless deployment harness

A ~1,000-line skill with its own deploy, rollback, preflight and verify scripts. Its
deploy/rollback state machine duplicates `deploy-app.sh`, `prod-drift.sh` and
`verify-deploy.sh`, which are already wired into `build-docker.sh` and understand this repo's
bind-mount rules. One idea *was* taken: its `safe_extract.py` refuses archive members that are
not plain files, and reading it prompted the symlink check now in `script_deploy` — applied at
build time rather than extraction, so the archive never contains the thing.

### Frontend evidence-library extras

The fork's validation view adds a domain filter, a cluster ETA estimate, and CSV/PDF/JPEG
export over its floor map. The load-bearing part was the SU grouping, because that was the
piece carrying site data in code; the rest is additive polish that can follow if anyone asks
for it.

### `validation_provenance.py`

Not part of the evidence pipeline despite the name — it verifies script bundle SHAs before a
launch. A separate concern, left for a separate decision.

### The migration document

The fork's `PYTHONCM_MIGRATION.md` treats the broker as a production requirement. Importing it
would have shipped a document contradicting our own decision record. The two imported audits
carry a banner saying the decision records win where they disagree.

## What the port uncovered

Worth recording separately, because it is the argument for porting by reimplementation rather
than by copying: reading each mechanism closely enough to rebuild it found defects that
reading it casually would not. Three of the five were in code already on `main`.

- **A stale pass painted a broken rack green, indefinitely.** The fleet view read only the
  newest *pass* per rack, so once a rack had ever been accepted no later evidence could change
  its colour. Ours, live, fixed by the current-state overlay.
- **A comma-joined `target` bypassed a narrow deny.** Node-scoped actions authorize against
  `target`, and a Slurm nodelist names several nodes in one string; judging it whole reopened
  the fan-out bypass that all-of evaluation had closed. Found in a concurrent contribution,
  fixed before it merged.
- **The script bundle would ship a symlink.** `tar` reproduces what it is given, and the head
  extracts into a tree shared by every project on it. Ours, introduced earlier in this same
  port.
- **The per-node wrapper lost its exit status.** Inherited from the fork: a brace group runs
  `exit` in the caller's shell, so any command ending in `exit` terminated the wrapper before
  it recorded anything, and the missing status read as a failure however the work went. A
  subshell fixes it.
- **A settings document went stale invisibly.** Adding two scheduler knobs staled a
  *configuration* reference, which reads as unrelated to backend work. The rule that lists the
  generators had omitted one; it now lists all of them.

## Reading the two trees side by side

If you are comparing a fork file against its counterpart here and they differ, check these in
order before assuming something was missed:

1. **Is the difference a site fact?** Look for it on the `Platform`, `Project` or `Cluster`
   record — that is where most of them went.
2. **Is it a path?** `paths._script`, `script_tree`, or a configurable root with a code-owned
   basename.
3. **Is it a setting?** `_eget`, and it will be in
   [the settings reference](../reference/configuration/settings.md).
4. **Is it the broker, the watchdog, or a timer?** Deliberate; see above.
5. **Is it a name?** `slurm.*` became `scheduler.*`, and `nscale16k_*` became
   `external-evidence:*`.

If none of those explain it, it is probably a genuine gap and worth raising.
