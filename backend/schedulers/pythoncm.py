# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""BCM PythonCM scheduler backend.

PythonCM is version-locked to CMDaemon, so it is deliberately not imported by
the web process.  Every operation is delegated, through the configured
``Connection``, to ``nvis_pythoncm_exec.py`` on the BCM head.  The helper uses
``cluster.parallel.execute(wait=False)`` and returns its reconnectable UUID.

The helper also keeps a small, durable task directory on the execution host.
That closes an important PythonCM lifecycle gap: completed parallel commands
fall out of ``active_command_status()``, whereas Rack Status must retain their
exit status and logs after completion and across a web-process restart.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from schedulers.base import TERMINAL_STATES, JobHandle, JobResult, JobSpec, Scheduler

_logger = logging.getLogger(__name__)


class PythonCMScheduler(Scheduler):
    """Run exact-node work with BCM's native parallel-command service."""

    kind = "pythoncm"

    def __init__(
        self,
        connection,
        platform=None,
        profile: str = "control",
        *,
        env: dict[str, str] | None = None,
    ):
        self.conn = connection
        self.platform = platform  # accepted for the common provider constructor
        self.profile = profile
        self.env = env if env is not None else os.environ

    def _run(self, cmd: list[str], timeout: int = 120):
        try:
            return self.conn.run(cmd, timeout=timeout, profile=self.profile)
        except TypeError:
            return self.conn.run(cmd, timeout=timeout)

    def _scripts_dir(self) -> str:
        base = (self.env.get("RACK_REMOTE_BASE_PATH") or "").strip()
        if base:
            return f"{base.rstrip('/')}/bin/scripts"
        return str(Path(__file__).resolve().parents[2] / "bin" / "scripts")

    def _helper_argv(self, action: str, *args: str) -> list[str]:
        python = (
            self.env.get("NVIS_BCM_PYTHON")
            or "/cm/local/apps/python312/bin/python3"
        )
        helper = self.env.get("RACK_PYTHONCM_EXEC_SCRIPT") or (
            f"{self._scripts_dir()}/nvis_pythoncm_exec.py"
        )
        argv = [python, helper, action]
        for flag, name in (
            ("--module-path", "NVIS_PYTHONCM_PATH"),
            ("--host", "NVIS_BCM_HOST"),
            ("--port", "NVIS_BCM_PORT"),
            ("--cert", "NVIS_BCM_CERT"),
            ("--key", "NVIS_BCM_KEY"),
            ("--ca", "NVIS_BCM_CA"),
            ("--username", "NVIS_BCM_USERNAME"),
            ("--password-file", "NVIS_BCM_PASSWORD_FILE"),
            ("--cookie-file", "NVIS_BCM_COOKIE_FILE"),
        ):
            value = (self.env.get(name) or "").strip()
            if value:
                argv.extend((flag, value))
        return [*argv, *args]

    def _jobs_dir(self) -> str:
        configured = (self.env.get("RACK_PYTHONCM_JOBS_DIR") or "").strip()
        if configured:
            return configured.rstrip("/")
        base = (self.env.get("RACK_REMOTE_BASE_PATH") or "").strip()
        if base:
            return f"{base.rstrip('/')}/var/pythoncm-jobs"
        return "/var/tmp/nvis-pythoncm-jobs"

    @staticmethod
    def _json_from_output(stdout: str) -> dict[str, Any]:
        # SSH banners and module messages may precede the protocol response.
        for line in reversed((stdout or "").splitlines()):
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                return value
        raise RuntimeError("PythonCM helper returned no JSON response")

    def _call(self, action: str, *args: str, timeout: int = 120) -> dict[str, Any]:
        argv = self._helper_argv(action, *args)
        # Execution traffic has a dedicated broker so NCCL/NeMo submit/status
        # calls can never queue ahead of Rack Status control-plane reads.
        broker_socket = (
            self.env.get("NVIS_PYTHONCM_EXEC_SOCKET")
            or self.env.get("NVIS_PYTHONCM_SOCKET")
            or ""
        ).strip()
        proc = None
        result = None
        if broker_socket:
            import pythoncm_broker_client

            try:
                result = pythoncm_broker_client.request(
                    broker_socket, "exec", argv[2:], timeout=timeout
                )
            except (FileNotFoundError, ConnectionRefusedError):
                _logger.warning("PythonCM broker unavailable; using one-shot execution helper")
            except Exception as exc:
                # The broker accepted or partially handled the request. Submit and
                # cancel must never be replayed through the one-shot fallback.
                raise RuntimeError(
                    f"persistent PythonCM {action} broker failed: {type(exc).__name__}: {exc}"
                ) from exc
        if result is None:
            proc = self._run(argv, timeout=timeout)
            try:
                result = self._json_from_output(proc.stdout or "")
            except RuntimeError as exc:
                detail = (proc.stderr or proc.stdout or "").strip()[:1000]
                raise RuntimeError(
                    f"PythonCM {action} failed (rc={proc.returncode}): {detail or exc}"
                ) from exc
        if (proc is not None and proc.returncode != 0) or not result.get("ok"):
            error = result.get("error") or (
                (proc.stderr or "").strip() if proc is not None else ""
            ) or "unknown error"
            hint = result.get("hint")
            raise RuntimeError(
                f"PythonCM {action} failed: {error}" + (f" ({hint})" if hint else "")
            )
        return result

    def probe(self, *, timeout: int = 30) -> dict[str, Any]:
        """Verify that the execution path can run ``cmproc`` without submitting work.

        Query-only certificate authentication is insufficient for ProgramRunner.  The
        helper's ``probe`` action opens the execution-capable PythonCM connection but does
        not create a command, so callers can choose a reviewed fallback before any submit
        has been accepted.  Submit failures themselves must never be replayed.
        """
        return self._call("probe", timeout=max(1, timeout))

    @staticmethod
    def _payload(spec: JobSpec, jobs_dir: str) -> str:
        payload = {
            "name": spec.name,
            "nodelist": spec.nodelist or "",
            "nodes": spec.nodes,
            "ntasks_per_node": spec.ntasks_per_node,
            "script": spec.script or "",
            "command": spec.command or "",
            "output": spec.output or "",
            "env": spec.env,
            "launch_mode": spec.launch_mode,
            "execution_user": spec.execution_user or "",
            "timeout": spec.timeout,
            "jobs_dir": jobs_dir,
        }
        blob = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(blob).decode("ascii")

    def submit(self, spec: JobSpec) -> JobHandle:
        if not (spec.script or spec.command):
            raise ValueError("PythonCMScheduler.submit needs a script or command")
        if spec.nodes and spec.nodes > 1 and not spec.nodelist:
            raise ValueError(
                "PythonCMScheduler requires an exact nodelist for multi-node work"
            )
        if spec.launch_mode not in {"fanout", "leader", "head"}:
            raise ValueError("PythonCMScheduler launch_mode must be fanout, leader, or head")
        result = self._call("submit", "--payload", self._payload(spec, self._jobs_dir()))
        task_id = str(result.get("task_id") or "").strip()
        native_id = str(result.get("pythoncm_id") or "").strip()
        if not task_id or not native_id:
            raise RuntimeError("PythonCM submit did not return task_id and pythoncm_id")
        return JobHandle(
            # The CMDaemon ProgramRunner UUID is the authoritative scheduler id.
            # The helper's task_id is only the durable on-disk directory key.
            job_id=native_id,
            kind=self.kind,
            submit_output=json.dumps(
                {"task_id": task_id, "pythoncm_id": native_id}, sort_keys=True
            ),
        )

    def _status_payload(self, handle: JobHandle) -> dict[str, Any]:
        return self._call(
            "status", "--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id,
            timeout=60,
        )

    def status(self, handle: JobHandle) -> str:
        return str(self._status_payload(handle).get("state") or "UNKNOWN").upper()

    def cancel(self, handle: JobHandle) -> None:
        self._call(
            "cancel", "--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id,
            timeout=60,
        )

    @staticmethod
    def _decode(value: object) -> str:
        if not value:
            return ""
        try:
            return base64.b64decode(str(value)).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""

    def _result(self, handle: JobHandle) -> JobResult:
        payload = self._call(
            "result", "--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id,
            timeout=120,
        )
        state = str(payload.get("state") or "UNKNOWN").upper()
        stdout = self._decode(payload.get("stdout_b64"))
        stderr = self._decode(payload.get("stderr_b64"))
        try:
            rc = int(payload.get("returncode", 1))
        except (TypeError, ValueError):
            rc = 1
        output_path = str(payload.get("output") or "")
        if output_path:
            output_path = output_path.replace("%j", handle.job_id)
            error = self.conn.write_bytes(output_path, stdout.encode("utf-8"))
            if error:
                stderr += ("\n" if stderr else "") + f"unable to write {output_path}: {error}"
                if rc == 0:
                    rc = 1
                    state = "FAILED"
        node_results = [
            dict(row) for row in (payload.get("nodes") or []) if isinstance(row, dict)
        ]
        return JobResult(
            rc, state, stdout, stderr, handle.job_id, node_results=node_results
        )

    def wait(
        self,
        handle: JobHandle,
        *,
        timeout: int = 1800,
        poll_interval: int = 5,
    ) -> JobResult:
        deadline = time.monotonic() + max(1, timeout)
        state = "PENDING"
        while time.monotonic() < deadline:
            state = self.status(handle)
            if state in TERMINAL_STATES:
                return self._result(handle)
            time.sleep(max(0.05, poll_interval))
        try:
            self.cancel(handle)
        except Exception as exc:
            raise RuntimeError(
                f"PythonCM command {handle.job_id} timed out and cancellation failed: {exc}"
            ) from exc
        return JobResult(
            1,
            "CANCELLED",
            stderr="timeout expired; PythonCM cancellation requested",
            job_id=handle.job_id,
        )
