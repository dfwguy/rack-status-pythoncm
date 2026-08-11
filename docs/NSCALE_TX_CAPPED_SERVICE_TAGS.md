# Nscale TX — service tags for the trays with the 699 W slot-0 GPU cap

> **RESOLVED 2026-08-05 — 115 nodes remediated by AIC + Dell** via `ClearOOBSetPoint` +
> `PATCH PowerLimitWatts`, unlocked by the **Dell iDRAC Datacenter License** (the missing license was
> why every Redfish write previously returned HTTP 500). The tag lists below are historical.
> Operator runbook for the license gate:
> [`DELL_IDRAC_DATACENTER_LICENSE_HGX.md`](DELL_IDRAC_DATACENTER_LICENSE_HGX.md).
>
> **6 trays still capped** as of 2026-08-05 22:45 UTC, verified in band and over Redfish:
>
> | Node | Service tag | BMC | Note |
> |---|---|---|---|
> | h01-33 | 464HKK4 | 10.40.144.15 | known since the first survey — remediation coverage gap |
> | i17-11 | 5D4HKK4 | 10.40.150.38 | known; HGX Redfish tree also unreadable |
> | f02-35 | 778HKK4 | 10.40.136.35 | new rack — defect still live on new capacity |
> | g07-30 | DT7HKK4 | 10.40.141.48 | new rack |
> | g07-35 | 2M9HKK4 | 10.40.141.53 | new rack |
> | g07-37 | 6C9HKK4 | 10.40.141.55 | new rack |
>
> ```
> 778HKK4,DT7HKK4,2M9HKK4,6C9HKK4,464HKK4,5D4HKK4
> ```
>
> AIC runs a twice-daily sweep; logs on the head at `/home/nvis/logs/gpu-throttle/`.

**Updated 2026-07-31: the count is now 54, not 36.** All trays are **Dell PowerEdge XE9712**.
Full analysis: `NSCALE_TX_GB300_SLOT0_POWER_CAP.md`

## Current fleet state (in-band sweep, 2026-07-31)

```
864  Slurm nodes total
807  reachable / probed
 57  unreachable (54 down*, 3 drain*) — NOT assessed, so 54 is a floor, not a ceiling

751  healthy     enforced.power.limit = 1400,1400,1400,1400
 54  CAPPED      enforced.power.limit =  699, 699,1400,1400   <- always the slot-0 pair
  2  anomalous   only 3 GPUs enumerated (see below — unrelated defect)
```

54 of 807 reachable trays = **6.7%**. 108 GPUs at half TGP ≈ **75.7 kW** of provisioned GPU
capacity unavailable (54 × 1402 W).

No tray anywhere shows 699 W in any other GPU position — the signature is perfectly uniform.

### Growth since the first sweep

The earlier sweep found 36 of 543 trays. The cluster has since grown to 864 Slurm nodes, and
**all 36 original trays are still capped** (none self-healed) plus **18 new ones**:

```
i17-11,i17-36,j01-14,j01-29,j01-31,j02-18,j03-35,j05-35,j06-11,
j08-12,j16-36,j17-32,j17-35,j20-13,j20-18,j21-11,j21-13,j21-30
```

16 of the 18 are in the **`j` racks**, which did not exist in the earlier sweep. The defect is
therefore **reproducing on newly commissioned capacity**, which supports the provisioning-write
hypothesis rather than a wear/degradation mechanism.

## Service tags (comma-separated, for the Dell case)

All 54:

```
464HKK4,BC4HKK4,4K3HKK4,5S1HKK4,14BHKK4,GM9HKK4,1T7HKK4,1Q7HKK4,B08HKK4,308HKK4,
5CBHKK4,C87HKK4,1M4HKK4,1C7HKK4,HM4HKK4,J84HKK4,G18HKK4,J38HKK4,CW7HKK4,7Z7HKK4,
JQ1HKK4,618HKK4,DH3HKK4,2H4HKK4,FT3HKK4,3R9HKK4,9TBHKK4,6K7HKK4,304HKK4,344HKK4,
5D4HKK4,853HKK4,GYSZPK4,3FRZPK4,5N7HKK4,337HKK4,614HKK4,3R3HKK4,7Q7HKK4,GV3HKK4,
4H8HKK4,354HKK4,673HKK4,5K3HKK4,7C4HKK4,F64HKK4,B27HKK4,367HKK4,817HKK4,G0VZPK4,
JY3HKK4,JH7HKK4,JD9HKK4,FT8HKK4
```

The 18 newly identified ones only:

