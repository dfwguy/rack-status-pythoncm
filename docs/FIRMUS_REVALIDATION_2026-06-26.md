# Firmus (MEL01) — NCCL Re-validation Status — 2026-06-26

**Cluster:** FIRMUS / MEL01 · **Platform:** GB300 NVL72 · **Manager:** BCM
**Operator:** R. Marcelino Koike (NVIS OVA) · **Workload owner:** Giovanni
**Test:** `llmb-run microbenchmark_nccl` (all_reduce, fp8), MOP §7.3 — Phase 1
**Head node:** `mbcm01` (10.136.128.9, via `meljump`)

---

## 1. Why we are re-validating

NeMo pretrain **job 2191** (`pretrain_nemotronh_56b_fp8 … gpus144`, 2-rack)
crashed ~2 min into distributed init. Root cause was **not** SPCX — the Spectrum-X
plugin loaded correctly (`Loaded net plugin SPCX (v11)`, `SPCX Ver. 1.3-0`,
`/opt/hpcx/...`). The crash was an **NVLink/MNNVL P2P fabric** error during the
init barrier:

```
transport/p2p.cc:281 NCCL WARN Cuda failure 801 'operation not supported'
transport/p2p.cc:278 NCCL WARN Cuda failure 400 'invalid resource handle'
-> torch.distributed.DistBackendError: NCCL error ... unhandled cuda error
```

This is a GPU/NVLink-fabric (MNNVL/IMEX) health symptom, not a benchmark-config
issue. The re-validation establishes per-rack and scaled health before re-running
the Nemotron workload.

---

## 2. Scope — racks under test

All **8 requested racks validated.** RK0103 was initially excluded (node
`CN010331` down) but was **recovered** and validated in a second sweep (§5.5).

| In test (8) |
|---|
| RK0103, RK0104, RK0105, RK0112, RK0115, RK0208, RK0213, RK0215 |

Rack→node mapping verified from `sinfo` (rack id ≠ node hostname): each rack =
18 nodes (`CN<rack>[11-18,28-37]`) = 72 GPU.

---

## 3. Pre-flight results

### 3.1 Firmware uniformity — PASS
All 143 responding nodes identical (no skew):

| Component | Version |
|---|---|
| GPU driver | `580.105.08` |
| GPU VBIOS | `97.10.4A.00.1A` |
| GPU Inforom | `G548.0301.00.03` |

### 3.2 IMEX domain — PASS (per-rack, as designed)
`nvidia-imex` **active** on every node; GPU fabric `State: P0` (registered).
Each rack is its own 18-node IMEX domain — **correct** for NVL72 (one NVLink
domain per rack; cross-rack is RoCE/SPCX). Within each rack, `nodes_config.cfg`
is consistent (18 nodes).

### 3.3 Node health — RESOLVED
`MEL01-HC07-SVR-CN010331` (RK0103) was `drain*` / not responding (NVLink+ECC Xid,
hung reboot — same family as the 2191 crash). It was **recovered** (hard
power-cycle) and RK0103 returned to 18/18. RK0103 then passed 1/2/4-rack NCCL
(§5.5), so that Xid family is no longer reproducing.

---

## 4. Validation matrix (jobs 2221–2240)

NCCL all_reduce, fp8, `8B–16GiB`, SPCX plugin. Tiers cascade 1→2→4→7→8.
**RK0103 was added later** (jobs 2237–2240, after node `CN010331` recovery); its
results match the rest of the fleet and are **merged into the per-tier tables**
in §5.

| Tier | Jobs | Racks |
|---|---|---|
| 1-rack ×8 | 2221–2227, **2237** | each rack individually (incl. RK0103) |
| 2-rack ×5 | 2228–2231, **2238** | sequential pairs + `0103+0105` |
| 4-rack ×3 | 2232–2233, **2239** | rack groups incl. `0103+0105+0112+0115` |
| 7-rack | 2234 | 7 racks (504 GPU) |
| 8-rack | 2240 | all 8 racks (576 GPU) |

Container: `nvidia+nemo+26.04.00.sqsh` · HPCX 2.26 (`/opt/hpcx`).
Submission log (Sweep A) on head node: `/tmp/nvis_revalidation_20260626_071136/`.

