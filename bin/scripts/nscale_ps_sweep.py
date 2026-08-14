#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""nscale_ps_sweep — survey NVIDIA Power Smoothing state + GPU power setpoints over Redfish.

Read-only. Sweeps the 36 known 699 W-capped Nscale TX trays plus healthy controls and
reports, per GPU: PowerSmoothing Enabled / supported / remaining-lifetime / TMPWatts, and
EnvironmentMetrics PowerLimitWatts SetPoint + persistency.

Env: BMC_USER, BMC_PASSWORD. Optional BMC_CSV (node,ip) to source healthy controls.
"""

import base64
import csv
import json
import os
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CAPPED = {
    "h01-33": "10.40.144.15", "h03-16": "10.40.144.43", "h03-29": "10.40.144.47",
    "h03-34": "10.40.144.52", "h04-17": "10.40.144.62", "h04-34": "10.40.144.70",
    "h06-14": "10.40.145.23", "h06-16": "10.40.145.25", "h06-18": "10.40.145.27",
    "h06-34": "10.40.145.34", "h08-16": "10.40.145.61", "h17-14": "10.40.146.41",
    "h17-29": "10.40.146.47", "h18-34": "10.40.146.70", "h20-11": "10.40.147.20",
    "h20-28": "10.40.147.28", "h20-34": "10.40.147.34", "h21-29": "10.40.147.47",
    "h22-15": "10.40.147.60", "h22-17": "10.40.147.62", "i02-15": "10.40.148.24",
    "i02-30": "10.40.148.30", "i03-36": "10.40.148.54", "i04-14": "10.40.148.59",
    "i04-32": "10.40.148.68", "i05-30": "10.40.149.12", "i05-31": "10.40.149.13",
    "i08-11": "10.40.149.56", "i16-11": "10.40.150.20", "i16-28": "10.40.150.28",
    "i18-12": "10.40.150.57", "i18-35": "10.40.150.71", "i19-12": "10.40.151.3",
    "i19-14": "10.40.151.5", "i20-14": "10.40.151.23", "i20-33": "10.40.151.33",
}

USER = os.environ["BMC_USER"]
PW = os.environ["BMC_PASSWORD"]
AUTH = base64.b64encode(f"{USER}:{PW}".encode()).decode()
CTX = ssl._create_unverified_context()
GPUS = ("GPU_0", "GPU_1", "GPU_2", "GPU_3")


def get(bmc, path, timeout=25):
    req = urllib.request.Request(
        f"https://{bmc}{path}",
        headers={"Authorization": f"Basic {AUTH}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            return json.load(r)
    except Exception as exc:  # noqa: BLE001 - any transport/parse failure is just an unreachable BMC
        return {"__error__": str(exc)}


def probe(item):
    name, bmc = item
    row = {"node": name, "bmc": bmc, "ps": [], "life": [], "tmp": [],
           "sp": [], "persist": [], "supported": [], "error": None}
    for gpu in GPUS:
        ps = get(bmc, f"/redfish/v1/Systems/HGX_Baseboard_0/Processors/{gpu}/Oem/Nvidia/PowerSmoothing")
        if "__error__" in ps:
            row["error"] = ps["__error__"][:70]
            return row
        row["ps"].append(ps.get("Enabled"))
        row["supported"].append(ps.get("PowerSmoothingSupported"))
        row["life"].append(ps.get("RemainingLifetimeCircuitryPercent"))
        row["tmp"].append(ps.get("TMPWatts"))

        em = get(bmc, f"/redfish/v1/Systems/HGX_Baseboard_0/Processors/{gpu}/EnvironmentMetrics")
        if "__error__" in em:
            row["error"] = em["__error__"][:70]
            return row
        row["sp"].append((em.get("PowerLimitWatts") or {}).get("SetPoint"))
        row["persist"].append(((em.get("Oem") or {}).get("Nvidia") or {}).get("PowerLimitPersistency"))
    return row


def fmt(vals):
    return "[" + ",".join("-" if v is None else
                          ("T" if v is True else "F" if v is False else
                           str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                          for v in vals) + "]"


def render(rows, title):
    print(f"\n{'=' * 104}\n{title}\n{'=' * 104}")
    print(f"{'node':<9} {'bmc':<14} {'SetPoint g0-g3':<24} {'PS-Enabled':<11} "
          f"{'Persist':<10} {'Life%':<20} {'TMPWatts'}")
    print("-" * 104)
    for r in sorted(rows, key=lambda x: x["node"]):
        if r["error"]:
            print(f"{r['node']:<9} {r['bmc']:<14} ERROR: {r['error']}")
            continue
        print(f"{r['node']:<9} {r['bmc']:<14} {fmt(r['sp']):<24} {fmt(r['ps']):<11} "
              f"{fmt(r['persist']):<10} {fmt(r['life']):<20} {fmt(r['tmp'])}")


def main():
    healthy = {}
    csv_path = os.environ.get("BMC_CSV", "")
    want = int(os.environ.get("HEALTHY_COUNT", "6"))
    if csv_path and os.path.exists(csv_path):
        with open(csv_path, newline="") as fh:
            for rec in csv.DictReader(fh):
                node, ip = rec.get("node", ""), rec.get("ip", "")
                short = node.replace("ink50-dh01-", "")
                if short and short not in CAPPED and ip:
                    healthy[short] = ip
    # spread the controls across racks rather than taking six neighbours
    picks, seen = {}, set()
    for short, ip in sorted(healthy.items()):
        rack = short.split("-")[0]
        if rack in seen:
            continue
        seen.add(rack)
        picks[short] = ip
        if len(picks) >= want:
            break

    with ThreadPoolExecutor(max_workers=12) as pool:
        capped_rows = list(pool.map(probe, CAPPED.items()))
        healthy_rows = list(pool.map(probe, picks.items())) if picks else []

    render(capped_rows, f"CAPPED TRAYS ({len(capped_rows)})  —  legend: T=true F=false, order g0,g1,g2,g3")
    if healthy_rows:
        render(healthy_rows, f"HEALTHY CONTROL TRAYS ({len(healthy_rows)})")

    ok = [r for r in capped_rows if not r["error"]]
    err = [r for r in capped_rows if r["error"]]
    print(f"\n{'=' * 104}\nSUMMARY\n{'=' * 104}")
    print(f"capped probed OK: {len(ok)}/{len(capped_rows)}   unreachable: {len(err)}")
    if ok:
        any_ps = [r["node"] for r in ok if any(r["ps"])]
        print(f"trays with PowerSmoothing Enabled=true on ANY GPU: {len(any_ps)} {any_ps}")
        life = {v for r in ok for v in r["life"]}
        print(f"distinct RemainingLifetimeCircuitryPercent values: {sorted(life, key=str)}")
        pat = {}
        for r in ok:
            pat.setdefault(tuple(r["sp"]), []).append(r["node"])
        for k, v in sorted(pat.items(), key=lambda x: -len(x[1])):
            print(f"SetPoint pattern {list(k)}: {len(v)} trays")
        pp = {}
        for r in ok:
            pp.setdefault(tuple(r["persist"]), []).append(r["node"])
        for k, v in sorted(pp.items(), key=lambda x: -len(x[1])):
            print(f"PowerLimitPersistency pattern {list(k)}: {len(v)} trays")
    for r in healthy_rows:
        if not r["error"]:
            print(f"control {r['node']}: SetPoint={r['sp']} PS={r['ps']} "
                  f"persist={r['persist']} life={r['life']} TMPWatts={r['tmp']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
