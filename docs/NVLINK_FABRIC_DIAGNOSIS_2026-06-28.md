# MEL01 (Firmus) — NVLink Fabric Hang: Good-vs-Bad Diagnosis + Preflight Gate

**Date:** 28 Jun 2026 • **Cluster:** Firmus MEL01 (GB300 NVL72) • **Owner:** OVA / NVIS validation
**Goal:** With healthy racks now available (AIC weekend run: 54/63 racks > 900 GB/s), diff a
known-good node against a known-bad node to find *what configuration/state difference* makes
NCCL NVLS/MNNVL hang — so we can detect it **before** it happens.

## Method

Captured a full node-side fabric fingerprint (`bin/scripts/nvlink_fabric_fingerprint.sh`) on:

| Role | Node | Source | Drain reason |
|---|---|---|---|
| GOOD | `MEL01-HC07-SVR-CN010611` | `junli_val` validated pool (RK0106, fixed this weekend) | none (idle) |
| BAD  | `MEL01-HC07-SVR-CN010713` | `NVLINK_ISSUE` reservation (RK0107) | `NV_ERR_INVALID_STATE shown in dmesg` |

Plus head-side BCM/SDN: NV switch state and NMX-C/GFM for the good rack (RK0106) vs the bad
rack (RK0109/RK0107). Raw captures: `dist/nccl-logs/fabric-diff/`.

> ## ⚠️ Update (28 Jun, PM) — two *different* failure modes; the original hang was switch-side
>
> The good-vs-bad diff below was run against a **Class-A** node (`CN010713`, dirty GPU channel)
> whose rack had **already been SDN-reset by AIC over the weekend**. That is **not** the failure
> mode of the original validation hang. The operator confirmed the original symptom precisely:
>
> > *"The hang started during cross-rack validation, but I pinpointed that even a simple
> > single-node 2-GPU test would hang if it tried to use NVLink. The only working test was
> > disabling NVLink and forcing 2 GPUs to use IB/RoCE."*
>
> On an NVL72 there is **no direct GPU↔GPU NVLink** — every GPU's 18 NVLink5 ports go up into the
> rack's L1 **NVSwitch trays**, so even a 2-GPU *same-node* collective routes **GPU → NVSwitch →
> GPU**. A 2-GPU NVLink hang that *clears* by forcing RoCE therefore means the **NVSwitch SDN
> routing (programmed by NMX-C / GFM) was unprogrammed or wedged** — a *switch-side* fault. It is
> **invisible to every node-side static read**: `nvidia-smi` Fabric `Completed` proves only that
> the GPU *registered* (not that the switch has a *route*); IMEX `active`/`Domain UP`/`CliqueId`
> prove only the *control plane*; "18 NVLinks up" proves only **PHY link-up**, not SDN routing.
> The original **"0 switches in the NVLink domain"** was the real tell — the GPU's fabric had no
> reachable switch route. AIC's fix matches exactly: `nvswitch_sdn_reset.sh` +
> `nvswitches_configure_nmx_c.sh` (reprogram `MNNVL_TOPOLOGY`) — *at the switch*, which is why
> nothing on the node ever showed it.
>
> **Consequence:** no static check can catch this. The only reliable detector is a **functional
> data-path probe that FORCES the NVLink path under a hard timeout, with RoCE as the control
> leg** (the by-hand test, automated). `nvlink_preflight.sh` step 6 now does exactly this:
> forced-NVLink hang **+** forced-RoCE pass ⇒ **NVSwitch SDN routing dead, node OK → escalate
> FABRIC**; both hang ⇒ node/driver fault. This adds a **Class D** to the taxonomy below.

## Headline result

**There is no node-side or rack-side *fabric configuration* that differs between a healthy
node and a hang-prone (but bootable) node.** Every field we previously suspected is identical,
and the bad node even passes a single-node NVLS collective. The only difference is a **GPU/driver
runtime-state fault in `dmesg`**.

### Field-by-field diff (GOOD `CN010611` vs BAD `CN010713`)

| Field | GOOD | BAD | Discriminates? |
|---|---|---|---|
| GPU fabric `State` | Completed | Completed | no |
| GPU fabric `Status` | Success | Success | no |
| `CliqueId` | 32766 (0x7ffe) | 32766 (0x7ffe) | no — **this was a red herring** |
| `Health Summary` / `Route Unhealthy` | Healthy / False | Healthy / False | no |
| NVLink links @ GPU0 | 18 @ 53.125 GB/s | 18 @ 53.125 GB/s | no |
| NVLink error counters | 0 (FEC background only) | 0 (FEC background only) | no |
| topo (GPU↔GPU) | NV18 all | NV18 all | no |
| `nvswitch_proc_devices` | 0 | 0 | no — **0 is normal** (switches are rack-external) |
| `nvidia-smi` peer type | Switch Connected | Switch Connected | no |
| IMEX service / readiness | active / READY | active / READY | no |
| IMEX `Domain State` | UP (all 18 peers `C`) | UP (all 18 peers `C`) | no |
| IMEX channels | 4 | 4 | no |
| Driver / VBIOS | 580.105.08 / 97.10.4A | 580.105.08 / 97.10.4A | no |
| MNNVL line (NVLS probe) | `state 3 healthMask 0x1aa` | `state 3 healthMask 0x1aa` | no — **healthMask 0x1aa is normal** |
| Single-node NVLS all_reduce | rc=0, 673 GB/s | **rc=0, 672 GB/s** | no (!) |
| **`dmesg` RM/Xid errors** | **none** | **100× `NV_ERR_INVALID_STATE` + 112× `Xid`** | **YES — the only discriminator** |

