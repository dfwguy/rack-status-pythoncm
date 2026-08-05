# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The llmb auto-collect watcher must exist once and never fail silently.

Both llmb launchers carried their own copy of the watcher, and the copies drifted on exactly the
gate that matters: ``run_llmb_nccl.sh`` was fixed from ``-x`` to ``-r`` while ``run_llmb_nemo.sh``
kept ``-x``. Git tracks the collectors 100644 and the watcher invokes them as ``bash "$collect"``,
so on every deployed run the NeMo gate could not pass and auto-collect was skipped without a
word -- job 127327 ran 50/50 iterations and the dashboard reported "Stage 2 - NeMo: fail".

The watcher now lives in ``bin/scripts/lib/llmb_autocollect.inc.sh``. These tests drive it with a
fake squeue and a fake collector, so they need no cluster.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "bin" / "scripts"
INC = SCRIPTS / "lib" / "llmb_autocollect.inc.sh"
LAUNCHERS = [SCRIPTS / "run_llmb_nccl.sh", SCRIPTS / "run_llmb_nemo.sh"]


@pytest.fixture
def rig(tmp_path):
    """A fake squeue that reports the job already gone, and a collector that records its argv."""
    squeue = tmp_path / "squeue"
    squeue.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    squeue.chmod(0o755)

    argv = tmp_path / "collector_argv.txt"
    collector = tmp_path / "collect_fake.sh"
    # Deliberately NOT executable (0o644), like the deployed collectors: the include must still
    # arm, because it runs `bash "$collector"`.
    collector.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$@" > "{argv}"\n'
        f'printf "RACK_JOB_LOG_DIR=%s\\nLLMB_INSTALL=%s\\n" "$RACK_JOB_LOG_DIR" "$LLMB_INSTALL" >> "{argv}"\n',
        encoding="utf-8",
    )
    collector.chmod(0o644)

    logdir = tmp_path / "logs"
    logdir.mkdir()
    out = tmp_path / "llmb.out"
    out.write_text("some preamble\nSUBMITTED: jobid=127327 partition=defq\n", encoding="utf-8")
    return {
        "squeue": squeue,
        "collector": collector,
        "argv": argv,
        "logdir": logdir,
        "out": out,
        "tmp": tmp_path,
    }


def _arm(rig, *extra_args: str, tag: str = "run_llmb_test", log_base: str = "llmb-autocollect", **env: str):
    script = f'''
      source "{INC}"
      llmb_arm_autocollect "{tag}" "{rig['collector']}" "{rig['out']}" \\
        "{rig['logdir']}" "/fake/llmb" "{log_base}" {" ".join(f'"{a}"' for a in extra_args)}
    '''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "RACK_SLURM_SQUEUE": str(rig["squeue"]),
            "NVIS_LLMB_AUTOCOLLECT_SETTLE_SEC": "0",
            "NVIS_LLMB_AUTOCOLLECT_POLL_SEC": "1",
            **env,
        },
    )


