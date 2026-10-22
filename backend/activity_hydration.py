# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Keeping a launched validation activity's state true, after the launch returns.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A launch records an activity and exits; the run itself lives on for minutes or hours in Slurm. This is
the half that closes the loop -- ask the scheduler what happened, find the result log the job actually
wrote, and fold what it says back into the durable record.

Extracted because it is the substrate under two different domains: the rack-action launches (gpu
reset, hw check, nvswitch and imex operations) and the validation-activities API that displays them.
Thirteen routes reach it.

Why it is not simply "read the job state":

* A job leaves ``squeue`` the moment it ends, so a terminal state has to come from ``scontrol`` or
  ``sacct``, and the two disagree about field names.
* ``StdOut=`` is where the job SAID it would write, which is not always where the result landed -- a
  wrapper may redirect, and LLMB writes an intermediate file first. ``_stdout_is_llmb_intermediate``
  exists so an intermediate is not mistaken for the result.
* A finished job's log may not be flushed yet, so there is a collecting grace period
  (``_collecting_grace_expired``) before "no results" is believed. Declaring failure early is worse
  than waiting: it marks a passing rack as failed.
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import core
import host_exec
import native_status
import results_history
import validation_activities
from config_env import _env_int
from core import SLURM_JOB_ID_RE as _SLURM_SCANCEL_JOB_ID_RE
from core import SLURM_STDOUT_RE as _SLURM_STDOUT_RE
from platforms import get_platform as _get_platform
from results_ingest import _universal_enabled, _universal_fallback_for_log
from slurm_query import _run_scontrol_show_job, _slurm_job_state_base

_logger = logging.getLogger(__name__)


def _record_validation_activity(
    tool: str,
    *,
    stage: str,
    scope: str,
    rack: str,
    racks: list[str],
    log_dir: str,
    command_display: str,
    activity_id: str,
    operator: str = "",
    dry_run: bool = False,
    scheduler: str = "slurm",
    nodes: list[str] | None = None,
) -> str:
    """Persist a validation launch in the durable activities store."""
    return validation_activities.record_activity(
        tool,
        stage=stage,
        scope=scope,
        rack=rack,
        racks=racks,
        log_dir=log_dir,
        operator=operator,
        command_display=command_display,
        dry_run=dry_run,
        scheduler=scheduler,
        activity_id=activity_id,
        nodes=nodes,
    )


def _parse_scontrol_job_fields(text: str) -> tuple[str | None, str | None]:
    """Return (JobState, StdOut path) from ``scontrol show job`` output."""
    state_m = re.search(r"JobState=(\S+)", text or "")
    stdout_m = _SLURM_STDOUT_RE.search(text or "")
    state = state_m.group(1) if state_m else None
    stdout = stdout_m.group(1) if stdout_m else None
    if stdout in ("(none)", "/dev/null", ""):
        stdout = None
    return state, stdout


_SLURM_TERMINAL_JOB_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "NODE_FAIL",
        "PREEMPTED",
        "OUT_OF_MEMORY",
        "BOOT_FAIL",
        "DEADLINE",
    }
)


_SLURM_RUNNING_JOB_STATES = frozenset(
    {"RUNNING", "PENDING", "COMPLETING", "CONFIGURING", "SUSPENDED"}
)


def _task_needs_slurm_hydration(task: dict) -> bool:
    """True when a task with a Slurm ref still needs ladder refresh."""
    if not isinstance(task, dict):
        return False
    ref = str(task.get("scheduler_ref") or "").strip()
    if not ref:
        return False
    jid = ref.split(".")[0]
    if not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        return False
    wp = str(task.get("work_phase") or "").strip().lower()
    if wp in validation_activities.OPEN_WORK_PHASES or not wp:
        return True
    if not str(task.get("stdout_path") or "").strip() and not str(task.get("results_log_path") or "").strip():
        return True
    base = _slurm_job_state_base(str(task.get("scheduler_state") or ""))
    if not base:
        return True
    if base in _SLURM_RUNNING_JOB_STATES:
        return True
    # Terminal scheduler state but llmb intermediate StdOut with no results yet → keep
    # collecting, but only until the grace period runs out. Past that the collector is
    # never going to deliver, and re-querying Slurm forever costs an SSH round trip per poll.
    stdout = str(task.get("stdout_path") or "")
    if (
        "llmb-nccl_" in stdout.replace("\\", "/")
        and not str(task.get("results_log_path") or "").strip()
        and not _collecting_grace_expired(task.get("collecting_since"))
    ):
        return True
    return base not in _SLURM_TERMINAL_JOB_STATES