The bad node's flood is:
```
NVRM: nvAssertOkFailedNoLog: ... NV_ERR_INVALID_STATE ... NVA06F_CTRL_CMD_STOP_CHANNEL @ nv_gpu_ops.c:10957
```
i.e. the driver cannot cleanly stop GPU channels — a **dirty/stuck channel state left by a
prior killed job**, not an NVLink topology fault. It clears with a GPU reset / power-cycle.

### Head-side (BCM/SDN) — also identical

| Check | GOOD rack (RK0106) | BAD rack (RK0109) |
|---|---|---|
| NV switches in BCM | 18/18 `[ UP ]` | 18/18 `[ UP ]` |
| NMX-C (`nv show cluster apps nmx-controller`) | `status ok`, `CONTROL_PLANE_STATE_CONFIGURED`, `state disabled` | identical |

The only switches DOWN cluster-wide are rack **0102** (8 DOWN + 2 INSTALLER_UNREACHABLE); they
do not correspond to the node-level NVLINK_ISSUE drains.

## Why we couldn't "find the NV switch / saw 0 switches" before

- `nvswitch_proc_devices=0` is the **normal** state on these NVL72 compute nodes — the
  NVSwitches live in the rack, not the node; the node only exposes `Peer Type: Switch Connected`
  via `nvidia-smi`. "0 switches" was a misread of the healthy state.
- `CliqueId 32766` / `Default Partition` and `healthMask 0x1aa` are the **healthy single-domain
  defaults** (confirmed identical on validated hardware), not faults.
- The earlier NCCL *hangs* trace to (a) the **hand-built userspace lib mix** (proven in
  `docs/NCCL_NVLS_ROOTCAUSE_2026-06-27.md`) and (b) the per-node runtime faults below — not to a
  missing fabric configuration.

## The real failure taxonomy (from the live NVLINK_ISSUE drain reasons)

