# Cross-Rack NCCL — Round 2 (26 Jun) — Batch Work Plan

Coordination doc for running the NCCL all-reduce sweep across the racks in
`docs/26 Jun cross rack round 2.xlsx`. **Our scope = spreadsheet rows 2–17**
(the first 16 HC07 racks). Travis owns rows 18–41. One agent per batch.

Cluster: **Firmus / MEL01**, head node `ssh mbcm01`. All commands run on the head node.

---

## ⚠️ SUPERSEDED (27 Jun, late) — NVLS/MNNVL are NOT broken

The "fabric is broken, disable NVLS/MNNVL" finding below was **wrong**. Clean-room
testing (27 Jun) proved the NVLS/MNNVL fabric is healthy on the good racks and hits
**~915 GB/s** single-rack. The earlier `rc=124` hangs were the **hand-built
`/home/cmsupport/workspace` userspace lib mix** (deadlock at `NVLS importing
shareableHandle`), not the fabric. With a clean NCCL/CUDA userspace the *same binary*
runs to completion.

**Corrected recipe (use this):**
- **Enable** `NCCL_NVLS_ENABLE=1` (and `NCCL_MNNVL_ENABLE=1` + `NCCL_MNNVL_UUID=0x1969`
  for the cross-node NVLink domain) — they work.
- **Pin NCCL 2.29.2 + CUDA 13** (≈915 GB/s @16 GiB, algbw ~464/GPU). NCCL **2.30.7 is
  ~5% slower** (≈873) — same env, only the version differs.
- Use a **clean userspace** (NGC container or pip `torch` venv), never the hand-assembled
  workspace libs.
- Single-rack ~915 is **pure NVLink** (`NCCL_IB_DISABLE=1`); RoCE/SPCX only matters cross-rack.
- Genuinely-broken racks needing nvswitch SDN factory reset: **RK0106, RK0109** only
  (now in the `FIXME` reservation). 709 healthy nodes are in `DONT_TOUCH`.

Full details + per-version comparison: `docs/MEL01_NVLINK_FABRIC_ESCALATION.md`
(✅ RESOLVED section). Run logs: `dist/nccl-logs/{ours,junli}/`.

_Original (now-superseded) method finding kept below for history._

## CRITICAL method finding (SUPERSEDED — see above)

- **Both `NCCL_NVLS_ENABLE=1` and `NCCL_MNNVL_ENABLE=1` HANG the collective** on
  these racks. With either enabled, the communicator builds ("Connected all
  trees") then the first collective kernel deadlocks (job ends rc=124 timeout,
  zero data rows). Both rely on the NVSwitch/NVLink fabric, which is broken here.
  Confirmed even single-rack on a pristine rack — escalate to the fabric team.
- **The only combo that runs clean is `NCCL_NVLS_ENABLE=0` + `NCCL_MNNVL_ENABLE=0`**
  (pure ring/tree over in-tree **NET/IB** / RoCE). Validated: job 6022 (2-node)
  and 6062 (2-rack cross-rack). **No `spcx` plugin.**
- The harness defaults to `NVLS=0` + `MNNVL=0`. Do **not** re-enable NVLS, MNNVL,
  or the `spcx` plugin until the rack NVLink fabric is fixed.

---

## The harness (already staged)

`/home/cmsupport/workspace/nccl5x.sbatch` — faithful to the optimized
`aihpc_val .../gb300/nemo-nemotron5h_56b-mbridge-fp8.sh` NCCL section, but:
- `NCCL_NVLS_ENABLE=0` + `NCCL_MNNVL_ENABLE=0` (pure ring/tree over NET/IB), no spcx plugin
- container `/home/nvis/nemo-25.11.01.sqsh`, `all_reduce_perf_mpi -dfloat -b 8 -f 2 -g 1 -e 16G --iters 20`
- pre-run cleanup (kills stale GPU holders), **untimed container warm-up**, then
  **5 measured iterations**, 420 s timeout each
- output: `/home/cmsupport/workspace/nccl5x/nccl5x-<job-name>-<jobid>.out`

Env overrides (optional): `ITERS=5` (default), `NVLS=0` (default), `MNNVL=0` (default).

---

## Launch template

