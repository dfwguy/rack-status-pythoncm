# Teams message — request to AIC: add `Contain` to `PrologFlags` in slurm.conf

Paste-ready. Written 2026-08-07 from evidence captured on `ink50-dh01`.

---

**Request: add `Contain` to `PrologFlags` in slurm.conf — intermittent multi-node job failures**

Hi team — we've root-caused an intermittent failure that's been killing multi-node validation jobs,
and the fix is a one-line slurm.conf change. Details below so you can verify it independently.

**Symptom**

Multi-node jobs fail at a random step with:

```
srun: error: task N launch failed: Slurmd could not set up environment for batch job
```

and on the compute node's slurmd log:

```
error: Cannot enable memory in /sys/fs/cgroup/system.slice/slurmstepd.scope/job_<id>/cgroup.subtree_control: No such file or directory
error: common_cgroup_instantiate: unable to create cgroup '.../job_<id>/step_N'
error: unable to instantiate step StepId=<id>.N cgroup
error: proctrack_g_create: No such file or directory
error: job_manager: exiting abnormally: Slurmd could not set up environment for batch job
```

Slurm then reports `NODE FAILURE` and requeues. Example: job 131582 requeued 3× (16:28, 16:31,
16:57), failing at step .2 twice and step .4 once, before finally completing. Each attempt burned a
36-node reservation. We have also seen the same message on unrelated jobs (128340 on h15-29).

Misleading detail worth flagging: slurmctld always blames the **batch host** ("requeue job … due to
failure of node h19-11") even though the actual cgroup error is on a *different* compute node. The
batch host is fine — chasing it is a dead end.

**Root cause**

`PrologFlags = Alloc` — it does not include `Contain`.

Without `Contain`, the `job_<id>` cgroup's lifetime is tied to *steps* rather than to the
allocation. When a step ends and no other step is running on that node, slurmd destroys
`job_<id>`; the next `srun` has to recreate it. If a step launch lands while a teardown is still in
flight, the write to `job_<id>/cgroup.subtree_control` hits ENOENT and the step dies.

**Direct evidence** — sampled the cgroup tree at 5 Hz on two nodes of a live job (131812):

```
j01-14  18:18:18.96  job=[job_131812]  steps=[step_10]
j01-14  18:18:31.71  job=[NONE]        steps=[NONE]      <-- job cgroup destroyed
j01-14  18:18:34.56  job=[job_131812]  steps=[step_11]   <-- recreated 2.85s later
j01-14  18:19:27.47  job=[NONE]        steps=[NONE]
j01-14  18:19:28.29  job=[job_131812]  steps=[step_12]   <-- recreated 0.82s later
j01-14  18:20:20.39  job=[NONE]        steps=[NONE]
j01-14  18:20:22.22  job=[job_131812]  steps=[step_13]   <-- 1.83s
j01-14  18:21:18.14  job=[NONE]        steps=[NONE]
j01-14  18:21:18.55  job=[job_131812]  steps=[step_14]   <-- 0.41s
```

Four destroy/create cycles in 210 s on one node, with recreate windows of **0.41–2.85 s**.

The batch host behaves differently and confirms the mechanism:

```
j01-11 (batch host)  steps=[step_10,step_batch]  -> job cgroup NEVER disappears
j01-14 (compute)     steps=[step_10]             -> job cgroup disappears every gap
```

`step_batch` keeps ≥1 step alive on the batch host, so its job cgroup is never reaped. Compute
nodes have nothing between steps.

Our validation jobs run ~6 steps (NCCL all_reduce / alltoall / all_gather pre-checks, then the
training step, plus container cleanup steps). On 36 nodes that is roughly **180 destroy/create
races per job**. At sub-second windows, intermittent loss is expected — which matches what we see.

**Requested change**

```
# slurm.conf
PrologFlags=Alloc,Contain
```

then `scontrol reconfigure`.

`Contain` makes Slurm create the job container at **allocation** time via the ProcTrack plugin and
keep it for the job's lifetime, with steps created inside it. No step ever has to create
`job_<id>`, so the race window disappears entirely. This is the documented purpose of the flag and
is standard on multi-node clusters.

**Impact / risk**

- Low risk: `Contain` is additive and widely used. It does not change scheduling, limits, or
  accounting.
- It does mean the job container exists for the whole allocation, which is the intended behaviour
  and slightly *improves* stray-process cleanup.
- Current cgroup.conf is permissive (`ConstrainCores=no`, `ConstrainRAMSpace=no`,
  `ConstrainDevices=yes`), so no new constraints are introduced by this change.
- Requires a `scontrol reconfigure`; no node drain or job disruption needed.

**How to verify after the change**

Re-run the sampler on any compute node during a multi-step job — `job_<id>` should now persist for
the whole job instead of disappearing between steps:

```bash
while true; do
  printf '%s %s\n' "$(date +%T.%2N)" \
    "$(ls -d /sys/fs/cgroup/system.slice/slurmstepd.scope/job_*/ 2>/dev/null | xargs -r -n1 basename | tr '\n' ',')"
  sleep 0.2
done
```

Happy to run that verification with you once it's applied, or to provide the full slurmd logs from
the failing jobs (131582, 128340) if useful.

Thanks,
Rafael
