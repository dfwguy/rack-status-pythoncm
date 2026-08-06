# Bring-up automation (`auto-action-trigger`) — the Rack Status integration contract

The site's own validation framework, written by Travis, runs continuously on Nscale TX and is the
**authoritative** source of per-node hardware and benchmark verdicts there. Rack Status surfaces it
rather than re-running it.

**This document is deliberately not a copy of his documentation.** His tree carries 38 files of
Diátaxis-structured docs that are actively maintained; duplicating them here would fork a live
document and start drifting the same day. What is captured here is the part that is *ours*: the
contract we depend on, the configuration that changes what our UI shows, and the failure modes we
have actually hit. For how the framework itself works, read his docs — the map is at the bottom.

Verified on Nscale TX 2026-08-06. Applies to any BCM/NVL72 site running the same framework; the
paths below are that site's.

---

## 1. Where it lives

| | |
|---|---|
| Framework root | `/home/cmods/bringup/` on the **head node** (`cmods@192.168.10.2`, reachable only via `nscale-tx-ova`) |
| Project name | `auto-action-trigger` |
| Distribution | NFS-exported from the head to every compute node; upgraded by **tarball drop**, not by pull |
| Version control | **None on the deployment** — see the warning below |

> **The checkout is not the deployment.** His `explanation/architecture.md` is explicit that a git
> checkout is a *source* of the tree and never the tree itself, and that nothing in the repo updates
> the deployment. So an unversioned `/home/cmods/bringup` is by design.
>
> **What is not by design:** there is no `.git`, no remote, and no upstream URL anywhere in the tree,
> so on this host the source and the deployment are the same unversioned copy — a 58 KB sequencer and
> 436 KB of documentation with no backup. Tracked as a follow-up with Travis; until there is an
> upstream, reference his docs by path and expect them to move.

## 2. Architecture — the trigger chain

Three BCM monitoring triggers fire **while** a node's `devicestatus == UP` (`duringactions`, not
`enteractions`), so each re-evaluates every monitoring cycle rather than once on the transition.
Every gate can end a cycle with a plain `exit 0`; most cycles have nothing to do.

```
Node boots → BCM device status = UP
    ├── NodeHealthTrigger → actions/node_health.sh
    │       38 checks against a known-good baseline.
    │       Sets the node IDLE (all pass) or DOWN with a reason (any fail).
    │
    ├── FileCopyTrigger  → actions/file_copy.sh
    │       Rsyncs containers/ to node-local /tmp so job start reads the image off local disk.
    │
    └── LaunchJobTrigger → actions/launch_job.sh
            Sequences the BENCHMARKS list, submits the next single-node Slurm job when the node
            is idle and the prior run passed, drains on failure or overrun.
```

`launch_job.sh` is what produces the `bringup-<node>` jobs seen in `squeue` — one node each,
`--exclusive`, submitted as `JOB_USER` (`cmods`).

**Silence is the enemy** is the framework's central design rule, and it is why integration is
practical at all: every gate writes one line per cycle to `results/<node>/trigger.log` explaining
what it did or why it stopped. An `exit 0` with no trace line is considered a bug there.

## 3. BCM registration — the exact objects

Registration is *not* manual; it is scripted. Run from the framework root by someone with BCM admin
rights in `cmsh`:

```bash
bash pre-post/create-action-trigger.sh     # register (safe to re-run: removes then adds)
bash pre-post/delete-action-trigger.sh     # unregister
```

It creates six objects, in two singular-named cmsh modes:

| cmsh mode | Objects |
|---|---|
| `monitoring action` | `node_health_action`, `file_copy_action`, `launch_job_action` — type `script`, `runon node`, `timeout 2m`, `nodeenvironment yes` |
| `monitoring trigger` | `NodeHealthTrigger`, `FileCopyTrigger`, `LaunchJobTrigger` — `severity 10`, `duringactions <action>`, expression `devicestatus == UP` |

The action scripts are registered by **absolute path under `BRINGUP_HOME`** (default
`/home/cmods/bringup`), not relative to the registration script.

Three traps, all of which cost us time:

- **The cmsh modes are singular.** `monitoring action` and `monitoring trigger`. `monitoring setup;
  actions` does not exist, and it fails by printing a large unrelated listing that reads like
  success.
- **`cmsh` exits 0 even when a `set` inside a `-c` string was rejected.** It reports on stdout and
  commits anyway. His wrapper greps the output for `error:` / `Unable to find` / `No actions were
  set` precisely because exit status is worthless here. Do the same in anything we build.
- **A registration that fails quietly is how you end up believing a node is being health-checked
  when it is not.** Always verify (§6).