```
5D4HKK4,853HKK4,7Q7HKK4,GV3HKK4,4H8HKK4,354HKK4,673HKK4,5K3HKK4,7C4HKK4,
F64HKK4,B27HKK4,367HKK4,817HKK4,G0VZPK4,JY3HKK4,JH7HKK4,JD9HKK4,FT8HKK4
```

## Full mapping

`slot0` = `GPU_0`/`GPU_1` superchip (capped at 699 W) · `slot1` = `GPU_2`/`GPU_3` (normal 1400 W)

| Node | Service tag | BMC IP | slot0 module serial (699 W) | slot1 module serial (1400 W) |
|---|---|---|---|---|
| h01-33 | 464HKK4 | 10.40.144.15 | 1760426792748 | 1761126796657 |
| h03-16 | BC4HKK4 | 10.40.144.43 | 1761126796690 | 1760426793861 |
| h03-29 | 4K3HKK4 | 10.40.144.47 | 1650526037376 | 1761026792109 |
| h03-34 | 5S1HKK4 | 10.40.144.52 | 1760426795466 | 1760426795741 |
| h04-17 | 14BHKK4 | 10.40.144.62 | 1655025072549 | 1655225126954 |
| h04-34 | GM9HKK4 | 10.40.144.70 | 1654925110868 | 1654725038108 |
| h06-14 | 1T7HKK4 | 10.40.145.23 | 1762925791509 | 1653125029639 |
| h06-16 | 1Q7HKK4 | 10.40.145.25 | 1763225791599 | 1653125065039 |
| h06-18 | B08HKK4 | 10.40.145.27 | 1653125007955 | 1653325033572 |
| h06-34 | 308HKK4 | 10.40.145.34 | 1653325077479 | 1653325078175 |
| h08-16 | 5CBHKK4 | 10.40.145.61 | 1654925111012 | 1654925001557 |
| h17-14 | C87HKK4 | 10.40.146.41 | 1653325079906 | 1652925029175 |
| h17-29 | 1M4HKK4 | 10.40.146.47 | 1760426794514 | 1761126798217 |
| h18-34 | 1C7HKK4 | 10.40.146.70 | 1763025792857 | 1652725048609 |
| h20-11 | HM4HKK4 | 10.40.147.20 | 1760426793859 | 1760426800698 |
| h20-28 | J84HKK4 | 10.40.147.28 | 1760426793913 | 1761126791875 |
| h20-34 | G18HKK4 | 10.40.147.34 | 1653625063892 | 1652925028196 |
| h21-29 | J38HKK4 | 10.40.147.47 | 1653125003342 | 1653125002954 |
| h22-15 | CW7HKK4 | 10.40.147.60 | 1653125123613 | 1653125030302 |
| h22-17 | 7Z7HKK4 | 10.40.147.62 | 1953425791522 | 1763325796353 |
| i02-15 | JQ1HKK4 | 10.40.148.24 | 1760426793967 | 1760426793767 |
| i02-30 | 618HKK4 | 10.40.148.30 | 1653125008325 | 1653125002267 |
| i03-36 | DH3HKK4 | 10.40.148.54 | 1650426065396 | 1650426068385 |
| i04-14 | 2H4HKK4 | 10.40.148.59 | 1760426800297 | 1761126801272 |
| i04-32 | FT3HKK4 | 10.40.148.68 | 1760426801510 | 1650526037381 |
| i05-30 | 3R9HKK4 | 10.40.149.12 | 1765225793151 | 1765025792048 |
| i05-31 | 9TBHKK4 | 10.40.149.13 | 1655025071966 | 1765025792356 |
| i08-11 | 6K7HKK4 | 10.40.149.56 | 1653125030561 | 1653125066262 |
| i16-11 | 304HKK4 | 10.40.150.20 | 1650426066555 | 1650426040805 |
| i16-28 | 344HKK4 | 10.40.150.28 | 1761126793816 | 1761126795725 |
| **i17-11** | **5D4HKK4** | 10.40.150.38 | 1760426800637 | 1761126796986 |
| **i17-36** | **853HKK4** | 10.40.150.54 | 1650426066624 | 1650426067106 |
| i18-12 | GYSZPK4 | 10.40.150.57 | 1760926799379 | 1761126797229 |
| i18-35 | 3FRZPK4 | 10.40.150.71 | 1650926061866 | 1761026791968 |
| i19-12 | 5N7HKK4 | 10.40.151.3  | 1653325078987 | 1653125002100 |
| i19-14 | 337HKK4 | 10.40.151.5  | 1653325035772 | 1653325043244 |
| i20-14 | 614HKK4 | 10.40.151.23 | 1760426799351 | 1761126798257 |
| i20-33 | 3R3HKK4 | 10.40.151.33 | 1650426065501 | 1761026792252 |
| **j01-14** | **7Q7HKK4** | 10.40.152.5  | 1653125064205 | 1763225791958 |
| **j01-29** | **GV3HKK4** | 10.40.152.11 | 1760426791760 | 1761026791450 |
| **j01-31** | **4H8HKK4** | 10.40.152.13 | 1653125003313 | 1653125034258 |
| **j02-18** | **354HKK4** | 10.40.152.27 | 1761126793336 | 1760426794495 |
| **j03-35** | **673HKK4** | 10.40.152.53 | 1650426067431 | 1650426066560 |
| **j05-35** | **5K3HKK4** | 10.40.153.17 | 1650526036870 | 1650426065344 |
| **j06-11** | **7C4HKK4** | 10.40.153.20 | 1761026798189 | 1760426792140 |
| **j08-12** | **F64HKK4** | 10.40.153.57 | 1760526792087 | 1761126801615 |
| **j16-36** | **B27HKK4** | 10.40.154.36 | 1652925030232 | 1653125064238 |
| **j17-32** | **367HKK4** | 10.40.154.50 | 1653225032469 | 1765225798476 |
| **j17-35** | **817HKK4** | 10.40.154.53 | 1653325043455 | 1652725048590 |
| **j20-13** | **G0VZPK4** | 10.40.155.22 | 1652925038578 | 1761126796498 |
| **j20-18** | **JY3HKK4** | 10.40.155.27 | 1760526791742 | 1650426065565 |
| **j21-11** | **JH7HKK4** | 10.40.155.38 | 1653125065526 | 1653125008807 |
| **j21-13** | **JD9HKK4** | 10.40.155.40 | 1653125063761 | 1654925110033 |
| **j21-30** | **FT8HKK4** | 10.40.155.48 | 1762725800108 | 1763225790770 |

