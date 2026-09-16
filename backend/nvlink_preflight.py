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
"""Parse ``bin/scripts/nvlink_preflight.sh`` output into a per-node readiness table.

The script runs ON each node (via pdsh from the head) and emits, per node:

    ### nvlink_preflight <host> (quick=0) ###
    [PASS] gpus_enumerate          4/4
    [FAIL] nvlink_p2p_matrix       3 non-OK cell(s): NS ...
    [WARN] sharp_hello             only 2/4 GPU rails have SHARP ...
      nvlink_datapath              SKIPPED (venv/test missing: /cm/shared/nvtest/...)
    RESULT <host>: FAIL (do not use until drained/reset)

pdsh prefixes every line with ``<host>: ``, and writes transport failures
(``<host>: ssh exited with exit code 255``) to stderr.

Verdict policy: only a FAILED check makes a node not-ready.

* WARN does not block. SHARP being unprovisioned means NCCL falls back to NVLS_TREE, which is a
  performance note, not a readiness fault.
* SKIPPED does not block either, but it is always reported. The functional data-path probe is the
  only check that catches a wedged NVSwitch SDN route and it self-skips when its venv is absent
  (it is absent on Nscale TX), so ``nodes_with_skips`` tells the operator which coverage the run
  did not have — without withholding a verdict on the checks that did run.
* A node that never answered is still NOT a pass. Missing data is not a warning: callers pass the
  expected node list and every one of them gets a row, because a silent omission would read as
  "nothing wrong here".
"""
from __future__ import annotations

import re
from typing import Any

# ``<host>: <rest>`` as emitted by pdsh without dshbak.
_PDSH_LINE_RE = re.compile(r"^([^\s:]+):\s?(.*)$")

# ``[PASS] name detail`` / ``[FAIL] …`` / ``[WARN] …``
_CHECK_RE = re.compile(r"^\[(PASS|FAIL|WARN)\]\s+(\S+)\s*(.*)$", re.IGNORECASE)

# ``note()`` lines: two-space indent, padded name, then SKIPPED (reason).
_SKIP_RE = re.compile(r"^(\S+)\s+SKIPPED\b\s*(.*)$", re.IGNORECASE)

# ``RESULT <host>: PASS (NVLS-ready)``
_RESULT_RE = re.compile(r"^RESULT\s+(\S+?):\s*(PASS|FAIL)\b\s*(.*)$", re.IGNORECASE)

# ``### nvlink_preflight <host> (quick=1) ###`` — proves the script started on that node.
_HEADER_RE = re.compile(r"^###\s+nvlink_preflight\s+(\S+)\s+\(quick=(\d+)\)", re.IGNORECASE)

# pdsh transport failures, e.g. ``ssh exited with exit code 255``.
_SSH_FAIL_RE = re.compile(r"ssh exited with exit code\s+(\d+)|Connection (refused|timed out)", re.IGNORECASE)

#: The functional NVLink probe — the only check that detects a wedged NVSwitch SDN route.
DATAPATH_CHECK = "nvlink_datapath"

#: Node-level verdicts, worst first.
#:
#: Only a FAILED check makes a node not-ready. WARN does not (SHARP unprovisioned just means NCCL
#: falls back to NVLS_TREE) and neither does SKIPPED — a skipped check is reported alongside the
#: pass so it stays visible, rather than blocking the verdict. Missing data is different from a
#: warning, so a node that never answered or never finished still blocks the rack.
STATUS_ORDER = ("error", "no_response", "fail", "incomplete", "pass")

_STATUS_LABEL = {
    "pass": "ready",
    "fail": "not ready",
    "incomplete": "incomplete",
    "no_response": "no response",
    "error": "unreachable",
}


def status_label(status: str) -> str:
    """Operator-facing wording for a node verdict."""
    return _STATUS_LABEL.get(status, status or "unknown")


_BRACKET_RE = re.compile(r"^(?P<prefix>.*?)\[(?P<inner>[0-9,\-\s]+)\](?P<suffix>.*)$")


def expand_hostlist(spec: str) -> list[str]:
    """Expand one Slurm/pdsh hostlist token: ``r-[11-18,28-37]`` -> 18 hostnames.

    Needed because the expected-node list cannot be derived by splitting on commas: this site's
    tray range contains one (``[11-18,28-37]``), so a naive split yields ``r-[11-18`` and
    ``28-37]`` — two hosts that never answer, which is exactly how the first run of this feature
    reported "2 nodes did not respond" for a healthy 18-node rack. Zero padding is preserved
    from the range bounds (``[08-10]`` -> 08, 09, 10).
    """
    s = (spec or "").strip()
    if not s:
        return []
    m = _BRACKET_RE.match(s)
    if not m:
        return [s]
    prefix, inner, suffix = m.group("prefix"), m.group("inner"), m.group("suffix")
    out: list[str] = []
    for part in inner.split(","):
        p = part.strip()
        if not p:
            continue
        if "-" in p:
            lo_s, _, hi_s = p.partition("-")
            lo_s, hi_s = lo_s.strip(), hi_s.strip()
            if not (lo_s.isdigit() and hi_s.isdigit()):
                continue
            width = len(lo_s) if lo_s.startswith("0") else 0
            lo, hi = int(lo_s, 10), int(hi_s, 10)
            if lo > hi:
                continue
            for n in range(lo, hi + 1):
                out.append(f"{prefix}{str(n).zfill(width)}{suffix}")
        elif p.isdigit():
            width = len(p) if p.startswith("0") else 0
            out.append(f"{prefix}{str(int(p, 10)).zfill(width)}{suffix}")
    return out or [s]