## 4. Requirements

From his README, and worth knowing before blaming the integration:

- The tree must be deployed at `/home/cmods/bringup/`; the `cmods` home is the deployment root.
- `cmods` needs write access to `logs/`, `results/`, and `reports/`.
- Registration needs `cmsh` **BCM admin** access. The read-only reporting CLIs do not — they need
  only Slurm CLI access and read access to `results/`.
- Slurm CLI on `PATH` with a working `SLURM_CONF`. On Nscale TX that is
  `/cm/shared-ubuntu2404-x86_64/apps/slurm/etc/slurm/slurm.conf`; bare `sinfo` over SSH fails with
  `resolve_ctls_from_dns_srv: res_nsearch error` without it.

## 5. Settings that change what Rack Status shows

`SETTINGS` is the site's file and is **git-ignored on purpose**; `SETTINGS.default` is the shipped
copy and is replaced by every tarball drop. They are **not layered** — the site's `SETTINGS` is the
whole answer, with unset keys defaulted inside `settings.sh`. Editing `SETTINGS.default` in place
works until the next upgrade silently reverts it.

Every yes/no key is read by `setting_enabled`: `yes`, `true`, `1`, `on` are affirmative; anything
else — including a typo, including empty — is not.

The subset that determines whether our dashboard has anything to show:

| Key | Why we care |
|---|---|
| `PAUSE_JOBS` | `yes` stops benchmark submission. No new `bringup-*` jobs, no new result files — our Nodes view goes stale with no error anywhere |
| `PAUSE_HEALTH_CHECKS` | `yes` stops the 38-check pass; node health stops being refreshed |
| `PAUSE_CONTAINER_COPY` | `yes` stops image staging; jobs may then fail at container start |
| `PAUSE_RECOVERY` | Gates the recovery behaviour |
| `BENCHMARKS` | **The column set.** Our metric table mirrors this list — a benchmark added here appears as a new column |
| `JOBS` | Platform set (`nvl72`), selecting `jobs/<set>/` and `parsers/<set>/` |
| `DRAIN_AFTER_FAILED_HEALTH_CHECK`<br>`DRAIN_AFTER_FAILED_BENCHMARK` | Whether a failure drains the node. With both `no`, a failing node keeps testing |
| `RECOVER_SLURM_DRAINS` + `RECOVER_REASONS` | **The framework undrains nodes by drain reason.** See below |
| `MAX_QUEUED_JOBS`, `MAX_JOB_HOURS`, `DRAIN_WINDOW_MINUTES` | Throughput and overrun limits — they explain queue depth |
| `JOB_USER`, `JOB_ENV` | Submitting user and env injected into jobs |

### `RECOVER_REASONS` — do not race it

`RECOVER_SLURM_DRAINS=yes` makes the framework undrain nodes whose Slurm drain reason matches a
regex list. As configured on Nscale TX that list includes:

```
prolog|epilog|rogue ?process|batch job complete failure|Kill task failed|
node unexpectedly rebooted|node was stopped|not responding|duplicate jobid|
gres/gpu count|low socket|low cpus
```

This is the explanation for drained nodes that recover on their own with nobody touching them —
observed 2026-08-05, a node drained with `Prolog error` auto-resuming after ~45 minutes. Before
manually resuming anything, check whether the reason is on this list and whether recovery is
enabled; otherwise two actors are fighting over node state. It also reads
`/cm/local/apps/slurm/var/pre-post-job-logs/failed-pre-jobs` with a `RECOVER_PREJOB_SLACK_SECS`
grace period.

## 6. Verifying it is actually running

The framework being installed says nothing about it being *active*. Both halves must hold, and each
can be off independently:

```bash
# 1. Is it registered in BCM? (empty output = NOT registered, nothing will ever fire)
cmsh -c "monitoring action;  list" | grep -E 'node_health|file_copy|launch_job'
cmsh -c "monitoring trigger; list" | grep -E 'NodeHealth|FileCopy|LaunchJob'

# 2. Is it un-paused?
grep -E '^PAUSE_' /home/cmods/bringup/SETTINGS

# 3. Is it acting? (a fresh mtime is the only proof)
ls -lt /home/cmods/bringup/results | head -3
bash trigger-log.sh                     # what it decided, fleet-wide
bash trigger-debug.sh <rack>            # walk BCM → sbatch, name the broken link
```

Observed 2026-08-06: the three actions and three triggers were **absent** from `cmsh` while
`PAUSE_JOBS=yes`, `PAUSE_CONTAINER_COPY=yes` and `PAUSE_RECOVERY=yes` — the framework was fully
stood down, with the newest `results/` write ~11 h old. Nothing in Rack Status reports this, which is
the gap worth closing: our Nodes view cannot distinguish "framework off" from "nodes all passing".

