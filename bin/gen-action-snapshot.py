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
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Regenerate `backend/tests/action_snapshot.json` — the action *regression* snapshot.

This was a shell heredoc pasted inside a test docstring. A recipe nobody can run without
copy-pasting is a recipe that gets run wrong, and it is the reason the surrounding code kept
guessing action names instead of reading them.

**Not the same artifact as the vocabulary document.** This snapshot holds endpoint/method pairs, so
a route moving between modules or changing verb shows up as a reviewed diff.
`bin/gen-action-vocabulary.py` publishes what services and actions *exist*, which is what a policy
author or a test should read.

Regenerating is a deliberate act: the diff is the review.

Usage::

    bin/gen-action-snapshot.py
    bin/gen-action-snapshot.py --check   # exit non-zero if stale, without writing
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT = REPO_ROOT / "backend" / "tests" / "action_snapshot.json"


def render() -> str:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    os.environ.setdefault("RACK_STATUS_DEMO", "1")
    os.environ.setdefault("NVIS_LOG_SINKS", "file")
    os.environ.setdefault("NVIS_LOG_FILE", "/tmp/gen-action-snapshot.log")

    import app  # noqa: F401  (the registry is built during import)
    from audit import actions as audit_actions

    rows = [
        {
            "endpoint": entry.endpoint,
            "method": entry.method,
            "action": entry.action,
            "variant": entry.variant,
        }
        for entry in sorted(
            audit_actions.registry().entries, key=lambda e: (e.endpoint, e.method)
        )
    ]
    return json.dumps(rows, indent=1) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report staleness without writing")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != rendered:
            # The diff, not just the verdict: a contributor needs to see WHICH endpoint moved.
            # This is what the CI gate used `git diff` for, and `python:3.12-slim` ships no git —
            # see the note in gen-action-vocabulary.py::_diff.
            tracked = OUT.read_text(encoding="utf-8").splitlines() if OUT.exists() else []
            name = str(OUT.relative_to(REPO_ROOT))
            print("\n".join(difflib.unified_diff(
                tracked, rendered.splitlines(),
                fromfile=f"{name} (tracked)", tofile=f"{name} (regenerated)", lineterm="",
            )), file=sys.stderr)
            print(
                f"{OUT.relative_to(REPO_ROOT)} is stale.\nRun: bin/gen-action-snapshot.py",
                file=sys.stderr,
            )
            return 1
        print("action snapshot is current")
        return 0

    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
