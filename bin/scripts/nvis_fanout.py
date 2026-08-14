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
"""nvis_fanout — portable parallel SSH fan-out with dshbak-style output grouping.

A modern, dependency-light replacement for ``pdsh -w <nodes> ... | dshbak -c``:

  * Reads a shell snippet from stdin and runs it on every node concurrently over
    ``ssh`` (``bash -s``), then prints stdout **grouped by identical result** —
    exactly the readable "these N nodes all reported X" view dshbak gives.
  * Uses only the Python 3 standard library (``subprocess`` + ``concurrent.futures``),
    so it runs on virtually any install. No ``pdsh``, no ``dshbak``, no Perl.

Semantics chosen to match ``pdsh | dshbak -c`` so gating checks keep working:

  * Nodes that emit **no stdout are omitted** from the grouped output (like dshbak),
    so an "expect no output" check stays empty (== pass) when all nodes are clean.
  * Remote **stderr and connection/timeout errors go to this process's stderr**
    (like pdsh), never into the grouped stdout — so they don't corrupt gating that
    inspects stdout.

Usage:
    nvis_fanout.py --nodes n1,n2,... [--fanout N] [--connect-timeout S] [--cmd-timeout S] <<'EOF'
    <shell snippet run on each node>
    EOF
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import subprocess
import sys


def run_node(node: str, body: str, ssh_opts: list[str], cmd_timeout: int):
    """Run *body* on *node* via ``ssh ... bash -s``; return (node, stdout, stderr, rc)."""
    cmd = ["ssh", *ssh_opts, node, "bash", "-s"]
    try:
        p = subprocess.run(cmd, input=body, capture_output=True, text=True, timeout=cmd_timeout)
        return node, p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        return node, "", f"ssh timed out after {cmd_timeout}s", 124
    except Exception as exc:  # pragma: no cover - defensive
        return node, "", f"error: {exc}", 255


def main() -> int:
    ap = argparse.ArgumentParser(description="Parallel ssh fan-out with dshbak-style grouping.")
    ap.add_argument("--nodes", required=True, help="comma/space separated node hostnames")
    ap.add_argument("--fanout", type=int, default=64, help="max concurrent ssh sessions")
    ap.add_argument("--connect-timeout", type=int, default=15, help="ssh ConnectTimeout (s)")
    ap.add_argument("--cmd-timeout", type=int, default=120, help="per-node command timeout (s)")
    ap.add_argument("--no-coalesce", action="store_true", help="print per-node instead of grouping")
    # Tolerate stray pdsh-style flags if any leak through.
    args, _unknown = ap.parse_known_args()

    nodes = [n for n in args.nodes.replace(" ", ",").split(",") if n]
    if not nodes:
        print("nvis_fanout: no nodes provided", file=sys.stderr)
        return 2
    body = sys.stdin.read()

    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={args.connect_timeout}",
    ]

    outs: dict[str, str] = {}
    with cf.ThreadPoolExecutor(max_workers=max(1, args.fanout)) as ex:
        futs = {ex.submit(run_node, n, body, ssh_opts, args.cmd_timeout): n for n in nodes}
        for fut in cf.as_completed(futs):
            node, out, err, _rc = fut.result()
            outs[node] = out
            if err and err.strip():
                for line in err.rstrip("\n").splitlines():
                    print(f"{node}: {line}", file=sys.stderr)

    if args.no_coalesce:
        for n in nodes:
            if outs.get(n, "").strip():
                print(f"----------------\n{n}\n----------------")
                print(outs[n].rstrip("\n"))
        return 0

    # Group non-empty outputs by identical content (dshbak -c style). Silent nodes
    # are omitted so "expect no output" checks stay empty when everything is clean.
    groups: dict[str, list[str]] = {}
    for n in nodes:
        o = outs.get(n, "")
        if o.strip():
            groups.setdefault(o.rstrip("\n"), []).append(n)
    for out in groups:
        print("----------------")
        print(",".join(sorted(groups[out])))
        print("----------------")
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
