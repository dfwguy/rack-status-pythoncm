#!/usr/bin/env python3
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
"""Classify one deployment surface against a git ref. Helper for ``bin/prod-drift.sh``.

Reads ``<sha256>  <relpath>`` lines (as ``sha256sum`` emits them) for the remote surface and
compares them with the same paths at a git ref. Only content is compared, never mtime.

Env: REPO_ROOT, REF, LABEL, PREFIX, REMOTE_DIR, WHERE, FETCH (see prod-drift.sh).
Exit: 0 = no drift, 1 = drift.
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = os.environ["REPO_ROOT"]
REF = os.environ["REF"]
LABEL = os.environ.get("LABEL", "surface")
PREFIX = os.environ.get("PREFIX", "")
REMOTE_DIR = os.environ.get("REMOTE_DIR", "")
WHERE = os.environ.get("WHERE", "deploy")
FETCH = os.environ.get("FETCH", "")
# Bound on how many remote-only files --fetch will pull; each one is an SSH round trip.
FETCH_ONLY_CAP = int(os.environ.get("NVIS_DRIFT_FETCH_CAP", "40"))

# Paths that are expected to differ or are not code: deployment-local config, caches, build
# output, and editor/OS leftovers. Listing them keeps the signal about real code drift.
IGNORED_PREFIXES = (
    "deploy-secrets/",  # site secrets, deliberately never in git
    "static/spa/",  # build output
    "var/",  # runtime state and caches
    ".git/",
)
# Per-deployment config that is gitignored on purpose. Without these every clean deployment
# still reports drift, which is how a check like this gets ignored.
IGNORED_FILES = frozenset({".env", "docker-compose.override.yml"})
# Directory names that can appear at any depth: dependency, venv and cache trees.
IGNORED_DIRS = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        ".cursor",
    }
)
IGNORED_SUFFIXES = (".pyc", ".DS_Store", ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm")


def ignored(rel: str) -> bool:
    if rel.startswith(IGNORED_PREFIXES) or rel.endswith(IGNORED_SUFFIXES):
        return True
    if rel in IGNORED_FILES:
        return True
    parts = rel.split("/")
    if IGNORED_DIRS.intersection(parts[:-1]):
        return True
    # macOS AppleDouble sidecars (._foo) that rsync/scp leave behind.
    return any(p.startswith("._") for p in parts)


def git_blob_sha256_map(paths: list[str]) -> dict[str, str | None]:
    """sha256 of each path's blob at REF, or None when the ref has no such path.

    One ``git cat-file --batch`` process for the whole set: a deployment tree is thousands of
    files, and spawning ``git show`` per file made a full comparison take many minutes.
    """
    out: dict[str, str | None] = {}
    if not paths:
        return out
    proc = subprocess.Popen(
        ["git", "-C", REPO_ROOT, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout

    # Feed stdin from a thread. Writing the whole request first deadlocks as soon as git's
    # stdout buffer fills (one 680 KB blob is enough): git blocks writing output while we
    # block writing input.
    def _feed() -> None:
        try:
            proc.stdin.write(("".join(f"{REF}:{p}\n" for p in paths)).encode())
            proc.stdin.close()
        except BrokenPipeError:
            pass

    feeder = threading.Thread(target=_feed, daemon=True)
    feeder.start()
    try:
        for path in paths:
            header = proc.stdout.readline()
            if not header:
                out[path] = None
                continue
            fields = header.decode(errors="replace").split()
            # "<oid> missing" / "<spec> missing" for a path the ref does not carry.
            if len(fields) < 3 or fields[-1] == "missing":
                out[path] = None
                continue
            size = int(fields[2])
            body = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline git appends after the object
            out[path] = hashlib.sha256(body).hexdigest()
    finally:
        feeder.join(timeout=10)
        proc.stdout.close()
        proc.wait()
    return out


def main() -> int:
    remote: dict[str, str] = {}
    for line in Path(sys.argv[1]).read_text().splitlines():
        if not line.strip():
            continue
        sha, _, rel = line.partition("  ")
        # removeprefix, not lstrip: lstrip("./") eats every leading '.' and '/', turning
        # "./.gitignore" into "gitignore" — which then looks like a file git does not have.
        rel = rel.strip().removeprefix("./")
        if rel and not ignored(rel):
            remote[rel] = sha.strip()

    rels = sorted(remote)
    git_shas = git_blob_sha256_map([PREFIX + rel for rel in rels])
    differs: list[str] = []
    remote_only: list[str] = []
    for rel in rels:
        git_sha = git_shas.get(PREFIX + rel)
        if git_sha is None:
            remote_only.append(rel)
        elif git_sha != remote[rel]:
            differs.append(rel)

    print(f"  compared {len(remote)} file(s)")
    if differs:
        print(f"  CHANGED on the deployment but not in git ({len(differs)}):")
        for rel in differs:
            print(f"     {PREFIX}{rel}")
    if remote_only:
        print(f"  present only on the deployment ({len(remote_only)}):")
        for rel in remote_only:
            print(f"     {PREFIX}{rel}")
    if not differs and not remote_only:
        print("  clean")

    # Fetch BOTH classes, into separate trees.
    #
    # Changed files need a 3-way merge, so they come with their gitref counterpart. Remote-only
    # files have no git copy at all, which makes them the higher-risk class, not the lower one:
    # if a deploy removes one there is nothing to merge back from. This originally skipped them
    # on the theory that they are deployment-local config, but secrets/caches/build output are
    # already excluded by ignored() before we get here, so what survives is usually real code.
    # Measured on Nscale TX 2026-08-05: the three remote-only files were run_llmb_nemo.sh,
    # collect_llmb_nemo_log.sh and shim/srun -- a Stage 2 launcher the repo's own
    # validation_activities.py already referenced, its log collector, and an srun shim encoding
    # why registry pulls cannot run on an airgapped tray. Listing those and moving on would have
    # been how they got lost.
    #
    # Each fetch is a full SSH round trip through the jumphost, so cap the remote-only pull and
    # say so when the cap bites, rather than silently stalling on a pathological tree.
    if FETCH and differs:
        fetch_one(differs, "remote", with_gitref=True)
    if FETCH and remote_only:
        if len(remote_only) > FETCH_ONLY_CAP:
            print(
                f"  ({len(remote_only)} remote-only files exceeds the {FETCH_ONLY_CAP}-file "
                f"fetch cap — listed only. Raise NVIS_DRIFT_FETCH_CAP to pull them.)"
            )
        else:
            fetch_one(remote_only, "prodonly", with_gitref=False)

    return 1 if (differs or remote_only) else 0


def remote_cat_cmd(path: str) -> str:
    """Shell command that prints one remote file, surviving the ssh-inside-ssh hop.

    Same base64 trick prod-drift.sh uses: when the head node is reached through the deploy
    host, an unencoded command is re-parsed by the intermediate shell and runs on the wrong
    machine. Base64 is alphanumeric, so it passes through untouched.
    """
    b64 = base64.b64encode(f"cat '{path}'".encode()).decode()
    inner = f"echo {b64} | base64 -d | sh"
    if WHERE == "deploy":
        return f'ssh {os.environ["NVIS_DEPLOY_SSH"]} "{inner}"'
    head = "ssh -o BatchMode=yes"
    if os.environ.get("RACK_SSH_KEY"):
        head += f" -i {os.environ['RACK_SSH_KEY']}"
    if os.environ.get("RACK_SSH_PORT"):
        head += f" -p {os.environ['RACK_SSH_PORT']}"
    head += f" {os.environ.get('RACK_SSH_USER', 'root')}@{os.environ['RACK_SSH_HOST']}"
    via = os.environ.get("NVIS_HEAD_VIA")
    return f"ssh {via} \"{head} '{inner}'\"" if via else f'{head} "{inner}"'


def fetch_one(rels: list[str], subdir: str, *, with_gitref: bool) -> None:
    """Copy remote files locally so the drift can be reviewed.

    ``with_gitref`` also writes the ref's copy alongside, which only makes sense for files git
    actually has -- remote-only files land in their own tree with no counterpart to merge from.
    """
    root = Path(FETCH) / LABEL
    for rel in rels:
        dest_remote = root / subdir / rel
        dest_remote.parent.mkdir(parents=True, exist_ok=True)
        got = subprocess.run(
            remote_cat_cmd(f"{REMOTE_DIR}/{rel}"),
            shell=True,
            capture_output=True,
            check=False,
        )
        if got.returncode != 0:
            print(f"  !! could not fetch {rel}")
            continue
        dest_remote.write_bytes(got.stdout)
        if not with_gitref:
            continue
        blob = subprocess.run(
            ["git", "-C", REPO_ROOT, "show", f"{REF}:{PREFIX}{rel}"],
            capture_output=True,
            check=False,
        )
        if blob.returncode == 0:
            dest_git = root / "gitref" / rel
            dest_git.parent.mkdir(parents=True, exist_ok=True)
            dest_git.write_bytes(blob.stdout)

    if not with_gitref:
        # No git counterpart, so there is nothing to merge — these just need adding.
        print(f"  fetched {len(rels)} remote-only file(s) to {root}/{subdir}/")
        print("  these exist nowhere in git; to keep one:")
        print(f"     cp {root}/{subdir}/<file> {PREFIX}<file> && git add {PREFIX}<file>")
        return
    print(f"  fetched to {root}/ (remote/ and gitref/)")
    print("  to merge a file, using the commit the deployment was based on as the base:")
    print(f"     git show <base-commit>:{PREFIX}<file> > /tmp/base")
    print(f"     git merge-file -p {PREFIX}<file> /tmp/base {root}/{subdir}/<file> > /tmp/merged")


if __name__ == "__main__":
    sys.exit(main())
