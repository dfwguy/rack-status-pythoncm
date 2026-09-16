# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Running the read-only Slurm queries -- the commands, not the endpoints.

Author: Rafael M. Koike & Cursor Agent ("Ada")

``squeue``, ``sinfo -R`` and ``sacct`` invocations, shared because they have two kinds of caller:
the HTTP routes in ``api/slurm_queries.py`` and code inside ``app.py`` that needs the same data for a
payload. Command execution is not an endpoint, so it does not live in the API package.

Each returns a dict rather than raising on a non-zero exit: a busy or unreachable controller is a
reportable condition the UI shows, not a 500. A timeout reports the budget it spent.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone

import host_exec
import scheduler_runtime
from config_env import _eget, _env_int
from core import slurm_job_base_id as _slurm_job_base_id
from platforms import get_platform as _get_platform

_SACCT_FIELD_ORDER = (
    "job_id", "name", "partition", "nnodes", "nodelist", "start", "end", "elapsed", "state", "exit_code",
)


_SACCT_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL",
    "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE",
})


def _run_squeue() -> dict:
    """Run ``squeue`` after optional ``module load`` (same env semantics as *scancel* / *scontrol*)."""
    if scheduler_runtime.is_pythoncm():
        rows = scheduler_runtime.jobs(active_only=True)
        lines = ["JOBID NAME STATE TIME NODELIST"]
        lines.extend(
            f"{row['job_id']} {row['name']} {row['state']} "
            f"{row.get('elapsed') or '00:00:00'} {row.get('nodelist') or '-'}"
            for row in rows
        )
        return {
            "ok": True, "returncode": 0, "stdout": "\n".join(lines), "stderr": "",
            "command_display": "pythoncm active commands (Rack Status registry)",
            "jobs": rows, "scheduler": "pythoncm",
        }
    plat = _get_platform()
    shell_cmd = plat.squeue_cmd()
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "squeue executable not found; set RACK_SLURM_SQUEUE to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


def _run_sinfo_r() -> dict:
    """Run ``sinfo -R --long`` after optional ``module load`` (same env semantics as *squeue* / *scontrol*)."""
    if scheduler_runtime.is_pythoncm():
        nodes = scheduler_runtime.nodes()
        affected = [row for row in nodes if str(row.get("state") or "unknown") != "idle"]
        lines = ["REASON USER TIMESTAMP NODELIST"]
        lines.extend(
            f"{row.get('reason') or row.get('state') or 'unknown'} - - {row['node']}"
            for row in affected
        )
        return {
            "ok": True, "returncode": 0, "stdout": "\n".join(lines), "stderr": "",
            "command_display": "pythoncm node states", "nodes": affected,
            "scheduler": "pythoncm",
        }
    plat = _get_platform()
    shell_cmd = plat.sinfo_cmd("-R --long")
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "sinfo executable not found; set RACK_SLURM_SINFO to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"timeout after {timeout}s",
            "command_display": cmd_display,
        }
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
        "command_display": cmd_display,
    }


