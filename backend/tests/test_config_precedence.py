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
"""Where does a configuration value come from? -- pinned before the subsystem moves.

``_eget`` has ~120 call sites and decides, for every knob in the product, which of five sources
wins. Nothing tested that order. It is being extracted from ``app.py`` into its own module, and the
failure mode of getting it subtly wrong is the worst kind this codebase has: a setting that works
on one cluster and not on another, discovered by an operator in a datacenter rather than by CI.

So these are characterization tests. They were written and made green against the pre-move
implementation, and are re-run unchanged afterwards; they describe behaviour, not the new shape.
Documented precedence, highest first:

    cluster "high" overlay  >  job_env_overrides.json  >  os.environ  >  platform "low" overlay  >  default
"""
from __future__ import annotations

import json

import pytest

import app as A  # noqa: F401 - imported so the readers are exercised exactly as the app has them
import config_env as C

KEY = "RACK_VALIDATION_CT_RANGE"  # a real Settings-managed key, so allow-list filtering applies


@pytest.fixture
def store(tmp_path, monkeypatch):
    """An isolated overrides file plus a controllable catalog overlay.

    The overrides path is hardcoded to REPO_ROOT/var/cache, so it is patched rather than set via
    the environment -- the env var of that name is an *output* of the loader.

    Patched on ``config_env``, not ``app``: that is where these live now, and ``app`` only holds
    imported aliases. Rebinding an alias would not change what the function's own module-global
    lookup resolves to, so patching ``app`` would silently do nothing. The assertions below are
    unchanged from when this suite ran against the in-``app.py`` implementation.
    """
    path = tmp_path / "job_env_overrides.json"
    monkeypatch.setattr(C, "_job_env_overrides_path", lambda: path)
    monkeypatch.setattr(C, "_job_env_flock_path", lambda: tmp_path / "job_env_overrides.flock")
    monkeypatch.setattr(C, "_JOB_ENV_OVERRIDES_CACHE", None)
    monkeypatch.setattr(C, "_JOB_ENV_OVERRIDES_MTIME", None)
    monkeypatch.delenv("RACK_STATUS_DEMO", raising=False)  # demo mode skips the flock

    overlays = {"high": {}, "low": {}}

    class _Ctx:
        @staticmethod
        def overlays_for_active():
            return dict(overlays["high"]), dict(overlays["low"])

    monkeypatch.setattr(C, "_CATALOG_AVAILABLE", True)
    monkeypatch.setattr(C, "catalog_context", _Ctx)
    # Materialization writes into os.environ and re-enters the loader; not what these tests are
    # about, and it would leak between them.
    monkeypatch.setattr(C, "_materialize_job_env_overrides_into_os_environ", lambda ov: None)

    def write_overrides(data: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        A._job_env_overrides_invalidate_cache()

    return type("Store", (), {
        "write_overrides": staticmethod(write_overrides),
        "high": overlays["high"],
        "low": overlays["low"],
    })


class TestEgetPrecedence:
    def test_default_when_nothing_is_set(self, store, monkeypatch):
        monkeypatch.delenv(KEY, raising=False)
        assert A._eget(KEY, "fallback") == "fallback"

    def test_none_default_is_returned_as_none(self, store, monkeypatch):
        monkeypatch.delenv(KEY, raising=False)
        assert A._eget(KEY) is None

    def test_low_overlay_beats_default(self, store, monkeypatch):
        monkeypatch.delenv(KEY, raising=False)
        store.low[KEY] = "from-platform"
        assert A._eget(KEY, "fallback") == "from-platform"

    def test_process_env_beats_low_overlay(self, store, monkeypatch):
        """Platform geometry is a default, not an override -- an explicit env wins."""
        store.low[KEY] = "from-platform"
        monkeypatch.setenv(KEY, "from-env")
        assert A._eget(KEY) == "from-env"

    def test_overrides_file_beats_process_env(self, store, monkeypatch):
        """Saving in Settings must take effect without restarting the process."""
        monkeypatch.setenv(KEY, "from-env")
        store.write_overrides({KEY: "from-settings"})
        assert A._eget(KEY) == "from-settings"

    def test_high_overlay_beats_everything(self, store, monkeypatch):
        """Cluster identity is not negotiable: it decides which cluster we are talking to."""
        store.low[KEY] = "from-platform"
        monkeypatch.setenv(KEY, "from-env")
        store.write_overrides({KEY: "from-settings"})
        store.high[KEY] = "from-cluster"
        assert A._eget(KEY) == "from-cluster"

    def test_the_full_ladder_in_one_assertion(self, store, monkeypatch):
        monkeypatch.setenv(KEY, "env")
        store.low[KEY] = "low"
        store.high[KEY] = "high"
        store.write_overrides({KEY: "ov"})
        assert A._eget(KEY, "default") == "high"
        del store.high[KEY]
        assert A._eget(KEY, "default") == "ov"
        store.write_overrides({})
        assert A._eget(KEY, "default") == "env"
        monkeypatch.delenv(KEY)
        assert A._eget(KEY, "default") == "low"
        store.low.clear()
        assert A._eget(KEY, "default") == "default"


class TestBlankValuesDoNotWin:
    """A blank is "not configured", or clearing a Settings field would blank the whole knob."""

    def test_blank_high_overlay_falls_through(self, store, monkeypatch):
        store.high[KEY] = "   "
        monkeypatch.setenv(KEY, "from-env")
        assert A._eget(KEY) == "from-env"

    def test_blank_override_falls_through_to_env(self, store, monkeypatch):
        monkeypatch.setenv(KEY, "from-env")
        store.write_overrides({KEY: "  "})
        assert A._eget(KEY) == "from-env"

    def test_blank_low_overlay_falls_through_to_default(self, store, monkeypatch):
        monkeypatch.delenv(KEY, raising=False)
        store.low[KEY] = ""
        assert A._eget(KEY, "fallback") == "fallback"

    def test_but_an_empty_process_env_var_does_win(self, store, monkeypatch):
        """Asymmetric on purpose: `FOO=` in the environment is an explicit empty value."""
        monkeypatch.setenv(KEY, "")
        store.low[KEY] = "from-platform"
        assert A._eget(KEY, "fallback") == ""

    def test_values_are_stripped(self, store, monkeypatch):
        store.high[KEY] = "  spaced  "
        assert A._eget(KEY) == "spaced"


class TestIntVariants:
    def test_eget_int_reads_through_the_overlays(self, store, monkeypatch):
        monkeypatch.delenv(KEY, raising=False)
        store.low[KEY] = "18"
        assert A._eget_int(KEY, 4) == 18

    def test_eget_int_falls_back_on_a_non_number(self, store, monkeypatch):
        store.high[KEY] = "not-a-number"
        assert A._eget_int(KEY, 7) == 7

    def test_env_int_ignores_the_overlays(self, store, monkeypatch):
        """Deliberate: _env_int is the plain reader. Overlay-aware callers use _eget_int."""
        monkeypatch.delenv(KEY, raising=False)
        store.high[KEY] = "99"
        assert A._env_int(KEY, 5) == 5

    def test_job_env_int_prefers_settings_then_env(self, store, monkeypatch):
        monkeypatch.setenv(KEY, "3")
        assert A._job_env_int(KEY, 1) == 3
        store.write_overrides({KEY: "8"})
        assert A._job_env_int(KEY, 1) == 8

    def test_job_env_int_with_a_corrupt_override_uses_the_default_not_the_env(self, store, monkeypatch):
        """Pinning current behaviour: a bad override short-circuits rather than falling through."""
        monkeypatch.setenv(KEY, "3")
        store.write_overrides({KEY: "garbage"})
        assert A._job_env_int(KEY, 1) == 1


class TestSubprocessEnv:
    def test_layer_order_low_env_overrides_high_updates(self, store, monkeypatch):
        monkeypatch.setenv(KEY, "env")
        store.low[KEY] = "low"
        out = A._subprocess_env()
        assert out[KEY] == "env"
        store.write_overrides({KEY: "ov"})
        assert A._subprocess_env()[KEY] == "ov"
        store.high[KEY] = "high"
        assert A._subprocess_env()[KEY] == "high"
        assert A._subprocess_env({KEY: "call"})[KEY] == "call"

    def test_process_env_is_carried_through(self, store, monkeypatch):
        monkeypatch.setenv("SOME_UNMANAGED_VAR", "kept")
        assert A._subprocess_env()["SOME_UNMANAGED_VAR"] == "kept"

    def test_validation_test_defaults_when_unset(self, store, monkeypatch):
        monkeypatch.delenv("VALIDATION_TEST", raising=False)
        assert A._subprocess_env()["VALIDATION_TEST"] == "nccl_all_reduce,llm"

    def test_validation_test_default_is_overridable_by_the_caller(self, store, monkeypatch):
        monkeypatch.delenv("VALIDATION_TEST", raising=False)
        out = A._subprocess_env(validation_test_if_unset="nccl_all_reduce")
        assert out["VALIDATION_TEST"] == "nccl_all_reduce"

    def test_an_explicit_validation_test_is_not_replaced(self, store, monkeypatch):
        monkeypatch.setenv("VALIDATION_TEST", "chosen")
        assert A._subprocess_env()["VALIDATION_TEST"] == "chosen"

    def test_a_blank_validation_test_is_treated_as_unset(self, store, monkeypatch):
        monkeypatch.setenv("VALIDATION_TEST", "   ")
        assert A._subprocess_env()["VALIDATION_TEST"] == "nccl_all_reduce,llm"


class TestOverridesStore:
    def test_load_returns_what_was_written(self, store):
        store.write_overrides({KEY: "value"})
        assert A._job_env_overrides_load()[KEY] == "value"

    def test_load_is_cached_by_mtime_and_invalidation_re_reads(self, store):
        store.write_overrides({KEY: "first"})
        assert A._job_env_overrides_load()[KEY] == "first"
        store.write_overrides({KEY: "second"})  # write_overrides invalidates
        assert A._job_env_overrides_load()[KEY] == "second"

    def test_a_corrupt_overrides_file_does_not_raise(self, store, tmp_path):
        (tmp_path / "job_env_overrides.json").write_text("{not json", encoding="utf-8")
        A._job_env_overrides_invalidate_cache()
        assert isinstance(A._job_env_overrides_load(), dict)

    def test_the_caller_cannot_mutate_the_cache(self, store):
        """A returned dict is handed to subprocess env builders; aliasing the cache would be a bug."""
        store.write_overrides({KEY: "value"})
        first = A._job_env_overrides_load()
        first[KEY] = "mutated"
        assert A._job_env_overrides_load()[KEY] == "value"


class TestCatalogUnavailable:
    def test_eget_still_works_without_a_catalog(self, store, monkeypatch):
        """The catalog is optional; without it the ladder is just overrides > env > default."""
        monkeypatch.setattr(C, "_CATALOG_AVAILABLE", False)
        monkeypatch.setenv(KEY, "from-env")
        assert A._eget(KEY) == "from-env"
        store.write_overrides({KEY: "from-settings"})
        assert A._eget(KEY) == "from-settings"

    def test_an_exploding_overlay_is_survived(self, store, monkeypatch):
        """A broken catalog must not take out every configuration read in the product."""
        class _Boom:
            @staticmethod
            def overlays_for_active():
                raise RuntimeError("catalog on fire")

        monkeypatch.setattr(C, "catalog_context", _Boom)
        monkeypatch.setenv(KEY, "from-env")
        assert A._eget(KEY) == "from-env"
        assert A._subprocess_env()[KEY] == "from-env"
