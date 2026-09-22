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
"""PythonCM scheduler backend, for a BCM cluster with no Slurm.

BCM ships a Python client that talks to CMDaemon directly, and its ProgramRunner can run a
command across an exact node list. That is enough to be a `Scheduler`, so a cluster without a
workload manager still gets launches, status and cancellation through the same interface
every caller already uses.

**The library is never imported into this process.** `pythoncm` is generated against a
specific CMDaemon release, and one long-lived Greenlight serves clusters on different BCM
versions -- importing it here would pin the whole product to one cluster's release. Every
call is delegated over the configured `Connection` to `bin/scripts/nvis_pythoncm_exec.py`
**on the head**, which runs under the head's own matching interpreter. The consequence worth
stating: no new Python dependency, so there is nothing to vendor for an air-gapped site.

The wire between the two is one JSON object on stdout, and the helper is the only thing that
knows what `pythoncm` looks like. That boundary is what keeps a CMDaemon upgrade from being
a Greenlight release.
"""
from __future__ import annotations

import base64
import json
import logging
import time

import paths
from config_env import _eget

from schedulers.base import TERMINAL_STATES, JobHandle, JobResult, JobSpec, Scheduler

_logger = logging.getLogger(__name__)

#: Where `ProgramRunner` may place the work, mirrored in `JobSpec.launch_mode`.
LAUNCH_MODES = ("fanout", "leader", "head")

#: Interpreter on the head. Not ours to choose: it must be the one `pythoncm` was generated
#: against, which on BCM is the bundled Python rather than whatever `python3` resolves to.
_DEFAULT_HEAD_PYTHON = "/cm/local/apps/python312/bin/python3"

#: One authoritative status check per this many polls while waiting.
#:
#: The cheap offline poll cannot distinguish "still running" from "died without finishing",
#: because both look the same on disk. This bounds how long that confusion can last: at the
#: default five-second interval, a dead job is noticed within a minute, for one expensive
#: call per minute instead of twelve. Lower it and the head node pays; raise it and a dead
#: job holds its rack longer.
_VERIFY_EVERY = 12


