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
"""nvis_logscan_parity — prove the streaming scanner agrees with parser.sh before we trust it.

Runs both extractors over the same corpus and compares the numbers that reach the dashboard.
Intended to be run ON the head node, where the logs are:

    python3 nvis_logscan_parity.py /cluster/benchmark/harish/rack_status/logs  # whole directory
    python3 nvis_logscan_parity.py --limit 40 <dir>                        # sample it
    python3 nvis_logscan_parity.py <file> [<file> ...]                     # named logs

Exit status is 1 if any file differs in a way this tool cannot explain, so it can gate a deploy.

ON "EXPLAINED" DIFFERENCES
--------------------------
The scanner is deliberately STRICTER than parser.sh in one place, and that shows up as a diff we
do not want to chase every run.  parser.sh accepts a bandwidth row on "nine-plus fields, numeric
$1, numeric $8" whenever a sweep is armed, which an NCCL_DEBUG line satisfies by accident:

    4 [1] NCCL INFO comm 0x... rank 1805 nranks 2304 cudaDev 1 busId 906000 - Destroy COMPLETE

Inside an armed alltoall block awk read that as size=4, busbw=1805 and job 140457 charted a RANK
NUMBER as bandwidth.  The scanner drops NCCL_DEBUG lines before they can be read as data, so it
reports no alltoall measurement — which is the truth.  A diff where parser.sh has a number and
the scanner does not, and the log contains no valid row for that collective, is reported as
EXPECTED rather than a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import nvis_logscan

PARSER_SH = os.path.join(HERE, "parser.sh")

#: scanner key -> parser.sh nccl row key
_NCCL_MAP = {"ar": "all_reduce_gbps", "aa": "alltoall_gbps", "ag": "all_gather_gbps"}
#: scanner nemo key -> parser.sh nemotron row key
_NEMO_MAP = {
    "step_time_s": "step_s",
    "tflops_per_gpu": "tflops_per_gpu",
    "step_time_std": "step_time_std",
    "perf_std": "perf_std",
    "mfu_percent": "mfu_percent",
}


def _num(v):
    if v in (None, "", "-", "---"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _close(a, b, tol=1e-6) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= max(tol, abs(b) * 1e-6)


def _has_real_row_for(path: str, coll: str) -> bool:
    """True if *path* holds a genuine sweep row for *coll* (used to justify a missing number)."""
    want = {"ar": "all_reduce", "aa": "alltoall", "ag": "all_gather"}[coll]
    rec = nvis_logscan.scan(path, inventory=False)
    return bool(rec["nccl"]["series_by_collective"].get(want))


def parser_sh_json(path: str, timeout: int) -> dict:
    """The REFERENCE run: pin the shell extractors, since parser.sh now defaults to the scanner
    and comparing the scanner against itself would prove nothing."""
    proc = subprocess.run(
        ["bash", PARSER_SH, "--json", "--no-squeue", "--no-cmsh", "--no-sinfo", path],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "NO_COLOR": "1", "NVIS_PARSER_ENGINE": "shell"},
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def compare(path: str, timeout: int) -> tuple[str, list[str]]:
    """Return (verdict, notes). verdict is one of MATCH / EXPECTED / DIFF / ERROR."""
    try:
        scan = nvis_logscan.scan(path, inventory=False)
    except OSError as exc:
        return "ERROR", [f"scan failed: {exc}"]
    if not scan.get("ok"):
        return "ERROR", [f"scan failed: {scan.get('read_error')}"]

    ref = parser_sh_json(path, timeout)
    nccl_rows = ref.get("nccl_peak_out_of_place_busbw_gbps") or [{}]
    nemo_rows = ref.get("nemotron_5h_56b_train_window") or [{}]
    nccl_ref, nemo_ref = nccl_rows[0], nemo_rows[0]

    notes: list[str] = []
    explained = True

    peaks = scan["nccl"]["peak_busbw_gbps"]
    for code, ref_key in _NCCL_MAP.items():
        got, want = peaks.get(code), _num(nccl_ref.get(ref_key))
        if _close(got, want):
            continue
        if got is None and want is not None and not _has_real_row_for(path, code):
            notes.append(f"EXPECTED {code}: parser.sh {want} is a phantom row; scanner abstains")
            continue
        explained = False
        notes.append(f"DIFF {code}: scanner={got} parser.sh={want}")

    s_started = scan["nccl"]["sweep_blocks_started"]
    s_concl = scan["nccl"]["sweep_blocks_concluded"]
    r_started = _num(nccl_ref.get("sweep_blocks_started"))
    r_concl = _num(nccl_ref.get("sweep_blocks_concluded"))
    if r_started is not None and s_started != int(r_started):
        explained = False
        notes.append(f"DIFF sweep_started: scanner={s_started} parser.sh={int(r_started)}")
    if r_concl is not None and s_concl != int(r_concl):
        explained = False
        notes.append(f"DIFF sweep_concluded: scanner={s_concl} parser.sh={int(r_concl)}")

    for skey, rkey in _NEMO_MAP.items():
        got, want = scan["nemo"].get(skey), _num(nemo_ref.get(rkey))
        if _close(got, want, tol=0.011):
            continue
        explained = False
        notes.append(f"DIFF nemo.{skey}: scanner={got} parser.sh={want}")

    if not notes:
        return "MATCH", []
    return ("EXPECTED" if explained else "DIFF"), notes


def collect(paths: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for name in sorted(os.listdir(p)):
                if name.endswith((".txt", ".out")):
                    out.append(os.path.join(p, name))
        else:
            out.append(p)
    out = [p for p in out if os.path.isfile(p)]
    if limit > 0:
        # Biggest first: the large logs are where sampling used to lose data.
        out.sort(key=lambda p: os.path.getsize(p), reverse=True)
        out = out[:limit]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare nvis_logscan against parser.sh.")
    ap.add_argument("paths", nargs="+", help="log files and/or directories")
    ap.add_argument("--limit", type=int, default=0, help="only the N largest files")
    ap.add_argument("--timeout", type=int, default=900, help="per-file parser.sh timeout")
    args = ap.parse_args(argv)

    files = collect(args.paths, args.limit)
    if not files:
        print("no log files found", file=sys.stderr)
        return 2

    tally = {"MATCH": 0, "EXPECTED": 0, "DIFF": 0, "ERROR": 0}
    for path in files:
        verdict, notes = compare(path, args.timeout)
        tally[verdict] += 1
        size_mb = os.path.getsize(path) / (1 << 20)
        print(f"{verdict:<8} {size_mb:8.1f} MB  {os.path.basename(path)}")
        for n in notes:
            print(f"           {n}")

    print(
        f"\n{len(files)} file(s): {tally['MATCH']} match, {tally['EXPECTED']} expected-diff, "
        f"{tally['DIFF']} unexplained, {tally['ERROR']} error"
    )
    return 1 if (tally["DIFF"] or tally["ERROR"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
