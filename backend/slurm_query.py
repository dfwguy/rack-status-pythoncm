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

import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import host_exec
from config_env import _eget, _env_int
from core import SLURM_JOB_ID_RE, SLURM_STDOUT_RE
from core import rpath as _rpath
from core import slurm_job_base_id as _slurm_job_base_id
from core import slurm_job_base_id as core_slurm_job_base_id
from paths import _allowed_roots, _default_log_dir, _resolve_log_dir
from platforms import get_platform as _get_platform

_SACCT_FIELD_ORDER = (
    "job_id", "name", "partition", "nnodes", "nodelist", "start", "end", "elapsed", "state", "exit_code",
)


_SACCT_TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL",
    "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE",
})


def _dedupe_hostnames_preserve_spelling(hosts: list[str]) -> list[str]:
    """Drop duplicate hosts case-insensitively; keep the first spelling seen (prefer Slurm output order)."""
    out_map: dict[str, str] = {}
    for h in hosts:
        t = (h or "").strip()
        if not t:
            continue
        k = t.casefold()
        if k not in out_map:
            out_map[k] = t
    return sorted(out_map.values(), key=str.casefold)


# --- Live inventory (Slurm NodeNames) -------------------------------------------------
# The authoritative list of *real* node hostnames for node-targeting tools (sysinfo,
# pdsh, validation). Using inventory instead of a generated ``<rack>-<ctNN>`` template
# makes targets correct on any site naming scheme (e.g. Boston's
# ``bos01-a07-13-s1-dgx-03-c03`` under rack ``bos01-a07``). Short TTL cache so repeated
# target builds in one request don't re-run ``sinfo``.
#
# Shared object, not a value: mutate/reassign only through _all_slurm_nodenames below, which holds
# the lock. Rebinding _slurm_nodenames_cache in an importing module would give it a private cache.
_slurm_nodenames_cache: tuple[float, list[str]] = (0.0, [])
_slurm_nodenames_lock = threading.Lock()


