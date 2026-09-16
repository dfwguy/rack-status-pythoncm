#!/usr/bin/env python3
"""Fail-closed extraction of a Rack Status release archive."""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import tarfile


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("archive")
    p.add_argument("destination")
    p.add_argument("--max-files", type=int, default=50_000)
    p.add_argument("--max-bytes", type=int, default=8 * 1024**3)
    args = p.parse_args()
    dest = pathlib.Path(args.destination)
    if dest.exists():
        raise SystemExit("destination already exists")
    dest.mkdir(mode=0o700, parents=True)
    seen: set[str] = set()
    folded: set[str] = set()
    total = 0
    with tarfile.open(args.archive, "r:gz") as tf:
        members = tf.getmembers()
        if not members or len(members) > args.max_files:
            raise SystemExit("archive member count is empty or exceeds limit")
        roots: set[str] = set()
        for m in members:
            pure = pathlib.PurePosixPath(m.name)
            if pure.is_absolute() or not pure.parts or any(x in ("", ".", "..") for x in pure.parts):
                raise SystemExit(f"unsafe archive path: {m.name!r}")
            roots.add(pure.parts[0])
            norm = pure.as_posix()
            if norm in seen or norm.casefold() in folded:
                raise SystemExit(f"duplicate or case-colliding archive path: {norm}")
            seen.add(norm); folded.add(norm.casefold())
            if not (m.isdir() or m.isfile()):
                raise SystemExit(f"unsupported archive member type: {norm}")
            if m.isfile():
                total += m.size
                if total > args.max_bytes:
                    raise SystemExit("archive expanded-size limit exceeded")
        if len(roots) != 1:
            raise SystemExit("archive must contain exactly one top-level directory")
        for m in members:
            target = dest.joinpath(*pathlib.PurePosixPath(m.name).parts)
            if m.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                raise SystemExit(f"cannot read archive member: {m.name}")
            with src, target.open("xb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, 0o755 if m.mode & 0o111 else 0o644)
    print(dest / next(iter(roots)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
