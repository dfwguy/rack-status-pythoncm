"""Launch-dialog NEMO knobs -> job env (``_launch_env_from_body``).

The multirack launch modal exposes two NEMO controls: a "run NCCL sweep first" checkbox and a
training-steps selector. Both are plain POST-body fields that must land as env vars the
launchers actually read, so these tests pin the mapping.

The coupling worth guarding: ``max_steps`` has to set BOTH ``NVIS_NEMO_MAX_STEPS`` (what the job
runs) and ``NEMO_TARGET_STEPS`` (the minimum declared total ``lib/llm_label.inc.sh`` requires
before it labels a run finished, default 2010). Setting only the first makes a 50-step run
complete successfully and still show up as not-done on the dashboard.
"""
import os

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_WEB_AUTH_DISABLED"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import app as app_module


def test_with_nccl_true_sets_stage_flag_on():
    env = app_module._launch_env_from_body({"with_nccl": True})
    assert env["NVIS_LLMB_NEMO_WITH_NCCL"] == "1"


def test_with_nccl_false_skips_stage_one():
    env = app_module._launch_env_from_body({"with_nccl": False})
    assert env["NVIS_LLMB_NEMO_WITH_NCCL"] == "0"


def test_with_nccl_absent_leaves_launcher_default():
    """No checkbox in the body -> no override, so run_llmb_nemo.sh keeps its own default."""
    assert "NVIS_LLMB_NEMO_WITH_NCCL" not in app_module._launch_env_from_body({})


def test_max_steps_sets_both_run_and_label_thresholds():
    env = app_module._launch_env_from_body({"max_steps": 50})
    assert env["NVIS_NEMO_MAX_STEPS"] == "50"
    assert env["NEMO_TARGET_STEPS"] == "50"


def test_max_steps_soak_value():
    env = app_module._launch_env_from_body({"max_steps": 2010})
    assert env["NVIS_NEMO_MAX_STEPS"] == "2010"
    assert env["NEMO_TARGET_STEPS"] == "2010"


def test_max_steps_absent_leaves_script_default():
    env = app_module._launch_env_from_body({})
    assert "NVIS_NEMO_MAX_STEPS" not in env
    assert "NEMO_TARGET_STEPS" not in env


def test_max_steps_rejects_out_of_range_and_non_numeric():
    for bad in (0, -5, "fifty", None, 10**9):
        env = app_module._launch_env_from_body({"max_steps": bad})
        assert "NVIS_NEMO_MAX_STEPS" not in env, bad


def test_max_steps_rejects_bool_masquerading_as_int():
    """``True`` is an int in Python; it must not become ``NVIS_NEMO_MAX_STEPS=1``."""
    assert "NVIS_NEMO_MAX_STEPS" not in app_module._launch_env_from_body({"max_steps": True})


def test_nemo_knobs_compose_with_nccl_debug_and_custom_env():
    env = app_module._launch_env_from_body(
        {"with_nccl": False, "max_steps": 50, "nccl_debug": "INFO", "env": {"FOO": "1"}}
    )
    assert env["NVIS_LLMB_NEMO_WITH_NCCL"] == "0"
    assert env["NVIS_NEMO_MAX_STEPS"] == "50"
    assert env["NCCL_DEBUG"] == "INFO"
    assert env["FOO"] == "1"


def test_custom_env_can_override_the_selector():
    """Explicit KEY=VALUE wins, matching the documented precedence for the env textarea."""
    env = app_module._launch_env_from_body(
        {"max_steps": 50, "env": {"NVIS_NEMO_MAX_STEPS": "110"}}
    )
    assert env["NVIS_NEMO_MAX_STEPS"] == "110"
