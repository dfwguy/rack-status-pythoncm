# Nscale TX — Dell-requested triage of the 699 W slot-0 GPU cap

> **RESOLVED 2026-08-05.** AIC + Dell cleared 115 nodes with `ClearOOBSetPoint` +
> `PATCH PowerLimitWatts` after applying the **Dell iDRAC Datacenter License** — the license gate was
> the reason every write in this document returned HTTP 500. 6 trays remain capped and the
> provisioning writer is still unidentified. Current status in
> [`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md).
> Reusable checklist (any Dell HGX site):
> [`DELL_IDRAC_DATACENTER_LICENSE_HGX.md`](DELL_IDRAC_DATACENTER_LICENSE_HGX.md).

**Date:** 2026-07-31 · **Cluster:** Nscale TX / `ink50-dh01` · **Node:** `ink50-dh01-i05-30`
(service tag `9TBHKK4`, BMC `10.40.149.12`) · **GPU asked about:** PCI `00000009:06:00.0` = `GPU_1`

Ran the four checks Dell requested. Steps 1–3 (in-band `nvidia-smi`) confirm the clamp but cannot
root-cause it. Step 4 (iDRAC Redfish) does: a **persistent out-of-band power limit written to
`GPU_0`/`GPU_1`**.

**Scope as of 2026-07-31: 54 affected trays of 807 reachable (6.7%), across 30 racks** — up from the
36 first reported, because the cluster grew from 543 to 864 nodes and 16 of the 18 newly identified
trays are in the new `j` racks. 57 nodes are `down*`/`drain*` and unassessed, so 54 is a floor.
Node list, service tags and BMC IPs: [`NSCALE_TX_CAPPED_SERVICE_TAGS.md`](NSCALE_TX_CAPPED_SERVICE_TAGS.md).

Background and prior evidence: [`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md).

## GPU map on this tray

```
index, pci.bus_id,        name,         serial
0,     00000008:06:00.0,  NVIDIA GB300, 1765225793151   <- slot-0 module (capped)
1,     00000009:06:00.0,  NVIDIA GB300, 1765225793151   <- slot-0 module (capped), the GPU Dell asked about
2,     00000018:06:00.0,  NVIDIA GB300, 1765025792048   <- slot-1 module (healthy)
3,     00000019:06:00.0,  NVIDIA GB300, 1765025792048   <- slot-1 module (healthy)
```

GPUs 0/1 share a serial and GPUs 2/3 share a serial — the split is **per superchip module**, not per GPU.

## Step 1 — enforced limit vs default/max

```
$ nvidia-smi -q -d POWER --id 00000009:06:00.0 | grep -i "power limit"
        Current Power Limit                            : 699.00 W
        Requested Power Limit                          : 699.00 W
        Default Power Limit                            : 1400.00 W
        Min Power Limit                                : 200.00 W
        Max Power Limit                                : 1400.00 W
        Current Power Limit                            : 2900.00 W    <- module-level
        Requested Power Limit                          : 2900.00 W
        Default Power Limit                            : 2900.00 W
        Min Power Limit                                : 500.00 W
        Max Power Limit                                : 2900.00 W
```

**Confirmed.** Enforced 699 W, default and max both 1400 W. The module-level budget is untouched at
the factory 2900 W, so nothing is being taken away at the module tier.

## Step 2 — is the cap software-set?

```
$ nvidia-smi --query-gpu=power.limit,power.management --format=csv --id 00000009:06:00.0
power.limit [W], power.management
1400.00 W, Enabled
```

All four GPUs, for contrast:

```
index, pci.bus_id,       power.limit, power.default_limit, power.max_limit, power.min_limit, enforced.power.limit, power.management
0,     00000008:06:00.0, 1400.00 W,   1400.00 W,           1400.00 W,       200.00 W,        699.00 W,             Enabled
1,     00000009:06:00.0, 1400.00 W,   1400.00 W,           1400.00 W,       200.00 W,        699.00 W,             Enabled
2,     00000018:06:00.0, 1400.00 W,   1400.00 W,           1400.00 W,       200.00 W,        1400.00 W,            Enabled
3,     00000019:06:00.0, 1400.00 W,   1400.00 W,           1400.00 W,       200.00 W,        1400.00 W,            Enabled
```

**Dell's inference does not hold here.** Their rule was "`power.management` Supported + limit 699 W
=> software-set". But the software-visible `power.limit` is already **1400 W** while
`enforced.power.limit` is **699 W**. The driver is asking for full TGP and something below it is
clamping. That is the opposite of a software-set limit.

This matches the earlier decisive test: `sudo nvidia-smi -i 0 -pl 1400` reports
`"was set to 1400.00 W from 699.00 W. All done."` and `enforced.power.limit` still reads 699 W.

## Step 3 — persistent `nvidia-smi -pl`?