```bash
ssh mbcm01
cd /home/cmsupport/workspace
NL=$(scontrol show hostlist '<NODELIST BRACES BELOW>')
sbatch -J nccl5x-<batch> -w "$NL" /home/cmsupport/workspace/nccl5x.sbatch
squeue -u root | grep nccl5x
```

---

## Batches (rows 2–17)

Rack→node mapping: rack `RRRR` → `MEL01-HC07-SVR-CN[RRRR11-RRRR18,RRRR28-RRRR37]` (18 nodes, 72 GPUs).

### 4-rack batches

| Batch | Racks | Status (as of 26 Jun ~14:25 EDT) |
|---|---|---|
| **B1** | 0103,0104,0105,0108 | BLOCKED (on-site): CN010811 powered ON (BMC rf0 10.143.160.127 reachable, Redfish=ON) but host OS hung — REPEAT freeze under NCCL even after power-cycle; no ping/ssh. Escalated to on-site team for hands-on recovery (reseat). Other 71 nodes idle/clean. Hold B1 until CN010811 back. |
| **B2** | 0111,0112,0113,0114 | BLOCKED (hardware) for the official run. **Official B2 = 4 FULL racks = 72 nodes / 288 GPUs** — not recordable until all 4 are clean. Currently only 45/72 clean (0111×1, 0112×18, 0113×9, 0114×17); blocked by 0111×17 + 0113×9 NVLink-P2P drains + CN011432 NIC drain (left drained for fabric team). NOT recorded in spreadsheet. Diagnostic only (job 6065, 45 nodes/180 GPU, NET/IB NVLS=0/MNNVL=0): 5/5 rc=0, peak oop busbw ~185–191 GB/s @1 GiB → confirms NET/IB path is healthy; not an official datapoint. Full B2 needs the drained nodes repaired/undrained. |
| **B3** | 0115,0116,0201,0202 | PARTIAL (hardware, 26 Jun ~14:55 EDT): 0115/0116/0202 = 54/54 nodes fully clean (gpus=4, imex=active, ibup=53). cross-ready no longer holds our nodes (HC08-only now). NEW blocker = RK0201: `BADNODES` resv fences 12 nodes (`CN[020112-020113,020115-020117,020128,020130,020133-020137]`, activates 30 Jun); 6 have RoCE down (ibup=1: 020112,020115,020128,020130,020135,020136 → NET/IB unusable). Bad nodes are idle/none = schedulable now, so a full 72-node submit would hang on them. DECISION: HOLD B3 until RK0201 repaired so it runs as full 4-rack (72-node) batch. 6 nodes (020112,020115,020128,020130,020135,020136) have ALL 16 CX8/RoCE east-west ports DOWN — reported to on-site team 26 Jun ~15:17 EDT. Tracked by `BADNODES` resv. |
| **B4** | 0203,0204,0205,0208 | RUNNING (job 6042, NET/IB). RK0204 was power-cycled & fixed. |

**Nodelists:**
- B1: `MEL01-HC07-SVR-CN[010311-010318,010328-010337,010411-010418,010428-010437,010511-010518,010528-010537,010811-010818,010828-010837]`
- B2: `MEL01-HC07-SVR-CN[011111-011118,011128-011137,011211-011218,011228-011237,011311-011318,011328-011337,011411-011418,011428-011437]`
- B3: `MEL01-HC07-SVR-CN[011511-011518,011528-011537,011611-011618,011628-011637,020111-020118,020128-020137,020211-020218,020228-020237]`
- B4: `MEL01-HC07-SVR-CN[020311-020318,020328-020337,020411-020418,020428-020437,020511-020518,020528-020537,020811-020818,020828-020837]`

### 8-rack batches

| Batch | Racks | Notes |
|---|---|---|
| **B11** | 0103,0104,0105,0108,0111,0112,0113,0114 | = B1+B2 racks. Blocked until B1+B2 racks clean. |
| **B12** | 0115,0116,0201,0202,0203,0204,0205,0208 | = B3+B4 racks. Needs B4 free + B3 reservation handling. |

