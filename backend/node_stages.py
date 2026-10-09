# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Stage 1 and Stage 2 per node: the ladder below the rack.

A rack is validated by a run over all of its nodes; before that, each node earns its own two
stages, and the rack page shows them per host.

- **Stage 1** is the single-node NCCL run (the Stage-1 launcher with one host as its target). Its
  evidence is the run's result log, which the native rack workers attribute to the host it covered
  (``native_status._is_partial_rack_log``), and, while the job is still in Slurm, the open
  ``scope="node"`` activity that launched it.
- **Stage 2** is a single-node LLM training run. The NeMo Stage-2 recipe does not fit one node
  (the 56B GB300 config runs out of memory on 4 GPUs, and no smaller GB300 config ships in the
  container), so the node's Stage 2 is the bring-up framework's single-node LLM job, read with that
  framework's own verdicts. Which job is ``RACK_NODE_STAGE2_BRINGUP_JOB`` (default
  ``llm-pretrain-solo``).

Both answer in the rack table's vocabulary -- ``done``, ``fail``, ``ongoing``, ``pending`` -- so
the UI renders them with the same pills.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from config_env import _eget, _env_int

_logger = logging.getLogger(__name__)

DEFAULT_STAGE2_BRINGUP_JOB = "llm-pretrain-solo"

# Bring-up verdict word -> stage state. RETRY and INCOMPLETE mean the framework will run it again.
_BRINGUP_STATE = {
    "DONE": "done",
    "FAIL": "fail",
    "RUNNING": "ongoing",
    "RETRY": "ongoing",
    "INCOMPLETE": "ongoing",
    "NOT_RUN": "pending",
}


def stage2_bringup_job() -> str:
    return (_eget("RACK_NODE_STAGE2_BRINGUP_JOB") or "").strip() or DEFAULT_STAGE2_BRINGUP_JOB


def stage1_state(probe: dict[str, Any] | None, live_jid: str, running: set[str]) -> dict[str, Any]:
    """A node's Stage 1 from its newest single-node log and any live launch.

    A live launch wins: its result log is written only when the job ends, so the log on disk is
    the previous attempt's.
    """
    if live_jid and live_jid in running:
        return {"state": "ongoing", "job_id": live_jid}
    if not probe:
        return {"state": "pending"}
    jid = str(probe.get("jid") or "")
    out: dict[str, Any] = {"job_id": jid, "log": str(probe.get("_path") or "")}
    if jid and jid in running:
        out["state"] = "ongoing"
    elif probe.get("nccl_ok") is True:
        out["state"] = "done"
    else:
        out["state"] = "fail"
    return out


def stage2_state(verdict: dict[str, str] | None) -> dict[str, Any]:
    if not verdict:
        return {"state": "pending"}
    status = str(verdict.get("status") or "").upper()
    out: dict[str, Any] = {"state": _BRINGUP_STATE.get(status, "pending"), "status": status}
    reason = str(verdict.get("reason") or "").strip()
    if reason:
        out["reason"] = reason
    return out


class _BringupVerdicts:
    """The bring-up job's verdict for every node, refreshed in the background.

    The fleet status script takes about a minute on a 4,600-node cluster, which no page load
    should wait for. Readers get the last answer at once (``None`` until the first one lands) and
    an expired answer starts one refresh; concurrent readers never start a second.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, dict[str, str]]] | None = None
        self._at = 0.0
        self._refreshing = False
        self.error = ""

    def _ttl(self) -> int:
        return max(30, _env_int("RACK_NODE_STAGE_BRINGUP_TTL_SEC", 300))

    def get(self, *, wait: bool = False) -> dict[str, dict[str, dict[str, str]]] | None:
        with self._lock:
            stale = self._data is None or time.monotonic() - self._at >= self._ttl()
            start = stale and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            if wait:
                self._refresh()
            else:
                threading.Thread(target=self._refresh, name="node-stage-bringup", daemon=True).start()
        with self._lock:
            return self._data

    def _refresh(self) -> None:
        try:
            import bringup

            if not bringup.is_enabled():
                data: dict[str, dict[str, dict[str, str]]] = {}
            else:
                data = bringup.parse_status_check_fleet(bringup.run_status_check_all())
            with self._lock:
                self._data, self._at, self.error = data, time.monotonic(), ""
        except Exception as exc:  # noqa: BLE001 - keep the last answer; Stage 1 still renders
            _logger.warning("node stages: bring-up verdicts unavailable: %s", exc)
            with self._lock:
                self.error = str(exc)
                self._at = time.monotonic()
        finally:
            with self._lock:
                self._refreshing = False


_bringup = _BringupVerdicts()


def node_stages(
    log_dir: Path,
    nodes: list[tuple[str, str]],
    *,
    wait_for_bringup: bool = False,
) -> dict[str, Any]:
    """Stage 1 and Stage 2 for each ``(host, rack)`` in *nodes*."""
    import native_status
    import validation_activities

    running, times = native_status.load_squeue_jobs_map()
    node_logs = native_status.node_stage_logs_for_log_dir(log_dir) or {}
    try:
        live = validation_activities.live_stage_jobs_by_node()
    except Exception:  # noqa: BLE001 - the overlay is best-effort, like the rack table's
        live = {}
    # ``squeue -u <service user>`` misses jobs submitted as the operator who launched them, so ask
    # Slurm about the ones this answer depends on by id, exactly as the rack table does.
    hosts = {h for h, _ in nodes}
    want = {str((live.get(h) or {}).get("nccl") or "") for h in hosts}
    want |= {str(((node_logs.get(h) or {}).get("nccl") or {}).get("jid") or "") for h in hosts}
    native_status.augment_running_set_for_job_ids(running, times, sorted(j for j in want if j))
    job = stage2_bringup_job()
    verdicts = _bringup.get(wait=wait_for_bringup)
    rows = []
    for host, rack in nodes:
        logs = node_logs.get(host) or {}
        rows.append({
            "node": host,
            "rack": rack,
            "stage1": stage1_state(logs.get("nccl"), str((live.get(host) or {}).get("nccl") or ""), running),
            "stage2": stage2_state(((verdicts or {}).get(host) or {}).get(job))
            if verdicts is not None else {"state": "unknown"},
        })
    return {
        "nodes": rows,
        "stage2_source": f"bringup:{job}",
        "stage2_loading": verdicts is None,
        **({"stage2_error": _bringup.error} if _bringup.error else {}),
    }