def _query_scheduler_state_and_stdout(job_id: str) -> tuple[str | None, str | None, str]:
    """Return (JobState, StdOut path, evidence) via scontrol, then sacct.

    When both miss (purged), returns ``(None, None, \"missing\")`` — callers must fail-closed
    rather than freeze a prior RUNNING state.
    """
    jid = (job_id or "").strip().split(".")[0]
    if not jid or not _SLURM_SCANCEL_JOB_ID_RE.fullmatch(jid):
        return None, None, "invalid"
    try:
        sc = _run_scontrol_show_job(jid)
    except (OSError, ValueError):
        sc = {"ok": False, "stdout": ""}
    if sc.get("ok"):
        state, stdout = _parse_scontrol_job_fields(str(sc.get("stdout") or ""))
        if state:
            return state, stdout, "scontrol"
    # sacct retains history after MinJobAge; StdOut is not available from accounting.
    try:
        sa = host_exec.run_slurm_on_host(
            _get_platform().sacct_cmd(
                f"-j {shlex.quote(jid)} -X -P -n -o JobID,State,ExitCode"
            ),
            timeout=max(10, _env_int("RACK_SACCT_TIMEOUT", 30)),
        )
        for line in (sa.stdout or "").strip().splitlines():
            cols = line.split("|")
            if len(cols) < 2:
                continue
            # Prefer the base job line (not .batch / .extern).
            raw_id = cols[0].strip()
            if raw_id.split(".")[0] != jid:
                continue
            if "." in raw_id and not raw_id.endswith(jid):
                continue
            state = cols[1].strip().split()[0] if cols[1].strip() else None
            if state:
                return state, None, "sacct"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None, None, "missing"


def _stdout_is_llmb_intermediate(path: str | None) -> bool:
    name = Path(str(path or "")).name.lower()
    return name.startswith("llmb-nccl_") or "llmb-autocollect" in name


def _find_results_log_on_exec_host(log_dir: str, job_id: str, label: str) -> str | None:
    """Aggregated results log for *job_id*, discovered on the EXECUTION host.

    The log directory belongs to the host that runs the jobs, so this lists it
    through the transport layer (one ``find`` round trip) rather than with a
    process-local scan. In SSH mode that scan always came back empty, so any task
    whose results log has to be *discovered* — an llmb launch, whose Slurm StdOut is
    the intermediate ``llmb-nccl_*.out`` rather than the aggregated file — stayed in
    ``collecting`` forever even though the collector had written the results.
    """
    base = (log_dir or "").strip()
    jid = (job_id or "").strip()
    if not base or not jid.isdigit():
        return None
    try:
        found = host_exec.stat_dir(base, patterns=(f"*-{jid}.txt",), maxdepth=1)
    except Exception:  # noqa: BLE001 — discovery is best-effort; fall through to local
        found = {}
    if found:
        by_name = {PurePosixPath(str(p)).name: str(p) for p in found}
        picked = validation_activities.pick_results_log(list(by_name), jid, label=label)
        if picked:
            return by_name.get(picked)
    # Same-host deployments (RACK_EXEC_MODE=local) where stat_dir is unavailable.
    return validation_activities.find_results_log_for_job(base, jid, label=label)