class PythonCMScheduler(Scheduler):
    """Submit through BCM's ProgramRunner, via a helper on the head."""

    kind = "pythoncm"

    def __init__(self, connection, platform=None, profile: str = "control"):
        self.conn = connection
        self.platform = platform
        self.profile = profile
        self._helper: str = ""

    def _helper_path(self) -> str:
        """Resolve the helper once per scheduler, then reuse it.

        Correctness first, speed second. `script_tree` caches its layer probes for only 30
        seconds, so a long `wait` would re-resolve mid-job and could start polling a
        *different* project's copy of the helper if the active project changed underneath
        it — reading one job's state with another project's script. Pinning at construction
        ties a job to the helper it was submitted with.

        It also takes the remote directory probes out of the polling loop, which is the
        point of the offline poll: one round trip, nothing else.
        """
        if not self._helper:
            self._helper = str(paths._script("nvis_pythoncm_exec.py", "RACK_PYTHONCM_EXEC_SCRIPT"))
        return self._helper

    # -- plumbing ---------------------------------------------------------------------------

    def _run(self, cmd, timeout: int = 120):
        # RoutingConnection accepts profile=; a plain Connection does not. Same shape as
        # SlurmScheduler, deliberately: a provider should not care which it was handed.
        try:
            return self.conn.run(cmd, timeout=timeout, profile=self.profile)
        except TypeError:
            return self.conn.run(cmd, timeout=timeout)

    def _helper_argv(self, action: str, *args: str) -> list[str]:
        """Interpreter + helper path + action + connection flags.

        The helper is resolved through `paths._script`, so it follows the script tree's
        project / custom / platform / multi-platform layers like every other script. Reading
        ``RACK_REMOTE_BASE_PATH`` here instead -- as the source this was ported from did --
        is the bug that made two resolvers disagree about which copy of a script runs.
        """
        argv = [
            _eget("NVIS_BCM_PYTHON", "") or _DEFAULT_HEAD_PYTHON,
            self._helper_path(),
            action,
        ]
        # Only paths and non-secret identifiers. A password or cookie VALUE never reaches
        # argv, because argv is world-readable in `ps` on a shared head node; the helper
        # reads the file itself.
        for flag, key in (
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
            value = (_eget(key, "") or "").strip()
            if value:
                argv += [flag, value]
        return argv + list(args)

    def _jobs_dir(self) -> str:
        """Where the helper keeps durable per-task state on the head.

        Durable because CMDaemon **drops a completed parallel command** from its active
        status, so once a job ends the only record of how it went is what the helper wrote.
        That is the role `sacct` plays for Slurm.
        """
        explicit = (_eget("RACK_PYTHONCM_JOBS_DIR", "") or "").strip()
        if explicit:
            return explicit
        base = (_eget("RACK_REMOTE_BASE_PATH", "") or "").strip()
        return f"{base.rstrip('/')}/var/pythoncm-jobs" if base else "/var/tmp/nvis-pythoncm-jobs"

    @staticmethod
    def _json_from_output(stdout: str) -> dict:
        """The last JSON object on stdout.

        Last rather than first: a login shell on the head may print a banner, an MOTD or a
        module-load line before the helper runs, and none of that is ours to suppress.
        """
        for line in reversed((stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def _call(self, action: str, *args: str, timeout: int = 120) -> dict:
        argv = self._helper_argv(action, *args)
        proc = self._run(argv, timeout=timeout)
        result = self._json_from_output(proc.stdout or "")
        if proc.returncode != 0 or not result.get("ok"):
            detail = str(result.get("error") or (proc.stderr or "").strip() or "no output")
            hint = str(result.get("hint") or "")
            raise RuntimeError(
                f"pythoncm {action} failed: {detail[:400]}" + (f" ({hint[:200]})" if hint else "")
            )
        return result

    # -- Scheduler --------------------------------------------------------------------------

    def probe(self, *, timeout: int = 30) -> dict:
        """Is the head able to run a parallel command for us right now?

        Not part of the `Scheduler` interface, and deliberately non-mutating. Worth having
        because the three prerequisites -- the helper being deployed, a `pythoncm` matching
        the head's CMDaemon, and usable certificates -- fail in ways that look identical from
        a failed submit. Asking first turns one confusing launch failure into a clear answer.
        """
        return self._call("probe", timeout=timeout)

    def _payload(self, spec: JobSpec) -> str:
        body = {
            "name": spec.name,
            "nodelist": spec.nodelist,
            "nodes": spec.nodes,
            "ntasks_per_node": spec.ntasks_per_node,
            "script": spec.script,
            "command": spec.command,
            "output": spec.output,
            "env": dict(spec.env or {}),
            "launch_mode": spec.launch_mode,
            "execution_user": spec.execution_user or (_eget("RACK_PYTHONCM_USER", "") or "root"),
            "timeout": int(spec.timeout or 1800),
            "jobs_dir": self._jobs_dir(),
        }
        blob = json.dumps(body, separators=(",", ":")).encode("utf-8")
        # Base64 so a command containing quotes, newlines or shell metacharacters survives
        # the trip through a login shell intact. Nothing here is secret; this is quoting.
        return base64.urlsafe_b64encode(blob).decode("ascii")

    def submit(self, spec: JobSpec) -> JobHandle:
        if not (spec.script or spec.command):
            raise ValueError("PythonCMScheduler.submit needs a script or command")
        if spec.launch_mode not in LAUNCH_MODES:
            raise ValueError(
                f"unknown launch_mode {spec.launch_mode!r} (expected one of {LAUNCH_MODES})"
            )
        if (spec.nodes or 0) > 1 and not spec.nodelist:
            # A count alone cannot be honoured: there is no queue to choose nodes for us, so
            # "give me 18 nodes" has no meaning without saying which. Refusing beats picking.
            raise ValueError(
                "PythonCMScheduler needs an explicit nodelist for multi-node work; "
                "there is no queue to allocate from"
            )
        result = self._call("submit", "--payload", self._payload(spec), timeout=180)
        native_id = str(result.get("pythoncm_id") or "").strip()
        task_id = str(result.get("task_id") or "").strip()
        if not native_id:
            raise RuntimeError("pythoncm submit returned no ProgramRunner id")
        # The handle carries the ProgramRunner id, not our private task id: it is the one an
        # operator can find on the head with BCM's own tooling.
        return JobHandle(
            job_id=native_id,
            kind=self.kind,
            submit_output=json.dumps({"task_id": task_id, "pythoncm_id": native_id}),
        )

    def _status_payload(self, handle: JobHandle, *, offline: bool = False) -> dict:
        args = ["--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id]
        if offline:
            args.append("--offline")
        return self._call("status", *args, timeout=60)

    def status(self, handle: JobHandle) -> str:
        """The authoritative state. Always asks CMDaemon if the files cannot prove one."""
        return str(self._status_payload(handle).get("state") or "PENDING").upper()

    def cancel(self, handle: JobHandle) -> None:
        self._call(
            "cancel", "--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id, timeout=60
        )

    @staticmethod
    def _decode(value) -> str:
        if not value:
            return ""
        try:
            return base64.b64decode(str(value)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return ""

    def _result(self, handle: JobHandle, state: str) -> JobResult:
        payload = self._call(
            "result", "--jobs-dir", self._jobs_dir(), "--task-id", handle.job_id, timeout=120
        )
        stdout = self._decode(payload.get("stdout_b64"))
        nodes = payload.get("nodes")
        return JobResult(
            returncode=int(payload.get("returncode") or 0),
            state=str(payload.get("state") or state).upper(),
            stdout=stdout,
            stderr=self._decode(payload.get("stderr_b64")),
            job_id=handle.job_id,
            node_results=list(nodes) if isinstance(nodes, list) else [],
        )

    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        """Poll to a deadline, cheaply, and cancel rather than abandon on expiry.

        Most polls are `--offline`: they read the durable task files and cost one round
        trip. That matters because the expensive part of a PythonCM call is constructing
        the client -- about ten CPU-seconds on a large controller -- so a naive five-second
        poll would spend more of the head node's CPU watching an hour-long job than running
        it. A terminal state on disk is exact, so the cheap path still ends the wait
        correctly; only "not finished yet" is a guess.

        The guess has one blind spot: a job whose ranks died looks exactly like one still
        running. So every `_VERIFY_EVERY` polls one authoritative check runs, which bounds
        how long a dead job can masquerade as a live one without putting CMDaemon in the
        hot path.
        """
        deadline = time.monotonic() + max(1, timeout)
        polls = 0
        while time.monotonic() < deadline:
            polls += 1
            verify = polls % _VERIFY_EVERY == 0
            payload = self._status_payload(handle, offline=not verify)
            state = str(payload.get("state") or "PENDING").upper()
            # A terminal state is trusted from either path; an unauthoritative RUNNING is
            # only ever a reason to keep waiting, never to conclude.
            if state in TERMINAL_STATES:
                return self._result(handle, state)
            time.sleep(max(1, poll_interval))
        # Out of budget. Stop the work rather than only the waiting: a ProgramRunner command
        # outlives this process, and the next job's prolog would flag the leftovers as rogue
        # processes and drain the rack (`bcm-prolog-rogueprocess.mdc`).
        try:
            self.cancel(handle)
        except RuntimeError as exc:  # pragma: no cover - the timeout is the story, not this
            _logger.warning("pythoncm: could not cancel %s after timeout: %s", handle.job_id, exc)
        return JobResult(returncode=1, state="TIMEOUT", job_id=handle.job_id)
