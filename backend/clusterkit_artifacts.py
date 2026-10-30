# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Bounded local artifacts using descriptor-relative, no-follow filesystem access."""
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_ENTRIES = 4096
MAX_ARTIFACT_FILES = 2048


class ArtifactListingLimitError(ValueError):
    pass


PART = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def relative_parts(name):
    if not isinstance(name, str) or not name or len(name) > 515:
        raise ValueError("Invalid artifact name")
    parts = name.split("/")
    if len(parts) > 4 or any(p in {"", ".", ".."} or not PART.fullmatch(p) for p in parts):
        raise ValueError("Invalid artifact name")
    return parts


@contextmanager
def directory(path):
    """Open all components from / without following any symlink, including ancestors."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Artifact root must be absolute without traversal")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def read_bytes(root, name, limit=MAX_ARTIFACT_BYTES):
    parts = relative_parts(name)
    with directory(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Artifact must be a regular file")
                if info.st_size > limit:
                    raise ValueError("Artifact exceeds download limit")
                data = handle.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Artifact exceeds download limit")
                return data
        finally:
            os.close(fd)


def read_json(root, name):
    return json.loads(read_bytes(root, name))


def artifacts(root):
    rows, entries = [], 0

    def visit(fd, prefix, depth):
        nonlocal entries
        with os.scandir(fd) as iterator:
            for item in iterator:
                entries += 1
                if entries > MAX_ARTIFACT_ENTRIES or len(rows) >= MAX_ARTIFACT_FILES:
                    raise ArtifactListingLimitError("Artifact listing exceeds its bounded limit; no partial list returned")
                if not PART.fullmatch(item.name) or item.name in {".", ".."}:
                    continue
                name = prefix + item.name
                try:
                    info = item.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode) and depth < 3:
                        child = os.open(item.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        try:
                            visit(child, name + "/", depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode):
                        rows.append({"name": name, "size": info.st_size,
                                     "downloadable": info.st_size <= MAX_ARTIFACT_BYTES})
                except ArtifactListingLimitError:
                    raise
                except (OSError, ValueError):
                    continue  # Concurrent replacement is not permission to follow a link.

    try:
        with directory(root) as fd:
            visit(fd, "", 0)
    except ArtifactListingLimitError:
        raise
    except (OSError, ValueError):
        return []
    return sorted(rows, key=lambda row: row["name"])
