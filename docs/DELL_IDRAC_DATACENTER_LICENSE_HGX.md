# Dell iDRAC Datacenter License on HGX — GPU power management gate

**Audience:** operators and agents on Dell PowerEdge HGX trays (e.g. XE9712 / GB300 NVL72).
**Validated:** Nscale TX (`ink50-dh01`), 2026-08-05 — 115 nodes remediable only after Datacenter
license install. Full incident writeup:
[`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md).

## TL;DR — do this first

On **Dell HGX**, if Redfish GPU/HGX power writes fail with **HTTP 500** /
`Base.1.18.InternalError` / `IDRAC.2.15.SYS494`, **check iDRAC licensing before debugging
firmware, payloads, or “broken” Redfish handlers.**

```bash
# From a host with racadm + BMC reachability:
racadm --nocertwarn -r <bmc_ip> -u <user> -p <pass> license view -c idrac.embedded.1
# Expect: "iDRAC10 17G Datacenter" (or equivalent Datacenter description)
# Also: GET /redfish/v1/LicenseService/Licenses
```

- **Datacenter license present** → proceed with `ClearOOBSetPoint` / `PATCH PowerLimitWatts`.
- **Missing / Enterprise-only** → this is a **SKU / license design gate**, not a Redfish defect.
  Install the tag-bound Datacenter XML, then retry. Do not burn a day proving the PATCH payload.

**DGX does not have this gate.** Same GB300 silicon on an NVIDIA DGX appliance exposes OOB GPU
power controls without a third-party BMC license ladder. HGX-on-Dell customers can look “capped
and unfixable” until Datacenter is applied — that asymmetry is Dell’s product decision, not
an NVIDIA HGX firmware bug.

## What is gated (and what is not)

| Capability | Needs Dell iDRAC **Datacenter**? |
|---|---|
| Read GPU power / `PowerLimitWatts` over Redfish | Usually readable without Datacenter |
| `POST …#NvidiaEnvironmentMetrics.ClearOOBSetPoint` | **Yes** |
| `PATCH … PowerLimitWatts.SetPoint` | **Yes** |
| Chassis / module-level OOB power clear/set (same family) | **Yes** |
| In-band `nvidia-smi -pl` | No (host path; may not persist across BMC policy) |

**Enterprise is not enough** for GPU/HGX power management on these trays. Confirm the license
description says **Datacenter**.

The license does **not** invent a 699 W cap. A provisioning/OOB write can still leave a bad
`SetPoint`. Datacenter only **unlocks the APIs that clear or rewrite it**. Missing license +
bad setpoint = stranded capacity that looks like a platform defect.

## Misleading errors (why we wasted time)

When unlicensed, iDRAC returned:

- HTTP **500**
- `Base.1.18.InternalError`
- `IDRAC.2.15.SYS494`

instead of a clear **402/403 license** response. The resource still advertised `Allow: GET, PATCH`,
and even an empty `{}` PATCH 500’d — which correctly looked like a broken handler. It was an
**unlicensed feature** reported as an internal error. Treat that signature as “check license”
until proven otherwise. Filing the 500-vs-license status code as a Dell defect remains valid;
it is not a remediation blocker once Datacenter is installed.

## License crypto / tag lock (operator-relevant bits)

Dell ships per-tray XML files (e.g. `464HKK4-dc-17G-perp-license.XML`):

- Bound with `<Restrictions><ServiceTag>…</ServiceTag></Restrictions>`
- `TransferableLicense=false` — cannot move to another tray
- Digitally signed (W3C XMLDSig): **ECDSA-SHA384**, digest **SHA-384**, enveloped signature
- Signer: `CN=iDRAC10 License Signing PA` (Dell); issuer `CN=iDRAC10 License Root CA`
- iDRAC holds the public trust; Dell’s portal holds the private key

Editing the service tag in the XML breaks the signature → import fails. The install scripts only
**select** the matching file by tag; iDRAC **enforces** the bind on import.

## Install pattern (fleet)

Dell’s GB300i helper (contact historically: `cristian.gal@dell.com`) is the usual path:

1. Inventory CSV with Dell **service tag**, tray name (`ctNN`), and **BMC IP**
2. Archive of `*-dc-17G-perp-license.XML` (one per tag)
3. `racadm license import` per BMC (wrapper may delete non-matching entitlements first)

Sketch (credentials via env / flags — never commit them):

```bash
# Single tray
./install_idrac_license_rack.sh -i <bmc_ip> -t <SVCTAG> -d <dir_with_ward_cty_licenses_*.tar>

# Rack from sql.csv (serial, name=ct*, bmc_ip columns)
./install_idrac_license_rack.sh -s sql.csv -d .
```

Verify:

```bash
racadm --nocertwarn -r <bmc_ip> -u <user> -p <pass> license view -c idrac.embedded.1
```

Then remediate power (example shape — adjust URIs to the tray’s Redfish tree):

```text
POST  …/GPU_N/EnvironmentMetrics/Actions/Oem/NvidiaEnvironmentMetrics.ClearOOBSetPoint
PATCH …/GPU_N/EnvironmentMetrics   {"PowerLimitWatts": {"SetPoint": 1400}}
```

Confirm in-band: `nvidia-smi -q -d POWER` → enforced limit 1400 W on all four GPUs (GB300 tray).

## Decision tree

```text
Symptom: GPUs stuck below TGP (e.g. 699 W on slot-0) OR Redfish power write 500s
    │
    ├─ In-band: nvidia-smi shows low enforced limit?
    │     yes → OOB/persisted setpoint likely; continue
    │
    ├─ Redfish GET PowerLimitWatts readable; PATCH/ClearOOB → 500 / SYS494 / InternalError?
    │     yes → CHECK LICENSE NEXT (do not deep-dive DSP0266 yet)
    │
    ├─ racadm license view / LicenseService → Datacenter present?
    │     no  → install tag-bound Datacenter XML, re-verify, THEN ClearOOB + PATCH
    │     yes → real firmware/path bug or wrong URI; escalate with evidence
    │
    └─ DGX / non-Dell BMC?
          → this Dell Datacenter gate does not apply; use platform-native OOB docs
```

## BOM / engagement checklist

Before treating HGX-on-Dell bring-up as “power APIs ready”:

- [ ] Confirm **iDRAC Datacenter** (not only Enterprise) on the compute-tray BMC SKU
- [ ] Spot-check `license view` on a sample of newly commissioned racks
- [ ] Keep the signed XML archive and service-tag inventory reachable from the BMC management path
- [ ] After any license install, smoke one tray: ClearOOB + PATCH 1400 + `nvidia-smi` confirm
- [ ] If writes 500 with Datacenter already present, then debug firmware / Redfish tree health

## Related docs

| Doc | Role |
|---|---|
| [`NSCALE_TX_GB300_SLOT0_POWER_CAP.md`](NSCALE_TX_GB300_SLOT0_POWER_CAP.md) | Full 699 W investigation + resolution |
| [`NSCALE_TX_CAPPED_SERVICE_TAGS.md`](NSCALE_TX_CAPPED_SERVICE_TAGS.md) | Affected tags / remaining trays |
| [`NSCALE_TX_DELL_TRIAGE_699W.md`](NSCALE_TX_DELL_TRIAGE_699W.md) | Dell-requested triage packet |
| [`NSCALE_TX_DELL_RCA_REBUTTAL.md`](NSCALE_TX_DELL_RCA_REBUTTAL.md) | Pushback on incomplete RCA |
| [`.cursor/rules/dell-idrac-datacenter-license-hgx.mdc`](../.cursor/rules/dell-idrac-datacenter-license-hgx.mdc) | Agent short rule (always apply) |
