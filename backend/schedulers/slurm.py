"""Slurm scheduler backend (sbatch submit + sacct/squeue polling)."""
from __future__ import annotations

import shlex
import time

from schedulers.base import TERMINAL_STATES, JobHandle, JobResult, JobSpec, Scheduler

# Make the Slurm CLI usable over a bare (non-login, non-interactive) SSH/exec shell,
# mirroring bin/scripts/slurm-init.inc.sh: init Modules, add the BCM modulefile dirs
# to MODULEPATH (a bare init omits them), `module load slurm`, then fall back to the
# slurm bin dir on PATH (override via RACK_SLURM_BIN) so `sbatch` resolves regardless.
_SLURM_INIT = (
    "if ! command -v module >/dev/null 2>&1; then "
    "for _i in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh /usr/share/Modules/init/bash "
    "/cm/local/apps/environment-modules/current/init/bash; do [ -r \"$_i\" ] && { . \"$_i\"; break; }; done; fi; "
    "for _m in /cm/local/modulefiles /cm/shared/modulefiles; do [ -d \"$_m\" ] && MODULEPATH=\"$_m:${MODULEPATH:-}\"; done; export MODULEPATH; "
    "module load slurm 2>/dev/null || true; "
    "command -v sbatch >/dev/null 2>&1 || export PATH=\"${RACK_SLURM_BIN:-/cm/local/apps/slurm/current/bin}:$PATH\"; "
)


class SlurmScheduler(Scheduler):
    kind = "slurm"

    def __init__(self, connection, platform=None, profile: str = "control"):
        self.conn = connection
        self.platform = platform
        self.profile = profile

    def _run(self, cmd: str, timeout: int = 120):
        full = _SLURM_INIT + cmd
        # RoutingConnection accepts profile=; plain Connection does not.
        try:
            return self.conn.run(full, timeout=timeout, profile=self.profile)
        except TypeError:
            return self.conn.run(full, timeout=timeout)

    def _sbatch_argv(self, spec: JobSpec) -> list[str]:
        args = ["sbatch", "--parsable", f"--job-name={spec.name}"]
        if spec.nodes:
            args += ["-N", str(spec.nodes)]
        if spec.nodelist:
            args.append(f"--nodelist={spec.nodelist}")
        if spec.ntasks_per_node:
            args.append(f"--ntasks-per-node={spec.ntasks_per_node}")
        if spec.output:
            args.append(f"--output={spec.output}")
        if spec.reservation:
            args.append(f"--reservation={spec.reservation}")
        args += list(spec.extra_args)
        if spec.script:
            args.append(spec.script)
        elif spec.command:
            args += ["--wrap", spec.command]
        return args

    def submit(self, spec: JobSpec) -> JobHandle:
        if not (spec.script or spec.command):
            raise ValueError("SlurmScheduler.submit needs a script or command")
        cmd = shlex.join(self._sbatch_argv(spec))
        proc = self._run(cmd, timeout=120)
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 or not out:
            raise RuntimeError(f"sbatch failed (rc={proc.returncode}): {(proc.stderr or out).strip()[:500]}")
        # --parsable => "<jobid>[;cluster]" on the first line.
        job_id = out.splitlines()[0].split(";")[0].strip()
        return JobHandle(job_id=job_id, kind=self.kind, submit_output=out)

    def status(self, handle: JobHandle) -> str:
        proc = self._run(f"sacct -j {shlex.quote(handle.job_id)} -X -n -o State%30", timeout=60)
        line = (proc.stdout or "").strip().splitlines()
        if not line:
            return "PENDING"  # too new for sacct
        # Normalize "CANCELLED+" / "CANCELLED by ..." -> "CANCELLED".
        return line[0].strip().split()[0].rstrip("+").upper()

    def cancel(self, handle: JobHandle) -> None:
        self._run(f"scancel {shlex.quote(handle.job_id)}", timeout=60)

    def wait(self, handle: JobHandle, *, timeout: int = 1800, poll_interval: int = 5) -> JobResult:
        deadline = time.monotonic() + max(1, timeout)
        state = "PENDING"
        while time.monotonic() < deadline:
            state = self.status(handle)
            if state in TERMINAL_STATES:
                break
            time.sleep(max(1, poll_interval))
        stdout = self._read_output(handle) if state in TERMINAL_STATES else ""
        rc = 0 if state == "COMPLETED" else 1
        return JobResult(returncode=rc, state=state, stdout=stdout, job_id=handle.job_id)

    def _read_output(self, handle: JobHandle) -> str:
        """Best-effort: resolve the job's StdOut via scontrol and cat it."""
        proc = self._run(
            f"scontrol show job {shlex.quote(handle.job_id)} 2>/dev/null | "
            "grep -oE 'StdOut=[^ ]+' | head -1 | cut -d= -f2-", timeout=60,
        )
        path = (proc.stdout or "").strip().splitlines()
        if not path or not path[0]:
            return ""
        cat = self._run(f"cat {shlex.quote(path[0])} 2>/dev/null", timeout=120)
        return cat.stdout or ""
