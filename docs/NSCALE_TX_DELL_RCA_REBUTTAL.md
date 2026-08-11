# Reply to Dell RCA — 699 W GPU cap on Nscale TX (PowerEdge XE9712 / GB300)

Prepared 2026-07-31. Test node `ink50-dh01-h22-17`, service tag `7Z7HKK4`.
Full investigation: `NSCALE_TX_GB300_SLOT0_POWER_CAP.md`

> **RESOLVED 2026-08-05 — do not send this rebuttal as-is.** AIC + Dell fixed 115 nodes using
> `ClearOOBSetPoint` + `PATCH PowerLimitWatts` after applying the **Dell iDRAC Datacenter License**.
> See also [`DELL_IDRAC_DATACENTER_LICENSE_HGX.md`](DELL_IDRAC_DATACENTER_LICENSE_HGX.md).
> The HTTP 500 documented at length below was **the license gate**, not a broken request handler —
> iDRAC returned `InternalError` where it should have returned a 402/403 license error. The
> conformance argument is still valid as a *defect report about the error code*, but the endpoints
> work once licensed. 6 trays remain capped (`h01-33`, `i17-11`, `f02-35`, `g07-30`, `g07-35`,
> `g07-37`) and the provisioning writer that sets 699 W has not been identified, so new racks are
> still arriving capped. See the main investigation doc for current status.

## Where we agree

- The **module-level limit is untouched and healthy** — all four GPUs report
  `Module Current Power Limit = 2900.00 W`, on both the capped and the healthy module.
- This is **not a hardware fault and not a thermal event**. Confirmed independently: GPU core
  voltages 0.900/0.895/0.900/0.890 V (lowest is an *uncapped* GPU), per-module power within 4 W,
  no `hw_power_brake` / `hw_slowdown` / thermal flags under a 120 s tensor load logged at 100 ms,
  and capped GPUs peak 20 C *cooler* than healthy ones at full load with normal temperature-per-watt.

## Where the RCA does not hold

### 1. The proposed fix does not work — run verbatim, just now

```
$ sudo nvidia-smi -pl 1400 --id 00000009:06:00.0
  Power limit for GPU 00000009:06:00.0 was set to 1400.00 W from 699.00 W.
  All done.

$ nvidia-smi --query-gpu=index,pci.bus_id,power.limit,enforced.power.limit --format=csv
  0, 00000008:06:00.0, 1400.00 W, 699.00 W
  1, 00000009:06:00.0, 1400.00 W, 699.00 W     <- UNCHANGED after the fix
  2, 00000018:06:00.0, 1400.00 W, 1400.00 W
  3, 00000019:06:00.0, 1400.00 W, 1400.00 W
```

Identical result on `00000008`. Also: `nvidia-smi -pl default` is **not valid syntax** on this
driver — `ERROR: Option default is invalid for -pl`.

### 1b. DECISIVE: `power.limit` tracks `-pl`; `enforced.power.limit` does not

Using `-pl` with explicit `--scope=0` (GPU scope) and writing a distinctive value, 1399 W:

```
                          power.limit   enforced.power.limit   Module Current Power Limit
before                       1400 W            699 W                    2900 W
after  -pl 1399 --scope=0    1399 W  <--       699 W                    2900 W
after  -pl 1400 --scope=0    1400 W            699 W                    2900 W
```

`nvidia-smi` reported: *"Power limit for GPU 00000009:06:00.0 was set to 1399.00 W from 699.00 W."*

This single test settles two questions:

- **`power.limit` is not the module limit.** It moved to 1399 W while the module reading stayed at
  2900 W. Two independent fields; one was changed without touching the other.
- **The cap did not come from `-pl`.** Writing 1399 W via the exact mechanism alleged to have caused
  the problem did not move `enforced.power.limit` at all. `-pl` owns `power.limit` and has no
  authority over `enforced.power.limit`.

### 1c. `nvidia-smi -pl default` is not valid syntax on this driver

Driver / NVML **580.126.20**. Every variant rejected:

```
-pl default            -> ERROR: Option default is invalid for -pl
-pl DEFAULT            -> ERROR: Option DEFAULT is invalid for -pl
--power-limit=default  -> ERROR: Option default is invalid for --power-limit=default
-pl 0                  -> rejected: "should be between 200.00 W and 1400.00 W"
-pl 2900 --scope=1     -> "set to 2900.00 W from 2900.00 W" (module already default; no effect)
```

The only reset switches this driver exposes are `-rgc` (reset GPU clocks) and `-rmc` (reset memory
clocks). **There is no power-limit reset option**, so RCA step 2 cannot be executed as written.

