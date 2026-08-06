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

Two things this module is deliberate about, because both have burned this project before:

* A node that never answered is NOT a pass. Callers pass the expected node list and every
  one of them gets a row; a silent omission would read as "nothing wrong here".
* SKIPPED is not PASS. The functional NVLink data-path probe is the only check that catches
  a wedged NVSwitch SDN route, and it self-skips when its venv is absent (it is absent on
  Nscale TX). Reporting that as a pass would tell an operator the rack is NVLS-ready on the
  strength of the checks that provably cannot detect the fault.
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

#: Node-level verdicts, worst first. ``skipped`` outranks ``pass`` so a rack whose only
#: functional gate never ran is never presented as ready.
STATUS_ORDER = ("error", "no_response", "fail", "incomplete", "skipped", "pass")

_STATUS_LABEL = {
    "pass": "ready",
    "fail": "not ready",
    "skipped": "inconclusive",
    "incomplete": "incomplete",
    "no_response": "no response",
    "error": "unreachable",
}


def status_label(status: str) -> str:
    """Operator-facing wording for a node verdict."""
    return _STATUS_LABEL.get(status, status or "unknown")


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
        row = _row(host)
        row["transport_error"] = rest
        if row["status"] in ("no_response", "incomplete"):
            row["status"] = "error"

    for row in rows.values():
        # Quick mode does not print the data-path check at all (the whole block is gated on
        # quick=0), so absence — not a SKIPPED line — is what has to be caught. Without this a
        # quick run reads as an unqualified pass while never having exercised the NVLink data
        # path, which is the one fault this gate exists to find. Record it explicitly.
        if row["quick"] and not any(c["name"] == DATAPATH_CHECK for c in row["checks"]):
            row["checks"].append(
                {
                    "name": DATAPATH_CHECK,
                    "state": "skipped",
                    "detail": "not run in quick mode — cannot detect a wedged NVSwitch route",
                }
            )
            row["skipped"] += 1

        # A node that passed every check it ran, but skipped one, is inconclusive not ready.
        if row["status"] == "pass" and row["skipped"] > 0:
            row["status"] = "skipped"

    return [rows[h] for h in order]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Rack-level roll-up. ``ready`` is true only when every node is an unqualified pass."""
    counts: dict[str, int] = {s: 0 for s in STATUS_ORDER}
    for r in rows:
        counts[r.get("status", "no_response")] = counts.get(r.get("status", "no_response"), 0) + 1
    total = len(rows)
    passed = counts.get("pass", 0)
    return {
        "total": total,
        "passed": passed,
        "failed": counts.get("fail", 0),
        "skipped": counts.get("skipped", 0),
        "incomplete": counts.get("incomplete", 0),
        "no_response": counts.get("no_response", 0) + counts.get("error", 0),
        "ready": total > 0 and passed == total,
        "worst": next((s for s in STATUS_ORDER if counts.get(s)), "pass"),
    }
