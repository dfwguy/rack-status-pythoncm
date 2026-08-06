---
description: BCM Slurm "Prolog error" node drains — rogueprocess prejob healthcheck (Boston/BCM GB200 clusters). How to diagnose and clear.
alwaysApply: true
---

# BCM Slurm `Prolog error` drains = rogueprocess prejob healthcheck (don't relearn this)

Validated on Boston (BCM/Bright + Slurm) 2026-07-02, rack `bos01-b06`. Symptom: an undrained rack
**won't stay `idle`** — the moment a job launches, every node flips to `drain` with reason
**`Prolog error`**, and the job goes `launch failed requeued held`. This is almost never a hardware
fault — it's BCM's **`rogueprocess`** pre-job healthcheck flagging our own leftover login sessions.

## Root cause chain

1. `slurm.conf`: `Prolog=/cm/local/apps/cmd/scripts/prolog` (generic BCM dispatcher) +
   `PrologFlags=Alloc`. The dispatcher runs everything in
   `/cm/local/apps/slurm/var/prologs/` in sorted order and **fails the whole prolog on the first
   sub-script that exits non-zero**.
2. First sub-script `01-wlm-pre-job-validation` → `wlm-pre-post-job-validation` →
   `cm-health-check-for-wlm-job` runs the BCM **prejob health checks**.
3. `rogueprocess is FAIL` when it finds **any user process running outside a Slurm job** —
   e.g. a lingering `sshd: cmods@notty` + `systemd --user` + `(sd-pam)` triplet left over from our
   own manual `ssh`/`pdsh`/detached burn-in work. FAIL → prolog rc:1 → node drains + job held.
4. `50-prolog-enroot.sh` (creates enroot dirs) usually passes; it is **not** the culprit.

## Diagnose (exact failing check) — read the node's logs as root

`cmods` is a sudoer on the compute trays (NOPASSWD active as of 2026-07-02; before that it needed a
password). From the head, `ssh <node>` then:

```bash
# what drained it + generic "prolog failed rc:1" (no detail here):
sudo grep -iE 'prolog|error' /var/log/slurmd | tail
# which sub-script failed + its exit code:
sudo grep '|<JID>|' /var/log/slurm-prologs.log
# THE ACTUAL FAILING HEALTHCHECK (names the check + offending PID):
sudo cat /cm/local/apps/slurm/var/pre-post-job-logs/failed-pre-jobs/job.<JID>.log
#   e.g.  rogueprocess is FAIL, /usr/sbin/sshd (cmods)/* <pid> */
#         Pre-job health checks have failed
```

Confirm the rogue is a stale login session (multi-day `ELAPSED`), not a real job:
`sudo ps -u cmods -o pid,ppid,etimes,cmd`.

## Fix

**Short-term (unblock the rack now):** kill the stale `cmods` sessions on every node in the rack,
filtering by elapsed time so the *current* transient ssh session is never touched, then resume.

```bash
# per node, as root: kill cmods procs older than 600s (stale logins), drop linger
victims=$(ps -u cmods -o pid=,etimes= | awk '$2>600 {print $1}'); [ -n "$victims" ] && kill -9 $victims
loginctl disable-linger cmods
# resume must run as root (cmods hits "slurm_update error: Invalid user id" for state changes):
sudo scontrol update nodename="<comma-list>" state=resume reason="cleared rogue cmods sessions"
```

Verify: `sinfo -N -h -o '%N %t' | grep <rack>` → all `idle`, and
`sinfo -R -N -h -o '%N|%E' | grep <rack>` → none.

**Long-term (prevent recurrence):**
- Don't leave orphaned sessions on compute nodes — avoid `nohup`'d `ssh`/ControlPersist masters and
  detached burn-ins that outlive the shell; let sessions close cleanly.
- Ask **AIC/BCM admin** to whitelist `sshd` / `systemd --user` in the BCM `rogueprocess` prejob
  healthcheck so ordinary login sessions don't drain nodes.

## Two more ways a run poisons its own rack (Nscale TX, 2026-08-05)

Same end state — a rack that cannot schedule because it drained itself — but different checks,
and only the first self-heals. All three were seen on one afternoon on `ink50-dh01`.

**A. `rogueprocess` flags orphaned MPI ranks, not just login sessions.** The Boston case above is
stale `sshd`/`systemd --user`. On Nscale TX the failing check named our own leftovers from a
*previous NCCL run*:

```text
rogueprocess is FAIL, .../hpcx-v2.25.1/ompi/bin/orted (cmods),
  .../nccl-2.30.7/nccl-tests/build/all_reduce_perf (cmods), /usr/sbin/sshd (cmods)
```

An `srun`/MPI job that does not reap its ranks poisons the rack for the *next* launch: job N+1
starts, prolog fails, node drains, job goes `launch failed requeued held`. This one **self-heals**
once the orphans exit and the BCM healthcheck re-passes (observed: ~45 min, node auto-resumed).

**B. `Kill task failed` — the self-inflicted deadlock. Does NOT self-heal.** When a step's tasks
miss `UnkillableStepTimeout`, root drains those nodes naming the job that owned them:

```text
ink50-dh01-h15-15  drain  Kill task failed (JobId=127391 StepId=16) [root@...]
```

The trap: the job is **requeued into PENDING while holding the blame for draining the nodes it
needs**, so it waits forever on itself (seen on `j01`, 5 nodes, job 126811). Worse for a two-stage
campaign — Stage 1 NCCL completed `0:0` but its teardown drained two trays, so the Stage 2 NeMo
job submitted 3 minutes later sat in `ReqNodeNotAvail` indefinitely.

**Before resuming, check the node is actually clean** — this is the difference between the two
cases. `IDLE+DRAIN` plus no `cmods` processes and no `D`-state tasks means the drain is stale
bookkeeping and a resume sticks. Resuming while orphans are still present just re-drains on the
next launch:

```bash
ssh <node> 'ps -u cmods -o pid=,etimes=,stat=,cmd= | awk "\$2>300"; ps -eo stat=,cmd= | awk "\$1 ~ /D/"'
scontrol update nodename=ink50-dh01-h15-[15,33] state=resume reason="verified clean"
```

**`ReqNodeNotAvail, May be reserved for other job` is a liar.** It reads like queue contention. In
every case above there was *no* competing job — the cluster had ~1890 nodes idle and nothing
running. The job pinned an exact `ReqNodeList` and needed all N; one drained node blocked it.
Always check `sinfo -N -n <ReqNodeList>` for a non-`idle` member before assuming contention.

## Related gotchas seen alongside this

- **`scontrol update ... state=resume` privileges are per-cluster.** On Boston, `cmods` gets
  `Invalid user id` and node state changes need `sudo scontrol` (root is SlurmUser-privileged).
  On **Nscale TX the same account IS a Slurm operator** and `scontrol update state=resume` works
  directly — while `sudo` on that head node prompts for a password and is unusable non-interactively.
  Try the direct call before assuming you need root.
- **`/raid/data` staging (`run_singlerack.sh`) needs root on the *compute* trays.** A passwordless
  `visudo` edit on `slogin` does NOT help — the sudo runs on the nodes. As of 2026-07-02 NOPASSWD is
  propagated to the b0x compute trays, so `sudo -n` works and `RACK_SUDO_PASSWORD` is no longer
  required. If a node lacks it, staging fails with `sudo: a password is required`.
- Stale held jobs (`launch_failed_requeued_held`) + their `Dependency` followers pile up after these
  drains — `scancel` them to keep the queue clean.
