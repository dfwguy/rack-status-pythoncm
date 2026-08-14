# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Local (subprocess) execution mechanism."""
from __future__ import annotations

import fnmatch
import gzip
import os
import shutil
import subprocess

from transport.base import (
    DEFAULT_HEAD_BYTES,
    DEFAULT_TAIL_BYTES,
    Connection,
    Executor,
    FileStat,
    Process,
    merge_env,
)


class LocalExecutor(Executor):
    """Runs commands directly on the machine hosting the app."""

    kind = "local"

    # -- execution ----------------------------------------------------------

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
                cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env=merged, check=False,
            )
        bash_flag = "-lc" if login_shell else "-c"
        return subprocess.run(
            ["bash", bash_flag, cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=merged,
            check=False,
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

    # -- filesystem ---------------------------------------------------------

    def resolve(self, path: object) -> str:
        return os.path.realpath(os.path.expanduser(str(path)))

    def stat(self, path: object) -> FileStat | None:
        try:
            st = os.stat(str(path))
        except OSError:
            return None
        return FileStat(mtime=float(st.st_mtime), size=int(st.st_size))

    def stat_dir(
        self,
        base: object,
        *,
        patterns: tuple[str, ...] | None = None,
        maxdepth: int = 1,
    ) -> dict[str, FileStat]:
        out: dict[str, FileStat] = {}
        root = str(base)
        depth = max(1, int(maxdepth))
        try:
            walker = os.walk(root)
        except OSError:
            return out
        root_depth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in walker:
            if dirpath.rstrip(os.sep).count(os.sep) - root_depth >= depth - 1:
                dirnames[:] = []
            for name in filenames:
                if patterns and not any(fnmatch.fnmatch(name, p) for p in patterns):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                    if not os.path.isfile(full):
                        continue
                except OSError:
                    continue
                out[full] = FileStat(mtime=float(st.st_mtime), size=int(st.st_size))
        return out

    def makedirs(self, path: object) -> str | None:
        try:
            os.makedirs(str(path), exist_ok=True)
        except OSError as e:
            return f"{type(e).__name__}: {e}"
        return None

    def write_bytes(self, path: object, data: bytes) -> str | None:
        target = str(path)
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # Write-then-rename so a reader never sees a half-written artifact.
            tmp = f"{target}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except OSError as e:
            return f"{type(e).__name__}: {e}"
        return None

    def read_text(
        self,
        path: object,
        *,
        head_bytes: int = DEFAULT_HEAD_BYTES,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        whole_bytes: int | None = None,
        timeout: int = 120,
    ) -> tuple[str, dict[str, object]]:
        path_s = str(path)
        is_gzip = path_s.endswith(".gz")
        cap = whole_bytes if whole_bytes is not None else (head_bytes + tail_bytes)
        meta: dict[str, object] = {
            "bytes": None, "read_bytes": 0, "truncated": False, "gzip": is_gzip,
            "read_error": None, "target": self.kind,
        }
        try:
            size = os.path.getsize(path_s)
        except OSError as e:
            meta["read_error"] = f"{type(e).__name__}: {e}"
            return "", meta
        meta["bytes"] = size
        try:
            opener = gzip.open if is_gzip else open
            with opener(path_s, "rb") as fh:  # type: ignore[operator]
                if is_gzip or size <= cap:
                    # A gzip member's uncompressed length is unknown up front, so
                    # cap the decompressed stream rather than the file size.
                    raw = fh.read(cap + 1)
                    if len(raw) > cap:
                        raw = raw[:cap]
                        meta["truncated"] = True
                else:
                    head = fh.read(head_bytes)
                    fh.seek(-tail_bytes, os.SEEK_END)
                    tail = fh.read(tail_bytes)
                    elided = size - head_bytes - tail_bytes
                    raw = head + f"\n...[{elided} bytes elided]...\n".encode() + tail
                    meta["truncated"] = True
        except OSError as e:
            meta["read_error"] = f"{type(e).__name__}: {e}"
            return "", meta
        meta["read_bytes"] = len(raw)
        return raw.decode("utf-8", errors="replace"), meta

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def _test_flag(self, path: object, flag: str) -> bool:
        p = str(path)
        if flag == "-d":
            return os.path.isdir(p)
        if flag == "-f":
            return os.path.isfile(p)
        return os.path.exists(p)

    def describe(self) -> dict[str, object]:
        return {"kind": self.kind, "host": None}


def LocalConnection() -> Connection:
    """Back-compat factory: a :class:`Connection` over a :class:`LocalExecutor`."""
    return Connection(LocalExecutor())