**Nodelists:**
- B11: `MEL01-HC07-SVR-CN[010311-010318,010328-010337,010411-010418,010428-010437,010511-010518,010528-010537,010811-010818,010828-010837,011111-011118,011128-011137,011211-011218,011228-011237,011311-011318,011328-011337,011411-011418,011428-011437]`
- B12: `MEL01-HC07-SVR-CN[011511-011518,011528-011537,011611-011618,011628-011637,020111-020118,020128-020137,020211-020218,020228-020237,020311-020318,020328-020337,020411-020418,020428-020437,020511-020518,020528-020537,020811-020818,020828-020837]`

### 16-rack batch

| Batch | Racks | Notes |
|---|---|---|
| **B16** | all 16 (0103…0208) | Needs every rack above clean. Run last. |

**Nodelist:** all 16 rack ranges concatenated (B11 + B12 nodelists).

---

## Read & record results

Each `.out` has 5 `ITER i/5` blocks. We report **peak performance per iter** =
the **max out-of-place busbw across the whole size sweep** (NOT the avg, and NOT
the value at a fixed size — busbw rolls off at the largest sizes, so the peak
lands mid-sweep, typically ~1 GiB on NET/IB, not 16 GiB).

Column map (standard nccl-tests all-reduce, 13 cols): out-of-place = `time $6 algbw $7 busbw $8`,
in-place = `time $10 algbw $11 busbw $12`. **busbw is $8 (oop) / $12 (ip)** — `$11` is in-place *algbw*, do not use it.

```bash
F=/home/cmsupport/workspace/nccl5x/nccl5x-<batch>-<jobid>.out
# per-iter PEAK busbw (oop $8, ip $12) + the size where it peaks
awk '
/ITER [0-9]+\/5/ {it=$2; m8=0; m12=0; s8=0; s12=0; next}
/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+float/ {
  if ($8+0>m8){m8=$8; s8=$1}; if ($12+0>m12){m12=$12; s12=$1} }
/ITER [0-9]+ rc=/ {printf "%s peak_oop=%.2f@%s peak_ip=%.2f@%s GB/s\n",it,m8,s8,m12,s12}
' $F
grep 'Avg bus bandwidth' $F                            # avg per iter (context only, not the headline)
grep -E 'ITER [0-9]+/5|rc=' $F                          # iter boundaries + return codes
```

- A good iter ends `rc=0` and prints a full table with `#wrong=0`. `rc=124` = hang (see troubleshooting).
- Record the 5 iterations' **peak out-of-place busbw** into the spreadsheet cells for that batch.

### Where to record — `docs/26 Jun cross rack round 2.xlsx`, sheet **"Validation results"**

**Only record OFFICIAL batches** — every rack in the batch must be FULL and clean.
A 4-rack batch = 72 nodes / **288 GPUs** (18 nodes × 4 racks). Partial / degraded
runs (some racks drained) are diagnostics only — do **not** record them.

Results tables (iterations down the rows, batches across the columns):

| Section | Header row | Iter-1..5 rows | Batch → column |
|---|---|---|---|
| NCCL 4x racks  | row 3  (`G3=Batch 1 … P3=Batch 10`)   | rows **4–8**   (`F4:F8`)  | Batch N → col `G`+(N−1). **B2 → `H4:H8`** |
| NCCL 8x racks  | row 13 (`G13=Batch 11 … K13=Batch 15`) | rows **14–18** (`F14:F18`)| B11→`G14:G18`, B12→`H14:H18`, … |
| NCCL 16x racks | row 24 (`G24=Batch 16, H24=Batch 17`)  | rows **25–29** (`F25:F29`)| B16→`G25:G29` |
| NEMO 4x racks  | row 24 (`K24=Batch 1 … T24=Batch 10`)  | rows **25–29** (`J25:J29`)| Batch N → col `K`+(N−1) |

Write the per-iter **peak out-of-place busbw (GB/s)** value into each iteration cell.

---

## Node recovery procedure (for impaired racks)

GPU "CUDA-capable device(s) is/are busy or unavailable" → power cycle the rack:

