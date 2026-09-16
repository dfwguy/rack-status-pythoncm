#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
"""Ingest a collector-qualified 16K evidence snapshot into Rack Status."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
import external_validation_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    args = parser.parse_args()
    run_ids = external_validation_evidence.ingest_snapshot(args.snapshot)
    print(json.dumps({"ok": True, "evidence_runs_ingested": len(run_ids)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
