# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The NeMo/LLM verdict must be one rule, not one per surface.

``rack_status.sh`` (which answers the dashboard through ``--probe-json``) and ``parser.sh``
(which builds the Parser Results tables) each used to carry their own ``llm_label``. The
copies drifted: parser.sh checked only ``llm_stats_tsv`` then ``slurm_hard_fail``, so

  * a run that reached its declared target WITHOUT a step-timing window (a 50-step llmb
    benchmark, e.g. Nscale TX job 127327) read ``UNK`` in Parser Results while the
    dashboard read pass, and
  * a run that died on a Python traceback or CUDA OOM read ``UNK`` instead of ``FAIL``.

Both now source ``bin/scripts/lib/llm_label.inc.sh``. These tests pin the verdict table and
guard structurally against re-inlining, because a second copy is the only way this returns.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO_ROOT / "bin" / "scripts"
INC = SCRIPTS / "lib" / "llm_label.inc.sh"
RACK_STATUS = SCRIPTS / "rack_status.sh"
PARSER = SCRIPTS / "parser.sh"


# --- fixtures: one per branch of the verdict table ----------------------------------------

# Megatron-Bridge dialect, 100 windowed samples ending exactly at the declared target:
# stats parse AND the run finished, so OK *with numbers* and llm_ok true.
MEASURED = "".join(
    f"Step Time : 6.40s GPU utilization: 1287.3MODEL_TFLOP/s/GPU\n"
    f" [2026-08-05] iteration {i:8d}/    2010 | lm loss: 1.4E-02 |\n"
    for i in range(1911, 2011)
)

# Same measured window, but stopped at iteration 100 of 2010. ``llm_label`` says OK because
# the perf stats are real, yet the suite still refuses: stats prove the run was healthy while
# it lasted, not that it completed. Pinning this because it is easy to mistake for a bug.
MEASURED_UNFINISHED = "".join(
    f"Step Time : 6.40s GPU utilization: 1287.3MODEL_TFLOP/s/GPU\n"
    f" [2026-08-05] iteration {i:8d}/    2010 | lm loss: 1.4E-02 |\n"
    for i in range(1, 101)
)

# Reached its declared target, but too short to have a 70-89 window. This is the case
# parser.sh used to call UNK. With NEMO_TARGET_STEPS=50 it is a pass.
FINISHED_NO_WINDOW = (
    " [2026-08-05 17:15:19] iteration        1/      50 | lm loss: 1.2E+01 |\n"
    " [2026-08-05 17:20:37] iteration       50/      50 | lm loss: 1.4E-02 |\n"
    "[after training is done] datetime: 2026-08-05 17:20:38\n"
)

# Declared 2010, stopped at 50: cur < tot, so NOT finished and no fatal marker -> abstain.
TRUNCATED = " [2026-08-05] iteration       50/    2010 | lm loss: 3.1E+00 |\n"

# Slurm-level hard failure.
SLURM_FAIL = (
    "srun: error: node-11: task 0: Exited with exit code 1\n"
    " [2026-08-05] iteration       12/    2010 |\n"
)

# Python / CUDA fatal. parser.sh used to miss this entirely.
PYTHON_FATAL = (
    " [2026-08-05] iteration       12/    2010 |\n"
    "Traceback (most recent call last):\n"
    '  File "/nemo_run/code/train.py", line 1, in <module>\n'
)
CUDA_OOM = (
    " [2026-08-05] iteration        7/    2010 |\n"
    "torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n"
)

# Nothing recognisable: never guess.
NOISE = "+ export PYTHONUNBUFFERED=1\nsome unrelated output\n"


def _label_file(path: Path, target_steps: str = "2010") -> list[str]:
    """Tab-split output of the shared include's ``llm_label`` for *path*."""
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; llm_label "$1"', "_", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "NEMO_TARGET_STEPS": target_steps},
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rstrip("\n").split("\t")


def _verdict(tmp_path: Path, text: str, *, target_steps: str = "2010") -> str:
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4242.txt"
    p.write_text(text, encoding="utf-8")
    return _label_file(p, target_steps)[0]


# --- the verdict table ---------------------------------------------------------------------


def test_measured_run_is_ok_with_numbers(tmp_path):
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-1.txt"
    p.write_text(MEASURED, encoding="utf-8")
    fields = _label_file(p)
    assert fields[0] == "OK"
    assert fields[1] != "-", "a measured run must carry step_s"
    assert float(fields[2]) == pytest.approx(1287.3, rel=1e-3), "tflops/GPU"