```bash
# 1) drain
scontrol update nodename='MEL01-HC07-SVR-CN[RRRR11-RRRR18,RRRR28-RRRR37]' state=drain reason='power-cycle recovery'
# 2) power cycle via BCM (off, wait, on)
CSV=$(scontrol show hostnames 'MEL01-HC07-SVR-CN[RRRR11-RRRR18,RRRR28-RRRR37]' | paste -sd,)
cmsh -c "device; power -n $CSV off"; sleep 25; cmsh -c "device; power -n $CSV on"
# 3) wait ~8-10 min for boot, verify, then resume
scontrol update nodename='MEL01-HC07-SVR-CN[...]' state=resume
```

Verify after boot (GPUs enumerate, IMEX active):
```bash
pdsh -R ssh -w '<nodes>' 'echo $(hostname) gpus=$(nvidia-smi -L|grep -c GPU) imex=$(systemctl is-active nvidia-imex)'
```
Note: `roce_r0-r3` IB ports DOWN is **normal**; the `roce_vf_r0-r3` VFs are what NCCL uses (should be ACTIVE).

---

## Troubleshooting

- **rc=124, "Connected all trees" then no data rows** → MNNVL hang. Ensure harness has `MNNVL=0` (it does). If still hanging, that node set has a deeper fabric fault.
- **CUDA busy/unavailable** → stale GPU holder or bad GPU; harness kills holders; if it recurs on the same node after warm-up, power-cycle that rack.
- **PMIx send failed / node freezes mid-run** → that node is unstable; drain it, power-cycle, retest.
- **launch_failed_requeued_held** → a node hit a Prolog error post-boot; `scontrol update nodename=<node> state=resume`, then `scontrol release <jobid>` or resubmit.

---

## Coordination rules

- **One agent per batch.** Announce the batch + job id you took.
- We own **rows 2–17 (HC07 RK0103–RK0208)**. Do **not** touch HC08 / rows 18–41 (Travis).
- **`cross-ready` reservation** holds 0201/0202 (ours, B3) + HC08 0209/0212 (Travis). Do **not** unilaterally modify it — coordinate before using `--reservation=cross-ready` or adding/removing nodes. A `--reservation` job can only use nodes inside the reservation, so B3 (which also needs 0115/0116) needs the reservation expanded or 0201/0202 released first.
- Drain bad nodes with a clear `reason=` so the fabric team can triage.
- Commits → `mel01-validation` branch.

---

## Current assignment snapshot (26 Jun ~14:25 EDT)

- **B4** — running (job 6042), this session. Will record numbers when done.
- **B1** — BLOCKED on-site (26 Jun ~14:42 EDT). 71/72 nodes idle/clean; CN010811 powered ON (BMC healthy via dedicated rf0 port 10.143.160.127, Redfish=ON) but host OS is hung — repeat freeze under NCCL even after a power-cycle, no ping/ssh. IPMI-over-LAN disabled (GB300 BMC = Redfish only), so remote recovery exhausted. Escalated to on-site team for physical recovery (request submitted to on-site 26 Jun ~14:49 EDT); Slurm drain reason updated accordingly. Hold B1 until CN010811 is recovered and resumed, then run on NET/IB (harness MNNVL=0).
- **B2** — hardware-blocked for the official run (26 Jun ~15:23 EDT). Official B2 = 4 full racks / 288 GPUs; only 45/72 nodes clean (0111×17 + 0113×9 NVLink-P2P drains + CN011432 NIC drain). A 45-node/180-GPU NET/IB diagnostic (job 6065) ran clean (~187 GB/s peak), confirming the path — but it is NOT recorded as B2 (partial ≠ official). Hold B2 for the full 288-GPU run after the drained nodes are repaired/undrained.
- **B3** — picked up (26 Jun ~14:55 EDT). cross-ready blocker is GONE (now HC08-only). Real blocker = RK0201 hardware: 6 nodes RoCE-down + 12 in new `BADNODES` resv. 3 racks (0115/0116/0202, 54 nodes) are fully clean & idle. DECISION: HOLD B3 until RK0201 repaired (run as full 4-rack 72-node batch). 6 RK0201 nodes (020112,020115,020128,020130,020135,020136) have ALL 16 CX8/RoCE east-west ports DOWN — reported to on-site team 26 Jun ~15:17 EDT (tracked by `BADNODES` resv).
- **B11/B12/B16** — run after their constituent 4-rack racks are all clean.
