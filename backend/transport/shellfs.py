# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Filesystem access for targets reachable only by running a shell command.

Shared by every executor whose target is another machine (:class:`
~transport.ssh.SSHExecutor`, :class:`~transport.hop.HopExecutor`): the only
primitive available is "run a command over there", so ``stat`` becomes ``stat``,
``glob`` becomes ``find``, and reading a file means base64-ing it back.

Mixed into an Executor, it needs nothing but a working ``run()``. Every method is
total — transport failure is reported as absent/empty/``read_error`` rather than
raised, because an unreachable head node is something the UI renders, not
something a request handler should die on.
"""
from __future__ import annotations

import base64
import binascii
import os
import shlex
import subprocess
from uuid import uuid4

from transport.base import DEFAULT_HEAD_BYTES, DEFAULT_TAIL_BYTES, FileStat


class ShellFilesystem:
    """Implements the Executor filesystem API in terms of remote shell commands."""

    def _quiet_run(
        self,
        argv: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        """``run`` that converts transport failure into ``None`` instead of raising."""
        try:
            return self.run(argv, timeout=timeout)  # type: ignore[attr-defined]
        except (OSError, subprocess.SubprocessError):
            return None

    def resolve(self, path: object) -> str:
        # Deliberately offline: this is called on nearly every request, and the
        # remote path only needs textual normalisation. A local realpath() would
        # rewrite it through *this* container's filesystem. Collapsing ".." keeps
        # the containment checks that callers apply afterwards meaningful.
        return os.path.normpath(str(path))

    def stat(self, path: object) -> FileStat | None:
        proc = self._quiet_run(["stat", "-c", "%Y %s", str(path)], timeout=30)
        if proc is None or proc.returncode != 0:
            return None
        parts = (proc.stdout or "").strip().split()
        if len(parts) != 2:
            return None
        try:
            return FileStat(mtime=float(parts[0]), size=int(parts[1]))
        except (TypeError, ValueError):
            return None

    def stat_dir(
        self,
        base: object,
        *,
        patterns: tuple[str, ...] | None = None,
        maxdepth: int = 1,
    ) -> dict[str, FileStat]:
        argv = ["find", str(base), "-maxdepth", str(max(1, int(maxdepth))), "-type", "f"]
        if patterns:
            argv.append("(")
            for i, pat in enumerate(patterns):
                if i:
                    argv.append("-o")
                argv.extend(["-name", pat])
            argv.append(")")
        # One `find` with GNU -printf, not a stat round trip per file.
        argv.extend(["-printf", r"%p\t%s\t%T@\n"])
        proc = self._quiet_run(argv, timeout=60)
        out: dict[str, FileStat] = {}
        if proc is None or proc.returncode != 0:
            return out
        for line in (proc.stdout or "").splitlines():
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 3:
                continue
            try:
                out[fields[0]] = FileStat(mtime=float(fields[2]), size=int(fields[1]))
            except (TypeError, ValueError):
                continue
        return out

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
            "read_error": None, "target": getattr(self, "kind", "remote"),
        }
        # The payload is base64'd on the far side because stdout is decoded as
        # strict UTF-8 here, and a binary or latin-1 log would otherwise raise
        # mid-transfer. The size is printed on its own first line, ahead of the blob.
        reader = "zcat" if is_gzip else "cat"
        script = (
            'f="$1"; s=$(stat -c %s "$f" 2>/dev/null || echo -1); printf "%s\\n" "$s"; '
            '[ "$s" -lt 0 ] && exit 3; '
            f'if [ "$s" -le {cap} ]; then {reader} "$f"; '
            f'else head -c {head_bytes} "$f"; '
            f'printf "\\n...[%s bytes elided]...\\n" $((s-{cap})); '
            f'tail -c {tail_bytes} "$f"; fi | base64 | tr -d "\\n"'
        )
        proc = self._quiet_run(
            ["sh", "-c", script, "_", path_s], timeout=max(15, int(timeout))
        )
        if proc is None:
            meta["read_error"] = "remote read failed"
            return "", meta
        head_line, _, b64 = (proc.stdout or "").partition("\n")
        try:
            size = int(head_line.strip())
        except (TypeError, ValueError):
            size = -1
        if size < 0 or proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            meta["read_error"] = detail[-1][:300] if detail else "remote read failed"
            return "", meta
        meta["bytes"] = size
        meta["truncated"] = size > cap
        try:
            data = base64.b64decode(b64.strip() or "", validate=False)
        except (binascii.Error, ValueError) as e:
            meta["read_error"] = f"{type(e).__name__}: {e}"
            return "", meta
        meta["read_bytes"] = len(data)
        return data.decode("utf-8", errors="replace"), meta

    def which(self, name: str) -> str | None:
        proc = self._quiet_run(["command", "-v", name], timeout=30)
        if proc is None or proc.returncode != 0:
            return None
        found = (proc.stdout or "").strip().splitlines()
        return found[0] if found and found[0] else None

    def _test_flag(self, path: object, flag: str) -> bool:
        proc = self._quiet_run(["test", flag, str(path)], timeout=30)
        return bool(proc is not None and proc.returncode == 0)

    def makedirs(self, path: object) -> str | None:
        proc = self._quiet_run(["mkdir", "-p", str(path)], timeout=60)
        if proc is None:
            return "remote mkdir failed"
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            return detail[-1][:300] if detail else f"mkdir exit {proc.returncode}"
        return None

    def write_bytes(self, path: object, data: bytes) -> str | None:
        """Ship *data* base64-encoded and decode it on the far side.

        Base64 keeps arbitrary bytes (an .xlsx, a gzip) safe through the shell and
        the nested quoting of any hop chain. The payload goes inline in the command
        rather than over stdin, because a hop's payload is itself re-encoded at
        every leg and stdin is not plumbed through them.
        """
        target = str(path)
        parent = os.path.dirname(target)
        b64 = base64.b64encode(data).decode("ascii")
        # Decode to a temp file and rename, so a concurrent reader never sees a
        # partial artifact (mirrors the local executor's write-then-replace).
        #
        # The name carries a uuid because this one is REMOTE: two dashboards, or two workers,
        # writing the same artifact would otherwise share `<target>.tmp` on the head node and
        # the second `mv` would fail after the first had already moved it. `json_db`'s helper
        # cannot be used here -- the file is created by a shell on another machine.
        tmp = f"{target}.{uuid4().hex[:8]}.tmp"
        script = (
            (f"mkdir -p {shlex.quote(parent)} && " if parent else "")
            + f"printf %s {b64} | base64 -d > {shlex.quote(tmp)} "
            + f"&& mv -f {shlex.quote(tmp)} {shlex.quote(target)}"
        )
        proc = self._quiet_run(["sh", "-c", script], timeout=300)
        if proc is None:
            return "remote write failed"
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            return detail[-1][:300] if detail else f"write exit {proc.returncode}"
        return None
