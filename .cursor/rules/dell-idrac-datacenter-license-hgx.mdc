---
description: Dell HGX iDRAC Datacenter license gates GPU/HGX OOB power APIs — check license before treating Redfish 500 as a firmware bug
alwaysApply: true
---

# Dell HGX + iDRAC Datacenter — don't relearn the 699 W dead-end

Full runbook: `docs/clusters/DELL_IDRAC_DATACENTER_LICENSE_HGX.md`. Validated on Nscale TX
(GB300 / PowerEdge XE9712) 2026-08-05: 115 nodes fixed only after Datacenter install.
Incident detail: `docs/clusters/NSCALE_TX_GB300_SLOT0_POWER_CAP.md`.

- **Design gate, not a mystery bug.** On Dell HGX, Redfish
  `ClearOOBSetPoint` / `PATCH PowerLimitWatts` require **iDRAC Datacenter** (Enterprise is
  not enough). **NVIDIA DGX does not implement this BMC license ladder** for the same
  silicon — HGX-on-Dell can look “capped and unwritable” until the signed XML is imported.

- **First check when power writes 500:** `racadm license view -c idrac.embedded.1` or
  `GET /redfish/v1/LicenseService/Licenses`. Look for **Datacenter**. Missing license →
  install the service-tag-bound XML, then retry. Do **not** spend a day proving PATCH
  payloads or DSP0266 conformance first.

- **Misleading status:** unlicensed calls returned HTTP **500** /
  `Base.1.18.InternalError` / `IDRAC.2.15.SYS494` (resource still advertised `Allow: PATCH`).
  That is a licensing miss reported as InternalError — treat as “check license,” then
  escalate the wrong status code to Dell separately.

- **License files are tag-locked + signed.** Each `*XML` has
  `<Restrictions><ServiceTag>…</ServiceTag></Restrictions>`, `TransferableLicense=false`,
  and W3C XMLDSig (ECDSA-SHA384 / SHA-384) under Dell’s `iDRAC10 License Signing PA`.
  You cannot retarget by editing the tag. Install helpers only select the matching file;
  iDRAC enforces the bind.

- **License ≠ the cap.** A bad OOB `SetPoint` (e.g. 699 W on slot-0) is a separate
  provisioning write. Datacenter only **unlocks** clear/set. Order: license → ClearOOB →
  PATCH 1400 → confirm with `nvidia-smi -q -d POWER`.
