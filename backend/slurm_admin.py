# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Running the MUTATING Slurm commands -- the commands, not the endpoints.

Author: Rafael M. Koike & Cursor Agent ("Ada")

The write-side counterpart to ``slurm_query.py``, which is scoped by its own docstring to read-only
queries. Kept separate rather than folded in because the two have different blast radius: a
mis-scoped ``squeue`` returns the wrong list, a mis-scoped ``scontrol update`` drains a rack. Having
one module for each makes "which of these can change cluster state" answerable by import.

Same contract as the query side: each returns a dict rather than raising on a non-zero exit, because
a refusal from the controller is a reportable condition the UI shows rather than a 500. A missing
binary is the exception -- that is a deployment fault, so it raises OSError naming the env var that
fixes it.

Both honour the platform provider's command shape (``module load`` before ``scontrol`` on clusters
where it is not on PATH until modules load), so BCM-specific behaviour stays behind that interface.
"""

from __future__ import annotations

import os
import shlex
import subprocess

import host_exec
from config_env import _env_int
from core import SLURM_JOB_ID_RE
from platforms import get_platform as _get_platform


def _run_scontrol_node_update(target: str, state_kv: list[str]) -> dict:
    """Run scontrol update nodename=TARGET …; state_kv e.g. [\"state=drain\", \"reason=admin\"].

    By default runs under ``bash -lc`` after ``module load <RACK_SLURM_MODULE>`` (default *slurm*),
    matching cluster login environments where *scontrol* is not on PATH until modules load.
    Set RACK_SLURM_SKIP_MODULE_LOAD=1 to invoke *scontrol* directly (e.g. full path already on PATH).
    """
    return _run_scontrol(["update", f"nodename={target}", *state_kv])


def _run_scontrol_reboot(target: str, reason: str) -> dict:
    """``scontrol reboot ASAP nextstate=RESUME reason=… TARGET``: Slurm's own node reboot.

    Slurm drains the nodes at once (so no new job lands), reboots each one when its running
    jobs end, and returns it to service when it registers again -- a reboot that cannot cut a
    job short and needs nobody to undrain afterwards. Uses the cluster's ``RebootProgram``.
    """
    return _run_scontrol(["reboot", "ASAP", "nextstate=RESUME", f"reason={reason}", target])


def _run_scontrol(args: list[str]) -> dict:
    """Run ``scontrol <args>`` through the platform's Slurm command (module load, dry run)."""
    plat = _get_platform()
    shell_cmd = plat.scontrol_cmd(" ".join(shlex.quote(a) for a in args))
    cmd_display = shell_cmd

    dry = os.environ.get("RACK_SLURM_NODES_STATE_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
    if dry:
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "dry-run (RACK_SLURM_NODES_STATE_DRY_RUN)",
            "stderr": "",
            "command_display": cmd_display,
        }
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(
            shell_cmd,
            timeout=timeout,
        )
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


def _run_scancel(job_id: str) -> dict:
    """Run ``scancel JOBID`` after optional ``module load`` (same env semantics as *scontrol*)."""
    jid = (job_id or "").strip()
    if not SLURM_JOB_ID_RE.fullmatch(jid):
        raise ValueError("job_id must be a Slurm job id (digits, optional _TASK for arrays)")
    plat = _get_platform()
    shell_cmd = plat.scancel_cmd(shlex.quote(jid))
    cmd_display = shell_cmd
    timeout = max(5, _env_int("RACK_SLURM_SCONTROL_TIMEOUT", 120))
    try:
        proc = host_exec.run_slurm_on_host(shell_cmd, timeout=timeout)
    except FileNotFoundError:
        raise OSError(
            "scancel executable not found; set RACK_SLURM_SCANCEL to the full path "
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
