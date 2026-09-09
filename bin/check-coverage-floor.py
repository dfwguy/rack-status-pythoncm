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
"""Every file reaches the coverage floor, or is on a shrinking list of known exceptions.

Author: Rafael M. Koike & Cursor Agent ("Ada")

REQ-45: new code carries its own tests. A blanket "50% everywhere" gate cannot be switched on in a
product that predates the rule -- it would fail on day one and be disabled by the afternoon. So the
files already below the floor are named in EXCEPTIONS, and the gate enforces two things instead:

  1. Any file NOT on the list must meet the floor. A new file therefore cannot merge below it.
  2. A file ON the list must not get worse than its recorded value (plus a small tolerance).

The list may only shrink. Removing an entry is the reward for covering a file; adding one requires
editing this file in a merge request, which makes new debt a visible decision rather than a drift.

Usage:
    python -m pytest backend/tests --cov --cov-config=.coveragerc --cov-report=json:cov.json
    bin/check-coverage-floor.py cov.json

Exits non-zero on a violation so it can gate a CI job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FLOOR = 50.0

#: Allowed slippage on an exception file before the gate complains. Coverage moves by fractions of a
#: percent when unrelated code is added, and a gate that fails on noise gets switched off. Sized
#: against measured environment variance too: comparing a full working tree with a tracked-files-only
#: export moved 13 files, by up to 3.0 points. None of them are listed here today, but a listed file
#: that starts to vary between environments needs this raised rather than the gate disabled.
TOLERANCE = 1.5

#: Files below the floor when the gate was introduced (2026-08-24). THIS LIST MAY ONLY SHRINK.
#: The highest-risk entries are tracked as gaps G-1..G-6 in the SUTP.
#:
#: SEED THESE FROM A TRACKED-FILES-ONLY RUN, NOT YOUR WORKING TREE. The first version of this list
#: was measured in a full local tree and CI rejected it on the first push: universal_parser.py reads
#: 52.8% locally and 48.1% from a clean checkout, because a test picks up untracked sample data that
#: happens to be lying around. Reproduce CI's tree before touching these numbers:
#:
#:   git ls-tree --name-only HEAD | grep -v -e '^\.' -e '^frontend$' \
#:     | xargs git archive HEAD -- | tar -x -C <dir>
#:
#: See .cursor/rules/local-vs-ci-tree.mdc -- this is the fourth distinct instance of that gap.
EXCEPTIONS: dict[str, float] = {
    "backend/api/parser_results.py": 19.3,    # G-2
    "backend/api/exports.py": 22.3,           # G-2
    "backend/rack_status_engine.py": 25.5,    # G-2: needs a synthetic fleet fixture
    "backend/api/rack_power.py": 26.7,        # G-1: power, BMC recovery, MAC change
    "backend/api/terminal.py": 28.5,          # G-4: needs WebSocket upgrade simulation
    "backend/api/bug_report.py": 28.9,
    "backend/api/logs.py": 32.7,
    "backend/agent/registrations.py": 37.4,
    "backend/metrics_query.py": 45.4,
    # backend/api/diagnostics.py removed 2026-09-09: covered to 55.7% by test_hw_check_argv.py,
    # confirmed in a tracked-files-only export (the tree this gate runs in). It clears the floor by
    # 5.7 points, comfortably outside both the 1.5 tolerance and the 3.0-point maximum
    # working-tree-vs-clean-checkout variance measured when this list was seeded.
    "backend/universal_parser.py": 48.1,      # 986 statements; the NCCL/NeMo result scrapers
    "backend/api/tools.py": 48.3,             # G-2, and bypasses the exec chokepoint (G-6)
    "backend/api/bringup.py": 48.5,
    "backend/api/analytics.py": 49.8,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: check-coverage-floor.py <coverage-json>", file=sys.stderr)
        return 64

    path = Path(argv[1])
    if not path.is_file():
        print(f"coverage-floor: no such report: {path}", file=sys.stderr)
        return 64
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        files = report["files"]
    except (ValueError, KeyError) as exc:
        print(f"coverage-floor: {path} is not a coverage json report ({exc})", file=sys.stderr)
        return 64

    new_violations: list[str] = []
    regressions: list[str] = []
    improved: list[str] = []

    for name, data in sorted(files.items()):
        pct = float(data["summary"]["percent_covered"])
        if int(data["summary"]["num_statements"]) == 0:
            continue
        baseline = EXCEPTIONS.get(name)
        if baseline is None:
            if pct < FLOOR:
                new_violations.append(f"{name}: {pct:.1f}% is below the {FLOOR:.0f}% floor")
        elif pct >= FLOOR:
            improved.append(f"{name}: {pct:.1f}% — above the floor in THIS run")
        elif pct < baseline - TOLERANCE:
            regressions.append(
                f"{name}: {pct:.1f}%, was {baseline:.1f}% when listed — the list may only shrink"
            )

    stale = [n for n in EXCEPTIONS if n not in files]

    if improved:
        print("coverage-floor: listed files that cleared the floor in this run:")
        for line in improved:
            print(f"  {line}")
        print("  Confirm in a tracked-files-only tree before removing an entry -- a working tree can")
        print("  read higher than a clean checkout (see the note on EXCEPTIONS above).")
    if stale:
        print("coverage-floor: listed files that no longer exist (delete these entries):")
        for name in stale:
            print(f"  {name}")

    if new_violations or regressions:
        print()
        if new_violations:
            print(f"coverage-floor: FAIL — {len(new_violations)} file(s) below the floor and not "
                  "on the exception list:")
            for line in new_violations:
                print(f"  {line}")
            print("  Cover the file, or -- if it is genuinely untestable -- add it to EXCEPTIONS in")
            print("  this script with a reason, which makes the debt a reviewed decision (REQ-45).")
        if regressions:
            print(f"coverage-floor: FAIL — {len(regressions)} listed file(s) got worse:")
            for line in regressions:
                print(f"  {line}")
        return 1

    total = float(report["totals"]["percent_covered"])
    print(f"coverage-floor: PASS — {len(files)} files, total {total:.1f}%, "
          f"{len(EXCEPTIONS) - len(stale)} known exception(s) remaining")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