## 7. What Rack Status consumes

`backend/bringup.py`, exposed as `/api/bringup/{config,node,fleet}`.

**We shell out to his reporting CLIs instead of reading his artifacts and judging them ourselves,**
because the pass/fail floors live in `parsers/<set>/verdict.sh` and are tuned over time. A verdict we
compute is a verdict that silently disagrees with his after the next tuning.

| We call | We get | Note |
|---|---|---|
| `daily-report.sh -c` | Cluster CSV, `node,slurm,<job columns…>`. Each cell is the extracted **metric** when the run produced one, otherwise a **status word** (`NOT_RUN`/`INCOMPLETE`/`RUNNING`) | Does **not** encode pass/fail — *a FAIL keeps its number.* Never colour a cell from the metric alone |
| `status-check.sh <node>` | Authoritative per-job **status + reason** (`DONE`/`FAIL`/`RETRY`/`INCOMPLETE`/`RUNNING`/`NOT_RUN`) | Scoped to one node, so it is cheap |

`node_summary()` merges them: status and reason from `status-check`, metric from `daily-report`. The
cluster-wide call is cached (`RACK_BRINGUP_CACHE_TTL_SEC`, default 30 s) because it scans every result
directory and re-judges each one on every invocation.

Our env surface:

| Variable | Meaning |
|---|---|
| `RACK_BRINGUP_HOME` | Framework root, default `/home/cmods/bringup` |
| `RACK_BRINGUP_DISABLED` | Truthy disables the integration entirely |
| `RACK_BRINGUP_CACHE_TTL_SEC` | Fleet-report cache, default `30` |
| `RACK_STATUS_ALLOWED_PREFIXES` | Must include `/home/cmods/bringup/results` or the UI cannot open a job log — once set it is the *only* read gate |

Artifacts we read or link to, per node under `results/<node>/`: `<job>.txt` (job output, written
directly by `sbatch --output`, so a `RUNNING` cell is a file being appended to **right now**),
`.fail<N>.txt` archives of prior attempts, `quick-view.txt`, and `trigger.log`. Fleet CSVs land in
`reports/`.

## 8. His documentation — where to look

At `/home/cmods/bringup/docs/`, [Diátaxis](https://diataxis.fr/)-organised. Start at
`docs/README.md`; `CLAUDE.md` at the root is his brief for agents working in that tree.

| Question | Read |
|---|---|
| How do the pieces fit? | `explanation/architecture.md` |
| Why did it pick *that* job next? | `explanation/sequencing.md` |
| Who decides pass/fail, and what is cached? | `explanation/verdicts-and-the-cache.md`, `explanation/judging-benchmarks.md` |
| Why did a node drain / undrain itself? | `explanation/drain-recovery.md`, `how-to/manage-drains.md` |
| Every settings key | `reference/settings.md` |
| Every job and health check | `reference/jobs.md`, `reference/health-checks.md` |
| What a `trigger.log` line means | `reference/trace-lines.md`, `reference/reasons.md` |
| Nothing is running — why? | `how-to/diagnose-nothing-running.md` |
| A node failed — why? | `how-to/diagnose-a-failed-node.md` |
| NCCL falls back to `bond0` | `how-to/diagnose-ib-visibility.md` (and `ib-visibility.sh`) |
| Turn it off for maintenance | `how-to/pause-and-resume.md` |
| Add or enable a benchmark | `how-to/add-a-benchmark.md`, `how-to/enable-a-benchmark.md` |
| First time using it | `tutorials/first-bringup.md`, `tutorials/understanding-the-triggers.md` |

## 9. Integration principles this reinforced

1. **Integrate, don't duplicate.** The site already had authoritative validation; the win was
   surfacing it. This applies to its *documentation* as much as its verdicts.
2. **Shell out for verdicts.** Re-implementing pass/fail guarantees eventual disagreement.
3. **A metric is not a verdict.** `daily-report.sh` keeps the number on a FAIL.
4. **Absence of results is not health.** Registration and pause state are independent of each other
   and invisible in `sinfo`; treat "no data" as unknown, never as passing.

---

Related: [`NSCALE_TX_DEPLOYMENT.md`](NSCALE_TX_DEPLOYMENT.md) §5 (site context),
[`.cursor/rules/bcm-prolog-rogueprocess.mdc`](../.cursor/rules/bcm-prolog-rogueprocess.mdc)
(prolog drains and the self-poisoning failure modes).
