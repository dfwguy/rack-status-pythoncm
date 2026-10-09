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
"""nvis-p2p-agent -- converge this node on the images its desired-state document names.

Pull, not push: every poll the agent GETs ``<origin>/.nvis-p2p/desired/<hostname>.json`` and makes
the node match it -- download through the local Dragonfly peer, verify against the document's
sha256, install beside the peer root with a receipt. Nothing reaches in over SSH; status leaves as
Prometheus text in the node_exporter textfile directory, which Alloy already ships to the appserver.
See docs/DESIGN_p2p-pull-agent.md for the contract.

Standard library only: compute nodes are air-gapped and ship Python 3.12, nothing else is assumed.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

VERSION = "0.1.0"
SCHEMA_VERSION = 1
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.sqsh$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.:-]+$")
BACKOFF_FIRST, BACKOFF_CAP = 60.0, 900.0


class DesiredError(ValueError):
    """The desired-state document is malformed; the agent keeps the last good one."""


@dataclass(frozen=True)
class Image:
    filename: str
    sha256: str
    size: int
    priority: int = 0
    tag: str = "catalog-v1"
    application: str = "NVIS-P2P-Catalog"
    timeout_seconds: int = 3600


@dataclass(frozen=True)
class Desired:
    generation: int
    origin: str
    images: tuple[Image, ...]


def parse_desired(doc: object, node: str, default_origin: str = "") -> Desired:
    """Validate a desired-state document. Anything unexpected is refused, never half-applied."""
    if not isinstance(doc, dict):
        raise DesiredError("document is not an object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise DesiredError(f"unsupported schema_version {doc.get('schema_version')!r}")
    generation = doc.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise DesiredError("generation must be a non-negative integer")
    if doc.get("node") not in (None, node):
        raise DesiredError(f"document is for {doc.get('node')!r}, not {node!r}")
    origin = str(doc.get("origin") or default_origin).rstrip("/")
    if not _ORIGIN_RE.match(origin):
        raise DesiredError(f"origin {origin!r} is not http(s)://host[:port]")
    raw = doc.get("images", [])
    if not isinstance(raw, list):
        raise DesiredError("images must be a list")
    images: list[Image] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise DesiredError("each image must be an object")
        name, sha, size = entry.get("filename"), entry.get("sha256"), entry.get("bytes")
        if not isinstance(name, str) or not _FILENAME_RE.match(name):
            raise DesiredError(f"bad filename {name!r}")
        if name in seen:
            raise DesiredError(f"duplicate filename {name!r}")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise DesiredError(f"{name}: sha256 must be 64 lowercase hex digits")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise DesiredError(f"{name}: bytes must be a positive integer")
        timeout = entry.get("timeout_seconds", 3600)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 60:
            raise DesiredError(f"{name}: timeout_seconds must be an integer >= 60")
        seen.add(name)
        images.append(Image(
            filename=name, sha256=sha, size=size, priority=int(entry.get("priority", 0) or 0),
            tag=str(entry.get("tag") or "catalog-v1"),
            application=str(entry.get("application") or "NVIS-P2P-Catalog"),
            timeout_seconds=timeout,
        ))
    images.sort(key=lambda i: (i.priority, i.filename))
    return Desired(generation=generation, origin=origin, images=tuple(images))


def sha256_file(path: Path, cancel: threading.Event | None = None) -> str | None:
    """sha256 of *path*, or None when cancelled part-way (a 42 GB hash takes minutes)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(8 << 20):
            if cancel is not None and cancel.is_set():
                return None
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Track:
    """What the agent knows about one image between polls (memory only; disk is the truth)."""

    state: str = "queued"
    failures: int = 0
    next_attempt: float = 0.0
    last_error: str = ""


@dataclass
class Download:
    image: Image
    thread: threading.Thread
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    result: str = ""  # "installed" | "cancelled" | "failed: <why>"
    started: float = field(default_factory=time.time)


