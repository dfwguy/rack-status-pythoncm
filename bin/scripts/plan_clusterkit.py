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
"""Host-local administrator preview; no live execution/submission command exists."""
import sys

sys.dont_write_bytecode = True  # Print-only must not create import caches either.

import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from clusterkit_planner import PlanningError, SlurmInventory, plan_request
from clusterkit_profile import resolve_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["readiness", "plan"])
    parser.add_argument("--request", help="Product-level JSON request only")
    parser.add_argument("--local-inventory", action="store_true",
                        help="Explicitly query this host's Slurm via existing local transport; no SSH fanout")
    args = parser.parse_args()
    resolution = resolve_profile()
    if args.operation == "readiness":
        print(json.dumps(resolution.public(), indent=2))
        return 0 if resolution.ready else 2
    if not args.local_inventory or not args.request:
        parser.error("plan requires --local-inventory and --request; no submission is supported")
    if not resolution.ready:
        print(json.dumps(resolution.public(), indent=2))
        return 2
    try:
        from platforms import get_platform
        from transport.local import LocalConnection

        request = json.loads(args.request)
        plan = plan_request(request, resolution, SlurmInventory(LocalConnection(), get_platform()),
                            scripts_dir=str(Path(__file__).resolve().parent))
        print("PRINT-ONLY DRY-RUN: no job submission, allocation, result directory or config writes.")
        print(json.dumps(plan, indent=2))
        return 0
    except (ValueError, OSError, PlanningError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "submitted": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
