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
"""nvis_ingest_evidence.py — load a site collector's evidence snapshot into results history.

Run after the collector writes its snapshot. Idempotent: an unchanged snapshot is recognised
by content and skipped, so a timer may call this as often as it likes.

    nvis_ingest_evidence.py /path/to/validated-racks.json
    nvis_ingest_evidence.py --cluster-default          # the active cluster's configured path

Exits 0 on success, 2 when the snapshot is refused. A refusal is whole: a partial snapshot
imports nothing, because a rack missing from it is indistinguishable from a rack with no
evidence, and that gap would read as "not validated" rather than "we did not finish".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import external_evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", nargs="?", default="", help="path to the snapshot JSON")
    parser.add_argument(
        "--cluster-default",
        action="store_true",
        help="use the active cluster's evidence.snapshot_path",
    )
    args = parser.parse_args(argv)

    path = args.snapshot
    if args.cluster_default or not path:
        from catalog import context as catalog_context

        _project, cluster, _platform = catalog_context.resolve()
        path = cluster.evidence.snapshot_path if cluster else ""
        if not path:
            print(json.dumps({
                "ok": False,
                "error": "the active cluster declares no evidence.snapshot_path",
            }))
            return 2

    try:
        summary = external_evidence.ingest_snapshot(path)
    except external_evidence.SnapshotRejected as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **summary}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
