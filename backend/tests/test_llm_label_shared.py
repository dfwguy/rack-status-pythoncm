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
    """NEMO_TARGET_STEPS is an OPT-IN acceptance floor: when it IS set, a 50-step recipe still
    does not clear a 2010-step bar."""
    assert _verdict(tmp_path, FINISHED_NO_WINDOW, target_steps="2010") == "UNK"


def _finished_unset_target(tmp_path: Path, text: str) -> bool:
    """rc of nemo_training_finished_log with NEMO_TARGET_STEPS absent from the environment."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-4243.txt"
    p.write_text(text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "NEMO_TARGET_STEPS"}
    proc = subprocess.run(
        ["bash", "-c", f'source "{INC}"; nemo_training_finished_log "$1"', "_", str(p)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    return proc.returncode == 0


def test_a_short_run_that_reached_its_own_target_is_finished(tmp_path):
    """The 140457 case. A 50-step run is a real run: it declared 50 and reached 50.

    The old rule demanded tot >= 2010, so a 576-node run measuring 5.75 s/step at
    1432 TFLOP/s/GPU scored llm_ok=0 and never reached Charts or Analytics.
    """
    assert _finished_unset_target(tmp_path, FINISHED_NO_WINDOW) is True


def test_a_truncated_run_is_still_not_finished_without_a_target(tmp_path):
    """Dropping the floor must not make every log 'finished' — cur >= tot still has to hold."""
    assert _finished_unset_target(tmp_path, TRUNCATED) is False


def test_a_log_with_no_iteration_marker_is_not_finished(tmp_path):
    assert _finished_unset_target(tmp_path, NOISE) is False


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


# --- the sample window ---------------------------------------------------------------------
#
# The window used to be a fixed "matched lines 70-89". Every current recipe runs 50 steps
# (NVIS_NEMO_MAX_STEPS), so the window sat entirely PAST the end of the run: awk collected
# nothing, exited 1, and the log reported OK with a dash for every number. On Nscale TX that
# silently emptied the Analytics scaling tables -- 13 of the last 14 NeMo runs contributed
# nothing, including a 4-rack multirack run whose NCCL series was drawn in the chart right
# above the table it was missing from.


def _step_time_samples(n: int) -> str:
    """*n* Megatron-Bridge samples where the first 20% are deliberately slow warmup."""
    out = []
    for i in range(1, n + 1):
        if i <= n // 5:
            out.append("Step Time : 9.99s GPU utilization: 900.0MODEL_TFLOP/s/GPU\n")
        else:
            out.append("Step Time : 4.22s GPU utilization: 1954.6MODEL_TFLOP/s/GPU\n")
    return "".join(out)


def test_a_fifty_step_run_reports_numbers(tmp_path):
    """The production case: 50 real samples must average, not vanish."""
    p = tmp_path / "root-NEMO-p05-r01+p05-r02-72N-131805.txt"
    p.write_text(_step_time_samples(50), encoding="utf-8")
    fields = _label_file(p)
    assert fields[0] == "OK"
    assert fields[1] != "-", "50 samples must yield a step time, not a dash"
    assert float(fields[1]) == pytest.approx(4.22, rel=1e-3), "warmup must be excluded"
    assert float(fields[2]) == pytest.approx(1954.6, rel=1e-3)


def test_the_legacy_window_is_unchanged_for_long_runs(tmp_path):
    """A run with >=70 samples must still average exactly samples 70-89, so numbers recorded
    before short recipes existed stay directly comparable."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-9.txt"
    p.write_text(
        "".join(f"Step Time : {i}.00s GPU utilization: {i}00.0MODEL_TFLOP/s/GPU\n" for i in range(1, 201)),
        encoding="utf-8",
    )
    fields = _label_file(p)
    # mean(70..89) = 79.5 -> step 79.50s, tflops 7950.00
    assert float(fields[1]) == pytest.approx(79.50, rel=1e-4)
    assert float(fields[2]) == pytest.approx(7950.00, rel=1e-4)


def test_a_run_that_died_after_a_handful_of_steps_abstains(tmp_path):
    """Averaging 4 samples from a run that died early would dress up noise as a measurement."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-10.txt"
    p.write_text(_step_time_samples(4), encoding="utf-8")
    fields = _label_file(p)
    assert fields[1] == "-", "too few samples must abstain, not average"


def test_short_runs_work_in_the_train_step_timing_dialect_too(tmp_path):
    """The NeMo 2.0 dialect reads TFLOPs from a different field; both share one awk program."""
    p = tmp_path / "cmods-NEMO-1Rack-p05-r01-18N-11.txt"
    p.write_text(
        "".join("x | train_step_timing in s: 4.10 | tflops: 1900.0 | consumed_samples\n" for _ in range(50)),
        encoding="utf-8",
    )
    fields = _label_file(p)
    assert float(fields[1]) == pytest.approx(4.10, rel=1e-3)
    assert float(fields[2]) == pytest.approx(1900.0, rel=1e-3)


# --- structural guards: the copies must not come back -------------------------------------


def test_the_sample_window_is_defined_once():
    """Both dialects must share one awk program. They used to carry a copy each, differing
    only in a field index -- the same drift this include exists to prevent."""
    text = INC.read_text(encoding="utf-8")
    assert text.count("min_iter=70") == 1, "the window must be declared in exactly one place"
    assert text.count("_llm_stats_awk") >= 3, "both dialects should call the shared averager"


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
