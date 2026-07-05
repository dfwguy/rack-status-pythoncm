"""Local (subprocess) leaf connection."""
from __future__ import annotations

import subprocess

from transport.base import Connection, Process, merge_env


class LocalConnection(Connection):
    """Runs commands directly on the machine hosting the app (the head node)."""

    def is_remote(self) -> bool:
        return False

    def run(
        self,
        cmd,
        *,
        timeout: int = 120,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        login_shell: bool = True,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged = merge_env(env, env_extra)
        if isinstance(cmd, list):
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=merged
            )
        bash_flag = "-lc" if login_shell else "-c"
        return subprocess.run(
            ["bash", bash_flag, cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=merged,
        )

    def popen(
        self,
        cmd,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> Process:
        merged = merge_env(env, env_extra)
        if isinstance(cmd, list):
            proc = subprocess.Popen(
                cmd, cwd=cwd, env=merged, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        else:
            proc = subprocess.Popen(
                ["bash", "-lc", cmd],
                cwd=cwd,
                env=merged,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        return Process(_proc=proc)

    def wrap_argv(self, cmd, *, cwd: str | None = None, env_extra=None) -> list[str]:
        # Local: return the command as-is (historical wrap_for_host behaviour).
        return list(cmd) if isinstance(cmd, list) else ["bash", "-lc", cmd]