def _all_slurm_nodenames() -> list[str]:
    """All real Slurm ``NodeName`` values (``sinfo -N``), deduped; short TTL cache.

    Returns the last cached value (possibly empty) when Slurm is unavailable.
    """
    global _slurm_nodenames_cache
    ttl = max(0, _env_int("RACK_INVENTORY_NODENAMES_TTL_SEC", 60))
    ts, cached = _slurm_nodenames_cache
    if cached and (time.monotonic() - ts) < ttl:
        return cached
    with _slurm_nodenames_lock:
        ts, cached = _slurm_nodenames_cache
        if cached and (time.monotonic() - ts) < ttl:
            return cached
        try:
            proc = host_exec.run_slurm_on_host(
                _get_platform().sinfo_cmd('-N -h -o "%N"'),
                timeout=max(20, _env_int("RACK_SINFO_NODES_TIMEOUT", 60)),
            )
        except (OSError, subprocess.TimeoutExpired):
            return cached
        if proc.returncode != 0:
            return cached
        hosts = _dedupe_hostnames_preserve_spelling(
            [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        )
        if hosts:
            _slurm_nodenames_cache = (time.monotonic(), hosts)
            return hosts
        return cached


def _rack_node_hostnames_from_inventory(rack: str) -> list[str]:
    """Real node hostnames belonging to *rack*, derived from live Slurm inventory.

    Nodes are matched to the rack id by prefix (rack id followed by a separator), so
    this is **template-free** and works where the hostname is not ``<rack>-<ctNN>``.
    Returns ``[]`` when inventory is unavailable or nothing matches.
    """
    rid = (rack or "").strip()
    if not rid:
        return []
    hosts = _all_slurm_nodenames()
    if not hosts:
        return []
    rid_cf = rid.casefold()
    seps = ("-", "_", ".", ":", "/")
    matched: list[str] = []
    for h in hosts:
        hcf = h.casefold()
        if hcf == rid_cf or (hcf.startswith(rid_cf) and len(h) > len(rid) and h[len(rid)] in seps):
            matched.append(h)
    return matched


def _run_squeue() -> dict:
    """Run ``squeue`` after optional ``module load`` (same env semantics as *scancel* / *scontrol*)."""
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


def _run_sacct_recent(limit: int = 10, state: str = "COMPLETED") -> dict:
    """Recent finished Slurm jobs from ``sacct`` — the accounting DB retains history
    that ``squeue`` drops the instant a job ends (the "blink and you miss it" gap).

    Allocation rows only (``-X``), pipe-parseable (``-P``), newest-first, capped to *limit*.
    """
    plat = _get_platform()
    st = (state or "COMPLETED").strip().upper()
    if not re.fullmatch(r"[A-Z_,]+", st):
        st = "COMPLETED"
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


# --- job stdout resolution (reads; the write side is slurm_admin.py) ---------------
def _slurm_job_log_path(job_id: str, log_dir: Path) -> tuple[str, str]:
    """Resolve a finished job's stdout path: ``scontrol`` ``StdOut=`` (jobs still within
    MinJobAge), else glob the dashboard log dir for the launcher's ``*_<jid>.out`` /
    ``*-<jid>.txt`` (for jobs that have aged out of the controller)."""
    jid = core_slurm_job_base_id(job_id) or ""
    if not jid:
        return "", "invalid"
    try:
        r = _run_scontrol_show_job(jid)
        if r.get("ok"):
            m = SLURM_STDOUT_RE.search(str(r.get("stdout") or ""))
            if m:
                p = m.group(1).strip()
                if p and p.lower() not in ("(null)", "/dev/null", "none", "(none)"):
                    return p, "scontrol"
    except (ValueError, OSError):
        pass
    d = str(log_dir).rstrip("/")
    if d:
        listing = (
            f"ls -1t {shlex.quote(d)}/*[_-]{jid}.out {shlex.quote(d)}/*[_-]{jid}.txt "
            f"{shlex.quote(d)}/*-{jid}.out {shlex.quote(d)}/slurm-{jid}.out 2>/dev/null | head -1"
        )
        try:
            proc = host_exec.run_on_host(listing, timeout=max(5, _env_int("RACK_SLURM_LOGFIND_TIMEOUT", 20)))
            lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
            if lines:
                return lines[0], "logdir"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "", "not_found"


def _run_scontrol_show_job(job_id: str) -> dict:
    """Run ``scontrol show job JOBID`` after optional ``module load`` (same env as *scancel*)."""
    jid = (job_id or "").strip()
    if not SLURM_JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id must be a Slurm job id (digits, optional _TASK for arrays)")
    plat = _get_platform()
    tail = " ".join(shlex.quote(a) for a in ["show", "job", jid])
    shell_cmd = plat.scontrol_cmd(tail)
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "scontrol executable not found; set RACK_SLURM_SCONTROL to the full path "
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


def _resolve_slurm_job_stdout_path(path_raw: str, log_dir_raw: str | None) -> Path:
    """Resolve Slurm ``StdOut=`` path for read APIs; must be a file under allowed roots or dashboard log_dir."""
    raw = (path_raw or "").strip()
    if not raw or ".." in raw or "://" in raw:
        raise ValueError("invalid path")
    p = _rpath(Path(raw))
    # Asked of the execution host, which is where the file lives. Checking locally
    # was the bug behind "path does not exist" on Scheduler → open log.
    if not host_exec.is_file(p):
        raise ValueError("path does not exist")
    roots = _allowed_roots()
    if roots:
        ok = any(str(p).startswith(str(r) + os.sep) or p == r for r in roots)
        if not ok:
            raise ValueError("job stdout path must be under RACK_STATUS_ALLOWED_PREFIXES")
        return p
    # NB: do NOT call .resolve() on the roots here. _resolve_log_dir / _default_log_dir
    # already return correctly-formed paths per mode (resolved locally, left as-is on the
    # remote host in SSH mode), and `p` came through _resolve_path with the same policy.
    # Re-resolving in SSH mode rewrites a remote "/home/cmods/..." path into the local
    # macOS firmlink "/System/Volumes/Data/home/cmods/...", so the prefix check fails even
    # when the stdout file genuinely lives under log_dir.
    if log_dir_raw and str(log_dir_raw).strip():
        try:
            log_root = _resolve_log_dir(str(log_dir_raw).strip())
            p.relative_to(log_root)
            return p
        except ValueError:
            pass
    try:
        p.relative_to(_default_log_dir())
        return p
    except ValueError as e:
        raise ValueError(
            "job stdout path must be under the dashboard log directory (log_dir) or "
            "RACK_STATUS_DEFAULT_LOG_DIR, or configure RACK_STATUS_ALLOWED_PREFIXES"
        ) from e


# --- scontrol output parsing --------------------------------------------------------------------


def _slurm_job_state_base(state: str | None) -> str:
    return str(state or "").strip().upper().split("+")[0]


def _infer_num_nodes_from_nodelist(hosts: str) -> int | None:
    h = hosts.strip()
    if not h or re.search(r"[\[\]]", h):
        return None
    if "," in h:
        return h.count(",") + 1
    return 1


_SCONTROL_FIELDS = (
    "JobId", "JobName", "UserId", "JobState", "Reason", "ExitCode", "DerivedExitCode",
    "RunTime", "SubmitTime", "StartTime", "EndTime", "NodeList", "ReqNodeList",
    "NumNodes", "NumCPUs", "Partition", "StdOut", "StdErr", "WorkDir", "Command",
)


def _parse_scontrol_job(text: str) -> dict:
    """scontrol emits `Key=Value` pairs, space-separated, values may contain `/` and `:` but not ` `."""
    out: dict[str, str] = {}
    for m in re.finditer(r"([A-Za-z][A-Za-z0-9_]*)=(\S*)", text or ""):
        k, v = m.group(1), m.group(2)
        if k in _SCONTROL_FIELDS and k not in out:
            out[k] = v
    return out
