# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A download that stops moving is stopped and relaunched, not waited on until the wave times out.

Author: Rafael M. Koike

On NC a single node's dfget sat on 40 of 42.5 GB for over a quarter of an hour, idle, and held a
whole wave open until its 30-minute timeout. The rollout now watches how many bytes each ACTIVE
download has actually written (the check script reports the partial's allocated blocks, since dfget
preallocates its length) and treats a download as stalled once that number has not moved for
``NVIS_P2P_ROLLOUT_STALL_SECONDS``. A stalled node is stopped and relaunched -- the peer keeps the
pieces it has, and the scheduler picks new parents -- or deferred once it has used its launches.

What is pinned: a stall is stopped and relaunched; a download that is moving, or that has every
byte and is checksumming, is left alone -- the latter for twice the window or, for a large image, the
image's size at ``NVIS_P2P_ROLLOUT_VERIFY_MBPS`` when that is longer (a 36.8 GB NeMo image hashed
on a node under burn-in load outlasted 2 x 60 s and was discarded, NC 2026-10-20), then it too is a
stall; a node that stalls through all its launches is
deferred with a reason that says so; 0 turns the behaviour off. The check and stop scripts run end
to end through a stand-in ``ssh`` that executes the remote half locally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "deploy" / "p2p" / "scripts"
IMAGE = "nemo.sqsh"
FULL = 5000

