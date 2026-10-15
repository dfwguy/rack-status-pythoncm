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

The module-local readers that take a setting's name as a parameter (`grafana_embeds._env_float`,
`paths._script`, `core.truthy_env` under whatever alias a caller imports it as, ...) are recognised
by resolving each call to its definition, so each is pinned here by a setting read only through it,
and the audit that finds an unclassified one is exercised on a synthetic module.
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


# ---------------------------------------------------------------------------------------------
# Module-local readers that take the setting's name as a parameter
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, kind, default, reader",
    [
        # grafana_embeds._env / _env_float / _env_truthy -- raw.
        ("RACK_GRAFANA_HEALTH_TIMEOUT", "raw", "2.5", "grafana_embeds._env_float"),
        ("RACK_GRAFANA_THEME", "raw", "dark", "grafana_embeds._env"),
        ("RACK_GRAFANA_EMBEDS_DISABLED", "raw", None, "grafana_embeds._env_truthy"),
        # lake_sink._env and metrics_query._env -- same name as grafana's, each its own module.
        ("RACK_LAKE_QUEUE_MAX", "raw", "256", "lake_sink._env"),
        ("RACK_METRICS_TIMEOUT", "raw", "12", "metrics_query._env"),
        # core.truthy_env, called through `from core import truthy_env as _truthy_env`.
        ("RACK_UNIVERSAL_PARSER_DISABLED", "raw", None, "core.truthy_env"),
        # ... and through app.py's module-level re-binding `_truthy_env = core.truthy_env`.
        ("RACK_WEB_COOKIE_SECURE", "raw", None, "core.truthy_env"),
        ("RACK_WEB_ACCESS_TTL_SECONDS", "raw", None, "identity.tokens._configured_ttl"),
        ("RACK_SQUEUE_JOB_LOOKUP_DISABLED", "raw", None, "native_status._env_truthy"),
        ("RACK_NVFTP_SYNC_FORCE", "raw", None, "nvftp_cache_sync._truthy"),
        ("NVIS_LOG_EXCLUDE_MARKERS", "raw", None, "nvis_id_templates._env_marker_list"),
        # Ladder: the Settings screen can change these.
        ("RACK_CMSH_DS_NODE_FIND_PATTERN", "ladder", None, "native_status._native_env_ds"),
        ("RACK_VALIDATION_ACTIVITIES_REFRESH_INTERVAL_SEC", "ladder", "30.0",
         "app._validation_activities_refresh_number"),
        # paths._script: the key is the SECOND argument, and `import paths; paths._script(...)`
        # from schedulers/pythoncm.py resolves too.
        ("RACK_SYSINFO_SCRIPT", "ladder", None, "paths._script"),
        ("RACK_PYTHONCM_EXEC_SCRIPT", "ladder", None, "paths._script"),
        # stream_exec._merged_stdout_stream_generator: key and default are keywords, imported
        # into api/*.py.
        ("RACK_SYSINFO_LINEBUFFER", "ladder", "1", "stream_exec._merged_stdout_stream_generator"),
    ],
)
def test_the_scan_finds_settings_read_through_module_local_readers(
    scanned, name, kind, default, reader
):
    assert name in scanned, f"{name} is read through {reader} but missing from the scan"
    assert scanned[name][kind] is True
    if kind == "raw":
        assert scanned[name]["ladder"] is False, f"{name} has no ladder reader"
    if default is not None:
        assert scanned[name]["default"] == default


def test_every_classified_reader_still_exists_and_none_is_missing():
    # The CI gate in one assertion: a new `def _env(name)` anywhere in backend/, or a rename of
    # one already classified, fails here before it can thin the document.
    assert gsr._unclassified_readers() == []


def test_a_same_named_function_in_another_module_is_not_a_reader():
    # transport/topology.py has a `_truthy` that parses a dict value; only nvftp_cache_sync's
    # reads the environment, and resolution keeps them apart.
    index = gsr._modules()
    topology = index["transport.topology"]
    calls = [
        n for n in ast.walk(topology.tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_truthy"
    ]
    assert calls, "transport/topology.py no longer calls a local _truthy; pick another pair"
    assert all(gsr._resolve(c, topology, index) == ("transport.topology", "_truthy") for c in calls)
    reads = {id(node) for node, *_ in gsr._reads(topology, index)}
    assert not any(id(c) in reads for c in calls)


def _synthetic(source: str, name: str = "zz_probe") -> dict:
    path = gsr.REPO_ROOT / "backend" / f"{name}.py"
    index = gsr._modules()
    index[name] = gsr._index_module(path, ast.parse(source))
    return index


def test_the_audit_finds_a_new_unclassified_reader():
    index = _synthetic(
        "import os\n"
        "def _knob(name, default=''):\n"
        "    return os.environ.get(name) or default\n"
        "def _knob_bool(name):\n"
        "    return _knob(name) == '1'\n"
    )
    problems = gsr._unclassified_readers(index)
    # Both the direct reader and the wrapper over it are found.
    assert any(p.startswith("zz_probe._knob reads") for p in problems)
    assert any(p.startswith("zz_probe._knob_bool reads") for p in problems)


def test_the_audit_catches_an_env_int_that_reads_through_the_ladder():
    index = _synthetic(
        "from config_env import _eget\n"
        "def _env_int(name, default):\n"
        "    return int(_eget(name) or default)\n"
    )
    assert any("zz_probe._env_int is recognised by name as a raw read" in p
               for p in gsr._unclassified_readers(index))


def test_the_audit_flags_a_classified_reader_that_was_renamed(monkeypatch):
    monkeypatch.setitem(gsr._PARAM_READERS, ("grafana_embeds", "_env_gone"), gsr._Reader("raw"))
    assert "grafana_embeds._env_gone is classified but no longer defined; update the table" in (
        gsr._unclassified_readers()
    )


def test_relative_imports_resolve_to_the_package_module():
    # agent/registrations.py does `from .tools import AgentTool`; the disabled_env keyword is the
    # setting name.
    index = gsr._modules()
    assert index["agent.registrations"].names["AgentTool"] == ("agent.tools", "AgentTool")
    scanned = gsr.scan()
    assert "agent/registrations.py" in " ".join(sorted(scanned["RACK_GPU_RESET_DISABLED"]["modules"]))