def _run_sinfo_nodes(node_names: list[str]) -> dict:
    """Return current Slurm state and reason for an explicit node list.

    This deliberately queries Slurm even when PythonCM is the configured command
    scheduler.  Rack operators use this view to decide whether a node is actually
    allocatable in Slurm; PythonCM device state is already shown in the adjacent
    rack-status column and must not be presented as scheduler ground truth.
    """
    requested = list(dict.fromkeys(str(name).strip() for name in node_names if str(name).strip()))
    plat = _get_platform()
    nodespec = ",".join(requested)
    shell_cmd = plat.sinfo_cmd(
        f"-N -h -o '%N|%T|%E' -n {shlex.quote(nodespec)}"
    )
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "sinfo executable not found; set RACK_SLURM_SINFO to the full path "
            "or ensure module load is available"
        ) from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": -1,
            "nodes": [],
            "stderr": f"timeout after {timeout}s",
            "command_display": shell_cmd,
            "source": "slurm",
        }

    parsed: dict[str, dict[str, str]] = {}
    for line in (proc.stdout or "").splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) < 2 or not parts[0].strip():
            continue
        name = parts[0].strip()
        reason = parts[2].strip() if len(parts) > 2 else ""
        if reason in {"(null)", "none", "None"}:
            reason = ""
        parsed[name.casefold()] = {
            "node": name,
            "state": parts[1].strip() or "UNKNOWN",
            "reason": reason,
        }

    rows = [
        parsed.get(
            name.casefold(),
            {"node": name, "state": "UNKNOWN", "reason": "Not reported by Slurm"},
        )
        for name in requested
    ]
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "nodes": rows,
        "stderr": (proc.stderr or "").strip(),
        "command_display": shell_cmd,
        "source": "slurm",
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _run_sacct_recent(limit: int = 10, state: str = "COMPLETED") -> dict:
    """Recent finished Slurm jobs from ``sacct`` — the accounting DB retains history
    that ``squeue`` drops the instant a job ends (the "blink and you miss it" gap).

    Allocation rows only (``-X``), pipe-parseable (``-P``), newest-first, capped to *limit*.
    """
    st = (state or "COMPLETED").strip().upper()
    if not re.fullmatch(r"[A-Z_,]+", st):
        st = "COMPLETED"
    if scheduler_runtime.is_pythoncm():
        if st == "ALL":
            want_states: set[str] = set()
        elif st in ("FINISHED", "TERMINAL"):
            want_states = set(_SACCT_TERMINAL_STATES)
        else:
            want_states = {item for item in st.split(",") if item}
        jobs = []
        for row in scheduler_runtime.jobs(active_only=False):
            row_state = str(row.get("state") or "UNKNOWN").upper()
            if want_states and row_state not in want_states:
                continue
            jobs.append({
                "job_id": row["job_id"], "name": row.get("name") or "pythoncm-command",
                "partition": "pythoncm", "nnodes": str(len(row.get("nodes") or [])),
                "nodelist": row.get("nodelist") or "", "start": row.get("start") or "",
                "end": row.get("end") or "", "elapsed": row.get("elapsed") or "",
                "state": row_state, "exit_code": row.get("exit_code") or "",
            })
        jobs = jobs[:max(1, limit)]
        return {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "command_display": "pythoncm command history (Rack Status registry)",
            "jobs": jobs, "scheduler": "pythoncm",
        }
    plat = _get_platform()
    since = (_eget("RACK_SLURM_SACCT_SINCE") or "now-7days").strip() or "now-7days"
    if not re.fullmatch(r"[A-Za-z0-9:_+\-]+", since):
        since = "now-7days"
    # NB: do NOT pass sacct --state=… : with only -S it returns nothing on this Slurm
    # even though the same jobs report State=COMPLETED. Fetch all and filter in Python.
    fmt = "JobID,JobName,Partition,NNodes,NodeList,Start,End,Elapsed,State,ExitCode"
    args = f"-X -n -P -S {shlex.quote(since)} -o {shlex.quote(fmt)}"
    shell_cmd = plat.sacct_cmd(args)
    timeout = max(5, _env_int("RACK_SLURM_SACCT_TIMEOUT", 60))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError("sacct executable not found; ensure Slurm accounting (sacct) is available") from None
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "returncode": -1, "stdout": "", "stderr": f"timeout after {timeout}s",
            "command_display": shell_cmd, "jobs": [],
        }
    if st == "ALL":
        want_states: set[str] = set()
    elif st in ("FINISHED", "TERMINAL"):
        want_states = set(_SACCT_TERMINAL_STATES)
    else:
        want_states = {s for s in st.split(",") if s}
    jobs: list[dict] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < len(_SACCT_FIELD_ORDER):
            continue
        row = dict(zip(_SACCT_FIELD_ORDER, [p.strip() for p in parts]))
        if not _slurm_job_base_id(row.get("job_id")):
            continue
        # State can carry a suffix, e.g. "CANCELLED by 12345"; match the leading token.
        row_state = (row.get("state") or "").split()[0].upper() if (row.get("state") or "").strip() else ""
        if want_states and row_state not in want_states:
            continue
        jobs.append(row)
    # Newest first: End is ISO-ish (sorts lexically); tie-break on numeric job id.
    jobs.sort(key=lambda r: (r.get("end") or "", int(_slurm_job_base_id(r.get("job_id")) or 0)), reverse=True)
    jobs = jobs[: max(1, limit)]
    return {
        "ok": proc.returncode == 0, "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(), "stderr": (proc.stderr or "").strip(),
        "command_display": shell_cmd, "jobs": jobs,
    }
