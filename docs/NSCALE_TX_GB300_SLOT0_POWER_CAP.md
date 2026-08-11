# Nscale TX (GB300 NVL72) — 699 W GPU power cap investigation

**Date:** 2026-07-30, updated 2026-07-31 · **Cluster:** Nscale TX / `ink50-dh01` · **Node investigated:** `ink50-dh01-i05-30` (idle, no jobs disturbed)

## *** RESOLVED 2026-08-05 — the blocker was a missing Dell iDRAC Datacenter License ***

> **Canonical operator/agent runbook (reuse on any Dell HGX site):**
> [`DELL_IDRAC_DATACENTER_LICENSE_HGX.md`](DELL_IDRAC_DATACENTER_LICENSE_HGX.md)
> · agent rule: [`.cursor/rules/dell-idrac-datacenter-license-hgx.mdc`](../.cursor/rules/dell-idrac-datacenter-license-hgx.mdc)

**AIC + Dell remediated 115 nodes.** The fix was exactly the route this document identified —
`ClearOOBSetPoint` + `PATCH PowerLimitWatts` — and the reason every one of our attempts returned
**HTTP 500** was that **those Redfish endpoints require the Dell iDRAC Datacenter License**, which
the trays did not have. With the license applied, the same calls succeed.

**This is the single most important lesson in this document.** We spent significant effort proving,
correctly, that the payload was valid, that the resource advertised `Allow: GET, PATCH`, and that an
empty `{}` PATCH also 500'd — and concluded the request handler was broken. The handler was not
broken; it was **unlicensed**. iDRAC reported a licensing failure as
`Base.1.18.InternalError` / `IDRAC.2.15.SYS494` instead of the correct **402/403 with a license
message**, which is what sent us down the wrong path. Everything below about DSP0266 conformance
still stands as a defect report — returning 500 for "feature not licensed" is wrong and should be
filed — but it was never a functional dead end.

**Design note (HGX vs DGX):** this license gate is a **Dell iDRAC SKU decision on HGX trays**.
NVIDIA DGX appliances with the same GB300 silicon do not put OOB GPU power clear/set behind a
third-party BMC Datacenter entitlement. Treat missing Datacenter + Redfish 500 as a known
product behavior first — not as “Redfish is broken” — then install the tag-bound license and
retry. The 699 W setpoint itself was a separate provisioning write; Datacenter only unlocks the
APIs that clear it.

Corollary: **check licensing before concluding a Redfish endpoint is defective.** On Dell, the
Datacenter (not Enterprise) license gates GPU/HGX power management. Verify with
`racadm license view` or `GET /redfish/v1/LicenseService/Licenses`.

Also corrected: an earlier read of this event attributed the clearing of `h08-16` to that rack's
power cycle. It was not the power cycle — it was the AIC/Dell Redfish remediation. There is no
evidence in this investigation that an AC cycle clears the setpoint; a host reboot demonstrably does
not (see the reboot test below).

Ongoing monitoring: twice-daily sweep by AIC, logs at `/home/nvis/logs/gpu-throttle/` on the head
node (owned by `nvis`, not readable as `cmods`).

### Still outstanding as of 2026-08-05 22:45 UTC — 6 trays

An independent in-band + Redfish sweep of all **2015** nodes (the cluster has grown again; racks `d`
through `j`) finds 6 trays still carrying the cap, with the identical partial-write fingerprint
(`SetPoint 699` + `Persistency True` + `ReqPersist 699.0` on slot-0, no persistent write on slot-1,
chassis rollup 4198/5600 `Override`):

| Node | Service tag | BMC | Note |
|---|---|---|---|
| h01-33 | 464HKK4 | 10.40.144.15 | in the original 36 — missed by the remediation |
| i17-11 | 5D4HKK4 | 10.40.150.38 | in the original 54; **Redfish tree also unreadable** |
| f02-35 | 778HKK4 | 10.40.136.35 | newly commissioned rack |
| g07-30 | DT7HKK4 | 10.40.141.48 | newly commissioned rack |
| g07-35 | 2M9HKK4 | 10.40.141.53 | newly commissioned rack |
| g07-37 | 6C9HKK4 | 10.40.141.55 | newly commissioned rack |

Two distinct follow-ups, worth keeping separate:

1. **`h01-33` and `i17-11` were known and did not get fixed** — a coverage gap in the remediation
   pass, not a new occurrence. `i17-11` additionally returns nothing from its HGX Redfish tree, the
   same symptom `h06-16` showed, so it may have failed the remediation *because* of that.
2. **The four in `f02`/`g07` are on racks commissioned after the original survey.** The provisioning
   defect that writes 699 W is therefore **still live** on new capacity. Fixing the existing trays
   does not stop new ones appearing — the writer itself still needs to be found and corrected, which
   remains the open question for Dell/AIC.

## *** SCOPE UPDATE 2026-07-31 — it is 54 trays, not 36 ***

> **Every "36 trays / 543 trays / 19 racks" figure below is the 2026-07-30 snapshot.** Read them as
> historical. Current numbers:
>
> ```
> 864  Slurm nodes total          (was 543 — the cluster grew)
> 807  reachable / probed
>  57  unreachable (54 down*, 3 drain*) — unassessed, so 54 is a FLOOR
> 751  healthy    1400,1400,1400,1400
>  54  CAPPED      699, 699,1400,1400   = 6.7% of reachable, 30 racks
> ```
>
> All 36 originally reported trays are **still capped** — none self-healed. 18 more were found, and
> **16 of those are in the newly commissioned `j` racks**, so the defect is *reproducing on new
> capacity* rather than developing over time. That is a strong argument for a provisioning-time
> cause. Impact: **108 GPUs at half TGP ≈ 75.7 kW** of provisioned capacity unavailable.
>
> Node list, service tags, BMC IPs, module serials and per-rack distribution:
> [`NSCALE_TX_CAPPED_SERVICE_TAGS.md`](NSCALE_TX_CAPPED_SERVICE_TAGS.md).
>
> Two unrelated defects surfaced by the same sweep: `i17-34` and `j21-32` enumerate only **3 GPUs**;
> `h06-16` has a **broken Redfish tree** (every child of `HGX_Baseboard_0` 404s).

## *** UPDATE 2026-07-31 — the power-smoothing/VBIOS hypothesis is largely ruled out ***

