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
"""Administrator staging and compute entrypoints. There is NO submit operation."""
import sys

sys.dont_write_bytecode = True
import argparse
import json
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent.parent / 'backend'))
from clusterkit_execution import (
    batch,
    diagnostic_node,
    launch_node,
    load_attempt,
    preflight_node,
    prepare_attempt,
    rank_exec,
)
from clusterkit_results import read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['prepare', 'batch-preflight', 'batch-pairwise',
                                             'node-launch', 'node', 'init-path'])
    parser.add_argument('--plan', required=True)
    parser.add_argument('--stage', choices=['before', 'baseline', 'single-path', 'after'])
    args = parser.parse_args()
    try:
        if args.operation == 'prepare':
            print(json.dumps(prepare_attempt(read_json(args.plan), SCRIPTS), indent=2))
            return 0
        plan = load_attempt(args.plan, SCRIPTS)
        if args.operation == 'init-path':
            print(plan['effective_profile']['hpcx_init'])
            return 0
        if args.operation.startswith('batch-'):
            return batch(plan, args.plan, SCRIPTS, args.operation.removeprefix('batch-'))
        if not args.stage:
            parser.error('node execution requires --stage')
        if args.operation == 'node-launch':
            launch_node(plan, args.plan, SCRIPTS, args.stage)
        elif args.stage == 'before':
            preflight_node(plan, SCRIPTS)
        elif args.stage == 'after':
            diagnostic_node(plan, SCRIPTS)
        else:
            rank_exec(plan, SCRIPTS, args.stage)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI boundary must serialize all execution failures.
        print(json.dumps({'ok': False, 'error': str(exc)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
