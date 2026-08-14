# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Demo data generator for NVIS Rack Status.

When ``RACK_STATUS_DEMO=1`` is set, ``run_rack_status()`` returns synthetic rack data
so the dashboard can be demonstrated without access to cluster tools (cmsh, sinfo, etc.).

The generator creates a realistic-looking pod with configurable rack count and randomized
validation states that shift over time (simulating an active bring-up).
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from native_status import (
    compute_stage_states,
    empty_stage_summary,
    tally_stage,
    validation_status_hint,
)

_DEMO_POD = 5
_DEMO_RACK_COUNT = 18
_DEMO_CT_PER_RACK = 8

_STATES = ("done", "fail", "ongoing", "pending")
_SINFO_STATES = ("idle", "alloc", "drain", "mix", "down")


def _seeded_rng(rack: str, epoch_bucket: int) -> random.Random:
    seed = hashlib.md5(f"{rack}:{epoch_bucket}".encode()).hexdigest()
    return random.Random(seed)


def _generate_rows(now_ts: float) -> list[dict[str, str]]:
    bucket = int(now_ts) // 120
    rows: list[dict[str, str]] = []

    for r in range(1, _DEMO_RACK_COUNT + 1):
        rack_id = f"p{_DEMO_POD}-r{r:02d}"
        rng = _seeded_rng(rack_id, bucket)

        state = rng.choices(_STATES, weights=[45, 20, 20, 15], k=1)[0]

        if state == "ongoing":
            minutes = rng.randint(2, 45)
            nemo = f"ongoing [{minutes}:{rng.randint(0,59):02d}]"
        else:
            nemo = state

        job_id = str(rng.randint(100000, 999999)) if state != "pending" else "-"

        sinfo_parts: list[str] = []
        total_nodes = _DEMO_CT_PER_RACK
        remaining = total_nodes
        for si_state in rng.sample(_SINFO_STATES, k=rng.randint(1, 3)):
            if remaining <= 0:
                break
            cnt = rng.randint(1, remaining)
            remaining -= cnt
            sinfo_parts.append(f"{si_state}={cnt}")
        sinfo_cell = ",".join(sinfo_parts) if sinfo_parts else "idle=8"

        ct_nums = list(range(1, _DEMO_CT_PER_RACK + 1))
        up_ct = sorted(rng.sample(ct_nums, k=rng.randint(4, _DEMO_CT_PER_RACK)))
        dn_ct = [c for c in ct_nums if c not in up_ct]

        ct_parts: list[str] = []
        if dn_ct:
            ct_parts.append(f"DN:{rack_id}-ct[{_condense(dn_ct)}]")
        ct_parts.append(f"UP:{rack_id}-ct[{_condense(up_ct)}]")
        ct_cell = ";".join(ct_parts)

        nc_b: bool | None = None
        ll_b: bool | None = None
        if state == "fail":
            nc_b = rng.random() < 0.55
            ll_b = rng.random() < 0.55
            if nc_b and ll_b:
                ll_b = False
        elif state == "ongoing":
            # Stage 1 (NCCL) usually completes before Stage 2 (NeMo) — show a mix.
            nc_b = True if rng.random() < 0.5 else None
        nemo_hint = validation_status_hint(
            nemo,
            job_id,
            nccl_ok=nc_b,
            llm_ok=ll_b,
            validation_test="nccl_all_reduce,llm",
        )
        stage1, stage2 = compute_stage_states(nemo, nc_b, ll_b, "nccl_all_reduce,llm")

        rows.append({
            "rack": rack_id,
            "nemo": nemo,
            "stage1": stage1,
            "stage2": stage2,
            "last_job": job_id,
            "sinfo": sinfo_cell,
            "ct_down_unk": ct_cell,
            "nemo_hint": nemo_hint,
        })

    return rows


def _condense(nums: list[int]) -> str:
    if not nums:
        return ""
    sorted_u = sorted(set(nums))
    out: list[str] = []
    s = e = sorted_u[0]
    for n in sorted_u[1:]:
        if n == e + 1:
            e = n
        else:
            out.append(f"{s:02d}-{e:02d}" if s != e else f"{s:02d}")
            s = e = n
    out.append(f"{s:02d}-{e:02d}" if s != e else f"{s:02d}")
    return ",".join(out)


def _compute_summary(rows: list[dict[str, str]]) -> dict:
    summary: dict = {"done": 0, "fail": 0, "ongoing": 0, "pending": 0}
    stage1_summary = empty_stage_summary()
    stage2_summary = empty_stage_summary()
    for row in rows:
        nemo = row["nemo"]
        if nemo == "done":
            summary["done"] += 1
        elif nemo == "fail":
            summary["fail"] += 1
        elif nemo.startswith("ongoing"):
            summary["ongoing"] += 1
        else:
            summary["pending"] += 1
        tally_stage(stage1_summary, row.get("stage1", "pending"))
        tally_stage(stage2_summary, row.get("stage2", "pending"))
    summary["stage1"] = stage1_summary
    summary["stage2"] = stage2_summary
    return summary


def generate_demo_status(log_dir: Path, include_raw: bool = False) -> dict:
    """Return a complete /api/status-shaped payload with demo data."""
    now = time.time()
    rows = _generate_rows(now)
    summary = _compute_summary(rows)

    out: dict = {
        "ok": True,
        "returncode": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "log_dir": str(log_dir),
        "script": "demo_data.py (simulated)",
        "stderr": "",
        "legend": "DEMO MODE — simulated rack data (no cluster connection)",
        "rows": rows,
        "summary": summary,
        "footnotes": ["[demo] Data is synthetic — set RACK_STATUS_DEMO=0 to disable"],
        "comments": {
            f"p{_DEMO_POD}-r{r:02d}": _demo_comment(r)
            for r in range(1, _DEMO_RACK_COUNT + 1)
        },
        "physical_rf0": {
            f"p{_DEMO_POD}-r{r:02d}-ct{ct:02d}": f"10.0.{r}.{ct}"
            for r in range(1, _DEMO_RACK_COUNT + 1)
            for ct in range(1, _DEMO_CT_PER_RACK + 1)
        },
        "native_workers": {
            "mode": "demo",
            "note": "Synthetic data — RACK_STATUS_DEMO=1",
            "rack_count": _DEMO_RACK_COUNT,
            "workers": [],
        },
    }
    if include_raw:
        import json
        out["stdout"] = json.dumps({"racks": rows, "summary": summary}, indent=2)
    return out


def _demo_comment(rack_num: int) -> str:
    comments = [
        "",
        "NVSwitch firmware updated",
        "Replaced GPU tray 3",
        "Waiting on RMA",
        "IB cable reseat done",
        "",
        "Power supply replaced",
        "",
        "BIOS update pending",
        "",
        "New install — burn-in",
        "",
        "",
        "Network recabled",
        "Thermal issue tray 5",
        "",
        "Ready for production",
        "",
    ]
    return comments[(rack_num - 1) % len(comments)]