```
$ sudo nvidia-smi -pm 1
Persistence mode is already Enabled for GPU 00000008:06:00.0.
Persistence mode is already Enabled for GPU 00000009:06:00.0.
Persistence mode is already Enabled for GPU 00000018:06:00.0.
Persistence mode is already Enabled for GPU 00000019:06:00.0.
All done.
```

Default 1400 W > enforced 699 W, which by Dell's step-3 criterion would suggest a manual `-pl`.
It is not one:

- `power.limit` (the value a `-pl` would move) is **1400 W**, not 699 W.
- No boot-time setter — no unit, cron, or rc script applies `-pl` on these nodes.
- The same 699 W appears on **54 trays across 30 racks** as an identical integer, always on the
  slot-0 pair. A hand-applied `-pl` does not reproduce like that.

Also ruled out at this layer:

```
    Workload Power Profiles
        Requested Profiles                             : N/A
        Enforced Profiles                              : N/A

    Clocks Event Reasons
        SW Power Cap                                   : Not Active
        HW Power Brake Slowdown                        : Not Active
```

No workload power profile is applied, and no throttle reason is active. (`SW Power Capping` shows a
nonzero *cumulative* counter, but the *active* flag is Not Active — that counter is historical.)

## Step 4 — iDRAC / BMC GPU power policy

This is where the cap is visible. Read over Redfish from the head node against BMC `10.40.149.12`.

### Per-GPU `EnvironmentMetrics`

`/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_<N>/EnvironmentMetrics`

| | GPU_0 | GPU_1 | GPU_2 | GPU_3 |
|---|---|---|---|---|
| `PowerLimitWatts.SetPoint` | **699** | **699** | 1400 | 1400 |
| `PowerLimitWatts.DefaultSetPoint` | 1400 | 1400 | 1400 | 1400 |
| `PowerLimitWatts.AllowableMax` | 1400 | 1400 | 1400 | 1400 |
| `PowerLimitWatts.ControlMode` | Automatic | Automatic | Automatic | Automatic |
| `Oem.Nvidia.PowerLimitPersistency` | **True** | **True** | False | False |
| `Oem.Nvidia.RequestedPersistentPowerLimitWatts` | **699.0** | **699.0** | null | null |
| `Oem.Nvidia.RequestedOneshotPowerLimitWatts` | **699.0** | **699.0** | null | null |

The two capped GPUs carry a **persistent** OOB request for 699 W. The two healthy GPUs on the same
board have `PowerLimitPersistency = False` and null requests. This is a written configuration value.

### Chassis rollup

`/redfish/v1/Chassis/HGX_Chassis_0/Controls/TotalGPU_Power_0`

```
Name             System Power Control
SetPoint         4198      <- 699 + 699 + 1400 + 1400
DefaultSetPoint  5600      <- 4 x 1400
AllowableMax     5600
AllowableMin     800
ControlMode      Override  <- not Automatic
ControlType      Power
```

### Not a Dell chassis-level cap

`/redfish/v1/Chassis/System.Embedded.1/Power`

```
PowerControl[0]  System Power Control
  PowerLimit.LimitInWatts   32767      <- effectively uncapped
  PowerLimit.LimitException HardPowerOff
  PowerConsumedWatts        1029
```

`/redfish/v1/Chassis/System.Embedded.1/Controls` -> **404** (does not exist on this platform).

### Not a module-level cap

```
/redfish/v1/Chassis/HGX_ProcessorModule_0/Controls/HGX_ProcessorModule_0
  SetPoint 2900   AllowableMax 2900   ControlMode Automatic
/redfish/v1/Chassis/HGX_ProcessorModule_1/Controls/HGX_ProcessorModule_1
  SetPoint 2900   AllowableMax 2900   ControlMode Automatic
```

Both modules sit at the factory 2900 W. The 1402 W shortfall is applied per GPU, not per module.

### The vendor clear action exists but cannot be invoked

`GPU_1/EnvironmentMetrics` advertises:

```json
{
  "Oem": {
    "#NvidiaEnvironmentMetrics.ClearOOBSetPoint": {
      "target": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_1/EnvironmentMetrics/Actions/Oem/NvidiaEnvironmentMetrics.ClearOOBSetPoint"
    },
    "#NvidiaEnvironmentMetrics.ResetEDPp": {
      "target": "/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_1/EnvironmentMetrics/Actions/Oem/NvidiaEnvironmentMetrics.ResetEDPp"
    }
  }
}
```