| Class | Slurm drain reason | Reachable? | Node-side fabric | Root cause | Fix |
|---|---|---|---|---|---|
| **A** | `NV_ERR_INVALID_STATE shown in dmesg` | yes | looks healthy; single-node NVLS OK | dirty GPU channel state from a killed job | `gpu_reset.sh` / power-cycle |
| **C** | `NV_ERR_NOT_SUPPORTED shown in dmesg` | yes (HC08 set) | (not captured — Travis's rows) | RM runtime-state fault | reset / driver reload |
| **B** | `72 GPU NVLINKs not fully connected` / `Invalid GRES Links (-1,0,0,0)` | **no — "Not responding"** | links not enumerated | true NVLink-down / won't boot | nvswitch SDN reset + power-cycle (AIC) |
| **D** | *(none — passes all health checks)* the **original validation hang** | **yes** | **looks 100% healthy**: Fabric Completed, IMEX UP, 18 NVLinks up, single-node *static* checks all green. **Possible tell:** `Xid 149/154` / `knvlinkSetDegradedMode` in dmesg + `NS/TNS` cells in `topo -p2p n` | **NVSwitch SDN routing unprogrammed/wedged** (NMX-C/GFM) — even 2-GPU NVLink hangs, RoCE works | **fabric**: `nvswitch_sdn_reset` + `nvswitches_configure_nmx_c` (AIC) — *node needs no action* |

**Class D is the dangerous one**: it carries no drain reason and passes every static node check, so
Slurm happily schedules it and the collective then deadlocks. Only the forced-NVLink-vs-RoCE
functional probe (preflight step 6) exposes it.

Key insight: the **genuine NVLink-fabric fault (Class B) takes the node off the network
entirely** — it shows up as "Not responding" and matches AIC's "3 nodes still can't boot into
OS". A bootable node never exhibits a *fabric-config* difference; it only carries a *runtime*
fault (Class A/C) that Slurm's health check catches.

## Detection: `bin/scripts/nvlink_preflight.sh`

Because static fabric fields are not predictive, the gate checks the signals that are:

1. GPUs enumerate (catches Class B unreachable)
2. 18 NVLinks up + GPU0 sees 3 NV peers / valid GRES (catches Class B link-down)
3. GPU fabric State=Completed / Status=Success / Health=Healthy / Route=False
4. IMEX active + READY + Domain UP + 0 disconnected peers
5a. **`dmesg` clean of switch-side link faults: `Xid 149` (`NETIR_LINK_DOWN`) / `Xid 154` /
   `knvlinkSetDegradedMode` / `nvlink down|degraded`** ← the static node-side tell for **Class D/B**:
   link training negotiated a degraded state from a switch-side parameter mismatch. (Unverifiable
   on the current fleet — AIC already reset the broken racks — but this is the signature to gate on.)
5b. **`dmesg` clean of `NV_ERR_INVALID_STATE` / `NV_ERR_NOT_SUPPORTED` / GPU-lost** ← Class A/C discriminator
6. (default ON) **functional data-path gate** ← the **only** Class-D discriminator:
   - **Leg A:** 2-GPU `all_reduce` with the NVLink path **forced** (`NCCL_NVLS_ENABLE=1`,
     `NCCL_P2P_LEVEL=NVL`, `NCCL_SHM_DISABLE=1`, `NCCL_NET_DISABLE=1`, `NCCL_IB_DISABLE=1`) under
     `timeout` — disables every fallback so a dead switch can't be masked.
   - **Leg B (control, only if A fails):** same test forced onto **RoCE** (`NCCL_P2P_DISABLE=1`,
     `NCCL_NET_DISABLE=0`, `NCCL_IB_DISABLE=0`, NVLink off).
   - **A hang + B pass ⇒ NVSwitch SDN routing dead (Class D) → escalate FABRIC**, node is fine.
   - **A hang + B hang ⇒ node/driver fault → `gpu_reset` / power-cycle.**

Validated 28 Jun (live, with the forced-NVLink/RoCE functional probe):

```
CN010611 (good): all PASS, nvlink_datapath forced-NVLink rc=0 572.6 GB/s   -> exit 0  PASS
CN010713 (bad):  every static fabric field IDENTICAL to good + forced-NVLink rc=0 576.2 GB/s
                 dmesg_rm_clean FAIL: 100 NV_ERR_INVALID_STATE + 112 Xid    -> exit 1  FAIL
```

This run reconfirms the headline: the bad (Class-A) node is separated from the good node **only**
by the `dmesg` flood — Fabric/IMEX/NVLink/links/P2P-peers and even the forced-NVLink data path
(576 GB/s) are identical. The P2P matrix flag is `nvidia-smi topo -p2p n` (single dash /
`--p2pstatus`, **not** `--p2p`); with the correct flag it reads all-`OK` on both nodes (so, like
`topo -m`'s `NV18` matrix, it does not discriminate Class A but would catch a routing break
showing `NS/TNS/CNS`). Step 2b now uses `-p2p n`, tolerates space-indented rows, and skips
gracefully only if the option is genuinely absent.

> **Staging gotcha (arch-split `/cm/shared`):** the head node is **x86_64**
> (`/cm/shared/nvtest` → `…-x86_64/nvtest`) but the Grace compute nodes are **aarch64** and read a
> *separate* tree. Stage the script (and the `nv-real13` probe venv) to
> `/cm/shared-ubuntu2404-aarch64/nvtest/` or the compute nodes will silently run a stale copy.

Fleet sweep (fast, skips the probe):
```bash
pdsh -R ssh -w '<nodelist>' 'bash /cm/shared/nvtest/nvlink_preflight.sh --quick' | dshbak -c
```

## Recommendations

1. **Run `nvlink_preflight.sh --quick` across the allocation before any multi-rack NCCL job.**
   A single Class-A/B/C node in the set is enough to hang the whole collective; this drains it
   out first. **But `--quick` cannot catch Class D** (it skips the data-path probe and Class D
   passes every static check). To gate against the original switch-routing hang, run the **full**
   `nvlink_preflight.sh` (with the forced-NVLink/RoCE probe) on each node — it costs a ~10-30s
   2-GPU run per node but is the only thing that exposes a wedged NVSwitch SDN before the job does.
2. **Class A/C nodes:** `gpu_reset.sh` (or power-cycle) then re-check; most return to service
   without hardware action.
3. **Class B nodes:** hand to AIC for nvswitch SDN reset / physical recovery — they will not be
   reachable for in-band checks.
4. Keep using the validated software recipe from `docs/NCCL_NVLS_ROOTCAUSE_2026-06-27.md`
   (NCCL 2.29.2 + clean userspace); never the hand-built `/home/cmsupport/workspace` lib mix.

## Artifacts
- `bin/scripts/nvlink_fabric_fingerprint.sh` — full node fabric capture (+ optional NVLS probe)
- `bin/scripts/nvlink_preflight.sh` — pass/fail readiness gate (this diagnosis encoded)
- `dist/nccl-logs/fabric-diff/fp_good_CN010611.txt`, `fp_bad_CN010713.txt`, `preflight_validation.txt`