---

## 5. Results & metrics

### 5.1 1-rack (intra-NVL72 NVLink) — ✅ ALL PASS (8/8)

| Job | Rack | Peak busbw @16GiB (GB/s) | Avg busbw (GB/s) | Transport | Result |
|---|---|---|---|---|---|
| 2237 | RK0103 † | 914.1 | 225.4 | SPCX | COMPLETED 0:0 |
| 2221 | RK0104 | 910 | 225.2 | SPCX | COMPLETED 0:0 |
| 2222 | RK0105 | 910 | 225.1 | SPCX | COMPLETED 0:0 |
| 2223 | RK0112 | 898 | 223.9 | SPCX | COMPLETED 0:0 |
| 2224 | RK0115 | 917 | 225.2 | SPCX | COMPLETED 0:0 |
| 2225 | RK0208 | 912 | 224.5 | SPCX | COMPLETED 0:0 |
| 2226 | RK0213 | 914 | 224.9 | SPCX | COMPLETED 0:0 |
| 2227 | RK0215 | 915 | 224.9 | SPCX | COMPLETED 0:0 |

† RK0103 ran later (job 2237, after node `CN010331` recovery) — result matches the
fleet. Peak NVLink all-reduce **~910 GB/s**, tight spread (898–917) across all 8
racks, zero failures. No `p2p.cc 801` recurrence.

### 5.2 2-rack (cross-rack RoCE/SPCX) — ✅ ALL PASS (5/5)

Transport confirmed **SPCX** (not Socket) on every run — cross-rack RoCE
GPUDirect path is healthy, no TCP fallback. **Peak busbw (16 GiB) is the
meaningful metric** (see "Reading the numbers" below); the cross-rack drop from
~910 (1-rack NVLink) to ~800–827 (2-rack RoCE) is expected and matches the
playbook (826 GB/s, job 2183).

| Job | Racks | Peak busbw @16GiB (GB/s) | Avg busbw (GB/s) | Transport | Result |
|---|---|---|---|---|---|
| 2228 | 0104+0105 | 800.9 | 175.9 | SPCX ✅ | COMPLETED 0:0 |
| 2229 | 0112+0115 | 816.0 | 174.7 | SPCX ✅ | COMPLETED 0:0 |
| 2230 | 0208+0213 | 826.5 | 177.5 | SPCX ✅ | COMPLETED 0:0 |
| 2231 | 0215+0104 | 821.8 | 176.6 | SPCX ✅ | COMPLETED 0:0 |
| 2238 | 0103+0105 † | 816.8 | 174.2 | SPCX ✅ | COMPLETED 0:0 |

† RK0103 pairing ran later (after recovery).

> **Reading the numbers.** The benchmark sweeps 8 B → 16 GiB. Small messages are
> latency-bound (near-zero busbw) and pull the *average* down — so **avg busbw is
> not a health metric**, peak (at 16 GiB) is. 1-rack runs pure NVLink5 (~910
> GB/s); 2-rack+ must cross the rack boundary over Spectrum-X/ConnectX-8 RoCE,
> which caps busbw lower (~800–827). A failed/Socket-fallback run shows ~3 GB/s —
> so ~800+ on SPCX is a clear pass. (busbw values are **in-place**; out-of-place
> is within ~1–2%, e.g. 2228 oop 811.)

### 5.3 4-rack (288 GPU) — ✅ ALL PASS (3/3)

| Job | Racks | Peak busbw @16GiB (GB/s) | Avg busbw (GB/s) | Transport | Result |
|---|---|---|---|---|---|
| 2232 | first4 (0104,0105,0112,0115) | 613.7 | 135.1 | SPCX | COMPLETED 0:0 |
| 2233 | last3+first (0208,0213,0215,0104) | 633.4 | 136.8 | SPCX | COMPLETED 0:0 |
| 2239 | 0103+0105+0112+0115 † | 624.1 | 139.0 | SPCX | COMPLETED 0:0 |

† RK0103 group ran later (after recovery).