A fleet-wide sweep of NVIDIA **Power Smoothing** state (in-band `nvidia-smi power-smoothing` plus
the OOB `Oem/Nvidia/PowerSmoothing` Redfish object) says the mechanism proposed in the next section
is not active on this hardware. Details and the new leading hypothesis:
[**Power Smoothing sweep**](#power-smoothing-swept-across-all-36-capped-trays--6-controls-2026-07-31).
In short:

- **Power smoothing is `Enabled: false` on all 36 capped trays** (144 GPUs) and on all healthy
  controls. The release-note defect below is conditioned on it being *enabled*.
- **`RemainingLifetimeCircuitryPercent = 100.0` on every GPU probed** — the smoothing circuitry has
  never engaged.
- **Mechanism mismatch:** power smoothing acts on the TMP floor and ramp rates. It does not write
  `PowerLimitWatts.SetPoint`, which is where our 699 W actually lives.
- **The real discriminator is different from what this doc originally recorded:** healthy trays
  carry a *persistent OOB power limit of 1400 W on all four GPUs*; the capped trays carry a
  persistent 699 W on the slot-0 pair and **no persistent write at all** on the slot-1 pair. That
  looks like an interrupted or misparameterised provisioning write, not a ramp-up failure.

The firmware gap below is real and still worth closing, but it should no longer be presented to
Dell/NVIDIA as the identified root cause. Section retained unedited for provenance.

## *** SUPERSEDED HYPOTHESIS (2026-07-30) — known NVIDIA defect, VBIOS fix available ***

**NVIDIA DGX GB300 NVL72 Release Notes, Release 1.0.10 (15 July 2026), Improvement #12:**

> "Some GPUs may fail to ramp to full TGP when power smoothing ramp-up is enabled, resulting in
> reduced compute performance. **Implemented a VBIOS fix** to update the ramp-up logic so affected
> GPUs can complete ramp-up."

Firmware in that release vs what we run:

```
component     release 1.0.10        our trays              status
GPU VBIOS     97.10.4A.00.32        97.10.4A.00.1F         BEHIND  <-- contains the fix
HMC           GB200Nvl-25.09-4      GB200Nvl-25.08-B       BEHIND
EROT          01.04.0031.0000_n04   01.04.0031.0000_n04    match
CPLD          0.22                  0.22                   match
FPGA          1.60                  1.60                   match
```

### Why it fits every observation

GB300 power smoothing intentionally caps GPU power at workload start and ramps it up gradually to
protect the grid ("new maximum power levels are sent to the GPUs and gradually increased"). If the
ramp-up logic fails, the GPU stays pinned at the startup cap:

- **699 W is the startup cap** (~half of 1400 W) — a defined level, not a physical limit, which is
  why it is an identical integer on all 36 trays across 19 racks.
- `SetPoint 699` < `DefaultSetPoint 1400` with `ControlMode: Automatic` — a power-controller-managed
  value, exactly as the object reports.
- Delivered **out of band via SMBPBI**. Per NVIDIA the GPU PMU "selects the most conservative
  policy" among VBIOS / `nvidia-smi` / SMBPBI — which is why `nvidia-smi -pl 1400` reports success
  while `enforced.power.limit` stays 699.
- "**Some** GPUs" — an intermittent ramp-up failure. Matches 36 of 543 trays, scattered, with no
  infrastructure correlation.
- No thermal, power, PSU or hardware events anywhere — because nothing is faulty.

### Corrects an earlier inference in this document

Elsewhere this doc notes that firmware is identical between capped and healthy trays and concludes
firmware is "not the discriminator". That inference was wrong. The **buggy VBIOS is on every tray**;
the bug only *triggers* on some GPUs. Identical firmware is exactly what this defect predicts.

### Impact on the plan

**The fix is a firmware upgrade, not a Redfish write.** The iDRAC HGX PATCH 500 defect (documented
at length below) is real and still worth reporting to Dell, but it drops from *blocker* to *side
issue* — it was never the route to fixing this.

Sourcing caveat: these are **Dell XE9712** trays, so we need Dell's packaged equivalent of DGX GB300
release 1.0.10, not the NVIDIA bundle directly. Ask Dell which of their firmware releases carries
VBIOS `97.10.4A.00.32` + HMC `25.09-4`. Note Dell's code matrix lists XE9712 at iDRAC `1.30.07.12`
as both minimum *and* recommended (08 Apr 2026), so iDRAC `1.30.10.50` may not be validated here.

Bonus: release 1.0.10 also fixes XID 150/154 after GPU reset and a GPU boot failure when FLR is
issued while NVLink is training — plausibly relevant to the Xid 149/154 NVLink degradation seen on
`i05-30`.

**Confidence:** the symptom match is very strong but not yet proven on our hardware. Validate on one
capped tray before the fleet.

### No runtime workaround exists — all levers checked

NVIDIA lists this under *Improvements* (fixed by VBIOS), **not** under Known Issues. That
distinction matters: every Known Issue in that release note carries an explicit `Workaround:` line
(several saying "there is currently no workaround"). This item has none, because from NVIDIA's
perspective it is already fixed.

Every host-side lever was checked on a capped tray and is closed:

```
nvidia-smi -pl 1400     -> accepted, but PMU takes the lower SMBPBI value (proven earlier)
power-smoothing -ppd    -> "Insufficient Permissions" on EVERY field, even as root,
                           and IDENTICAL on capped and healthy GPUs -> host is not
                           authorised; power smoothing is HMC-managed out-of-band
power-profiles -l       -> only "Max-Q" offered (lower power, wrong direction)
power-profiles -gr/-ge  -> none requested, none engaged
boost-slider -l         -> "No supported devices found"
Redfish EnvironmentMetrics (the SMBPBI front-end) -> HTTP 500, see BLOCKER section
```

Note the three "sources of power limit" (VBIOS / `nvidia-smi` / SMBPBI) are *sources*, not three
tools: VBIOS is the ceiling (`AllowableMax 1400`), and **Redfish `EnvironmentMetrics` is the
front-end to SMBPBI** — so Redfish and SMBPBI are the same path, not alternatives.

### GPU reset — tested, does not apply

Attempted on `h20-28` (drained). `nvidia-smi -r -i 0,1` refused with *"In use by another client"*
even after stopping `nvidia-persistenced`, `nv-hostengine` (DCGM) and `slurmd`. The remaining holder
is **`nvidia-imex`**, which pins the GPUs for the multi-node NVLink domain; stopping it risks the
rack's NVLink fabric and is more disruptive than a reboot.

**The hypothesis was already disproven regardless:** a full host reboot does *not* clear the cap
(`i05-30` rebooted 06:07, came back at 699 W; all 36 trays capped continuously across reboots for
the full 7-day telemetry window). A reboot re-initialises the GPUs far more thoroughly than
`nvidia-smi -r`, so a GPU reset could not succeed where a reboot fails.

Node was fully restored afterwards (all services active, nvlink clean, returned to `idle`).

### Two incidental findings (not faults)

- `nvidia-fabricmanager` reports **`failed` on every node**, capped and healthy alike. Normal for
  this deployment, which uses IMEX. Looks alarming if encountered cold.
- `nvidia-dcgm-exporter` is **`inactive`/`disabled` by design** on all nodes; DCGM metrics are
  collected centrally from the head via `dcgm-pdsh` (`job_role="dcgm-pdsh"`), not per-node.

Source: <https://docs.nvidia.com/dgx/dgxgb300nvl72-release-notes/improvements.html>

## Power Smoothing swept across all 36 capped trays + 6 controls (2026-07-31)

Prompted by the obvious question the superseded section never actually tested: *is power smoothing
even switched on?* Swept with `bin/scripts/nscale_ps_sweep.py` (read-only, parallel Redfish) plus an
in-band probe where the BMC is unroutable. **Coverage: 36/36 capped trays and 6 healthy controls
spread across 6 racks.**

### Result 1 — power smoothing is OFF everywhere

```
$ sudo nvidia-smi power-smoothing -i 1 --state=DISABLED
Power Smoothing is already disabled on this device      # all 4 GPUs, every tray tested
```

Out of band, `/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_<N>/Oem/Nvidia/PowerSmoothing`:

```
                                    capped trays (29 OOB-probed)   healthy controls (6)
PowerSmoothingSupported             true                           true
Enabled                             false   <-- on all 4 GPUs      false
RemainingLifetimeCircuitryPercent   100.0                          100.0
RampUpWattsPerSecond                20.0                           20.0
RampDownWattsPerSecond              20.0                           20.0
TMPFloorPercent                     89.990234375                   89.990234375
AppliedPresetProfile                PresetProfiles/0               PresetProfiles/0
```

All five preset profiles are byte-identical between a capped and a healthy GPU. The remaining 7
trays (`h06-16` + the six on the unroutable `10.40.147.x` BMC subnet) were confirmed in band: all
report *"Power Smoothing is already disabled"* on all four GPUs and still show `enforced.power.limit`
699/699/1400/1400.

Two consequences:

1. **The release-note defect does not apply as written** — it is conditioned on power smoothing
   ramp-up being *enabled*. `RemainingLifetimeCircuitryPercent` at a pristine 100.0 on every GPU
   indicates the smoothing circuitry has never engaged.
2. **Wrong control path anyway.** Power smoothing acts through `TMPFloorPercent` and the ramp rates.
   The 699 W lives in `PowerLimitWatts.SetPoint` with `PowerLimitPersistency = True`, which power
   smoothing does not write.

`TMPWatts` does differ (733 W on capped GPUs vs ~1456 W on healthy) but it *tracks* the enforced
limit rather than explaining it — roughly the enforced limit plus module overhead. It is downstream.

### Result 2 — the actual discriminator, and it is not what we thought

Comparing `RequestedPersistentPowerLimitWatts` on a capped tray against healthy controls reframes
the whole thing:

```
                        GPU_0        GPU_1        GPU_2        GPU_3
i05-30   (capped)
  SetPoint              699          699          1400         1400
  PowerLimitPersistency True         True         False        False
  ReqPersistentWatts    699.0        699.0        null         null

h01-11 / h02-11 / h05-11 (healthy)
  SetPoint              1400         1400         1400         1400
  PowerLimitPersistency True         True         True         True
  ReqPersistentWatts    1400.0       1400.0       1400.0       1400.0
```

A **persistent out-of-band power limit was written fleet-wide** — healthy trays carry an explicit
persistent 1400 W on all four GPUs. On the 36 capped trays that same pass wrote **699 W to the
slot-0 pair and never wrote the slot-1 pair at all** (`Persistency: False`, requested values null).

This is a much better fit than a ramp-up bug: it looks like a provisioning/commissioning write that
was either misparameterised or interrupted partway through the tray. It also explains the perfectly
uniform 699 integer across 19 racks, which an intermittent hardware-triggered failure would not
produce.

### Result 3 — corrections to earlier claims in this document

- **`ControlMode: Override` on `TotalGPU_Power_0` is normal, not a red flag.** Healthy trays report
  `ControlMode: Override` with `SetPoint 5600` = `DefaultSetPoint 5600`. Only the *value* is
  anomalous on the capped trays, not the mode.
- **`ControlMode` on the per-GPU `EnvironmentMetrics` is `Automatic` on capped and healthy alike** —
  it does not indicate a power-controller-managed value, as the superseded section inferred.
- **`PowerLimitPersistency = False` on the capped trays' slot-1 GPUs is the anomaly**, not the
  `True` on slot-0. Healthy trays are `True` on all four.

### Result 4 — one tray with a broken Redfish tree

`h06-16` (`10.40.145.25`) answers `/redfish/v1/Systems` and lists `HGX_Baseboard_0`, but every child
under it 404s (`Processors`, all four `GPU_<N>/EnvironmentMetrics`, all `PowerSmoothing`). The other
35 trays expose the full tree. In-band the tray behaves like every other capped tray. Worth raising
with Dell as a separate HMC/iDRAC proxy fault.

### Reproduce

```bash
BMC_USER=… BMC_PASSWORD=… BMC_CSV=/tmp/bmc-targets-up.csv HEALTHY_COUNT=6 \
  python3 bin/scripts/nscale_ps_sweep.py
```

Must run from the BCM head (`cmods@192.168.10.2`) — the OVA has no route to the BMC subnets, and
`10.40.147.x` is unroutable from the head and from compute nodes alike.

## Plain-language summary (safe to lift for email / Slack / the vendor case)

There are **two independent problems**. Keeping them separate matters — conflating them will get the
vendor case pushed back.

**Problem 1 — a stale power setpoint on 54 trays** (36 when first counted)**.** Something wrote **699 W** to the two slot-0 GPUs
(`GPU_0`, `GPU_1`) against a factory default of **1400 W** each. It rolls up to a tray total of
4198 W vs the 5600 W default. Origin is unknown and no longer recoverable from the trays (the iDRAC
Lifecycle Controller log goes back only to 20-27 June and contains no such write). Not a hardware, thermal, PSU, PDU or
firmware-version fault — all measured and excluded. Likely an NVIDIA / commissioning question.

**Problem 2 — the iDRAC cannot write HGX resources at all.** Every Redfish PATCH to an HGX-proxied
object returns **HTTP 500**, so the stale setpoint cannot be cleared. This affects **every tray in
the cluster, including perfectly healthy ones** — the healthy tray `i05-11` (GPU_0 = 1400 W,
TotalGPU = 5600 W) fails identically. Squarely a Dell iDRAC10 `1.30.07.13` firmware defect.

**The firmware defect did NOT cause the low power limit.** It prevents the remedy. Do not state it
the other way round.

**Consequence beyond these 36 trays:** while Problem 2 stands, *no* Redfish-based power management
(DPS, power capping, workload power profiles) can function on any tray in this cluster.

**How Problem 2 is proven, in two commands:** a PATCH carrying an **empty JSON object** `{}` — which
by definition changes nothing — returns 500 on the HGX resource, while the same empty PATCH returns
200 on a Dell-native resource on the same controller in the same session. Since the payload contains
no properties, no validation, read-only check or write can be involved; the fault is in the request
handler. Per DSP0266 v1.23.0 the only permitted responses are **200** (success/partial, §7.6),
**400** with `PropertyNotWritable` (all properties unwritable, §7.6 + §9.9.5.3), or **405** (resource
does not support PATCH, §7.5.3 — which would also require `Allow` to omit PATCH). The iDRAC returns
`Allow: GET, PATCH` and then 500, which matches none of them.

## Summary — ROOT CAUSE CONFIRMED

**A tray-level out-of-band GPU power cap of 4198 W has been written to the iDRAC/HMC on 54 trays
(36 as of this section's original writing), against a factory default of 5600 W.** It is a stale
configuration value, not a hardware fault.

```
/redfish/v1/Chassis/HGX_Chassis_0/Controls/TotalGPU_Power_0

               CAPPED tray          HEALTHY tray
SetPoint         4198 W       vs      5600 W        <-- the only difference in the entire tree
DefaultSetPoint  5600 W               5600 W
AllowableMax     5600 W               5600 W
```

`5600 = 4 x 1400 W`. The 1402 W shortfall is absorbed entirely by the slot-0 module, so its two
GPUs land at **699 W each** while slot 1 keeps its full 1400 W each:

```
699 + 699 + 1400 + 1400 = 4198   <-- exactly the observed distribution
```

A cluster-wide sweep confirms **all 36 affected trays sum to exactly 4198 W**, always on gpu0/gpu1,
with no other value anywhere in the cluster. It is not a PSU, PDU, thermal, or torque problem, and
it is not random.

The override is written **per GPU** (the tray figure is a rollup) and NVIDIA exposes a purpose-built
`#NvidiaEnvironmentMetrics.ClearOOBSetPoint` action to clear it.

> **BLOCKER:** every attempt to clear it — the vendor's own action, direct `PATCH`, chassis level,
> per-GPU level, basic auth and session auth — returns **HTTP 500 `IDRAC.2.15.SYS494`**. Reads work
> fine. The setpoint is currently **readable but not writable**, which makes this a Dell/NVIDIA
> case. See "BLOCKER" and "Escalation summary" below.

## What the node shows

`nvidia-smi` reports the GPU's rated capability intact, but its effective ceiling halved:

```
index, serial,        power.limit, default, max,     enforced
0,     1765225793151, 1400.00 W,   1400 W,  1400 W,  699.00 W   <- slot-0 module
1,     1765225793151, 1400.00 W,   1400 W,  1400 W,  699.00 W   <- slot-0 module
2,     1765025792048, 1400.00 W,   1400 W,  1400 W,  1400.00 W
3,     1765025792048, 1400.00 W,   1400 W,  1400 W,  1400.00 W
```

GPUs 0/1 share serial `1765225793151` and GPUs 2/3 share `1765025792048` — that is how we confirmed the split is **per-superchip**, not per-GPU. 699 W is essentially half of the 1400 W rated TGP.

### The decisive test

Trying to raise the limit in band:

```
$ sudo nvidia-smi -i 0 -pl 1400
Power limit for GPU 00000008:06:00.0 was set to 1400.00 W from 699.00 W.  All done.

-> enforced.power.limit STILL reads 699.00 W
```

The driver accepts the change and `power.limit` moves, but `enforced.power.limit` stays pinned. **Nothing in the OS or driver layer is imposing this** — it is being clamped underneath, by the module firmware. The limit was restored to the 1400 W default afterwards, so the node is back to baseline.

## What we ruled out

- **PSU / power connector fault** — all 121 BMC sensors identical to a healthy tray (only fan RPM and temp jitter differ); SEL has zero power events.
- **PDU / rack power** — tray draw normal (1023 W vs 1056 W on a healthy tray); healthy and capped trays are interleaved within the same racks.
- **Module batch defect** — capped slot-0 serials span every production batch, and healthy trays carry slot-0 modules with *adjacent* serials. Example: healthy `h22-11` slot0 = `1653125003275` vs capped `h21-29` slot0 = `1653125003342`.
- **Firmware skew** — identical on both groups; see detail below.
- **Software power cap** — no workload power profile applied (`Requested`/`Enforced Profiles: N/A`); no boot-time `nvidia-smi -pl` setter; `acpi_power_meter` Grace socket caps are 300 W on both.
- **Thermal / power brake** — no throttle reason active; HW power brake and HW/SW thermal all `Not Active`. Full thermal analysis below.

## Firmware comparison (detail)

Every in-band-readable firmware component matches exactly across capped and healthy trays:

```
VBIOS         97.10.4A.00.1F
GSP           580.126.20
Inforom img   G548.0301.00.03   OEM 2.1   ECC 7.16
BMC           1.30
Board part    06DC4DA00   (Dell PowerEdge XE9712)
```

Verified on `i05-30` + `i08-11` (capped) and `i05-11` + `i05-12` (healthy). Critically, this also
holds **within** a capped tray: the 699 W module and the 1400 W module on the same board run
byte-identical firmware. Firmware *version* is therefore definitively not the discriminator.

**Caveat / open item:** `nvfwupd` is not installed on these nodes, so the deeper platform firmware
stack — ERoT, FPGA/CPLD, module power controller, PDB — could **not** be enumerated. That requires
the BMC's Redfish `FirmwareInventory` (no credentials available to us). Firmware *configuration*
on the module power controller is the one hypothesis still standing, and it is exactly where a bug
of this shape would live. **This is the specific thing to ask NVIDIA/Dell to check.**

## Thermal analysis — the cap is NOT thermally driven

Seven days of DCGM temperature paired with power draw, bucketed by power band:

```
group                                 400-600W   690-715W   900-1100W  1300-1420W
CAPPED gpu0/1 (slot0)                 39.4 C     44.3 C        -           -
SIBLING gpu2/3 (slot1, same tray)     37.7 C        -        54.3 C     64.6 C
CONTROL gpu0/1 (healthy tray)         33.0 C        -        53.2 C     65.4 C
CONTROL gpu2/3 (healthy tray)         33.7 C        -        55.3 C     67.6 C
```

Findings:

1. **Capped GPUs top out at 44 C** — about 20 C *cooler* than healthy GPUs sustain at full power.
   Enormous thermal headroom; nothing for a thermal governor to react to.
2. **Temperature-per-watt is normal**, which directly tests the torque / cold-plate hypothesis.
   The healthy trend is ~0.03 C/W (54 C @ ~1000 W, 66 C @ ~1400 W), extrapolating to **~45 C @ 700 W**.
   Capped GPUs measure **44.3 C @ 700 W** — right on the line. Thermal resistance is normal, so
   cold-plate contact on those slot-0 modules is fine. A badly torqued module would show high
   temperature *per watt*; it does not.
3. **Zero thermal throttle events** — `HW Thermal Slowdown 0 us`, `SW Thermal Slowdown 0 us`, all
   reasons `Not Active`. T.Limit thresholds (`Shutdown -5 C`, `Slowdown -2 C`, `Max Operating 0 C`)
   are identical on capped and healthy GPUs, so no per-GPU threshold misprogramming.
4. **Causality runs the other way**: these GPUs are cool *because* they are capped, not capped
   because they are hot. A thermal cap would also be dynamic (varying with load and inlet temp);
   this is a hard constant 699 W even at 27 C idle.

**Secondary observation (unrelated to the cap):** in the 400-600 W band the affected trays run
~5 C warmer than control trays — but *both* modules do (39.4 C slot 0, 37.7 C slot 1, vs ~33 C on
controls). Since it hits both modules equally it cannot explain a slot-0-only cap, and 39 C is
nowhere near any throttle point. Sample counts are small (23-73) and the band is wide, so some may
just be differing power distributions within the bucket. Worth a look at coolant flow on those
trays eventually, but it is not this issue.

## Load test — rules out a power rail fault

Run on `ink50-dh01-h20-28` (Slurm-drained for exclusivity, then resumed). All 4 GPUs driven with
`dcgmproftester13 -t 1004` (tensor) for 120 s, telemetry logged at **100 ms**.

```
gpu   limitW      p99W    %of limit    meanW      sd     maxT
0       1400     697.7      49.8%      694.0   16.51 W    44 C
1       1400     697.9      49.9%      693.3   17.23 W    44 C
2       1400    1387.6      99.1%     1363.9   61.91 W    64 C
3       1400    1372.3      98.0%     1352.8   62.96 W    63 C
```

Throttle reasons while loaded:

```
gpu0: sw_power_cap 100%    (no hw_slowdown, no hw_power_brake, no thermal)
gpu1: sw_power_cap 100%    (no hw_slowdown, no hw_power_brake, no thermal)
gpu2: sw_power_cap 100%    (no hw_slowdown, no hw_power_brake, no thermal)
gpu3: sw_power_cap 100%    (no hw_slowdown, no hw_power_brake, no thermal)
```

**There is no power rail fault.** A degraded VR phase or missing rail would produce
`hw_power_brake_slowdown` assertions, `hw_slowdown`, power oscillation, or an inability to hold the
target. None occur. GPU0/1 hold 694 W mean with sd **16.5 W** — *steadier* than their healthy
siblings at 62 W (which wobble more only because they are doing 4x the work against a higher
ceiling). `sw_power_cap` active on all four is expected (every GPU sits at its own ceiling) and does
not discriminate; the discriminating fact is the **absence of any hardware distress flag** on the
capped pair.

What we see is a clean software power cap at **49.8-49.9% of rated TGP** — mathematically half, held
rock-steady, with power delivery underneath behaving perfectly.

## Is it a power-delivery / PDU problem? Measured, not inferred

Direct electrical measurement from the tray sensors:

```
HGX_GPU_0_Voltage_0   0.900 V     <- capped at 699 W
HGX_GPU_1_Voltage_0   0.895 V     <- capped at 699 W
HGX_GPU_2_Voltage_0   0.900 V     <- full 1400 W
HGX_GPU_3_Voltage_0   0.890 V     <- full 1400 W
```

The capped pair's rails are **not** sagging — 10 mV spread, and the lowest reading belongs to
GPU_3, which runs at full power. Per-module power at idle agrees: **473.3 / 471.2 / 475.3 / 475.2 W**,
all within 4 W.

Both modules report `Module Current Power Limit = Max = 2900 W` (factory default). That is exactly
where firmware would encode "this module can only take half" — and it is untouched. Likewise the
capped GPUs still report `AllowableMax: 1400`; a capability-based derate lowers `AllowableMax`, not
just `SetPoint`.

Structural points:

- The value sits in a writable config property whose vendor action is named **`ClearOOBSetPoint`**
  — *out-of-band setpoint*, i.e. externally written. Hardware derating does not present that way.
- 699 = exactly `floor(1400/2) - 1`, an identical integer on 36 trays in 19 racks. Physical delivery
  limits vary per unit (connector resistance, VR binning); they do not converge on one integer.
- Under load: no `hw_power_brake_slowdown`, no `hw_slowdown`, power stable at sd 16.5 W. A module
  at a delivery wall asserts brake and oscillates.
- All 19 racks contain **both** capped and healthy trays on the same busbar/PDU.

### PS_RUN_PWR_FAULT in the LC log — checked, NOT correlated with the cap

**Correction to an earlier note in this doc:** the LC log does *not* only retain ~1 day.
`racadm lclog view` silently truncates to **100 records** by default; `-n <count>` returns the full
history, which goes back to 20-27 June (1500-3000 records per tray). Re-swept with `-n 5000`.

The deeper history does contain `PS_RUN_PWR_FAULT` entries. They are **not** correlated with the cap:

```
                    PS_RUN_PWR_FAULT > 0    PS_RUN_PWR_FAULT = 0
CAPPED   (18)              9  (50%)                9
HEALTHY  (15)              8  (53%)                7
```

- **Nine capped trays have entirely clean logs** — `h08-16 h18-34 h20-11 h20-28 h22-15 h22-17
  i02-30 i19-12 i20-33` show `PWRFAULT=0 THERM=0 LEAK=0 FANRED=0` back to 20-26 June, yet are capped.
- **The healthy neighbour can have more faults than the capped one**: rack i08 -> `i08-11` (capped)
  4 faults, `i08-12` (healthy) **8**.

The events are **rack-wide cooling incidents**, not per-tray power-feed faults — timestamps cluster
across whole racks within seconds, hitting capped and healthy trays alike:

```
h01   h01-33 (capped)  07-07 23:40:14 | h01-12 (healthy) 23:41:15 | h01-11 (healthy) 23:41:16
h03   h03-11 (healthy) 07-08 00:03:51 | h03-29 (capped)  00:04:08 | h03-16 (capped)  00:04:47
h06   h06-11 (healthy) 07-09 20:24:10 | h06-16 (capped)  20:24:36 | h06-14 (capped)  20:24:38
```

`THERM`, `LEAK` and `FANRED` counts track `PWRFAULT` ~1:1 in every row. The `i08-11` sequence shows
the mechanism — the power fault is a *consequence* of a thermal emergency shutdown, not a cause:

```
22:42:51  GPU 3 reached maximum operating temperature
22:43:27  component exceeded temperature threshold, attempt to shut down
22:43:30  PS_RUN_PWR_FAULT              <- fires AFTER the shutdown attempt
22:43:48  leak detection sensor of the NVLS
22:44:05  fans below critical threshold, fan redundancy lost
```

These rack-level cooling incidents are a real finding worth its own investigation, but they are a
**separate issue** from the 699 W cap.

### Would a power fault show in BIOS / boot logs? Yes — and all are clean

```
host kernel log   0 lines matching rail|vreg|pgood|undervolt|power fault|brownout|psu
iDRAC SEL         only fan events from 2026-07-13
iDRAC LC log      zero power/voltage entries (full log via racadm, not the truncated Redfish view)
Redfish health    all chassis OK except the 4 NVLink-error GPUs on i05-30
```

### Per-PSU wattage monitoring: does not exist on these trays (by design)

Full tray sensor inventory:

```
24 x Cel (temperature)      10 x W (power)        4 x V (voltage, one per GPU)
16 x RPM (fan)               4 x J (energy)       1 x A (current, tray total)
 2 x Pa (pressure, liquid)
```

Every `PowerSupplies` collection is **empty** and IPMI PSU sensors read `disabled` / `Not Readable`
— NVL72 compute trays are **busbar-fed with no PSUs in the tray**. There are also **no per-module
input** power/voltage/current sensors (modules expose 4 sensors each, none electrical).

Available power telemetry is limited to: tray total (`SystemBoardPwrConsumption` 1019 W,
`SystemBoardCurrentConsumption` 4 A), total GPU (`TotalGPU_Power_0` 690 W), and per-GPU power +
core voltage.

**Open item / honest limitation:** the PSUs live in the **rack power shelf**, outside these BMCs.
Per-PSU wattage would have to be queried from the power shelf's own management. That is the one
source not yet examined — though the mixed-rack evidence above makes a shelf fault implausible.

## Root cause — confirmed via iDRAC Redfish

Credentials `root` / BCM (thanks Sofya) gave access to the iDRAC Redfish tree. Comparing a capped
tray (`i05-30`, BMC `10.40.149.12`) against a healthy one in the same rack (`i05-11`, BMC
`10.40.149.2`) produced exactly one difference:

```
                        CAPPED i05-30      HEALTHY i05-11
TotalGPU_Power_0        SetPoint 4198  vs  SetPoint 5600     <-- THE ONLY DIFFERENCE
  (DefaultSetPoint)              5600               5600
  (AllowableMax)                 5600               5600
HGX_ProcessorModule_0   SetPoint 2900  ==  SetPoint 2900
HGX_ProcessorModule_1   SetPoint 2900  ==  SetPoint 2900
GPU0-3 ClockLimit_0     identical      ==  identical
```

Note the per-module budgets are both untouched at their 2900 W default — the cap is applied at the
**tray total** level, and the HMC's distribution of the reduced budget is what starves slot 0.

Full Redfish `FirmwareInventory` was also compared and is **identical across all 17 components**,
including the layers not visible in band:

```
AcceleratorBaseboard  20.26.03.17      System FPGA   101.000.000
BIOS                  1.0.3            Mezz-MCU      0003.00.0173.0000
BMC / LifecycleCtrl   1.30.07.13       TPM           15.23.69.0
InfiniBand.Integrated 40.48.1126       NIC.Slot      32.47.2526
```

This closes the last open item from the earlier firmware comparison. Firmware is definitively not
the discriminator at any layer; the discriminator is a single written setpoint.

**Where the 699 W came from is still unknown, and is not recoverable from the tray.** The iDRAC
Lifecycle Controller log goes back to **20-27 June** (1500-3000 records per tray, retrieved with
`racadm lclog view -n 5000`; note the default view truncates to 100 records) and contains **no
power-limit / setpoint write event** anywhere in that window.

Two dead ends worth recording so nobody re-treads them:

- The Redfish `PowerControl` changes from **10.40.190.1** in the LC log are **host power-state
  actions**, not cap writes — FQDD `iDRAC.Embedded.1#HostPowerCtrl`, alongside "Requested system
  powercycle" and warm-reset entries. That is BCM rebooting nodes. It did **not** write the cap.
- **NVIDIA DPS `NodeManager/Domains`** (the documented domain power-budget API with `COMP_GPU`
  policies) does **not exist** on these trays — 404 on `Managers/BMC`, `Managers/HGX_BMC_0`, and
  `Managers/iDRAC.Embedded.1` variants, on both capped and healthy nodes. That API belongs to
  NVIDIA-BMC platforms; this Dell-proxied platform does not expose it. So no local DPS domain
  policy is holding the budget.

Since *something* did successfully write 699 W, and the write path now returns 500, one plausible
sequence is that the write succeeded before the iDRAC firmware update (`Last Firmware Update:
06/25/2026 19:20:10`) and the stale value has been unclearable since. Unproven — the log history
needed to confirm it is gone.

## OPERATIONAL TRAP — DCGM silently SKIPS these GPUs

`dcgmi diag -r targeted_power -p "targeted_power.target_power=1020.0;targeted_power.test_duration=300"`
pre-checks the enforced limit against 75% of target (765 W) , sees 699 W, and **skips the GPU rather
than failing it**:

```
| targeted_power  | Fail                                          |
|                 | GPU0: Skip     <- warning, not a failure      |
|                 | GPU1: Skip                                    |
|                 | GPU2: Pass                                    |
|                 | GPU3: Pass                                    |
```

Any DCGM-based power acceptance test therefore reports **Pass on the two healthy GPUs and quietly
Skip the two broken ones**. A validation gate that only greps per-GPU `Fail` lets these 36 trays
through. This plausibly explains how the condition got past acceptance. **Check whether the site's
validation treats `Skip` as benign.**

## Real performance impact

This is **not cosmetic.**

Seven days of DCGM telemetry shows the capped GPUs never exceeded **709 W**, while their slot-1 siblings in the same tray, running the same jobs, peaked at **~1390 W**.

Half the GPUs on each of these 36 nodes are running at 50% power, so any multi-node job that includes one will be paced by it. That is roughly **two racks' worth of degraded capacity**.

It also **survives reboots** — `i05-30` came up at 06:07 today and is still capped, and the affected set has been the identical 36 nodes for the full 7 days of retained telemetry.

## Affected nodes (36)

```
ink50-dh01-h01-33, h03-[16,29,34], h04-[17,34], h06-[14,16,18,34], h08-16,
h17-[14,29], h18-34, h20-[11,28,34], h21-29, h22-[15,17],
i02-[15,30], i03-36, i04-[14,32], i05-[30,31], i08-11, i16-[11,28],
i18-[12,35], i19-[12,14], i20-[14,33]
```

Cluster-wide sweep result: **507 nodes fully at 1400 W · 36 nodes capped · 321 unreachable.**
The capped set matches Travis's list exactly — no more, no less.

## Two things worth flagging separately

1. **`i05-30` also has Xid 149 `NETIR_LINK_DOWN`** on all four GPUs, with all four marked `Degraded` and `GPU Reset Required`. This is **unrelated** to the power cap and needs its own reset.
2. **The sweep could not reach 321 nodes** — the entire `j` row (all 22 racks) plus `i17`. The count of 36 covers the 543 reachable nodes only; the j-row should be re-swept once it is up.

## Where the override actually lives (per-GPU, not tray-level)

The 4198 W on `HGX_Chassis_0/Controls/TotalGPU_Power_0` is a **rolled-up sum**. The real override is
written **per GPU**, under the HMC-backed processor objects:

```
/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_<N>/EnvironmentMetrics

GPU_0  PowerLimitWatts: SetPoint=699   DefaultSetPoint=1400   <-- overridden
GPU_1  PowerLimitWatts: SetPoint=699   DefaultSetPoint=1400   <-- overridden
GPU_2  PowerLimitWatts: SetPoint=1400  DefaultSetPoint=1400
GPU_3  PowerLimitWatts: SetPoint=1400  DefaultSetPoint=1400
```

699 + 699 + 1400 + 1400 = 4198. Each GPU object carries its own
`#NvidiaEnvironmentMetrics.ClearOOBSetPoint` and `#NvidiaEnvironmentMetrics.ResetEDPp` actions.

Note the GPUs are under `Systems/HGX_Baseboard_0/Processors/`, **not** under
`Systems/System.Embedded.1/Processors/` (which holds only the two Grace CPUs). Per-GPU objects under
`Chassis/HGX_GPU_<N>/Controls/` expose only `ClockLimit_0`, not a power limit — easy to miss.

## BLOCKER — every OOB write path returns HTTP 500

The cap is **readable but not writable**. Every documented remediation path fails identically with
`Base.1.18.InternalError` / `IDRAC.2.15.SYS494`, while all reads work perfectly:

| Attempted write | Result |
|---|---|
| `POST` GPU_0/GPU_1 `ClearOOBSetPoint` (per-GPU) | HTTP 500 |
| `PATCH` GPU_0/GPU_1 `PowerLimitWatts.SetPoint=1400` | HTTP 500 |
| `POST` `HGX_Chassis_0` `ClearOOBSetPoint` | HTTP 500 |
| `POST` `HGX_ProcessorModule_0/_1` `ClearOOBSetPoint` | HTTP 500 |
| `PATCH` `TotalGPU_Power_0` `SetPoint=5600` (also 4500, float, `ControlMode` Manual/Automatic/Disabled) | HTTP 500 |
| All of the above retried with a **Redfish session token** instead of basic auth | HTTP 500 |
| Session token + `If-Match: "<etag>"` (resource does return `ETag: "B53CB709"`) | HTTP 500 |
| Session token + `If-Match: *` | HTTP 500 |
| Session token + `OData-Version: 4.0` | HTTP 500 |
| Session token + `If-Match: *` + `OData-Version: 4.0` | HTTP 500 |

### PROOF the request is correct: an EMPTY PATCH also 500s

DSP0266 v1.23.0 §7.6: *"Services may accept a PATCH method with an empty JSON object, which
indicates that the service should make no changes to the resource."* An empty `{}` carries no
properties — no validation, no read-only check, no write can be involved:

```
HGX-proxied  Systems/HGX_Baseboard_0/Processors/GPU_0/EnvironmentMetrics
  empty object {}        -> HTTP 500   Base.1.18.InternalError, IDRAC.2.15.SYS494
  unknown property       -> HTTP 500   Base.1.18.InternalError, IDRAC.2.15.SYS494
  annotation only        -> HTTP 500   Base.1.18.InternalError, IDRAC.2.15.SYS494
  read-only property     -> HTTP 500   Base.1.18.InternalError, IDRAC.2.15.SYS494

CONTROL  Chassis/System.Embedded.1/Power  (same iDRAC, same session)
  empty object {}        -> HTTP 200   Base.1.18.Success
  unknown property       -> HTTP 400   Base.1.18.PropertyUnknown
  annotation only        -> HTTP 400   Base.1.18.NoOperation
  read-only property     -> HTTP 400   Base.1.18.PropertyUnknown
```

**The failure is in the request handler itself, not the payload.** The control column shows the same
iDRAC implementing DSP0266 exactly as written on Dell-native resources — so the service knows the
rules; only the HGX handler is broken.

### DSP0266 v1.23.0 clauses violated on the HGX path

| Clause | Normative requirement | Observed |
|---|---|---|
| §7.6 | PATCH with empty `{}` -> service should make no changes | HTTP 500 |
| §7.6 | All properties read-only/unknown/unsupported -> **shall** return **400** listing non-updatable properties | HTTP 500, `RelatedProperties: []` empty |
| §7.6 | Partial failure incl. "a service-side error occurred" -> **shall** return **200** with extended info | HTTP 500 |
| §7.5.3 | Resource exists but does not support the operation -> **shall** return **405** | HTTP 500 |
| §6.2 (Allow) | `Allow` on a 200 GET **shall** list allowable operations | Returns `Allow: GET, PATCH` — declares PATCH allowable, then 500s |
| §9.9.5.3 | Read-only -> include `PropertyNotWritable`; internal error preventing update -> include `PropertyNotUpdated` | Neither present; bare generic `InternalError` |
| Status table | 500 = "Service encountered an **unexpected** condition" | Matches Dell's own SYS494 text, "unknown internal error" |

This definitively refutes "EnvironmentMetrics is read-only by design": per §7.6 and §9.9.5.3 a
read-only property yields **400 + `PropertyNotWritable`**, which the Dell-native endpoint
demonstrates on the same controller. A read-only resource never yields 500.

Client-side explanations are exhausted. The Redfish session lifecycle behaves exactly as documented
(`POST /redfish/v1/SessionService/Sessions` -> `201 Created` + `X-Auth-Token` + `Location`;
`DELETE` -> `204`), the token authenticates GETs, and **the same token completes a PATCH against the
Dell-native endpoint with HTTP 200 in the same session**. Auth method, session vs basic, ETag /
concurrency control, OData headers, payload shape and endpoint choice have all been tested and
excluded.

The control object advertises `Allow: GET, PATCH` and the HMC reports healthy
(`Health: OK`, FW `GB200Nvl-25.08-B`, uptime ~12 days), so this is not a permissions or syntax
problem.

### Full 500 payload (no specific cause given)

```json
{"error": {"@Message.ExtendedInfo": [
  {"MessageId": "Base.1.18.InternalError", "Severity": "Critical",
   "MessageArgs": ["/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_0/EnvironmentMetrics"],
   "RelatedProperties": [],
   "Message": "The request failed due to an internal service error.  The service is still operational.",
   "Resolution": "Resubmit the request.  If the problem persists, consider resetting the service."},
  {"MessageId": "IDRAC.2.15.SYS494", "Severity": "Warning",
   "MessageArgs": ["/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_0/EnvironmentMetrics"],
   "RelatedProperties": [],
   "Message": "The request failed due to an internal service error. The service is still operational.",
   "Resolution": "Indicates that the request failed for an unknown internal error but that the service is still operational."}],
 "code": "Base.1.18.GeneralError"}}
```

`MessageArgs` only echoes the URI; `RelatedProperties` is empty; Dell's own SYS494 text says
"unknown internal error". Response header carries `Connection: close`.

### Controller versions

```
iDRAC FirmwareVersion   1.30.07.13      Model: "17G Monolithic"  => iDRAC10 (not iDRAC9)
RedfishVersion          1.22.1          Registries: Base.1.18, IDRAC.2.15
System                  PowerEdge XE9712, BIOS 1.0.3
HMC (HGX_BMC_0)         GB200Nvl-25.08-B
```

Note for doc lookups: `developer.dell.com/apis/2978/versions/7.xx/` is the **iDRAC9** API set
(7.xx firmware series). This is **iDRAC10**, which uses the 1.xx series — that spec does not
describe this controller.

### CONTROL TEST: a HEALTHY tray fails identically -> fleet-wide defect

The same idempotent PATCH set, run against a healthy tray and the capped one side by side:

```
HEALTHY i05-11 (GPU_0=1400, TotalGPU=5600)     CAPPED i05-30 (GPU_0=699, TotalGPU=4198)
  HGX  GPU_0/EnvironmentMetrics     -> 500       HGX  GPU_0/EnvironmentMetrics     -> 500
  HGX  ProcessorModule_0/EnvMetrics -> 500       HGX  ProcessorModule_0/EnvMetrics -> 500
  HGX  Controls/TotalGPU_Power_0    -> 500       HGX  Controls/TotalGPU_Power_0    -> 500
  DELL System.Embedded.1/Power      -> 200       DELL System.Embedded.1/Power      -> 200
```

**The HGX proxy write path is broken on every tray, not just the capped ones.** This separates two
independent problems:

1. **Stale 699 W on 36 trays** — a historical write, origin unknown.
2. **Broken HGX write path on all trays** — a current iDRAC firmware defect.

Consequences:

- Dell can reproduce this on **any healthy XE9712 with iDRAC10 1.30.07.13** — no capped tray needed.
- Since 699 W *was* successfully written to 36 trays at some point but no tray accepts writes now,
  the path must have worked previously. Consistent with (but not proof of) the
  `Last Firmware Update: 06/25/2026` breaking it.
- **Scope is bigger than these 36 trays:** any Redfish-based power management (DPS, power capping,
  workload power profiles) cannot function anywhere on this cluster until this is fixed.

### Firmware update considerations

```
INSTALLED   BMC Firmware Inventory   1.30.07.13
PREVIOUS    BMC Firmware Inventory   1.30.07.13   [Previous-113033-1.30.07.13__BMC]
```

**There is no rollback safety net** — the previous-image slot holds the *same* version, so an update
is forward-only and cannot be reverted to a known-good iDRAC.

`UpdateService.SimpleUpdate` is available OOB (HTTP/HTTPS/NFS/CIFS/TFTP; apply `Immediate`,
`OnReset`, or `OnStartUpdateRequest`), so the update itself is mechanically easy.

Recommendation: **open the Dell case before updating.** Ask which iDRAC10 build fixes PATCH on HGX
`EnvironmentMetrics`/`Controls`, rather than updating blind and risking another build with the same
defect across 36+ trays with no way back. Testing an update on one already-capped, drained tray in
parallel is reasonable — it is already degraded, and if the update re-initialises the HMC it may
clear the stale setpoint as a side effect.

Also worth confirming with Dell/NVIDIA: the HMC reports firmware **`GB200Nvl-25.08-B` on a GB300
system**. Possibly just bundle naming, but confirm the correct GB300 bundle is installed.

### Isolating the break: Dell-native writes work, HGX-proxied writes do not

Idempotent PATCH test — each request writes back the value the object *already* reports, so no
state changes:

```
Dell-native  Chassis/System.Embedded.1/Power        LimitInWatts=32767  -> HTTP 200
HGX proxied  Processors/GPU_0/EnvironmentMetrics    SetPoint=699        -> HTTP 500
HGX proxied  Chassis/HGX_ProcessorModule_0/EnvMetr  SetPoint=2900       -> HTTP 500
```

The Redfish write path on this iDRAC **works**; only the HGX/HMC-proxied resources fail.

This also rules out the "EnvironmentMetrics is read-only by design" reading:

- NVIDIA documents `PATCH /redfish/v1/Systems/HGX_Baseboard_0/Processors/{gpu_id}/EnvironmentMetrics`
  as the supported way to set a GPU power limit on **GB200/GB300** (`GPU_0`-`GPU_3`, four GPUs),
  with payload `{"PowerLimitWatts": {"SetPoint": N}}` — identical to what we sent.
  See <https://docs.nvidia.com/datacenter/dps/versions/latest/guides/concepts/redfish/>
- Redfish requires **405 Method Not Allowed** (and PATCH absent from `Allow`) when no write handler
  exists. This resource advertises `Allow: GET, PATCH` and then returns 500.
- The object exposes mutating POST actions (`ClearOOBSetPoint`, `ResetEDPp`) — read-only telemetry
  nodes do not.
- `GPU_0.SetPoint` is already non-default (699 vs 1400), so the property is writable by some path.

**Do not "fix" this via `Chassis/System.Embedded.1/Power`.** That is Dell's chassis *input* power
cap, a different mechanism: it would leave `GPU_0.SetPoint` at 699 and cannot clear the defect. It
currently reads `LimitInWatts: 32767` (disabled sentinel) with `LimitException: "HardPowerOff"`,
and `racadm get System.ServerPwr.PowerCapSetting` returns `RAC1122: not supported on the server`.
A low value there (e.g. 350 W) against a tray idling at **1029 W** / ~5.6 kW loaded would risk
hard-powering off the node.

### The failure never reaches the HMC (iDRAC-side defect)

Snapshotting the HMC journal, issuing the PATCH, then re-reading shows the request produces:

- **no new HMC journal entries** (newest stayed at 19:23:14 while the PATCH ran at ~19:40)
- **no iDRAC Lifecycle Controller entry** (only login/logout noise)
- **no Dell job** created

So the iDRAC rejects the write internally, *before* proxying to the HMC. Reads pass through to the
HMC correctly; writes die on the iDRAC side. This is an **iDRAC `1.30.07.13` firmware defect in the
write path for proxied `HGX_*` resources** — PATCH is advertised but appears unimplemented.

Caveat: the HMC journal does carry D-Bus errors (`DBus response error for HotPluggable: Invalid
request descriptor [generic:53]`, `Chassis and assembly are not connected through association`,
`Didn't find the inventory object`) but they are timestamped *before* our attempts and did not
recur during them — pre-existing noise, not the cause.

**A host reboot does not clear the setpoint** — `i05-30` rebooted at 06:07 and came back capped.
Note also that nothing is "pending activation": no write was ever accepted, so a power cycle would
only help if it clears the stale setpoint itself.

### Remaining options (in revised order)

1. **`racadm racreset`** on the iDRAC — matches Dell's own "consider resetting the service"
   Resolution, and the evidence points at the iDRAC, not the HMC.
2. **iDRAC firmware update** past `1.30.07.13`.
3. **AC power drain** of the tray (a real drain, not the warm reboot already proven ineffective).
4. **`Manager.Reset` on `HGX_BMC_0`** — deprioritised: the HMC is not the failing component.
5. **Dell/NVIDIA case** — most likely required. See the escalation summary below.

Caveat on BMC reachability: the BMCs sit on multiple subnets and not all are routable from the
head node (`10.40.149.x` is reachable; `10.40.147.x` is not). A remediation sweep needs a host with
routes to every BMC subnet, or must be driven from BCM.

## Escalation summary for Dell / NVIDIA

- 54 of 807 reachable GB300 XE9712 trays (36 of 543 when first measured) have a stale OOB GPU power setpoint of **699 W** on the two
  slot-0 GPUs (`GPU_0`, `GPU_1`), against a `DefaultSetPoint` of 1400 W. Tray rollup reads 4198 W vs
  a 5600 W default.
- Identical firmware on capped and healthy trays across **all 17 Redfish inventory components**.
- No hardware fault: BMC sensors, SEL, thermals, and a 100 ms-resolution load test all clean; the
  rail holds 694 W with sd 16.5 W and asserts **no** `hw_slowdown` / `hw_power_brake`.
- **Every Redfish write to clear the setpoint returns HTTP 500 `IDRAC.2.15.SYS494`** — including the
  vendor's own `ClearOOBSetPoint` action, on both the per-GPU and chassis objects, with both basic
  and session auth. Reads succeed. This is the blocking issue.
- The failing write **never reaches the HMC**: it generates no HMC journal entry, no iDRAC LC log
  entry, and no Dell job. The iDRAC rejects it locally despite advertising `Allow: GET, PATCH`.
  Points to an iDRAC `1.30.07.13` firmware defect rather than an HMC fault.
- **Dell-native Redfish writes succeed on the same controller** (idempotent PATCH to
  `Chassis/System.Embedded.1/Power` returns HTTP 200) — only HGX/HMC-proxied resources 500. So this
  is not a general Redfish, auth, or payload problem.
- **Reproducible on a HEALTHY tray**, not just capped ones — `i05-11` (GPU_0=1400, TotalGPU=5600)
  returns 500 on the identical no-op writes. Dell needs no special hardware state to reproduce.
- Payload verified against NVIDIA's own documentation: `{"PowerLimitWatts": {"SetPoint": N}}` on
  `Systems/HGX_Baseboard_0/Processors/GPU_<N>/EnvironmentMetrics`, documented expected response
  `200 OK` + `Base.1.18.1.Success`.
- **The request itself is provably correct**: a PATCH with an **empty JSON object** (`{}`), which
  modifies nothing, also returns 500. The failure is in the request handler, not the payload.
  Against DSP0266 v1.23.0 the HGX path violates §7.6 (empty-object PATCH; 400-for-unwritable),
  §7.5.3 (405 for unsupported operation), §6.2 (`Allow: GET, PATCH` then 500) and §9.9.5.3
  (`PropertyNotWritable` / `PropertyNotUpdated` absent) — while the **same controller** is fully
  conformant on `Chassis/System.Embedded.1/Power` (`{}`->200, unknown->400 `PropertyUnknown`,
  annotation-only->400 `NoOperation`).
- **No rollback available**: the previous-image slot holds the same `1.30.07.13`.
- **Blast radius beyond the 36 trays**: no Redfish-based power management (DPS, power capping,
  workload power profiles) can function on any tray in this cluster while this defect stands.
- Ask Dell: (a) is PATCH on `Systems/HGX_Baseboard_0/Processors/GPU_<N>/EnvironmentMetrics` and
  `Chassis/HGX_ProcessorModule_<N>/EnvironmentMetrics` implemented in **iDRAC10 1.30.07.13**
  (Model "17G Monolithic"), given NVIDIA documents both as the supported GB300 write path and the
  iDRAC advertises `Allow: GET, PATCH`; (b) which firmware fixes it; (c) the supported procedure to
  clear a stale `PowerLimitWatts.SetPoint` while the proxy write path 500s.
- Ask NVIDIA: what wrote 699 W to the slot-0 GPUs in the first place, and whether the HMC persists
  that setpoint across AC cycles.

## Suggested next steps

1. **Unblock the write path** — attempted on `i05-30`, all paths returned HTTP 500 (see BLOCKER).
   Next escalation rung, in order of increasing disruption: `Manager.Reset` on `HGX_BMC_0` ->
   `racadm racreset` -> AC drain of the tray -> Dell/NVIDIA case.
2. **Find what wrote 699 W.** Until that is identified the fix may be undone on the next
   commissioning run or power event. The tray's own logs cannot answer it (LC log reaches back to
   20-27 June with no setpoint-write event; use `racadm lclog view -n 5000`, since the default view
   truncates to 100 records). Ask the site team directly whether a power-budget policy or DPS-style tool was
   applied during bring-up, and check for a central record. Note `4198 x 18 trays = 75.6 kW/rack`
   vs `5600 x 18 = 100.8 kW`, consistent with a deliberate per-rack ceiling — but the 36 affected
   trays are scattered 1-4 per rack across 19 racks with healthy neighbours, so any such policy was
   applied per-tray, not per-rack.
3. **Re-sweep the j-row and i17** (321 nodes unreachable during this sweep) for the same setpoint.
4. **Audit the validation gate** for `Skip` handling (see the DCGM trap above) — otherwise this
   class of defect will keep passing acceptance silently.
5. **Interim mitigation** — until fixed, keep these 36 nodes out of performance-sensitive
   multi-node jobs; they are guaranteed stragglers.

No hardware action is required. Reseat / RMA / torque inspection are **not** indicated.

## How to reproduce the sweep

```bash
# per node
nvidia-smi --query-gpu=index,serial,power.limit,enforced.power.limit --format=csv

# a tray is affected when gpu0+gpu1 report enforced = 699 W while gpu2+gpu3 report 1400 W
```

## How to reproduce the load test

Drain the node first so nothing lands on it mid-test, and resume it afterwards.

```bash
# 1. hold the node (cmods has privilege for this on Nscale TX)
scontrol update nodename=<node> state=drain reason="power rail load test"

# 2. 100ms telemetry logger (background)
nvidia-smi --format=csv,nounits \
  --query-gpu=index,timestamp,power.limit,power.draw,power.draw.instant,pstate,clocks.gr,\
clocks.mem,utilization.gpu,temperature.gpu,clocks_throttle_reasons.active,\
clocks_throttle_reasons.sw_power_cap,clocks_throttle_reasons.hw_slowdown,\
clocks_throttle_reasons.hw_thermal_slowdown,clocks_throttle_reasons.hw_power_brake_slowdown,\
clocks_throttle_reasons.sw_thermal_slowdown \
  -lms 100 -f /tmp/smi-load.csv &

# 3. drive all 4 GPUs to their ceiling (no power pre-check, unlike dcgmi diag)
sudo dcgmproftester13 --no-dcgm-validation -t 1004 -d 120 -i 0,1,2,3

# 4. stop logger, clean up, return to service
scontrol update nodename=<node> state=resume reason="load test complete"
```

Note: `dcgmi diag -r targeted_power` is **not** suitable for exercising the capped GPUs — it skips
any GPU whose enforced limit is below 75% of the target. Use `dcgmproftester` instead.