class Agent:
    def __init__(self, base: Path, node: str, desired_url: str, textfile_dir: Path | None,
                 default_origin: str = "", log=print):
        self.base = base
        self.target_dir = base.parent
        self.node = node
        self.desired_url = desired_url
        self.textfile_dir = textfile_dir
        self.default_origin = default_origin
        self.log = log
        self.desired: Desired | None = None
        self.etag = ""
        self.fetch_errors = 0
        self.last_poll = 0.0
        self.tracks: dict[str, Track] = {}
        self.download: Download | None = None
        for sub in ("receipts", "staging", "quarantine", "logs", "agent"):
            (base / sub).mkdir(parents=True, exist_ok=True)

    # -- desired state ---------------------------------------------------------------------------

    def fetch(self) -> None:
        req = urllib.request.Request(self.desired_url, headers={"If-None-Match": self.etag} if self.etag else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read(1 << 20)
                etag = resp.headers.get("ETag", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return
            if exc.code == 404:
                self._adopt(Desired(generation=0, origin=self.default_origin or "http://unset", images=()), "")
                return
            self.fetch_errors += 1
            self.log(f"AGENT_FETCH_ERROR status={exc.code}")
            return
        except (urllib.error.URLError, OSError) as exc:
            self.fetch_errors += 1
            self.log(f"AGENT_FETCH_ERROR {exc}")
            return
        try:
            desired = parse_desired(json.loads(body), self.node, self.default_origin)
        except (ValueError, DesiredError) as exc:
            self.fetch_errors += 1
            self.log(f"AGENT_DESIRED_REJECTED {exc}")
            return
        self._adopt(desired, etag)

    def _adopt(self, desired: Desired, etag: str) -> None:
        old = self.desired
        if old is None or desired.generation != old.generation:
            # A new generation is a retry: forget failures and backoff, keep what is on disk.
            self.tracks.clear()
            self.log(f"AGENT_GENERATION generation={desired.generation} images={len(desired.images)}")
        self.desired, self.etag = desired, etag

    # -- reconcile -------------------------------------------------------------------------------

    def receipt_path(self, image: Image) -> Path:
        return self.base / "receipts" / f"{image.filename}.sha256"

    def is_ready(self, image: Image) -> bool:
        """Installed at the pinned size with a receipt for the pinned hash (hash it once if not)."""
        target = self.target_dir / image.filename
        try:
            if target.stat().st_size != image.size:
                return False
        except FileNotFoundError:
            return False
        line = f"{image.sha256}  {target}"
        receipt = self.receipt_path(image)
        if receipt.is_file() and line in receipt.read_text(encoding="utf-8").splitlines():
            return True
        if sha256_file(target) == image.sha256:
            receipt.write_text(line + "\n", encoding="utf-8")
            return True
        return False

    def reconcile(self, now: float) -> None:
        desired = self.desired
        wanted = {i.filename: i for i in desired.images} if desired else {}
        dl = self.download
        if dl is not None:
            if not dl.thread.is_alive():
                self._finish(dl, now)
            elif dl.image.filename not in wanted or wanted[dl.image.filename] != dl.image:
                self.log(f"AGENT_CANCEL filename={dl.image.filename}")
                dl.cancel.set()
                _stop(dl.proc)
        for name in list(self.tracks):
            if name not in wanted:
                del self.tracks[name]
        for image in wanted.values():
            track = self.tracks.setdefault(image.filename, Track())
            if self.download is not None and self.download.image.filename == image.filename:
                track.state = "downloading"
            elif track.state == "ready" or self.is_ready(image):
                track.state = "ready"
            elif track.failures and now < track.next_attempt:
                track.state = "backoff"
            else:
                track.state = "queued"
        if self.download is None and desired is not None:
            for image in desired.images:
                if self.tracks[image.filename].state == "queued":
                    self._start(image, desired.origin)
                    self.tracks[image.filename].state = "downloading"
                    break

    def _start(self, image: Image, origin: str) -> None:
        dl = Download(image=image, thread=threading.Thread(target=lambda: None))
        dl.thread = threading.Thread(target=self._run_download, args=(dl, origin), daemon=True,
                                     name=f"download-{image.filename}")
        self.download = dl
        self.log(f"AGENT_DOWNLOAD_START filename={image.filename} bytes={image.size}")
        dl.thread.start()

    def _run_download(self, dl: Download, origin: str) -> None:
        try:
            self._download(dl, origin)
        except Exception as exc:  # noqa: BLE001 -- a thread must report, never die silently
            dl.result = f"failed: {type(exc).__name__}: {exc}"

    def _download(self, dl: Download, origin: str) -> None:
        image, base = dl.image, self.base
        partial = base / "staging" / f"{image.filename}.partial"
        partial.unlink(missing_ok=True)
        cmd = [
            "timeout", "--kill-after=60", str(image.timeout_seconds),
            "ionice", "-c2", "-n7", "nice", "-n", "10", str(base / "bin" / "dfget"),
            "--endpoint", str(base / "run" / "dfdaemon.sock"), "--no-progress", "--force-hard-link",
            "--timeout", f"{image.timeout_seconds}s", "--priority", "6",
            "--digest", f"sha256:{image.sha256}",
            "--content-for-calculating-task-id", f"nvis-p2p-sha256:{image.sha256}",
            "--application", image.application, "--tag", image.tag, "--disable-back-to-source",
            f"{origin}/{image.filename}", "-O", str(partial),
        ]
        log_path = base / "logs" / f"agent-{image.filename}.log"
        with log_path.open("ab") as logf:
            dl.proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            rc = dl.proc.wait()
        if dl.cancel.is_set():
            partial.unlink(missing_ok=True)
            dl.result = "cancelled"
            return
        if rc != 0:
            dl.result = "failed: timeout" if rc in (124, 137) else f"failed: dfget exit {rc}"
            return
        try:
            size = partial.stat().st_size
        except FileNotFoundError:
            dl.result = "failed: no partial after dfget"
            return
        if size != image.size:
            dl.result = f"failed: size {size} != {image.size}"
            return
        actual = sha256_file(partial, dl.cancel)
        if actual is None:
            partial.unlink(missing_ok=True)
            dl.result = "cancelled"
            return
        if actual != image.sha256:
            partial.rename(base / "quarantine" / f"{image.filename}.badsha.{int(time.time())}")
            dl.result = f"failed: sha256 {actual[:12]} != pin {image.sha256[:12]}"
            return
        target = self.target_dir / image.filename
        if target.exists():
            target.rename(base / "quarantine" / f"{image.filename}.replaced.{int(time.time())}")
        partial.rename(target)
        self.receipt_path(image).write_text(f"{image.sha256}  {target}\n", encoding="utf-8")
        dl.result = "installed"

    def _finish(self, dl: Download, now: float) -> None:
        self.download = None
        track = self.tracks.setdefault(dl.image.filename, Track())
        if dl.result == "installed":
            track.state, track.failures, track.last_error = "ready", 0, ""
            self.log(f"AGENT_INSTALLED filename={dl.image.filename}")
        elif dl.result == "cancelled":
            track.state = "queued"
            self.log(f"AGENT_CANCELLED filename={dl.image.filename}")
        else:
            track.failures += 1
            track.last_error = dl.result
            track.next_attempt = now + min(BACKOFF_CAP, BACKOFF_FIRST * 2 ** (track.failures - 1))
            track.state = "backoff"
            self.log(f"AGENT_FAILED filename={dl.image.filename} failures={track.failures} why={dl.result!r}")

    # -- status ----------------------------------------------------------------------------------

    def metrics(self) -> str:
        gen = self.desired.generation if self.desired else -1
        lines = [
            "# HELP nvis_p2p_agent_up nvis-p2p-agent loop alive; generation it acted on.",
            "# TYPE nvis_p2p_agent_up gauge",
            f'nvis_p2p_agent_up{{generation="{gen}",version="{VERSION}"}} 1',
            "# TYPE nvis_p2p_agent_last_poll_timestamp_seconds gauge",
            f"nvis_p2p_agent_last_poll_timestamp_seconds {self.last_poll:.0f}",
            "# TYPE nvis_p2p_agent_fetch_errors_total counter",
            f"nvis_p2p_agent_fetch_errors_total {self.fetch_errors}",
            "# TYPE nvis_p2p_image_state gauge",
            "# TYPE nvis_p2p_image_bytes gauge",
            "# TYPE nvis_p2p_image_expected_bytes gauge",
            "# TYPE nvis_p2p_image_failures_total counter",
            "# TYPE nvis_p2p_image_download_started_timestamp_seconds gauge",
        ]
        for image in self.desired.images if self.desired else ():
            track = self.tracks.get(image.filename, Track())
            fn = image.filename
            for state in ("ready", "downloading", "queued", "backoff", "failed"):
                lines.append(f'nvis_p2p_image_state{{filename="{fn}",state="{state}"}} {int(track.state == state)}')
            # Bytes are reported only once installed. dfget preallocates the partial, so neither its
            # size nor its allocated blocks say how far a download has got; claiming progress from
            # them would be a lie. A running download reports when it started instead.
            lines.append(f'nvis_p2p_image_bytes{{filename="{fn}"}} {image.size if track.state == "ready" else 0}')
            dl = self.download
            if track.state == "downloading" and dl is not None and dl.image.filename == fn:
                lines.append(f'nvis_p2p_image_download_started_timestamp_seconds{{filename="{fn}"}} {dl.started:.0f}')
            lines.append(f'nvis_p2p_image_expected_bytes{{filename="{fn}"}} {image.size}')
            lines.append(f'nvis_p2p_image_failures_total{{filename="{fn}"}} {track.failures}')
        return "\n".join(lines) + "\n"

    def write_status(self) -> None:
        text = self.metrics()
        status = {
            "node": self.node, "version": VERSION, "last_poll": self.last_poll,
            "generation": self.desired.generation if self.desired else None,
            "images": {n: vars(t) for n, t in self.tracks.items()},
        }
        _atomic_write(self.base / "agent" / "status.json", json.dumps(status, indent=2))
        if self.textfile_dir is not None:
            prom = self.textfile_dir / "nvis_p2p_agent.prom"
            try:
                _atomic_write(prom, text)
            except PermissionError:
                # The collector directory is root's; the installer hands the agent this one file
                # rather than the directory. Rewrite it in place -- a scrape can at worst see a
                # short read of a few hundred bytes, and the next poll fixes it.
                try:
                    with prom.open("r+", encoding="utf-8") as fh:
                        fh.write(text)
                        fh.truncate()
                except OSError as exc:
                    self.log(f"AGENT_METRICS_UNWRITABLE {exc}")
            except OSError as exc:
                self.log(f"AGENT_METRICS_UNWRITABLE {exc}")

    def poll(self) -> None:
        self.fetch()
        self.reconcile(time.time())
        self.last_poll = time.time()
        self.write_status()

    def shutdown(self) -> None:
        dl = self.download
        if dl is not None and dl.thread.is_alive():
            dl.cancel.set()
            _stop(dl.proc)
            dl.thread.join(timeout=40)


def _stop(proc: subprocess.Popen | None) -> None:
    """TERM the download's process group, KILL it if it is still there 30 s later."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    def _kill_later() -> None:
        time.sleep(30)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    threading.Thread(target=_kill_later, daemon=True).start()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _origin_from_refresh_env(base: Path) -> str:
    env = base / "catalog-refresh.env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("NVIS_P2P_CATALOG_URL="):
                m = re.match(r"(https?://[^/]+)", line.split("=", 1)[1])
                if m:
                    return m.group(1)
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", type=Path, default=Path(os.environ.get("NVIS_P2P_BASE", "/opt/containers/.nvis-p2p")))
    ap.add_argument("--node", default=socket.gethostname().split(".")[0])
    ap.add_argument("--origin", default="", help="http(s)://host:port; default from catalog-refresh.env")
    ap.add_argument("--desired-url", default="", help="default <origin>/.nvis-p2p/desired/<node>.json")
    ap.add_argument("--textfile-dir", type=Path, default=Path("/var/lib/node_exporter/textfile_collector"))
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--once", action="store_true", help="one poll, then wait for any download and exit")
    args = ap.parse_args(argv)

    origin = (args.origin or _origin_from_refresh_env(args.base)).rstrip("/")
    desired_url = args.desired_url or (f"{origin}/.nvis-p2p/desired/{args.node}.json" if origin else "")
    if not desired_url:
        print("AGENT_NO_ORIGIN set --origin or install catalog-refresh.env", file=sys.stderr)
        return 64
    textfile = args.textfile_dir if args.textfile_dir.is_dir() else None

    def log(msg: str) -> None:
        print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)

    agent = Agent(args.base, args.node, desired_url, textfile, default_origin=origin, log=log)
    lock = (args.base / "agent" / "agent.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("AGENT_ALREADY_RUNNING")
        return 75
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log(f"AGENT_START version={VERSION} node={args.node} desired={desired_url} metrics={textfile}")
    while not stop.is_set():
        agent.poll()
        if args.once and agent.download is None:
            break
        stop.wait(2.0 if args.once else args.interval * random.uniform(0.8, 1.2))
    agent.shutdown()
    agent.write_status()
    log("AGENT_STOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