def _wait_for(path: Path, timeout: float = 30.0) -> bool:
    """The watcher is detached, so the caller cannot ``wait`` for it -- poll for its output."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            return True
        time.sleep(0.1)
    return False


# --- job id parsing -----------------------------------------------------------------------


def test_jobid_is_read_from_llmb_run_output(rig):
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; llmb_jobid_from_output "$1"', "_", str(rig["out"])],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip() == "127327"


def test_missing_output_file_yields_no_jobid(tmp_path):
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; llmb_jobid_from_output "$1"', "_", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.stdout.strip() == ""


# --- arming -------------------------------------------------------------------------------


def test_a_non_executable_collector_still_gets_armed_and_run(rig):
    """The exact 127327 regression: mode 644 must not disable auto-collect."""
    assert oct(rig["collector"].stat().st_mode)[-3:] == "644"
    proc = _arm(rig)
    assert proc.returncode == 0, proc.stderr
    assert "auto-collect armed for job 127327" in proc.stdout
    assert _wait_for(rig["argv"]), f"collector never ran; watcher log: {_watcher_log(rig)}"
    assert rig["argv"].read_text(encoding="utf-8").splitlines()[0] == "127327"


def test_extra_args_reach_the_collector_after_the_jobid(rig):
    """The NeMo collector needs the rack id to name the aggregated log."""
    proc = _arm(rig, "ink50-dh01-h01")
    assert proc.returncode == 0, proc.stderr
    assert _wait_for(rig["argv"]), f"collector never ran; watcher log: {_watcher_log(rig)}"
    lines = rig["argv"].read_text(encoding="utf-8").splitlines()
    assert lines[0] == "127327"
    assert lines[1] == "ink50-dh01-h01"


def test_the_watcher_exports_the_log_dir_and_llmb_install(rig):
    _arm(rig)
    assert _wait_for(rig["argv"]), f"collector never ran; watcher log: {_watcher_log(rig)}"
    body = rig["argv"].read_text(encoding="utf-8")
    assert f"RACK_JOB_LOG_DIR={rig['logdir']}" in body
    assert "LLMB_INSTALL=/fake/llmb" in body


def test_watcher_output_lands_in_a_named_log(rig):
    _arm(rig, log_base="llmb-nemo-autocollect")
    log = rig["logdir"] / "llmb-nemo-autocollect_127327.log"
    _wait_for(rig["argv"])
    assert log.is_file()


def test_the_watcher_starts_even_without_setsid(rig):
    """setsid is missing on some hosts; the watcher must still run rather than die unnoticed."""
    fake_bin = rig["tmp"] / "nosetsid"
    fake_bin.mkdir()
    for tool in ("bash", "nohup", "grep", "sleep", "seq", "cut", "head"):
        src = subprocess.run(["command", "-v", tool], capture_output=True, text=True, shell=False)
        path = src.stdout.strip() or f"/usr/bin/{tool}"
        target = fake_bin / tool
        if not target.exists() and Path(path).exists():
            target.symlink_to(path)
    proc = _arm(rig, PATH=str(fake_bin))
    assert proc.returncode == 0, proc.stderr
    assert _wait_for(rig["argv"]), f"watcher did not run without setsid; log: {_watcher_log(rig)}"


def _watcher_log(rig) -> str:
    logs = list(rig["logdir"].glob("*.log"))
    return logs[0].read_text(encoding="utf-8") if logs else "(no watcher log)"


# --- skipping is always loud ---------------------------------------------------------------


def test_an_unreadable_collector_warns_and_does_not_arm(rig):
    rig["collector"].unlink()
    proc = _arm(rig)
    assert proc.returncode == 1
    assert "WARNING" in proc.stderr
    assert "collect_fake.sh" in proc.stderr
    assert not rig["argv"].exists()


def test_an_unparseable_jobid_warns_and_does_not_arm(rig):
    rig["out"].write_text("llmb-run failed before submitting\n", encoding="utf-8")
    proc = _arm(rig)
    assert proc.returncode == 1
    assert "could not parse jobid" in proc.stderr
    assert not rig["argv"].exists()


def test_the_kill_switch_skips_quietly(rig):
    proc = _arm(rig, NVIS_LLMB_AUTOCOLLECT="0")
    assert proc.returncode == 0
    assert "WARNING" not in proc.stderr
    assert not rig["argv"].exists()


# --- structure ----------------------------------------------------------------------------


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_launcher_sources_the_shared_include(launcher: Path):
    assert "lib/llmb_autocollect.inc.sh" in launcher.read_text(encoding="utf-8")


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_launcher_has_no_inline_watcher_left(launcher: Path):
    body = launcher.read_text(encoding="utf-8")
    assert "setsid nohup" not in body, f"{launcher.name} still inlines the watcher"
    assert "jobid=[0-9]" not in body, f"{launcher.name} still parses the jobid itself"


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_no_launcher_gates_the_collector_on_the_execute_bit(launcher: Path):
    """The drift that caused 127327. -x can never pass on a git-tracked 644 collector."""
    for line in launcher.read_text(encoding="utf-8").splitlines():
        assert not re.search(r"-x\s+\"\$\{?_collect", line), f"{launcher.name}: {line.strip()}"


def test_the_include_gates_on_readable():
    body = INC.read_text(encoding="utf-8")
    assert '-r "$collector"' in body
    assert '-x "$collector"' not in body


@pytest.mark.parametrize("launcher", LAUNCHERS, ids=lambda p: p.name)
def test_launcher_still_parses(launcher: Path):
    assert subprocess.run(["bash", "-n", str(launcher)], timeout=60).returncode == 0