**Bold** rows are the 18 newly identified on 2026-07-31.

## Distribution by rack

No rack is wholly affected, and every affected rack also contains healthy trays.

```
4  h06
3  h03  h20  j01  j21
2  h04  h17  h22  i02  i04  i05  i16  i17  i18  i19  i20  j17  j20
1  h01  h08  h18  h21  i03  i08  j02  j03  j05  j06  j08  j16
```

30 racks across the `h`, `i` and `j` rows.

## NOT a manufacturing-lot pattern

The `HKK4` / `ZPK4` tag suffixes look like a batch signal but are not — a healthy control sample
shows the same distribution:

```
capped set      52 x HKK4,  2 x ZPK4   (96% / 4%)
healthy sample  17 x HKK4,  3 x ZPK4   (85% / 15%)
```

Both suffixes are cluster-wide manufacturing lots. Do not present this as a correlation.

Module serials are likewise **not** a batch signal — capped slot-0 serials span every production
batch, and healthy trays carry slot-0 modules with adjacent serials (healthy `h22-11` slot0
`1653125003275` vs capped `h21-29` slot0 `1653125003342`).

## Two unrelated anomalies found in the same sweep

Worth raising with Dell separately; neither is a power-cap issue.

- **`i17-34` and `j21-32` enumerate only 3 GPUs** instead of 4.
- **`h06-16` (`1Q7HKK4`) has a broken Redfish tree.** `/redfish/v1/Systems` lists
  `HGX_Baseboard_0`, but every child 404s — `Processors`, all four `GPU_<N>/EnvironmentMetrics`,
  all `PowerSmoothing`. The other 53 trays expose the full tree from the same client and
  credentials. In band the tray is indistinguishable from its peers.

## How to collect these

In-band, no BMC credentials needed:

```bash
sudo dmidecode -s system-serial-number     # Dell service tag
sudo dmidecode -s system-product-name      # PowerEdge XE9712
sudo ipmitool lan print 1 | awk -F': ' '/^IP Address  /{print $2}'   # BMC IP
nvidia-smi --query-gpu=index,serial,enforced.power.limit --format=csv
```

Fleet-wide identification of capped trays (run from the BCM head):

```bash
sinfo -N -h -o '%N' | sort -u | xargs -P 96 -I{} bash -c \
  'v=$(ssh -o BatchMode=yes -o ConnectTimeout=8 {} \
     "nvidia-smi --query-gpu=enforced.power.limit --format=csv,noheader,nounits \
      | tr -d \" \" | paste -sd, -" 2>/dev/null); echo "{} ${v:-UNREACHABLE}"'
```

Other sources for the service tag: `racadm getsysinfo` (`SVC Tag`), Redfish
`/redfish/v1/Systems/System.Embedded.1` (`SKU` / `SerialNumber`), Redfish service root
(`Oem.Dell.ServiceTag`), `ipmitool fru print 0` (`Product Serial`).