def expand_pdsh_w(w: str) -> list[str]:
    """Every hostname a ``pdsh -w`` value targets, splitting only on top-level commas."""
    hosts: list[str] = []
    depth = 0
    buf = ""
    for ch in w or "":
        if ch == "[":
            depth += 1
            buf += ch
        elif ch == "]":
            depth = max(0, depth - 1)
            buf += ch
        elif depth == 0 and (ch == "," or ch.isspace()):
            if buf.strip():
                hosts.extend(expand_hostlist(buf))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        hosts.extend(expand_hostlist(buf))
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    return [h for h in hosts if not (h in seen or seen.add(h))]


def _blank_row(host: str) -> dict[str, Any]:
    return {
        "host": host,
        "status": "no_response",
        "result_detail": "",
        "started": False,
        "quick": None,
        "checks": [],
        "passed": 0,
        "failed": 0,
        "warned": 0,
        "skipped": 0,
        "first_failure": "",
        "transport_error": "",
    }


def parse_nvlink_preflight_output(
    stdout: str,
    stderr: str = "",
    expected_hosts: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One row per node, in the order given by *expected_hosts* (then any extras seen).

    Rows carry every check the node reported plus counts, so the UI can show *why* a node
    is not ready without the operator opening the raw log.
    """
    rows: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    expected_by_fold = {
        str(host).strip().casefold(): str(host).strip()
        for host in (expected_hosts or [])
        if str(host).strip()
    }

    def _row(host: str) -> dict[str, Any]:
        if host not in rows:
            rows[host] = _blank_row(host)
            order.append(host)
        return rows[host]

    for host in expected_hosts or []:
        h = (host or "").strip()
        if h:
            _row(h)

    for raw in (stdout or "").splitlines():
        m = _PDSH_LINE_RE.match(raw.rstrip())
        if not m:
            continue
        host, rest = m.group(1).strip(), m.group(2).strip()
        if not host:
            continue
        # Slurm may emit shell diagnostics such as
        # ``/usr/bin/bash: /root/.bash_profile: Permission denied`` alongside
        # labelled task output.  The text has the same colon shape as pdsh, but
        # it is not a node and must never create a false no-response row.
        if expected_by_fold:
            canonical = expected_by_fold.get(host.casefold())
            if canonical is None:
                continue
            host = canonical
        row = _row(host)

        hm = _HEADER_RE.match(rest)
        if hm:
            row["started"] = True
            row["quick"] = hm.group(2) == "1"
            # Started but not yet judged; a RESULT line below will settle it.
            if row["status"] == "no_response":
                row["status"] = "incomplete"
            continue

        cm = _CHECK_RE.match(rest)
        if cm:
            state = cm.group(1).lower()
            check = {"name": cm.group(2), "state": state, "detail": cm.group(3).strip()}
            row["checks"].append(check)
            if state == "pass":
                row["passed"] += 1
            elif state == "fail":
                row["failed"] += 1
                if not row["first_failure"]:
                    row["first_failure"] = f"{check['name']}: {check['detail']}".strip().rstrip(":")
            else:
                row["warned"] += 1
            continue

        sm = _SKIP_RE.match(rest)
        if sm:
            row["checks"].append(
                {"name": sm.group(1), "state": "skipped", "detail": sm.group(2).strip()}
            )
            row["skipped"] += 1
            continue

        rm = _RESULT_RE.match(rest)
        if rm:
            row["status"] = rm.group(2).lower()
            row["result_detail"] = rm.group(3).strip().strip("()")
            continue

    # pdsh reports transport failures on stderr; those nodes produced no verdict at all.
    for raw in (stderr or "").splitlines():
        m = _PDSH_LINE_RE.match(raw.strip())
        if not m:
            continue
        host, rest = m.group(1).strip(), m.group(2).strip()
        if not host or not _SSH_FAIL_RE.search(rest):
            continue
        if expected_by_fold:
            canonical = expected_by_fold.get(host.casefold())
            if canonical is None:
                continue
            host = canonical
        row = _row(host)
        row["transport_error"] = rest
        if row["status"] in ("no_response", "incomplete"):
            row["status"] = "error"

    for row in rows.values():
        # Quick mode does not print the data-path check at all (the whole block is gated on
        # quick=0), so record it as skipped explicitly. It does not change the verdict, but it
        # keeps "this run did not exercise the NVLink data path" visible in the detail instead of
        # being silently absent.
        if row["quick"] and not any(c["name"] == DATAPATH_CHECK for c in row["checks"]):
            row["checks"].append(
                {
                    "name": DATAPATH_CHECK,
                    "state": "skipped",
                    "detail": "not run in quick mode — cannot detect a wedged NVSwitch route",
                }
            )
            row["skipped"] += 1

    return [rows[h] for h in order]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Rack-level roll-up.

    ``ready`` requires every node to have passed. Warnings and skipped checks do not block it;
    ``nodes_with_skips`` reports them separately so the UI can say which coverage was missing
    without withholding the green verdict.
    """
    counts: dict[str, int] = {s: 0 for s in STATUS_ORDER}
    for r in rows:
        counts[r.get("status", "no_response")] = counts.get(r.get("status", "no_response"), 0) + 1
    total = len(rows)
    passed = counts.get("pass", 0)
    return {
        "total": total,
        "passed": passed,
        "failed": counts.get("fail", 0),
        "incomplete": counts.get("incomplete", 0),
        "no_response": counts.get("no_response", 0) + counts.get("error", 0),
        # Informational: nodes that passed but did not run every check.
        "nodes_with_skips": sum(1 for r in rows if r.get("skipped")),
        "nodes_with_warnings": sum(1 for r in rows if r.get("warned")),
        "ready": total > 0 and passed == total,
        "worst": next((s for s in STATUS_ORDER if counts.get(s)), "pass"),
    }
