# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A node's download cannot outlive its limit, and a launch never re-adopts one that has.

Author: Rafael M. Koike

On NC a dfget ran for ten hours past its own ``--timeout 6h``. The rollout gave up on the node after
thirty minutes, but nothing on the node ended the transfer, and every later launch saw a live pid and
reported it "active" -- so the same hung download was re-adopted, and timed out again, run after run.

Pinned here, against the real node scripts with a stand-in dfget and an ``ssh`` that runs the remote
half locally:

* ``download-catalog-image.sh`` enforces ``NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS`` from outside dfget
  (it is killed when the limit passes), says so, and tells dfget the same limit;
* it takes the origin from the node's catalog-refresh settings (or ``NVIS_P2P_ORIGIN``), with no site
  address in the script, and refuses to guess when there is none;
* ``launch-catalog-image-node.sh`` passes the limit on, leaves a young download alone, and replaces a
  "running" one older than the limit instead of re-adopting it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts"
IMAGE = "nemo.sqsh"
CONTENT = b"catalog image bytes\n"
SHA = hashlib.sha256(CONTENT).hexdigest()

_GNU = all(shutil.which(t) for t in ("timeout", "ionice", "sha256sum")) and subprocess.run(
    ["stat", "-c", "%s", "/"], capture_output=True, check=False).returncode == 0
needs_gnu = pytest.mark.skipif(not _GNU, reason="needs GNU coreutils + util-linux (the nodes and CI have them)")
_PROCPS = bool(shutil.which("pkill") and shutil.which("pgrep"))

# A stand-in dfget: records its arguments, then hangs or writes the image to its -O path.
_DFGET = r"""#!/usr/bin/env bash
printf '%s\n' "$@" > "$FAKE/dfget.args"
case ${FAKE_DFGET:-ok} in
  hang) exec sleep 300 ;;
  ok)   out=""; while (( $# )); do [[ $1 == -O ]] && out=$2; shift; done
        printf 'catalog image bytes\n' > "$out" ;;
esac
"""


@pytest.fixture
def node(tmp_path):
    """A peer root as a node has it: catalog, bin/dfget, catalog-refresh settings."""
    base = tmp_path / "containers" / ".nvis-p2p"
    for sub in ("catalog", "bin", "run", "logs"):
        (base / sub).mkdir(parents=True)
    (base / "catalog" / "images.tsv").write_text(f"1\tNeMo\t{IMAGE}\t{len(CONTENT)}\t{SHA}\n")
    (base / "bin" / "dfget").write_text(_DFGET)
    (base / "bin" / "dfget").chmod(0o755)
    (base / "catalog-refresh.env").write_text(
        "NVIS_P2P_CATALOG_URL=http://10.0.0.5:12130/.nvis-p2p/catalog/images.tsv\n")
    fake = tmp_path / "fake"
    fake.mkdir()
    env = {**os.environ, "NVIS_P2P_BASE": str(base), "FAKE": str(fake)}
    return base, fake, env


def _download(env, **extra):
    return subprocess.run(["bash", str(SCRIPTS / "download-catalog-image.sh"), IMAGE], env={**env, **extra},
                          capture_output=True, text=True, timeout=120, check=False)