### 2. It cannot have been set by `nvidia-smi -pl 699`

- `-pl` writes the **`power.limit`** field. On every capped GPU `power.limit = 1400 W` — its
  default. Only `enforced.power.limit` is 699 W. A `-pl 699` would have left `power.limit = 699`.
- The proposed fix uses the **same mechanism as the alleged cause**. If a `-pl` write produced this
  state, a `-pl` write would clear it. It does not — nvidia-smi reports success and the enforced
  value does not move.

### 3. Persistence mode does not persist `-pl` settings

Persistence mode keeps the driver loaded; it does not preserve power limits across a reboot.
Dell's own XE9680 power-capping paper states of `nvidia-smi -pl`: *"This setting is not persistent
and will reset after a reboot."*

Empirically: `h22-17` booted `2026-07-29 20:34:59` and is still capped; `i05-30` rebooted
`2026-07-30 06:07` and came back at 699 W. All originally identified trays have stayed capped across reboots for the
entire retained telemetry window.

### 4. The `power.limit` field semantics are inverted in the RCA

The RCA states `power.limit` "reflects the module-level board limit". It does not — the module
limit is a **separate readout** and reads 2900 W:

```
00000008:06:00.0  GPU    Current Power Limit =  699.00 W
00000008:06:00.0  Module Current Power Limit = 2900.00 W
```

NVML: `power.limit` = software limit currently set (what `-pl` writes);
`enforced.power.limit` = effective limit = `min(software, board/OOB/thermal)`.
Neither is the module limit. `1400 != 2900`, so these are demonstrably different fields.

### 5. Scope is larger than one GPU on one node

Not "GPU1". It is **both** GPUs of the slot-0 module (`00000008` *and* `00000009`) on **54 trays =
108 GPUs**, across **30 racks**, while `00000018`/`00000019` remain at 1400 W on the same boards.

**Scope update 2026-07-31.** A re-sweep of the now-864-node cluster finds **54 affected trays of
807 reachable (6.7%)**, not the 36 of 543 originally reported. All 36 original trays are still
capped, plus 18 more — 16 of them in the newly commissioned `j` racks. 57 nodes are `down*`/`drain*`
and could not be assessed, so 54 is a floor. Full list and service tags:
`NSCALE_TX_CAPPED_SERVICE_TAGS.md`.
The proposed command targets only one of the two. Cluster-wide sweep of 543 reachable trays: exactly
54 affected, always gpu0+gpu1, always exactly 699 W, no other value anywhere.

### 6. An *in-band* `nvidia-smi` script cannot produce this signature

Such a script would have to simultaneously: land in `enforced.power.limit` but **not** `power.limit`;
survive reboots; be immune to `-pl`; and target exactly gpu0+gpu1 on 54 of 807 trays scattered
across 30 racks — each with healthy neighbours on the same busbar. No `-pl` / NVML-based script has
any of those properties. This rules out the specific cause the RCA proposes.

It does **not** rule out an *out-of-band* provisioning write, which has all four properties. See
below — that is now our leading hypothesis.

## What we believe it actually is

**Updated 2026-07-31.** We previously proposed the power-smoothing ramp-up defect fixed in GPU VBIOS
`97.10.4A.00.32` (DGX GB300 NVL72 release 1.0.10). **We have since tested that on the hardware and
it does not hold** — reported here in full because it also narrows the field usefully.

Power smoothing is **disabled on every capped tray** (checked across all 36 known at the time of the
sweep, 144 GPUs) and on healthy controls, whether
queried in band or through the HMC:

```
$ sudo nvidia-smi power-smoothing -i 1 --state=DISABLED
Power Smoothing is already disabled on this device

/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_<N>/Oem/Nvidia/PowerSmoothing
  Enabled                            false      (all 4 GPUs, capped and healthy)
  RemainingLifetimeCircuitryPercent  100.0      (circuitry has never engaged)
  RampUp/RampDownWattsPerSecond      20.0 / 20.0   identical capped vs healthy
  TMPFloorPercent                    89.990234375  identical capped vs healthy
```

The release-note improvement is conditioned on power smoothing ramp-up being *enabled*, and power
smoothing acts on the TMP floor and ramp rates — not on `PowerLimitWatts.SetPoint`, where the 699 W
actually sits. Wrong feature, and switched off.

**What the data does point to: a persistent out-of-band power-limit write.**