def test_measured_but_incomplete_is_still_ok_at_the_label_level(tmp_path):
    """llm_label reports on evidence quality; completeness is the suite's job (see the
    end-to-end test). A healthy-but-truncated run is OK here and NOT llm_ok there."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-3.txt"
    p.write_text(MEASURED_UNFINISHED, encoding="utf-8")
    assert _label_file(p)[0] == "OK"


def test_finished_without_a_window_is_ok(tmp_path):
    """The 127327 case: 50/50 complete, no 70-89 window. parser.sh used to say UNK."""
    assert _verdict(tmp_path, FINISHED_NO_WINDOW, target_steps="50") == "OK"


def test_finished_below_the_target_is_not_ok(tmp_path):
    """tot must also clear NEMO_TARGET_STEPS: a 50-step recipe is not a 2010-step soak."""
    assert _verdict(tmp_path, FINISHED_NO_WINDOW, target_steps="2010") == "UNK"


def test_truncated_run_abstains(tmp_path):
    assert _verdict(tmp_path, TRUNCATED) == "UNK"


def test_slurm_hard_failure_is_fail(tmp_path):
    assert _verdict(tmp_path, SLURM_FAIL) == "FAIL"


def test_python_traceback_is_fail(tmp_path):
    """parser.sh had no python-fatal check at all."""
    assert _verdict(tmp_path, PYTHON_FATAL) == "FAIL"


def test_cuda_oom_is_fail(tmp_path):
    assert _verdict(tmp_path, CUDA_OOM) == "FAIL"


def test_noise_abstains(tmp_path):
    assert _verdict(tmp_path, NOISE) == "UNK"


def test_peak_tflops_is_overridable(tmp_path):
    """MFU denominator is a platform property, not a constant. Default stays 5000."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-2.txt"
    p.write_text(MEASURED, encoding="utf-8")
    default_mfu = float(_label_file(p)[5])
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; llm_label "$1"', "_", str(p)],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "NVIS_LLM_PEAK_TFLOPS": "2500"},
    )
    assert proc.returncode == 0, proc.stderr
    halved_peak_mfu = float(proc.stdout.rstrip("\n").split("\t")[5])
    # MFU is printed with one decimal, so doubling a rounded value can differ by up to 0.1
    # in each operand; compare within that quantisation rather than a relative epsilon.
    assert abs(halved_peak_mfu - default_mfu * 2) <= 0.2, (default_mfu, halved_peak_mfu)


# --- structural guards: the copies must not come back -------------------------------------


@pytest.mark.parametrize("script", [RACK_STATUS, PARSER])
def test_script_sources_the_shared_include(script: Path):
    text = script.read_text(encoding="utf-8")
    assert "lib/llm_label.inc.sh" in text, f"{script.name} must source the shared include"


@pytest.mark.parametrize("script", [RACK_STATUS, PARSER])
@pytest.mark.parametrize(
    "func",
    ["llm_label", "llm_stats_tsv", "nemo_training_finished_log", "nemo_python_fatal_fail"],
)
def test_script_does_not_redefine_the_verdict(script: Path, func: str):
    """A local definition would shadow the include and let the two surfaces drift again.

    parser.sh keeps abstaining *stubs* guarded by ``declare -F`` for partial deploys; those
    are single-line fallbacks, not definitions, so they are allowed.
    """
    for line in script.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^\s*{func}\s*\(\)\s*\{{", line):
            assert "declare -F" in line, f"{script.name} redefines {func}: {line.strip()}"


def test_include_owns_the_mfu_denominator():
    """`perf` must come from the include so both surfaces compute MFU identically."""
    assert re.search(r'^perf="\$\{perf:-', INC.read_text(encoding="utf-8"), re.MULTILINE)
    for script in (RACK_STATUS, PARSER):
        body = script.read_text(encoding="utf-8")
        assert not re.search(r"^perf=\d+", body, re.MULTILINE), (
            f"{script.name} still hardcodes perf; the include owns it"
        )


# --- end-to-end: the two surfaces must agree ----------------------------------------------

_BASH_MAJOR = int(
    subprocess.run(
        ["bash", "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True
    ).stdout.strip()
    or 0
)


@pytest.mark.skipif(_BASH_MAJOR < 4, reason="rack_status.sh needs bash 4+ (declare -A)")
@pytest.mark.parametrize(
    "text,target,want_llm_ok",
    [
        (MEASURED, "2010", True),
        (FINISHED_NO_WINDOW, "50", True),
        (FINISHED_NO_WINDOW, "2010", False),
        (TRUNCATED, "2010", False),
        (PYTHON_FATAL, "2010", False),
        (CUDA_OOM, "2010", False),
        # OK from llm_label is necessary but not sufficient: the suite also demands the run
        # be finished (or still queued). Measured-but-incomplete must not pass.
        (MEASURED_UNFINISHED, "2010", False),
    ],
)
def test_probe_json_llm_ok_matches_the_suite(tmp_path, text, target, want_llm_ok):
    """``rack_status.sh --probe-json`` is what the dashboard reads; it must agree."""
    import json

    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4242.txt"
    p.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        [
            "bash", str(RACK_STATUS), "--no-squeue", "--no-cmsh", "--no-sinfo",
            "--probe-json", str(p),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "NEMO_TARGET_STEPS": target,
            "VALIDATION_TEST": "llm",  # isolate the LLM half from NCCL requirements
            "NO_COLOR": "1",
        },
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["llm_ok"] is want_llm_ok, payload
    # One-way implication: llm_ok can only be true when llm_label said OK. The converse does
    # not hold, because the suite additionally requires the run to be finished or queued.
    letter = _label_file(p, target)[0]
    if payload["llm_ok"]:
        assert letter == "OK", f"probe passed but the include says {letter}"