### 5.4 7-rack (504 GPU) — ✅ PASS

| Job | Racks | Peak busbw @16GiB (GB/s) | Avg busbw (GB/s) | Transport | Result |
|---|---|---|---|---|---|
| 2234 | all 7 (0104,0105,0112,0115,0208,0213,0215) | 608.9 | 126.2 | SPCX | COMPLETED 0:0 |

### 5.5 8-rack full-fabric capstone (576 GPU) — ✅ PASS

All 8 racks (incl. recovered RK0103) in one 576-GPU collective — clean SPCX, no
fabric errors.

| Job | Racks | Peak busbw @16GiB (GB/s) | Avg busbw (GB/s) | Transport | Result |
|---|---|---|---|---|---|
| 2240 | all 8 (0103,0104,0105,0112,0115,0208,0213,0215) | 626.5 | 130.9 | SPCX | COMPLETED 0:0 |

### 5.6 Scaling summary (peak all-reduce busbw, in-place @16GiB)

| Scale | GPUs | Transport | Peak busbw (GB/s) | Notes |
|---|---|---|---|---|
| 1-rack | 72 | NVLink5 (intra-NVL72) | **898–917** | 8 racks incl. RK0103 (914), tight spread |
| 2-rack | 144 | RoCE / SPCX | **800–827** | cross-rack boundary; matches playbook (826) |
| 4-rack | 288 | RoCE / SPCX | **614–633** | in line with playbook (~643) |
| 7-rack | 504 | RoCE / SPCX | **609** | all-7 capstone |
| 8-rack | 576 | RoCE / SPCX | **626** | full 8-rack capstone (2240) |

The 1→2-rack step (NVLink→RoCE) is the expected fabric-boundary drop; at ≥4 racks
busbw settles into a stable **~610–633 GB/s** band (8-rack 626 ≈ 4-rack), i.e. it
does **not** degrade as more racks/cross-rack hops are added — the cross-rack
RoCE fabric scales cleanly to the full 8-rack / 576-GPU domain. Every tier ran on
**SPCX** (no Socket fallback), and **no `p2p.cc 801` / `Cuda failure 400` recurred
at any scale.**

### 5.7 NCCL 2.30.7 perf study + apples-to-apples vs Meta — ✅ +7–8%

All re-validation above ran the container's NCCL **2.29.x** core. To chase the gap
to Meta's reference (**668 GB/s @ 4 GiB** all-reduce), we built **NCCL `v2.30.7-1`**
and **nccl-tests** from source on an aarch64 compute node (CUDA 13.0.2 + HPC-X 2.26)
and ran `all_reduce_perf` **bare-metal** (`srun --mpi=pmix`, no container, no
llmb-run) across a locked 4-rack / 288×GB300 allocation — the same HC08 racks as the
2.29 baseline. Logs confirm `NCCL version 2.30.7+cuda13.0` on all 288 ranks and
cross-rack traffic over `NET/SPCX/GDRDMA` (Spectrum-X plugin loads against 2.30.7;
exports `ncclNet_v5..v11`). Build/binaries: `/home/cmsupport/workspace/nccl-2307/`.

> An earlier attempt to test 2.30.x by swapping the **container image** silently ran
> **2.29.3** — HPC-X 2.26's bundled `libnccl` shadowed it via `LD_LIBRARY_PATH`.
> The from-source bare-metal build is the only path that genuinely loads 2.30.7.

**4-rack (288 GPU) all-reduce busbw, in-place, by message size:**

| Message | NCCL 2.30.7 | NCCL 2.29.x baseline | Δ |
|--------:|------------:|---------------------:|----|
| 1 GiB | 388 GB/s | — | — |
| 2 GiB | 489 GB/s | — | — |
| **4 GiB** | **576 GB/s** | ~532 (job 2276) | **+8%** |
| 8 GiB | 636 GB/s | — | — |
| **16 GiB** | **669 GB/s** | 614–633 (§5.3) | **+6–9%** |

- **2.30.7 is a real, bankable win** (+7–8% at the large sizes that matter), validated
  with the version actually loaded.