def _collecting_grace_expired(collecting_since: str | None) -> bool:
    """True when a task has waited longer than the collector grace period.

    Without a bound, "terminal in Slurm but no aggregated log yet" is a state with
    no exit: the UI shows the run as unfinished forever and the refresh loop keeps
    paying for Slurm round trips. A collector that has not produced anything within
    the grace period is not going to, so the task resolves on the scheduler's
    verdict instead. ``RACK_VALIDATION_COLLECT_GRACE_SEC`` (default 900) tunes it.
    """
    raw = str(collecting_since or "").strip()
    if not raw:
        return False
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    grace = max(60, _env_int("RACK_VALIDATION_COLLECT_GRACE_SEC", 900))
    return (datetime.now(timezone.utc) - started).total_seconds() > grace


def _derive_task_work_phase(
    *,
    scheduler_state: str | None,
    evidence: str,
    stdout_path: str | None,
    results_log_path: str | None,
    label: str,
    collecting_since: str | None = None,
) -> str:
    """Map ladder evidence → tasks[].work_phase."""
    base = _slurm_job_state_base(scheduler_state)
    if evidence == "squeue" or base in _SLURM_RUNNING_JOB_STATES:
        return "running"
    if results_log_path:
        if base in ("FAILED", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL"):
            return "failed"
        return "done"
    if base in _SLURM_TERMINAL_JOB_STATES:
        # Terminal Slurm but aggregated result not bound yet — llmb collector lag.
        if _stdout_is_llmb_intermediate(stdout_path) or (
            (label or "").lower() in ("nccl", "nemo") and not results_log_path
        ):
            if base in ("FAILED", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL"):
                return "failed"
            # Waited long enough for a collector that never delivered: take the
            # scheduler's verdict rather than showing the run as unfinished forever.
            if _collecting_grace_expired(collecting_since):
                return "done" if base == "COMPLETED" else "failed"
            return "collecting"
        if base == "COMPLETED":
            return "done"
        return "failed"
    if evidence == "missing":
        return "unknown"
    return "submitted"


def _ingest_new_activity_result(log_dir: str, results_path: str, rack_hint: str | None) -> None:
    """Persist a newly discovered terminal result in SQLite and the Parquet lake.

    LLMB submission wrappers return as soon as ``sbatch`` accepts the job.  The real
    aggregated result appears later, so the generic async worker's terminal hook cannot
    capture it.  Activity hydration is the first place that sees that final path; ingest
    exactly on the empty-path -> result-path transition to avoid adding recurring reads or
    SSH work to dashboard polling.
    """
    if not results_path or not _universal_enabled() or not core.history_enabled():
        return
    try:
        if results_history.latest_run_for_log(results_path) is not None:
            return
        parsed = _universal_fallback_for_log(
            Path(log_dir),
            results_path,
            rack_hint=rack_hint,
            ingest=True,
        )
        if not parsed or not parsed.get("history_run_id"):
            _logger.warning("activity result ingest produced no run for %s", results_path)
    except Exception as e:  # noqa: BLE001 - capture must not break activity polling
        _logger.warning("activity result ingest failed for %s: %s", results_path, e)


def _hydrate_activity_task_logs(activity_id: str, rec: dict | None = None) -> None:
    """Evidence ladder: squeue → scontrol/sacct → result artefacts; fail-closed on purge."""
    aid = (activity_id or "").strip()
    if not aid:
        return
    if rec is None:
        rec = validation_activities.get_activity(aid)
    if not rec:
        return
    if rec.get("source") == "clusterkit-api":
        from clusterkit_service import refresh_activity_record
        refreshed = refresh_activity_record(aid, validation_activities)
        rec.update(refreshed["activity"])
        return
    log_dir = str(rec.get("log_dir") or "")
    tasks = [t for t in (rec.get("tasks") or []) if isinstance(t, dict)]
    # Batch squeue lookup for all refs that still need refresh.
    want_ids = [
        str(t.get("scheduler_ref") or "").strip().split(".")[0]
        for t in tasks
        if _task_needs_slurm_hydration(t)
    ]
    running: set[str] = set()
    job_times: dict[str, str] = {}
    if want_ids:
        try:
            running, job_times = native_status.load_squeue_jobs_map()
            native_status.augment_running_set_for_job_ids(running, job_times, want_ids)
        except Exception:  # noqa: BLE001 — hydration is best-effort
            running, job_times = set(), {}

    for task in tasks:
        if not _task_needs_slurm_hydration(task):
            continue
        ref = str(task.get("scheduler_ref") or "").strip().split(".")[0]
        label = str(task.get("label") or "validation")
        stdout_path = str(task.get("stdout_path") or "").strip() or None
        results_path = str(task.get("results_log_path") or "").strip() or None
        had_results_path = bool(results_path)
        evidence = "none"
        state: str | None = None

        if ref in running:
            state = "RUNNING"
            evidence = "squeue"
        else:
            state, sc_stdout, evidence = _query_scheduler_state_and_stdout(ref)
            if sc_stdout:
                stdout_path = sc_stdout

        # Bind aggregated result log when possible (and promote intermediate StdOut).
        if not results_path:
            found = _find_results_log_on_exec_host(log_dir, ref, label)
            if found:
                results_path = found
        if stdout_path and not _stdout_is_llmb_intermediate(stdout_path):
            low = Path(stdout_path).name.lower()
            if "nccl-1rack" in low or "nemo-1rack" in low:
                results_path = results_path or stdout_path

        if evidence == "missing" and not results_path:
            # Fail-closed: do not keep a stale RUNNING after scontrol/sacct purge.
            prev = _slurm_job_state_base(str(task.get("scheduler_state") or ""))
            if prev in _SLURM_RUNNING_JOB_STATES or not prev:
                state = "UNKNOWN"
            else:
                state = prev or "UNKNOWN"

        collecting_since = str(task.get("collecting_since") or "").strip() or None
        work_phase = _derive_task_work_phase(
            scheduler_state=state,
            evidence=evidence,
            stdout_path=stdout_path,
            results_log_path=results_path,
            label=label,
            collecting_since=collecting_since,
        )
        # Stamp the clock on entry so the grace period is measured from when the
        # task first started waiting on a collector, not from the latest refresh.
        if work_phase == "collecting":
            collecting_since = collecting_since or datetime.now(timezone.utc).isoformat()
        else:
            collecting_since = ""  # empty string clears the stored value
        task_phase = str(task.get("phase") or "")
        if work_phase == "running":
            task_phase = "running"
        elif work_phase in ("done", "failed"):
            task_phase = "done" if work_phase == "done" else "error"

        # This is deliberately a one-shot transition hook. A periodic whole-directory scan
        # would add head-node I/O on every UI poll; one capped read when the final artifact
        # first appears captures the run without turning observability into workload.
        if results_path and not had_results_path and work_phase in ("done", "failed"):
            _ingest_new_activity_result(log_dir, results_path, str(rec.get("rack") or "") or None)

        validation_activities.add_or_update_task(
            aid,
            ordinal=int(task.get("ordinal") or 1),
            label=label,
            task_id=str(task.get("task_id") or ""),
            scheduler_state=state,
            stdout_path=stdout_path,
            results_log_path=results_path,
            work_phase=work_phase,
            evidence=evidence,
            phase=task_phase or None,
            collecting_since=collecting_since,
        )
        task["scheduler_state"] = state
        if stdout_path is not None:
            task["stdout_path"] = stdout_path
        if results_path is not None:
            task["results_log_path"] = results_path
        task["work_phase"] = work_phase
        task["evidence"] = evidence
        task["collecting_since"] = collecting_since or None
        if task_phase:
            task["phase"] = task_phase

    rec["work_phase"] = validation_activities.rollup_work_phase(tasks)


def _update_validation_activity(activity_id: str, **kwargs: object) -> None:
    """Persist activity fields and hydrate Slurm task logs when wrapper stdout carries job ids."""
    validation_activities.update_activity(activity_id, **kwargs)
    _hydrate_activity_task_logs(activity_id)
