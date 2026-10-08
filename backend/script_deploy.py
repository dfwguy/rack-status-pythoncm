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
"""Push the app's own `bin/scripts/` to the cluster head, over the app's connection.

`bin/deploy-scripts.sh` has done this since the beginning and still does, but it runs from a
developer's workstation with `rsync` and a single `ssh` hop. That is the wrong shape for the
first-run wizard, for two reasons that are not about convenience:

- **The operator setting up an appliance has no workstation in the path.** They have a
  browser pointed at the container. Telling them to go and run a shell script is where a
  first-run flow stops being a first-run flow.
- **`rsync` cannot reach a head that takes more than one hop.** Nscale TX is Teleport, then
  a password gate, then the BCM head. Our `Connection` composes that chain already; a
  second SSH mechanism beside it would have to learn the whole topology over again, which
  is exactly the parallel-structure mistake `core-designs.mdc` §1 is about.

So this ships the same bytes through the same transport every other remote command uses.
The shell script keeps its job -- a developer pushing an edit mid-debug -- and the two agree
on what a deploy *is*, because the exclusion rules and the destination layout below are
lifted from it deliberately.

**What is never sent.** `custom/` belongs to the deployment (scripts operators added), and
`project-*/` belongs to a project. A deploy updates what *ships*; it has no opinion on
either, and since neither is in the archive, extraction cannot touch them. There is no
delete pass, matching the script's lack of `--delete`: a file only present on the head is
somebody's, not ours to remove.
"""
from __future__ import annotations

import io
import logging
import os
import shlex
import tarfile
import time
import uuid
from pathlib import Path

import host_exec
import script_tree
from paths import REPO_ROOT

_logger = logging.getLogger(__name__)

#: Where the shared set lands in the layered tree, and the name of its source directory.
SHARED_LAYER = "multi-platform"

#: Source-tree directories that are not part of the shared layer.
#: `platform/` is a *container* of layers, each of which is deployed beside the shared one;
#: copying it wholesale would bury every platform's scripts inside the default set.
_NOT_SHARED = ("platform", "custom")

#: Build artifacts and editor leftovers, same list as the shell script's rsync excludes.
_SKIP_SUFFIXES = (".pyc",)
_SKIP_DIRS = ("__pycache__",)

#: How much of the archive rides in one command.
#:
#: The ceiling is **`MAX_ARG_STRLEN`, not `ARG_MAX`**, and that distinction is the whole
#: reason this is chunked. Linux caps a *single* argv element at 128 KiB regardless of the
#: 2 MiB total, and `Connection.write_bytes` puts the base64 payload inline in one `sh -c`
#: string -- it has to, because stdin is not plumbed through a hop chain. Today's bundle is
#: ~290 KiB, so an unchunked write would fail with `E2BIG` on the first real cluster while
#: passing every local test against a one-hop connection.
#:
#: 64 KiB of raw bytes is ~88 KiB of base64 plus the script around it: comfortably under the
#: cap, with room for the quoting each additional hop adds.
CHUNK_BYTES = 64 * 1024


#: ``config/`` holds what Rack Status publishes into a deployed copy (``node_config``); only its
#: README ships. Anything else there in the source tree is a stray local publish, and shipping it
#: would overwrite the cluster's own published settings on every deploy.
_CONFIG_DIR = "config"
_CONFIG_SHIPPED = ("README.md",)


def _is_wanted(path: Path) -> bool:
    if any(part in _SKIP_DIRS for part in path.parts):
        return False
    if _CONFIG_DIR in path.parts[:-1] and path.name not in _CONFIG_SHIPPED:
        return False
    if path.suffix in _SKIP_SUFFIXES:
        return False
    return ".bak" not in path.name


class UnsafeScript(ValueError):
    """A file in the script tree must not be shipped, so nothing is."""


def _check_shippable(path: Path, source: Path) -> None:
    """Refuse anything that is not a plain file, before it can be archived.

    `tar` faithfully reproduces what it is given, and the far side extracts as the deploy
    user into a tree shared by every project on the head. A symlink in `bin/scripts/` would
    therefore be recreated *there*, still pointing wherever it pointed here -- so a link
    named `sysinfo.sh` aimed at `/etc/passwd` becomes a file in the scripts tree that reads
    or writes outside it. Device nodes and hard links are refused for the same reason.

    Checked at **build** time rather than trusting the extraction, because this is the side
    that can name the offending file and the side where the error is actionable. It is the
    discipline of the touchless harness's `safe_extract`, applied one step earlier: the
    archive never contains the thing, so no extractor has to be careful.

    Nothing in `bin/scripts/` is a symlink today. That is exactly when to add the check --
    afterwards it is an incident.
    """
    if path.is_symlink():
        raise UnsafeScript(
            f"{path.relative_to(source)} is a symlink ({os.readlink(path)}); "
            "the script tree is deployed to a shared head node and must contain only "
            "regular files"
        )
    if not path.is_file():
        raise UnsafeScript(f"{path.relative_to(source)} is not a regular file")