@needs_gnu
class TestDownloadLimit:
    def test_a_hung_transfer_is_ended_at_the_limit(self, node):
        base, fake, env = node
        start = time.monotonic()
        proc = _download(env, FAKE_DFGET="hang", NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS="2")
        assert proc.returncode == 124, proc.stdout + proc.stderr
        assert f"CATALOG_DOWNLOAD_TIMEOUT filename={IMAGE} seconds=2" in proc.stdout
        assert time.monotonic() - start < 30            # not dfget's own idea of a timeout
        args = (fake / "dfget.args").read_text().splitlines()
        assert args[args.index("--timeout") + 1] == "2s"
        assert not (base.parent / IMAGE).exists()

    def test_the_default_limit_is_an_hour(self, node):
        _, fake, env = node
        assert _download(env).returncode == 0
        args = (fake / "dfget.args").read_text().splitlines()
        assert args[args.index("--timeout") + 1] == "3600s"

    def test_a_good_transfer_is_verified_and_installed_from_the_derived_origin(self, node):
        base, fake, env = node
        proc = _download(env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (base.parent / IMAGE).read_bytes() == CONTENT
        assert (base / "receipts" / f"{IMAGE}.sha256").read_text() == f"{SHA}  {base.parent / IMAGE}\n"
        assert "http://10.0.0.5:12130/" + IMAGE in (fake / "dfget.args").read_text().splitlines()

    def test_an_explicit_origin_wins(self, node):
        _, fake, env = node
        assert _download(env, NVIS_P2P_ORIGIN="http://seed.example:9000").returncode == 0
        assert "http://seed.example:9000/" + IMAGE in (fake / "dfget.args").read_text().splitlines()


class TestDownloadRefusals:
    def test_no_origin_is_refused_not_guessed(self, node):
        base, fake, env = node
        (base / "catalog-refresh.env").unlink()
        proc = _download(env)
        assert proc.returncode == 64 and "CATALOG_ORIGIN_UNKNOWN" in proc.stderr
        assert not (fake / "dfget.args").exists()

    @pytest.mark.parametrize("value", ["0", "1h", "-5"])
    def test_a_malformed_limit_is_refused(self, node, value):
        _, fake, env = node
        proc = _download(env, NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS=value)
        assert proc.returncode == 64 and "CATALOG_BAD_TIMEOUT" in proc.stderr
        assert not (fake / "dfget.args").exists()


@pytest.fixture
def launch_env(tmp_path):
    """A node as the launch script sees it over ssh: a peer socket and a stand-in download script."""
    bindir = tmp_path / "sshbin"
    bindir.mkdir()
    (bindir / "ssh").write_text('#!/usr/bin/env bash\nwhile [[ $1 == -o ]]; do shift 2; done\nshift\nexec "$@"\n')
    (bindir / "ssh").chmod(0o755)
    base = tmp_path / "c" / ".nvis-p2p"
    for sub in ("run", "bin", "logs"):
        (base / sub).mkdir(parents=True)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(base / "run" / "dfdaemon.sock"))
    record = tmp_path / "download.env"
    (base / "bin" / "download-catalog-image.sh").write_text(
        f'#!/usr/bin/env bash\necho "$NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS $1" > {record}\nexec sleep 300\n')
    (base / "bin" / "download-catalog-image.sh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "NVIS_P2P_BASE": str(base)}
    yield env, base, record
    sock.close()
    pid_file = base / "run" / f"catalog-{IMAGE}.pid"
    if pid_file.exists():
        subprocess.run(["kill", pid_file.read_text().strip()], capture_output=True, check=False)


def _launch(env, *extra, **more):
    return subprocess.run(["bash", str(SCRIPTS / "launch-catalog-image-node.sh"), "node1", IMAGE, *extra],
                          env={**env, **more}, capture_output=True, text=True, timeout=60, check=False)


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)


@pytest.mark.skipif(not _PROCPS, reason="needs ps/pkill (procps; CI installs it, the nodes have it)")
class TestLaunch:
    def test_starts_the_download_with_the_limit(self, launch_env):
        env, _, record = launch_env
        proc = _launch(env, "1800")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("CATALOG_LAUNCH_STARTED") and "timeout=1800s" in proc.stdout
        _wait_for(record)
        assert record.read_text().split() == ["1800", IMAGE]

    def test_a_young_download_is_left_alone(self, launch_env):
        env, base, _ = launch_env
        first = _launch(env, "3600")
        pid = (base / "run" / f"catalog-{IMAGE}.pid").read_text().strip()
        second = _launch(env, "3600")
        assert first.returncode == second.returncode == 0
        assert second.stdout.startswith("CATALOG_LAUNCH_ACTIVE") and f"pid={pid}" in second.stdout
        assert (base / "run" / f"catalog-{IMAGE}.pid").read_text().strip() == pid

    def test_a_download_older_than_the_limit_is_replaced_not_re_adopted(self, launch_env):
        env, base, _ = launch_env
        _launch(env, "1", NVIS_P2P_STALE_GRACE_SECONDS="0")
        old = (base / "run" / f"catalog-{IMAGE}.pid").read_text().strip()
        time.sleep(2.2)
        proc = _launch(env, "1", NVIS_P2P_STALE_GRACE_SECONDS="0")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.startswith("CATALOG_LAUNCH_STARTED") and f"replaced_stale_pid={old}" in proc.stdout
        new = (base / "run" / f"catalog-{IMAGE}.pid").read_text().strip()
        assert new != old
        assert subprocess.run(["kill", "-0", old], capture_output=True, check=False).returncode != 0

    def test_refuses_without_a_running_peer(self, launch_env):
        env, base, _ = launch_env
        (base / "run" / "dfdaemon.sock").unlink()
        proc = _launch(env, "3600")
        assert proc.returncode == 69 and "no-peer-socket" in proc.stderr

    @pytest.mark.parametrize(("args", "fragment"), [(("0",), "BAD_TIMEOUT"), (("3600",), "REJECTED")])
    def test_refuses_bad_input(self, launch_env, args, fragment):
        env, _, _ = launch_env
        name = IMAGE if fragment == "BAD_TIMEOUT" else "../etc/passwd"
        proc = subprocess.run(["bash", str(SCRIPTS / "launch-catalog-image-node.sh"), "node1", name, *args],
                              env=env, capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == 64 and fragment in proc.stderr
