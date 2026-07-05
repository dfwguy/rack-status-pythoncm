# MEL01 (Firmus) — NVLink / MNNVL Fabric Fault — Escalation

**Date:** 27 Jun 2026  •  **Cluster:** Firmus MEL01-HC07 (GB300 NVL72)  •  **Owner:** OVA / NVIS validation
**Severity:** ~~High~~ → **RESOLVED (27 Jun, late)** — fabric is healthy; the "hang" was a userspace stack issue, not the fabric. See resolution below.

---

## ✅ RESOLVED (27 Jun 2026) — root cause found, fabric is HEALTHY

The original "broken NVLink fabric" hypothesis below is **DISPROVEN**. After a day of
clean-room testing with isolated userspace stacks, the NVLink/NVLS/MNNVL fabric on the
healthy racks works correctly and hits the expected ~915 GB/s. The earlier hangs were
**two separate non-fabric issues** that happened to look identical in the logs:

**Cause 1 — our hand-built userspace lib mix (the hang).**
The bare stack at `/home/cmsupport/workspace` (hand-assembled CUDA 13 + HPC-X/SPCX libs)
deadlocked at `NVLS importing shareableHandle`. The **exact same `all_reduce_perf` binary**,
pointed at a clean NCCL/CUDA userspace, runs flawlessly. Proven by building isolated
PyTorch venvs (`pip … torch`) on the compute node and running NVLS — it sets up, runs,
and tears down multicast (MC) handles cleanly. It was never the fabric.

**Cause 2 — NCCL 2.30.7 vs 2.29.2 (~5% perf, not a hang).**
With identical env, binary harness, rack, and CPU binding — only the NCCL library changed:

| NCCL | algbw/GPU | busbw @16 GiB (72 GPU, RK0216) |
|---|---|---|
| **2.29.2** | 464.3 | **915.6** ✅ (= team's 917.9 within 0.25%) |
| **2.30.7** | 442.6 | 872.9 |

`NCCL_MNNVL_UUID`, `NCCL_MIN_CTAS`, `NCCL_P2P_LEVEL`, CPU binding, message size — all held
constant; 2.30.7 stayed ~873 regardless. The ~5% gap is a **2.30.7 NVLS all-reduce
regression on GB300**.

**Independent fleet confirmation:** the cluster team (Junli) measured **42 racks at
915–935 GB/s** single-rack with NCCL 2.29.2. Our clean reproduction on RK0216 (915.6)
matches. 709 healthy nodes are reserved `DONT_TOUCH`.

**Genuinely-broken racks are a small, separate set:** RK0106 (`CN[010611-618,628-637]`)
and RK0109 (`CN[010911-918,928-937]`) have a real **NVLS / NVSwitch SDN** fault and need an
**nvswitch SDN factory reset** (now fenced in the `FIXME` reservation). Those are the only
true fabric faults; do not generalize them to the fleet.

### Validation recipe (for the report / handoff)
- **Pin NCCL 2.29.2 + CUDA 13** for single-rack validation (≈915 GB/s); flag 2.30.7 as ~5% slower.
- Use a **clean userspace** (NGC container or a pip `torch` venv) — never the hand-assembled
  `/home/cmsupport/workspace` lib mix.
- The single-rack ~915 number is **pure NVLink + NVLS** (`NCCL_IB_DISABLE=1`); SPCX/RoCE is
  irrelevant intra-rack and only matters for cross-rack.
- busbw scales as `2(n-1)/n` — quote per-GPU **algbw (~464)** for the true NVLink rate; a
  4-GPU run (~685 busbw) and a 72-GPU run (~915 busbw) are both healthy.

_Everything below is the original (now-superseded) escalation, kept for history._

---

## Summary

NCCL collectives **hang** on these racks whenever the NVLink-based transports are
enabled — either **NVLink-SHARP (`NCCL_NVLS_ENABLE=1`)** or **multi-node NVLink
(`NCCL_MNNVL_ENABLE=1`)**. The communicator initializes fine ("Connected all
trees"), then the **first collective kernel deadlocks** and the job times out
(`rc=124`) with **zero data rows**. The collective only completes when **both are
disabled** and traffic falls back to pure ring/tree over **NET/IB (RoCE)**.

Net effect: the NVL72 NVLink fabric is unusable for collectives. Cross-rack
all_reduce is capped at the **RoCE-only ceiling (~84–90 GB/s busbw)** instead of
the expected **600–800+ GB/s** with a healthy NVLink fabric.

## Symptoms

- `all_reduce_perf_mpi` with `NVLS=1` or `MNNVL=1`: communicator builds, prints
  `Connected all trees`, then hangs in the first collective → `timeout`/`rc=124`,
  no benchmark rows emitted.
- Reproduced at **single-rack** (pristine rack), **2-rack**, and **4-rack** scale.
- `NVLS=0 + MNNVL=0` (NET/IB only): runs clean every time.

## Evidence (Slurm jobs, container `nemo-25.11.01.sqsh`, in-tree NET/IB, no spcx)

| Job | Scope | NVLS | MNNVL | Result |
|---|---|---|---|---|
| 5990 / 5998 / 6018 / 6019 / 6042 / 6060 | 1–4 rack | 1 | 1 | **HANG** (rc=124, 0 rows) |
| 6022 | 2-node (1 rack) | 0 | 0 | clean |
| 6062 | 2-rack cross-rack | 0 | 0 | clean |
| 6064 (B4: RK0203/0204/0205/0208) | 4-rack / 288 GPU | 0 | 0 | clean, 5/5 iters, **avg 89.5 @4 GiB / 82.4 @16 GiB GB/s** |

Corroborating drain reasons already on the cluster (independent of NCCL):
- **RK0111, RK0113** — `NVLink P2P all not fully connected`
- **RK0114** — `E/W nics cross rail ping test failed`
- **RK0108** — 4 nodes `down*` / not responding (one, `CN010811`, did not recover after power cycle)

## Impact

- Cross-rack collective bandwidth degraded ~**7–9×** (RoCE-only ~84–90 GB/s vs
  600–800+ GB/s expected on NVL72). Directly hurts large-model training/all-reduce
  throughput and time-to-first-token.
- All round-2 cross-rack batches (B1–B16) will show the same low ceiling until the
  NVLink fabric is repaired — numbers in `26 Jun cross rack round 2.xlsx` should be
  annotated "NVLink fabric disabled (broken) — RoCE-only".

## Requested fabric-team actions

1. **IMEX / fabric service health** — verify `nvidia-imex` (the GB300 fabric
   service; `nvidia-fabricmanager` is expected inactive on these systems) is
   active and the IMEX domain config is correct across all HC07 nodes.
2. **NVSwitch / NVLink link state** — `nvidia-smi nvlink -s`, NVSwitch error
   counters, and the per-rack `NVLink P2P all not fully connected` faults
   (RK0111/0113 already flagged).
3. **MNNVL topology** — confirm the multi-node NVLink topology / partition is
   correctly provisioned for cross-rack; the build succeeds but the kernel
   deadlocks, suggesting a control-plane-OK / data-plane-broken fabric.
4. **Repair + reverify** — once fixed, rerun a single-rack `NVLS=1, MNNVL=1`
   all_reduce; a clean run with busbw climbing past the RoCE ceiling confirms the
   fix. Then re-enable in the harness (`NVLS=1 MNNVL=1`).

## Repro (for the fabric team)

```bash
# on mbcm01 head; single rack, NVLink path ON -> expect HANG (rc=124)
cd /home/cmsupport/workspace
NVLS=1 MNNVL=1 sbatch -J nccldbg -w <one-rack-nodelist> nccldbg.sbatch   # NCCL_DEBUG=INFO
# control: NVLink path OFF -> clean
NVLS=0 MNNVL=0 sbatch -J nccldbg -w <one-rack-nodelist> nccldbg.sbatch
```

`nccldbg.sbatch` sets `NCCL_DEBUG=INFO` / `NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH,ENV,BOOTSTRAP`
— attach the hanging job's log (last lines after "Connected all trees") to the fabric ticket.