Every write to it — and every `PATCH` to any HGX-proxied resource — returns **HTTP 500**
`Base.1.18.InternalError` / `IDRAC.2.15.SYS494`, including on healthy trays. Reads work. Detail and
the empty-`{}` proof are in
[`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md).

## Conclusion

The in-band commands Dell asked for confirm the symptom but cannot localize it — they show a 699 W
clamp sitting under a healthy 1400 W default, with the driver already requesting 1400 W.

The iDRAC data localizes it. Three things are true on this tray:

1. **The cap is an out-of-band, persistent GPU power limit** on `GPU_0`/`GPU_1`
   (`PowerLimitPersistency = True`, `RequestedPersistentPowerLimitWatts = 699.0`), surfacing as
   chassis `TotalGPU_Power_0` in `Override` mode at 4198 W against a 5600 W default.
2. **It is not** a software `nvidia-smi -pl`, not a workload power profile, not a Dell
   `System.Embedded.1` power cap, and not a module-level budget — all four measured and excluded.
3. **We cannot clear it ourselves.** NVIDIA's own `ClearOOBSetPoint` action and the documented
   `PATCH` both return HTTP 500 on iDRAC10 `1.30.07.13`, on capped and healthy trays alike.

This triage identifies *where the cap lives*, not *what wrote it*.

**Origin — updated 2026-07-31.** We had proposed the power-smoothing ramp-up defect fixed in GPU
VBIOS `97.10.4A.00.32`. A sweep of every known capped tray plus healthy controls ruled that out: power
smoothing is `Enabled: false` everywhere with `RemainingLifetimeCircuitryPercent` at 100.0, and it
does not write `PowerLimitWatts.SetPoint` in any case. The sweep found the real discriminator —
healthy trays carry a persistent OOB limit of **1400 W on all four GPUs**, while the capped trays
carry **699 W on slot-0 and no persistent write at all on slot-1**, which points at a
misparameterised or interrupted provisioning write. Full evidence in
[`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md), section
"Power Smoothing swept across all 36 capped trays".

## Asks for Dell

1. Which Dell or NVIDIA tooling performs the **persistent out-of-band GPU power-limit write** during
   XE9712 commissioning, and can we see its logs? Healthy trays show
   `RequestedPersistentPowerLimitWatts = 1400.0` on all four GPUs; the affected trays show 699.0 on
   slot-0 and null on slot-1. Is a partial write a known failure mode?
2. Is `#NvidiaEnvironmentMetrics.ClearOOBSetPoint` implemented in iDRAC10 `1.30.07.13`, and is the
   HTTP 500 on all HGX-proxied `PATCH`/`POST` a known defect? It is reproducible in two curl
   commands on any healthy XE9712 in this cluster.
3. Is there a supported out-of-band path to clear a stale `PowerLimitWatts.SetPoint` while that
   write path returns 500?
4. Which Dell XE9712 firmware bundle carries GPU VBIOS `97.10.4A.00.32` + HMC `GB200Nvl-25.09-4`?
   We are behind on both and want to close the gap regardless of its relationship to this issue.

## Reproduce

In band, on the node:

```bash
nvidia-smi --query-gpu=index,pci.bus_id,name,serial --format=csv
nvidia-smi -q -d POWER --id 00000009:06:00.0 | grep -i "power limit"
nvidia-smi --query-gpu=power.limit,power.management --format=csv --id 00000009:06:00.0
nvidia-smi --query-gpu=index,pci.bus_id,power.limit,power.default_limit,power.max_limit,\
power.min_limit,enforced.power.limit,power.management --format=csv
sudo nvidia-smi -pm 1
nvidia-smi -q --id 00000009:06:00.0 | grep -iE "Power Limit|Persisten|Profile|Requested|Enforced"
```

Out of band, against the tray BMC (reachable from the BCM head, not from the OVA):

```bash
for N in 0 1 2 3; do
  curl -sk -u "$BMC_USER:$BMC_PASSWORD" \
    "https://$BMC/redfish/v1/Systems/HGX_Baseboard_0/Processors/GPU_$N/EnvironmentMetrics" \
    | jq '{SetPoint: .PowerLimitWatts.SetPoint,
           Default: .PowerLimitWatts.DefaultSetPoint,
           Persist: .Oem.Nvidia.PowerLimitPersistency,
           Requested: .Oem.Nvidia.RequestedPersistentPowerLimitWatts}'
done
curl -sk -u "$BMC_USER:$BMC_PASSWORD" \
  "https://$BMC/redfish/v1/Chassis/HGX_Chassis_0/Controls/TotalGPU_Power_0" \
  | jq '{SetPoint, DefaultSetPoint, ControlMode}'
curl -sk -u "$BMC_USER:$BMC_PASSWORD" \
  "https://$BMC/redfish/v1/Chassis/System.Embedded.1/Power" \
  | jq '.PowerControl[0].PowerLimit'
```

Credentials live in `/opt/observability/nscaletx/exporters/.env` on the OVA (`BMC_USER`,
`BMC_PASSWORD`) — the BMC subnet is reachable from the BCM head at `192.168.10.2`, not from the OVA.