def _add_tree(tar: tarfile.TarFile, source: Path, prefix: str, manifest: list[str]) -> None:
    """Add every wanted file under *source*, placed at *prefix* in the archive."""
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        # Judged with the walked directory's own name in front, so a rule about a top-level
        # directory (config/ ships only its README) still sees which directory a file is in.
        if not _is_wanted(Path(source.name) / relative):
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        _check_shippable(path, source)
        arcname = f"{prefix}/{relative.as_posix()}"
        # add() carries the mode, which is what keeps a script executable on the far side.
        tar.add(path, arcname=arcname)
        manifest.append(arcname)


def build_bundle(source_root: Path | None = None) -> tuple[bytes, list[str]]:
    """A gzipped tar laid out exactly as the destination tree, plus its file list.

    Laying the archive out as `multi-platform/...` and `<platform-id>/...` means one
    extraction into the tree root puts everything where `script_tree` looks for it. The
    alternative -- one transfer per layer -- would be several round trips over a chain where
    each one costs a second.
    """
    root = source_root or (REPO_ROOT / "bin" / "scripts")
    if not root.is_dir():
        raise FileNotFoundError(f"no scripts to deploy: {root} is not a directory")

    manifest: list[str] = []
    buffer = io.BytesIO()
    # mtime=0 so an unchanged tree produces identical bytes, which makes "did anything
    # change" answerable by comparing digests later without a second mechanism.
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=6) as tar:
        for path in sorted(root.iterdir()):
            if not _is_wanted(Path(path.name)):
                continue
            if path.is_dir() and not path.is_symlink():
                if path.name not in _NOT_SHARED:
                    _add_tree(tar, path, f"{SHARED_LAYER}/{path.name}", manifest)
                continue
            _check_shippable(path, root)
            tar.add(path, arcname=f"{SHARED_LAYER}/{path.name}")
            manifest.append(f"{SHARED_LAYER}/{path.name}")

        platform_root = root / "platform"
        if platform_root.is_dir():
            for layer in sorted(platform_root.iterdir()):
                if layer.is_dir():
                    _add_tree(tar, layer, layer.name, manifest)

    return buffer.getvalue(), manifest


def _layers_in(manifest: list[str]) -> list[str]:
    return sorted({name.split("/", 1)[0] for name in manifest})


