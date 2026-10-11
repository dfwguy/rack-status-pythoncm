# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Launch-dialog NEMO step selector -> job env (``_launch_env_from_body``).

The multirack launch modal lets the operator pick the NEMO training length: 50 for the perf
benchmark or 2010 for the soak. It is a plain POST-body field that must land as env vars the
launchers actually read, so these tests pin the mapping.

The coupling worth guarding: ``max_steps`` has to set BOTH ``NVIS_NEMO_MAX_STEPS`` (what the job
runs, read by nemo-nemotron5h_56b-mbridge-fp8.sh) and ``NEMO_TARGET_STEPS`` (the minimum declared
total ``lib/llm_label.inc.sh`` requires before it labels a run finished, default 2010). Setting
only the first makes a 50-step run complete successfully and still show up as not-done.
"""
import os

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

from api import validation_runs


def test_max_steps_sets_both_run_and_label_thresholds():
    env = validation_runs._launch_env_from_body({"max_steps": 50}, scope="multirack")
    assert env["NVIS_NEMO_MAX_STEPS"] == "50"
    assert env["NEMO_TARGET_STEPS"] == "50"


def test_max_steps_soak_value():
    env = validation_runs._launch_env_from_body({"max_steps": 2010}, scope="multirack")
    assert env["NVIS_NEMO_MAX_STEPS"] == "2010"
    assert env["NEMO_TARGET_STEPS"] == "2010"


def test_max_steps_absent_leaves_script_default():
    env = validation_runs._launch_env_from_body({}, scope="multirack")
    assert "NVIS_NEMO_MAX_STEPS" not in env
    assert "NEMO_TARGET_STEPS" not in env


def test_max_steps_rejects_out_of_range_and_non_numeric():
    for bad in (0, -5, "fifty", None, 10**9):
        env = validation_runs._launch_env_from_body({"max_steps": bad}, scope="multirack")
        assert "NVIS_NEMO_MAX_STEPS" not in env, bad


def test_max_steps_rejects_bool_masquerading_as_int():
    """``True`` is an int in Python; it must not become ``NVIS_NEMO_MAX_STEPS=1``."""
    assert "NVIS_NEMO_MAX_STEPS" not in validation_runs._launch_env_from_body({"max_steps": True}, scope="multirack")


def test_step_selector_composes_with_nccl_debug_and_custom_env():
    env = validation_runs._launch_env_from_body(
        {"max_steps": 50, "nccl_debug": "INFO", "env": {"FOO": "1"}}, scope="multirack"
    )
    assert env["NVIS_NEMO_MAX_STEPS"] == "50"
    assert env["NCCL_DEBUG"] == "INFO"
    assert env["FOO"] == "1"


def test_custom_env_can_override_the_selector():
    """Explicit KEY=VALUE wins, matching the documented precedence for the env textarea."""
    env = validation_runs._launch_env_from_body(
        {"max_steps": 50, "env": {"NVIS_NEMO_MAX_STEPS": "110"}}, scope="multirack"
    )
    assert env["NVIS_NEMO_MAX_STEPS"] == "110"