```
                        GPU_0    GPU_1    GPU_2    GPU_3
capped tray i05-30
  PowerLimitWatts.SetPoint          699      699      1400     1400
  Oem.Nvidia.PowerLimitPersistency  True     True     False    False
  RequestedPersistentPowerLimitWatts 699.0   699.0    null     null

healthy trays h01-11 / h02-11 / h05-11
  PowerLimitWatts.SetPoint          1400     1400     1400     1400
  Oem.Nvidia.PowerLimitPersistency  True     True     True     True
  RequestedPersistentPowerLimitWatts 1400.0  1400.0   1400.0   1400.0
```

Healthy trays carry an explicit **persistent 1400 W on all four GPUs** — so a persistent OOB power
limit was written across the fleet. On the affected trays that pass wrote **699 W to the slot-0
pair and never wrote the slot-1 pair at all**. That signature — a uniform wrong value on the first
module, no write on the second — reads as a misparameterised or interrupted provisioning write, and
it explains the identical 699 integer across 19 racks far better than an intermittent fault would.

This is consistent with everything above: it is applied out of band, which is exactly why
`-pl 1400` reports success while the enforced limit stays at 699.

## Requests

1. **Which Dell or NVIDIA tooling performs the persistent OOB GPU power-limit write during
   commissioning/provisioning of XE9712 trays?** We need to identify the writer and see its logs —
   the iDRAC Lifecycle Controller log only reaches back to 20–27 June and contains no such event.
2. Given healthy trays show `RequestedPersistentPowerLimitWatts = 1400.0` on all four GPUs and the
   affected trays show 699.0 on slot-0 and null on slot-1, **is a partial/failed write a known
   failure mode of that tooling?**
3. What is the supported way to clear or rewrite a persistent OOB setpoint given item 4 below?
4. Separate defect, same platform: **every Redfish PATCH to an HGX-proxied resource returns HTTP 500**
   on iDRAC10 `1.30.07.13`, including NVIDIA's documented
   `PATCH /redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_<N>/EnvironmentMetrics`
   and the OEM `ClearOOBSetPoint` action. Reproducible on a **healthy** tray with an **empty** JSON
   body `{}`, while the same empty PATCH returns 200 on `Chassis/System.Embedded.1/Power`. Details
   and DSP0266 conformance analysis in the main report.
5. Third defect, one tray: on `h06-16` (service tag `1Q7HKK4`, BMC `10.40.145.25`)
   `/redfish/v1/Systems` lists `HGX_Baseboard_0`, but **every child resource under it returns 404** —
   `Processors`, all four `GPU_<N>/EnvironmentMetrics`, all `PowerSmoothing`. The other 35 affected
   trays expose the full tree from the same client and credentials. In band the tray is
   indistinguishable from its peers.
6. Lower priority, but still open: which Dell XE9712 firmware bundle carries GPU VBIOS
   `97.10.4A.00.32` + HMC `GB200Nvl-25.09-4`? We are behind on both (`97.10.4A.00.1F` /
   `25.08-B`) and want to close the gap regardless of its relationship to this issue.

## Affected service tags (54, as of 2026-07-31)

```
464HKK4,BC4HKK4,4K3HKK4,5S1HKK4,14BHKK4,GM9HKK4,1T7HKK4,1Q7HKK4,B08HKK4,308HKK4,
5CBHKK4,C87HKK4,1M4HKK4,1C7HKK4,HM4HKK4,J84HKK4,G18HKK4,J38HKK4,CW7HKK4,7Z7HKK4,
JQ1HKK4,618HKK4,DH3HKK4,2H4HKK4,FT3HKK4,3R9HKK4,9TBHKK4,6K7HKK4,304HKK4,344HKK4,
5D4HKK4,853HKK4,GYSZPK4,3FRZPK4,5N7HKK4,337HKK4,614HKK4,3R3HKK4,7Q7HKK4,GV3HKK4,
4H8HKK4,354HKK4,673HKK4,5K3HKK4,7C4HKK4,F64HKK4,B27HKK4,367HKK4,817HKK4,G0VZPK4,
JY3HKK4,JH7HKK4,JD9HKK4,FT8HKK4
```

The 18 identified on 2026-07-31 (16 in the newly commissioned `j` racks):

```
5D4HKK4,853HKK4,7Q7HKK4,GV3HKK4,4H8HKK4,354HKK4,673HKK4,5K3HKK4,7C4HKK4,
F64HKK4,B27HKK4,367HKK4,817HKK4,G0VZPK4,JY3HKK4,JH7HKK4,JD9HKK4,FT8HKK4
```

Node-to-tag-to-BMC mapping and module serials: `NSCALE_TX_CAPPED_SERVICE_TAGS.md`.
