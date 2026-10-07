# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which calls the settings reference counts as reading a setting, and how.

Author: Rafael M. Koike

`bin/gen-settings-reference.py` claims completeness -- every setting the code reads -- and the
`sources-of-truth` CI job holds the committed document to it. A call shape the scan does not
recognise is therefore a silent hole, not a crash: until `config_env.env_int` and its wrappers were
taught to the scan, 99 of the 100 settings read through them were missing, and nothing failed. So
the classification of each reader is pinned here, in both directions -- recognised, and (for the
WSGI ``environ`` that is not configuration) deliberately not -- and the scan is checked end to end
against settings the backend actually reads through each helper.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

GENERATOR = Path(__file__).resolve().parents[2] / "bin" / "gen-settings-reference.py"
_spec = importlib.util.spec_from_file_location("gen_settings_reference", GENERATOR)
gsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gsr)


def _call(source: str) -> ast.Call:
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.Call)
    return node


@pytest.mark.parametrize(
    "source, kind",
    [
        # The process environment, read directly.
        ("os.environ.get('RACK_X')", "raw"),
        ("os.getenv('RACK_X')", "raw"),
        ("getenv('RACK_X')", "raw"),
        ("env_int('RACK_X', 30)", "raw"),
        ("config_env.env_int('RACK_X', 30, min_valid=1)", "raw"),
        ("_env_int('RACK_X', 30)", "raw"),
        ("config_env._env_int('RACK_X', 30)", "raw"),
        # The resolution ladder -- what the Settings screen can influence.
        ("_eget('RACK_X')", "ladder"),
        ("config_env._eget_int('RACK_X', 30)", "ladder"),
        ("_job_env_int('RACK_X', 30)", "ladder"),
        ("config_env._job_env_int('RACK_X', 30)", "ladder"),
        # Not configuration: the WSGI request environ, and unrelated lookups.
        ("environ.get('HTTP_HOST')", None),
        ("settings.get('RACK_X')", None),
        ("int('30')", None),
    ],
)
def test_each_reader_is_classified(source, kind):
    assert gsr._is_env_read(_call(source)) == kind


def test_a_literal_default_is_recorded_for_env_int():
    assert gsr._literal_default(_call("env_int('RACK_X', 900, min_valid=1)")) == "900"
    assert gsr._literal_default(_call("_job_env_int('RACK_X', 600)")) == "600"
    assert gsr._literal_default(_call("env_int('RACK_X', OTHER)")) == ""


@pytest.fixture(scope="module")
def scanned():
    return gsr.scan()


@pytest.mark.parametrize(
    "name, kind, default",
    [
        # Read only through config_env.env_int (raw) -- each was missing before the scan knew it.
        ("RACK_WEB_SESSION_HOURS", "raw", "8"),
        ("RACK_LOCK_MAX_AGE_SEC", "raw", "21600"),
        # Read through a module-local _env_int wrapper that delegates to env_int.
        ("RACK_NATIVE_SINFO_TIMEOUT_SEC", "raw", None),
        # Read through _job_env_int: the Settings job environment wins, so it is on the ladder.
        ("RACK_VALIDATION_TIMEOUT", "ladder", "600"),
        ("RACK_HOST_READ_TIMEOUT", "ladder", "90"),
    ],
)
def test_the_scan_finds_settings_read_through_each_helper(scanned, name, kind, default):
    assert name in scanned, f"{name} is read by the backend but missing from the scan"
    assert scanned[name][kind] is True
    if default is not None:
        assert scanned[name]["default"] == default


def test_test_scaffolding_is_not_a_setting(scanned):
    # tests/ read throwaway names through env_int; the scan skips tests/ entirely.
    assert "ZZ_ENV_INT_PROBE" not in scanned