def deploy(*, dry_run: bool = False, profile: str = "script", timeout: int = 600) -> dict:
    """Send the shipped script layers to the head node's tree.

    Returns a summary rather than raising on a remote failure: the wizard shows this to an
    operator who needs to know *which* part did not work, and an exception carrying a
    truncated stderr is a worse answer than a structured one.
    """
    started = time.monotonic()
    bundle, manifest = build_bundle()
    tree = script_tree.tree_root()
    summary: dict[str, object] = {
        "ok": True,
        "dry_run": dry_run,
        "dest": tree,
        "files": len(manifest),
        "bytes": len(bundle),
        "layers": _layers_in(manifest),
        "chunks": max(1, -(-len(bundle) // CHUNK_BYTES)),
    }
    if dry_run:
        # Deliberately no connection attempt: the point of a preview is to answer "what
        # would be sent" on a deployment whose SSH is not working yet.
        summary["detail"] = f"would send {len(manifest)} file(s) to {tree}"
        return summary

    conn = host_exec.connection(profile)

    # Prove the target can run a command BEFORE writing a byte. The transport is asymmetric
    # in a way that makes this necessary rather than tidy: with no SSH host configured,
    # `write_bytes` and `makedirs` still succeed -- against the LOCAL filesystem -- while
    # `run` raises. Writing first therefore staged 280 KB of tarball chunks into the app's
    # own source tree and only then discovered it could not extract them, leaving the parts
    # behind because the cleanup needs the same `run` that just failed.
    unreachable = cannot_execute(conn)
    if unreachable:
        return {**summary, "ok": False, "error": unreachable}

    staging = f"{tree.rstrip('/')}/.nvis-deploy-{uuid.uuid4().hex[:8]}"
    try:
        error = conn.makedirs(staging)
        if error:
            return {**summary, "ok": False, "error": f"cannot create {staging}: {error}"}

        for index in range(0, len(bundle), CHUNK_BYTES):
            part = f"{staging}/part-{index // CHUNK_BYTES:04d}"
            error = conn.write_bytes(part, bundle[index:index + CHUNK_BYTES])
            if error:
                return {**summary, "ok": False, "error": f"upload failed at {part}: {error}"}

        # Reassemble and extract in one command, so a chain is crossed once more, not twice.
        # `cat part-*` relies on the zero-padded names sorting in write order.
        quoted = shlex.quote(staging)
        script = (
            f"set -e; cd {quoted}; cat part-* > bundle.tgz; "
            f"mkdir -p {shlex.quote(tree)}; "
            f"tar -xzf bundle.tgz -C {shlex.quote(tree)}; "
            f"cd /; rm -rf {quoted}"
        )
        proc = conn.run(["sh", "-c", script], timeout=timeout)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return {**summary, "ok": False, "error": f"extract failed: {detail[:400]}"}
        # The command removes its own staging directory as its last act, so the directory
        # still being there means the extraction did not happen however the exit code read.
        # Same rule as `verify-deploy.sh`: "up" is not "working", and a deploy that reports
        # success without landing the files is the failure that costs the most to diagnose.
        if conn.is_dir(staging):
            return {
                **summary,
                "ok": False,
                "error": f"extract reported success but {staging} still exists; nothing landed",
            }
    except Exception as exc:  # noqa: BLE001 - the transport raises several unrelated types
        # Broad on purpose. This used to catch OSError alone, so `transport.NotConfigured`
        # escaped as a 500 *and* skipped the structured result the caller shows an operator.
        return {**summary, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:400]}
    finally:
        # The staging directory is removed by the command above on success. This is the
        # failure path: leaving a few hundred KB of parts on the head every failed attempt
        # would accumulate silently on the one filesystem nobody watches.
        _cleanup(conn, staging)

    # The tree now exists, so `script_tree`'s memo of "is this deployment layered" is stale.
    # Without this the very next resolution still answers with the flat legacy path, and the
    # bootstrap that follows in the wizard looks for defaults that it cannot see.
    script_tree.clear_cache()
    summary["catalog"] = _publish_catalog(profile)
    summary["seconds"] = round(time.monotonic() - started, 1)
    summary["detail"] = f"sent {len(manifest)} file(s) to {tree}"
    _logger.info("script-deploy: %s", summary["detail"])
    return summary


def _publish_catalog(profile: str) -> dict:
    """Ship the active P2P cluster's image catalog with the scripts that check images against it.

    The launchers verify a node's image by ``<scripts>/config/image-catalog.tsv``, so a fresh
    script tree without it would refuse every container launch. Not P2P, or no authored catalog:
    nothing to ship, and that is not a deploy failure.
    """
    import p2p_catalog
    from catalog import context as catalog_context

    try:
        _project, cluster, _platform = catalog_context.resolve()
    except Exception as exc:  # noqa: BLE001 - catalog trouble must not fail a script deploy
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    if cluster is None or not cluster.p2p_enabled:
        return {"ok": True, "skipped": "P2P is not enabled on the active cluster"}
    if not p2p_catalog.has_catalog(cluster.id):
        return {"ok": True, "skipped": "no authored P2P catalog"}
    return p2p_catalog.publish_to_scripts(cluster.id, profile=profile)


def cannot_execute(conn) -> str:
    """"" if the target really runs commands for us, else why not.

    It echoes a nonce and checks it comes back, rather than trusting a zero exit. That is
    not paranoia about the shell -- it is that **an exit code is a claim and the echoed
    nonce is evidence**, and this product has two ways to get the claim without the deed.
    A transport with no SSH host configured raises here, before anything is written; and a
    test harness that stubs `subprocess` returns a confident success for a command that
    never ran. Both used to get past this point and stage 280 KB of tarball into whatever
    `tree_root()` resolved to -- on an unconfigured host, the app's own source tree -- then
    fail to extract it and fail to clean up, because cleanup needs the same execution that
    was never real.

    Checking output rather than status is what makes the deploy refuse both cases with a
    useful message instead of leaving litter and reporting success.
    """
    nonce = uuid.uuid4().hex[:12]
    try:
        proc = conn.run(["printf", "%s", nonce], timeout=30)
    except Exception as exc:  # noqa: BLE001 - NotConfigured, OSError, subprocess errors
        return f"cannot run commands on the target: {type(exc).__name__}: {exc}"[:400]
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return f"cannot run commands on the target: {detail[:300]}"
    if nonce not in (proc.stdout or ""):
        return (
            "the target reported success without running the command "
            "(no output from a probe that only echoes); refusing to stage a deploy it "
            "cannot extract"
        )
    return ""


def _cleanup(conn, staging: str) -> None:
    try:
        conn.run(["sh", "-c", f"rm -rf {shlex.quote(staging)}"], timeout=60)
    except Exception:  # noqa: BLE001 - best effort; the deploy's own result is the answer
        _logger.debug("script-deploy: could not remove staging dir %s", staging)