- **Apples-to-apples vs Meta (4 GiB):** 2.30.7 = **576** vs Meta **668**. 2.30.7 closes
  part of the gap (532→576); ~14% remains. Note we already hit **668.7 @ 16 GiB** —
  Meta's 4 GiB figure — so the residual is purely the smaller-message regime, exactly
  what in-network reduction (SHARP) targets.

#### SHARP / CollNet A/B (4-rack, 1–4 GiB) — ⚠️ blocked on switch-side provisioning

Per Elessar (SHARP needed to reach Meta's numbers), we A/B'd `NCCL_COLLNET_ENABLE`
on the same allocation:

| Arm | 4 GiB busbw | Avg busbw | Algorithm used |
|-----|------------:|----------:|----------------|
| A — CollNet **OFF** | 580 GB/s | 477.2 | NVLS_TREE (default) |
| B — CollNet **ON** (`=1`) | ~575 GB/s | 474.3 | **NVLS_TREE (fell back)** |

No change — and the NCCL INIT log says why:

```
NET/Plugin: Loaded collnet plugin SPCX (v11)
ncclCollNet_init -> 3
Local CollNet device counts across ranks: min 0 max 0
Rank 0: 0 CollNet devices
```

The flag was honored and the SPCX CollNet plugin initialized, but it found **0
CollNet devices** → NCCL silently reverted to NVLS_TREE. **Switch-side SHARP is not
provisioned on the MEL01 Spectrum-X fabric** (no aggregation managers presenting
CollNet devices to the NICs). No compute-side env flag can enable it — this is a
**fabric/infra action item**. Once SHARP AMs are enabled on the switches, re-run this
exact A/B; the 2.30.7 build + SPCX v11 CollNet plugin are ready and proven.

---

## 6. Acceptance criteria

- **1-rack:** NVLink all-reduce ≳ 800 GB/s peak, run COMPLETED. → **MET (8/8)**
- **2-rack:** SPCX (not Socket), peak ≈826 GB/s, 0 timeouts. → **MET (5/5: 800–827)**
- **4-rack:** SPCX, peak ≈643 GB/s. → **MET (3/3: 614–633)**
- **7-rack:** SPCX capstone. → **MET (609 GB/s)**
- **8-rack:** full-fabric capstone (576 GPU). → **MET (626 GB/s)**
- **No** `p2p.cc 801` / `Cuda failure 400` recurrence at any scale. → **clean (all tiers)**

---

## 7. Next steps

1. **Nemotron pretrain re-run** (the original 2191 workload): metadata recipe now
   supports rack-aligned scales incl. **576 (8-rack)**. Run 2-rack (144) /
   4-rack (288) / 8-rack (576) training and collect step-time → TT1T per MOP §7.4.
   (2-node smoke test 2251 FAILED ~80 s in — under triage before scaling up.)
2. **Adopt NCCL 2.30.7** for the workload containers/runs — +7–8% all-reduce busbw,
   validated bare-metal (build at `/home/cmsupport/workspace/nccl-2307/`). When
   testing via container, ensure 2.30.7 isn't shadowed by HPC-X's bundled `libnccl`
   (put the 2.30.7 lib first on `LD_LIBRARY_PATH`).
3. **Fabric action item — enable switch-side SHARP/CollNet** on the MEL01
   Spectrum-X switches (aggregation managers presenting CollNet devices). This is the
   remaining lever to close the 4 GiB gap to Meta (576→~668). Re-run the §5.7 A/B to
   confirm once provisioned.
4. Hand validated fleet + numbers back to Giovanni / Elessar.

> **Status:** NCCL re-validation **COMPLETE — all tiers green (1/2/4/7/8-rack)
> across all 8 racks** (RK0103 recovered and re-validated). No NVLink-P2P /
> SPCX-fallback issues at any scale; 8-rack/576-GPU fabric confirmed healthy.
> **NCCL 2.30.7** validated bare-metal: +7–8% (576 GB/s @4 GiB, 669 @16 GiB, 4-rack).
> **SHARP A/B inconclusive — blocked on switch-side provisioning** (0 CollNet devices).
> Last updated: 2026-06-26 ~13:00 (head-node time).