_GNU_STAT = subprocess.run(["stat", "-c", "%s", "/"], capture_output=True, check=False).returncode == 0
_PROCPS = bool(shutil.which("pkill") and shutil.which("pgrep"))
# The rollout tees its log through a process substitution; bash 3.2 (macOS's /bin/bash) loses that
# output. The nodes, the app server and CI all run bash 5.
_BASH_MAJOR = int(subprocess.run(["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True,
                                 check=False).stdout.strip() or 0)

# A stand-in for check-catalog-image-node.sh whose answer depends on the node's scripted behaviour
# and on what the launch/stop stand-ins have done to it.
_CHECK = r"""#!/usr/bin/env bash
n=$1; d="$FAKE/state-$n"; mkdir -p "$d"
calls=$(( $(cat "$d/calls" 2>/dev/null || echo 0) + 1 )); echo "$calls" > "$d/calls"
mode=$(cat "$FAKE/mode-$n")
if [[ ! -e $d/launched ]]; then printf '%s\tPENDING\tnot_started\n' "$n"; exit 0; fi
case $mode in
  stall)        if [[ -e $d/stopped ]]; then printf '%s\tREADY\treceipt_valid\n' "$n";
                else printf '%s\tACTIVE\tpid=1 written=100\n' "$n"; fi ;;
  always-stall) printf '%s\tACTIVE\tpid=1 written=100\n' "$n" ;;
  progress)     if (( calls >= 7 )); then printf '%s\tREADY\treceipt_valid\n' "$n";
                else printf '%s\tACTIVE\tpid=1 written=%s\n' "$n" "$((calls * 100))"; fi ;;
  verifying)    if (( calls >= 4 )); then printf '%s\tREADY\treceipt_valid\n' "$n";
                else printf '%s\tACTIVE\tpid=1 written=%s\n' "$n" "$FULL"; fi ;;
  slow-verify)  if (( calls >= 9 )); then printf '%s\tREADY\treceipt_valid\n' "$n";
                else printf '%s\tACTIVE\tpid=1 written=%s\n' "$n" "$FULL"; fi ;;
  hung-full)    if [[ -e $d/stopped ]]; then printf '%s\tREADY\treceipt_valid\n' "$n";
                else printf '%s\tACTIVE\tpid=1 written=%s\n' "$n" "$((FULL + 40960))"; fi ;;
esac
"""
_LAUNCH = r"""#!/usr/bin/env bash
mkdir -p "$FAKE/state-$1"; touch "$FAKE/state-$1/launched"; echo "launch $1" >> "$FAKE/calls.log"
echo "$3" > "$FAKE/limit-$1"
echo "CATALOG_LAUNCH_STARTED node=$1"
"""
_STOP = r"""#!/usr/bin/env bash
mkdir -p "$FAKE/state-$1"; touch "$FAKE/state-$1/stopped"; echo "stop $1" >> "$FAKE/calls.log"
echo "CATALOG_STOPPED node=$1"
"""


def _rollout(tmp_path: Path, modes: dict[str, str], stall: int, max_launches: int = 3, timeout: int = 60,
             full: int = FULL, **extra_env: str):
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "catalog").mkdir()
    (root / "catalog" / "images.tsv").write_text(f"1\tNeMo\t{IMAGE}\t{full}\t{'a' * 64}\n")
    for name, body in (("check-catalog-image-node.sh", _CHECK), ("launch-catalog-image-node.sh", _LAUNCH),
                       ("stop-catalog-image-node.sh", _STOP), ("preheat-catalog-image.sh", "#!/bin/bash\n")):
        (root / "scripts" / name).write_text(body)
        (root / "scripts" / name).chmod(0o755)
    fake = tmp_path / "fake"
    fake.mkdir()
    for node, mode in modes.items():
        (fake / f"mode-{node}").write_text(mode)
    roster = tmp_path / "roster.nodes"
    roster.write_text("\n".join(modes) + "\n")
    env = {**os.environ, "NVIS_P2P_ROOT": str(root), "FAKE": str(fake), "FULL": str(full),
           "NVIS_P2P_ROLLOUT_POLL_SECONDS": "1", "NVIS_P2P_ROLLOUT_STALL_SECONDS": str(stall),
           "NVIS_P2P_ROLLOUT_MAX_LAUNCHES": str(max_launches), "NVIS_P2P_ROLLOUT_TIMEOUT_SECONDS": str(timeout),
           **extra_env}
    proc = subprocess.run(["bash", str(SCRIPTS / "rollout-catalog-image-fleet.sh"), IMAGE, str(roster), "t"],
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    calls = (fake / "calls.log").read_text().splitlines() if (fake / "calls.log").exists() else []
    run_dir = next((root / "state").glob("rollout-t-*"))
    deferred = dict(line.split("\t")[:2] for line in (run_dir / "deferred.tsv").read_text().splitlines()[1:])
    # The rollout tees its output into the run's log; the log is complete when the script returns,
    # its stdout through the tee may not be yet.
    proc.stdout = (run_dir / "rollout.log").read_text()
    return proc, calls, deferred


@pytest.mark.skipif(_BASH_MAJOR < 4, reason="the rollout needs bash >= 4 (nodes, app server and CI have 5)")
class TestStallHandling:
    def test_a_stalled_download_is_stopped_and_relaunched(self, tmp_path):
        proc, calls, deferred = _rollout(tmp_path, {"a": "stall"}, stall=2)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls == ["launch a", "stop a", "launch a"]
        assert "ROLLOUT_STALLED" in proc.stdout and "ROLLOUT_COMPLETE total=1 ready=1 deferred=0" in proc.stdout
        assert deferred == {}

    @pytest.mark.parametrize("mode", ["progress", "verifying"])
    def test_a_moving_or_checksumming_download_is_left_alone(self, tmp_path, mode):
        proc, calls, deferred = _rollout(tmp_path, {"a": mode}, stall=2)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls == ["launch a"]
        assert "ROLLOUT_STALLED" not in proc.stdout and deferred == {}

    def test_a_complete_download_that_never_finishes_is_stopped_after_twice_the_window(self, tmp_path):
        """Every byte allocated (even a little over: blocks round up) and no receipt for longer than a
        checksum takes is a hung dfget, not a slow checksum -- on NC one sat like that for ten hours."""
        start = time.monotonic()
        proc, calls, deferred = _rollout(tmp_path, {"a": "hung-full"}, stall=2)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls == ["launch a", "stop a", "launch a"]
        assert deferred == {}
        assert time.monotonic() - start >= 4          # not before twice the window

    def test_a_node_that_stalls_through_its_launches_is_deferred_and_stopped(self, tmp_path):
        proc, calls, deferred = _rollout(tmp_path, {"a": "always-stall", "b": "progress"}, stall=2, max_launches=2)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert [c for c in calls if c.endswith(" a")] == ["launch a", "stop a", "launch a", "stop a"]
        assert deferred == {"a": "stalled_2s_after_2_launches"}
        assert "ROLLOUT_COMPLETE_WITH_DEFERRALS total=2 ready=1 deferred=1" in proc.stdout

    def test_zero_turns_stall_handling_off_and_a_timeout_still_stops_the_download(self, tmp_path):
        """Deferred has to mean stopped: a download left running past its wave was re-adopted by the
        next launch on NC, run after run."""
        proc, calls, deferred = _rollout(tmp_path, {"a": "always-stall"}, stall=0, timeout=4)
        assert calls == ["launch a", "stop a"]
        assert deferred["a"].startswith("timeout_after_")
        assert "ROLLOUT_TIMEOUT_STOPPED nodes=1" in proc.stdout

    def test_every_launch_carries_the_download_limit(self, tmp_path):
        _rollout(tmp_path, {"a": "progress"}, stall=2, NVIS_P2P_DOWNLOAD_TIMEOUT_SECONDS="1234")
        assert (tmp_path / "fake" / "limit-a").read_text().strip() == "1234"

    def test_the_download_limit_defaults_to_an_hour(self, tmp_path):
        _rollout(tmp_path, {"a": "progress"}, stall=2)
        assert (tmp_path / "fake" / "limit-a").read_text().strip() == "3600"

    def test_a_malformed_stall_window_is_refused(self, tmp_path):
        proc, _, _ = _rollout_bad(tmp_path)
        assert proc.returncode == 64 and "ROLLOUT_BAD_LIMIT value=soon" in proc.stderr

    def test_a_malformed_verify_rate_is_refused(self, tmp_path):
        proc, _, _ = _rollout_bad(tmp_path, NVIS_P2P_ROLLOUT_STALL_SECONDS="2", NVIS_P2P_ROLLOUT_VERIFY_MBPS="fast")
        assert proc.returncode == 64 and "ROLLOUT_BAD_LIMIT value=fast" in proc.stderr


#: 10 GB at 1000 MB/s is a 10 s verify window -- longer than twice a 2 s stall window.
BIG = 10_000_000_000


@pytest.mark.skipif(_BASH_MAJOR < 4, reason="the rollout needs bash >= 4 (nodes, app server and CI have 5)")
class TestVerifyWindowScalesWithTheImage:
    def test_a_large_image_still_checksumming_past_twice_the_window_is_left_alone(self, tmp_path):
        proc, calls, deferred = _rollout(tmp_path, {"a": "slow-verify"}, stall=2, full=BIG,
                                         NVIS_P2P_ROLLOUT_VERIFY_MBPS="1000")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls == ["launch a"]
        assert "ROLLOUT_STALLED" not in proc.stdout and deferred == {}
        assert "stall_seconds=2 verify_stall_seconds=10" in proc.stdout

    def test_the_same_checksum_on_a_flat_window_is_stopped(self, tmp_path):
        """0 keeps the old rule, which is what discarded the 36.8 GB image on busy NC nodes."""
        proc, calls, _ = _rollout(tmp_path, {"a": "slow-verify"}, stall=2, full=BIG,
                                  NVIS_P2P_ROLLOUT_VERIFY_MBPS="0")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls[:2] == ["launch a", "stop a"]
        assert "verify_stall_seconds=4" in proc.stdout

    def test_a_large_image_that_never_finishes_is_still_stopped_and_says_which_window(self, tmp_path):
        start = time.monotonic()
        proc, calls, deferred = _rollout(tmp_path, {"a": "hung-full"}, stall=2, full=BIG, max_launches=1,
                                         NVIS_P2P_ROLLOUT_VERIFY_MBPS="1000")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert calls == ["launch a", "stop a"]
        assert deferred == {"a": "stalled_10s_after_1_launches"}
        assert time.monotonic() - start >= 10

    def test_a_small_image_keeps_twice_the_window(self, tmp_path):
        proc, _, _ = _rollout(tmp_path, {"a": "verifying"}, stall=2, NVIS_P2P_ROLLOUT_VERIFY_MBPS="1")
        assert "stall_seconds=2 verify_stall_seconds=4" in proc.stdout


def _rollout_bad(tmp_path, **env_extra: str):
    roster = tmp_path / "r.nodes"
    roster.write_text("a\n")
    env = {**os.environ, "NVIS_P2P_ROOT": str(tmp_path), "NVIS_P2P_ROLLOUT_STALL_SECONDS": "soon", **env_extra}
    proc = subprocess.run(["bash", str(SCRIPTS / "rollout-catalog-image-fleet.sh"), IMAGE, str(roster)],
                          env=env, capture_output=True, text=True, timeout=30, check=False)
    return proc, None, None


@pytest.fixture
def fake_ssh(tmp_path):
    """An ``ssh`` that drops its options and host and runs the remote command here."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").write_text('#!/usr/bin/env bash\nwhile [[ $1 == -o ]]; do shift 2; done\nshift\nexec "$@"\n')
    (bindir / "ssh").chmod(0o755)
    base = tmp_path / "containers" / ".nvis-p2p"
    for sub in ("run", "staging", "receipts", "logs"):
        (base / sub).mkdir(parents=True)
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "NVIS_P2P_BASE": str(base)}
    return env, base


def _sleeper() -> subprocess.Popen:
    """A stand-in download: a shell with a child, as download-catalog-image.sh runs dfget."""
    return subprocess.Popen(["bash", "-c", "sleep 300 & wait"])


class TestCheckReportsWrittenBytes:
    def _check(self, env):
        out = subprocess.run(["bash", str(SCRIPTS / "check-catalog-image-node.sh"), "node1", IMAGE, str(FULL), "a" * 64],
                             env=env, capture_output=True, text=True, timeout=30, check=True).stdout
        return out.rstrip("\n").split("\t")

    @pytest.mark.skipif(not _GNU_STAT, reason="needs GNU stat (the nodes and CI have it)")
    def test_an_active_download_reports_the_bytes_it_has_written(self, fake_ssh):
        env, base = fake_ssh
        proc = _sleeper()
        try:
            (base / "run" / f"catalog-{IMAGE}.pid").write_text(f"{proc.pid}\n")
            with open(base / "staging" / f"{IMAGE}.partial", "wb") as fh:
                fh.truncate(64 * 1024 * 1024)          # preallocated length, mostly holes
                fh.write(b"x" * (1024 * 1024))         # 1 MiB actually written
            node, state, detail = self._check(env)
            assert (node, state) == ("node1", "ACTIVE")
            written = int(detail.split("written=")[1])
            assert 1024 * 1024 <= written < 64 * 1024 * 1024
        finally:
            proc.kill()

    def test_before_a_partial_exists_there_is_nothing_to_measure(self, fake_ssh):
        env, base = fake_ssh
        proc = _sleeper()
        try:
            (base / "run" / f"catalog-{IMAGE}.pid").write_text(f"{proc.pid}\n")
            assert self._check(env) == ["node1", "ACTIVE", f"pid={proc.pid} written=-1"]
        finally:
            proc.kill()

    @pytest.mark.skipif(not _GNU_STAT, reason="needs GNU stat (the nodes and CI have it)")
    def test_a_finished_image_is_ready_beside_the_peer_directory(self, fake_ssh):
        env, base = fake_ssh
        target = base.parent / IMAGE
        target.write_bytes(b"x" * FULL)
        (base / "receipts" / f"{IMAGE}.sha256").write_text(f"{'a' * 64}  {target}\n")
        assert self._check(env) == ["node1", "READY", "receipt_valid"]


@pytest.mark.skipif(not _PROCPS, reason="needs pkill/pgrep (procps; CI installs it, the nodes have it)")
class TestStopScript:
    def _stop(self, env):
        return subprocess.run(["bash", str(SCRIPTS / "stop-catalog-image-node.sh"), "node1", IMAGE],
                              env=env, capture_output=True, text=True, timeout=60, check=True).stdout

    def test_stops_the_download_and_its_child_then_clears_the_pid(self, fake_ssh):
        env, base = fake_ssh
        proc = _sleeper()
        pid_file = base / "run" / f"catalog-{IMAGE}.pid"
        pid_file.write_text(f"{proc.pid}\n")
        time.sleep(0.3)
        children = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True, check=False).stdout.split()
        assert children, "the stand-in download should have a child"
        out = self._stop(env)
        assert out.startswith("CATALOG_STOPPED") and f"pid={proc.pid}" in out
        assert proc.wait(timeout=15) is not None
        assert not pid_file.exists()
        for child in children:
            assert subprocess.run(["kill", "-0", child], capture_output=True, check=False).returncode != 0

    def test_nothing_running_is_reported_not_failed(self, fake_ssh):
        env, base = fake_ssh
        (base / "run" / f"catalog-{IMAGE}.pid").write_text("999999\n")
        assert self._stop(env).startswith("CATALOG_NOT_RUNNING")

    def test_refuses_a_filename_that_is_not_a_catalog_image(self, fake_ssh):
        env, _ = fake_ssh
        proc = subprocess.run(["bash", str(SCRIPTS / "stop-catalog-image-node.sh"), "node1", "../etc/passwd"],
                              env=env, capture_output=True, text=True, timeout=30, check=False)
        assert proc.returncode != 0


def test_the_new_stop_script_ships_executable():
    """The rollout runs it by path, like the check and launch scripts beside it."""
    assert os.access(SCRIPTS / "stop-catalog-image-node.sh", os.X_OK)
