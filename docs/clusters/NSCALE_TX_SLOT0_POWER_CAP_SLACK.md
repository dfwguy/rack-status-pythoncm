Slack-formatted update (uses Slack's native *bold* / `code` syntax, not markdown — paste as-is).
Source report: docs/NSCALE_TX_GB300_SLOT0_POWER_CAP.md

---

:rotating_light: *Nscale TX (GB300) — 54 trays running half their GPUs at 50% power, and we can't clear it*

*SCOPE UPDATE 31 Jul — it is 54 trays, not 36.* Re-swept the cluster, which has grown from 543 to 864 nodes. Of *807 reachable trays, 54 (6.7%) are capped* — all 36 originally reported are still capped, plus *18 new ones, 16 of them in the newly commissioned `j` racks*. That means the defect is *reproducing on new capacity*, not degrading over time. 57 nodes are `down*`/`drain*` and could not be checked, so 54 is a floor. That is *108 GPUs at half TGP ≈ 75.7 kW* of provisioned capacity unavailable. Signature is perfectly uniform — no tray anywhere shows 699 W in any other GPU position. Tags and node list: `NSCALE_TX_CAPPED_SERVICE_TAGS.md`.

*THE PROBLEM*

36 of 543 reachable compute trays have their two slot-0 GPUs (`gpu0`/`gpu1`) capped at *699 W instead of 1400 W*. The slot-1 pair (`gpu2`/`gpu3`) is fine at 1400 W. Exactly the 36 nodes Travis flagged — a cluster-wide sweep confirms no more, no less, and it is always the slot-0 pair.

Impact is real, not cosmetic. Seven days of DCGM telemetry shows the capped GPUs never exceeding *709 W* while their siblings in the same tray, running the same jobs, peak at *~1390 W*. Any multi-node job that includes one of these trays gets paced by it — roughly two racks' worth of degraded capacity.

*WHAT WE TESTED*

Ruled out with measurements, not inference:

• *Not PSU / PDU* — all 19 racks containing capped trays also contain healthy trays on the same busbar. A dropped phase can't cap 1-4 trays while 14-17 neighbours run full.
• *Not power delivery* — GPU core voltages are identical (0.900 / 0.895 / 0.900 / 0.890 V) and the lowest belongs to an *uncapped* GPU. Per-module power within 4 W. Module budgets untouched at the factory 2900 W.
• *Not thermal* — capped GPUs peak at 44 C, about 20 C cooler than healthy GPUs at full load. Temperature-per-watt is normal, so cold-plate contact and torque are fine.
• *Not a module batch defect* — capped and healthy trays carry slot-0 modules with adjacent serial numbers.
• *Not firmware version* — identical across all 17 Redfish inventory components (BIOS, BMC, FPGA, AcceleratorBaseboard, ERoT...).
• *Not a hardware fault under load* — 120 s tensor load logged at 100 ms: no `hw_power_brake`, no `hw_slowdown`, power rock-steady at 16.5 W standard deviation.
• *Nothing in any log* — host kernel, iDRAC SEL, and the full Lifecycle Controller log contain zero power/voltage/rail events.

Root cause is a *stale out-of-band setpoint*: `GPU_0`/`GPU_1` report `PowerLimitWatts.SetPoint = 699` against `DefaultSetPoint = 1400`. A written configuration value, not a hardware limit.

*WHAT WE DID TO FIX IT*

NVIDIA exposes a purpose-built action for exactly this case, plus a documented PATCH:

```
POST  .../Processors/GPU_0/EnvironmentMetrics/Actions/Oem/NvidiaEnvironmentMetrics.ClearOOBSetPoint
PATCH /redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_0/EnvironmentMetrics
      {"PowerLimitWatts": {"SetPoint": 1400}}
```

We tried every documented route: per-GPU clear, module-level clear, chassis-level clear, direct PATCH at all three levels, basic auth, Redfish session token, `If-Match` with the resource ETag, and `OData-Version` headers — in every combination.

*WHAT HAPPENED, AND SHOULDN'T*

Every write returns *HTTP 500* (`Base.1.18.InternalError` / `IDRAC.2.15.SYS494`). Reads work perfectly.

The proof it isn't us — a PATCH carrying an *empty JSON object* `{}`, which by definition changes nothing:

```
HGX resource   PATCH {}  ->  HTTP 500   Base.1.18.InternalError
Dell resource  PATCH {}  ->  HTTP 200   Base.1.18.Success
```

Same controller, same session, seconds apart. An empty payload has no properties, so no validation, no read-only check and no write can be involved. *The fault is in the request handler itself.*

Per the Redfish spec (DSP0266 v1.23.0) the only legal responses are *200* (success), *400 + PropertyNotWritable* (not writable), or *405* (PATCH unsupported — which would also require the `Allow` header to omit PATCH). The iDRAC advertises `Allow: GET, PATCH` and then returns 500, matching none of them. The spec defines 500 as "an unexpected condition"; Dell's own SYS494 text says "unknown internal error".

We also confirmed this *fails identically on perfectly healthy trays* — not just the capped ones.

*UPDATE 31 Jul — the VBIOS theory below did NOT survive testing*

We checked whether power smoothing is even switched on. It is not — `Enabled: false` on *all 36 capped trays* (144 GPUs) and on healthy controls, with `RemainingLifetimeCircuitryPercent` at a pristine 100% everywhere, meaning the smoothing circuitry has never engaged. The release-note fix is conditioned on power smoothing ramp-up being *enabled*, and power smoothing acts on the TMP floor and ramp rates, not on `PowerLimitWatts.SetPoint` where our 699 W actually lives. Wrong feature, and switched off.

What the sweep found instead is better. Healthy trays carry an explicit *persistent* out-of-band power limit of *1400 W on all four GPUs* (`RequestedPersistentPowerLimitWatts = 1400.0`). The 36 affected trays carry *699 W on the slot-0 pair and no persistent write at all on slot-1*. So a persistent OOB power-limit write went across the fleet, and on these 36 it either used the wrong value or stopped halfway through the tray. That explains the perfectly uniform 699 integer across 19 racks much better than an intermittent hardware-triggered failure does.

Net: the *observation* is unchanged — a stale persistent OOB setpoint we cannot clear because iDRAC 500s on every HGX write. The *origin* is now "who ran the provisioning write, and why did it half-complete", not a VBIOS bug. We are still behind on VBIOS/HMC and should close that gap, but it is no longer the headline. Section below kept for provenance.

*SUPERSEDED CONCLUSION — it's a known NVIDIA defect with a VBIOS fix already published*

NVIDIA DGX GB300 NVL72 Release Notes, *Release 1.0.10* (15 July 2026), Improvement #12:

```
"Some GPUs may fail to ramp to full TGP when power smoothing ramp-up is enabled,
 resulting in reduced compute performance. Implemented a VBIOS fix to update the
 ramp-up logic so affected GPUs can complete ramp-up."
```

Firmware in that release vs ours:

```
component     release 1.0.10        our trays              status
GPU VBIOS     97.10.4A.00.32        97.10.4A.00.1F         BEHIND  <- the fix
HMC           GB200Nvl-25.09-4      GB200Nvl-25.08-B       BEHIND
EROT/CPLD/FPGA                      all match
```

*Why it fits everything.* GB300 power smoothing deliberately caps GPU power at workload start and ramps it up gradually to protect the grid. If the ramp-up logic fails, the GPU stays pinned at the startup cap — which is ~half of 1400 W, i.e. our 699 W. It is delivered out-of-band via SMBPBI, and NVIDIA states the GPU PMU "selects the most conservative policy" among VBIOS / nvidia-smi / SMBPBI, which is exactly why `nvidia-smi -pl 1400` reports success while the enforced limit stays 699. "*Some* GPUs" matches 36 of 543 trays scattered with no infrastructure correlation. And nothing is faulty, which is why no thermal/power/PSU event exists anywhere.

This also explains why capped and healthy trays run *identical* firmware: the buggy VBIOS is on every tray; the bug only *triggers* on some GPUs.

*So there are two issues, and the priority has flipped:*

1. *The 699 W cap* — known NVIDIA VBIOS defect. *Fix = firmware upgrade, not a Redfish write.* This is the one that matters.
2. *Dell iDRAC10 `1.30.07.13` cannot write any HGX-proxied Redfish resource* (cluster-wide, healthy trays included). Still a real defect worth reporting, but it drops from blocker to side issue — it was never the route to fixing #1.

*Next steps*
• Ask Dell for their packaged equivalent of DGX GB300 release 1.0.10 — specifically which Dell firmware release carries VBIOS `97.10.4A.00.32` + HMC `25.09-4`. We need the Dell bundle, not the NVIDIA one, on XE9712.
• Validate on one capped tray before the fleet. Symptom match is very strong but unproven on our hardware.
• Still file the iDRAC Redfish PATCH bug separately — reproducible in two curl commands on any healthy XE9712.
• Heads up: Dell's code matrix lists XE9712 at iDRAC `1.30.07.12` as both minimum *and* recommended (08 Apr 2026), so iDRAC `1.30.10.50` may not be validated for this platform. Don't assume.
• Until upgraded, keep these 36 nodes out of performance-sensitive multi-node jobs.

Bonus: release 1.0.10 also fixes XID 150/154 after GPU reset and a GPU boot failure when FLR is issued while NVLink is training — likely relevant to the Xid 149/154 NVLink degradation we saw on `i05-30`.

Source: https://docs.nvidia.com/dgx/dgxgb300nvl72-release-notes/improvements.html
Full technical writeup with all commands and outputs: `docs/NSCALE_TX_GB300_SLOT0_POWER_CAP.md`
