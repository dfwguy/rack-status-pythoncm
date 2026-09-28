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
"""The launch configuration loader — DC-3 (FR-1..FR-15, FR-23..FR-28).

`TestTC19EmptyStringWins` is the one to keep. `os.environ.setdefault` treats `""` as present, which
is correct; the obvious alternative — `if not os.environ.get(k)` — treats it as absent and would
overwrite a deliberately-empty variable. Both implementations pass every other test in this file,
so that case is the only thing standing between the two.

`TestTC22TheImportOrderInvariant` guards the reason this module exists at all. `config_env.py:737`
snapshots the environment at import and `:773` pops Settings-managed keys absent from that
snapshot, so a value materialized too late is deleted mid-startup with no error. The AST half is
transitive on purpose: `paths.py:30` imports `config_env`, so reaching for the obvious
`paths.REPO_ROOT` helper would violate the rule without the literal name ever appearing here.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import ClassVar

import config_launch as L
import pytest

BACKEND = Path(__file__).resolve().parent.parent


def write(tmp_path: Path, body: str, name: str = "rackstatus.toml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in ("RACK_SSH_HOST", "GUNICORN_THREADS", "NVIS_LOG_SINKS", "SOME_KEY", "X"):
        monkeypatch.delenv(name, raising=False)


class TestTC1PathResolution:
    def test_the_default_is_the_config_directory(self, monkeypatch):
        """One directory holds operator configuration: the file, and `tls/` beside it."""
        monkeypatch.delenv(L.CONFIG_PATH_ENV, raising=False)
        assert L.resolve_path() == L.REPO_ROOT / "config" / "rackstatus.toml"
        assert L.config_dir() == L.REPO_ROOT / "config"

    def test_an_explicit_path_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(L.CONFIG_PATH_ENV, str(tmp_path / "other.toml"))
        assert L.resolve_path() == tmp_path / "other.toml"

    def test_the_repo_root_is_computed_not_imported(self):
        """DD-1: importing `paths.REPO_ROOT` would drag `config_env` onto this module's graph."""
        assert L.REPO_ROOT.name == "nvis-rack-status" or (L.REPO_ROOT / "backend").is_dir()

    def test_an_example_file_is_never_read_as_config(self, tmp_path):
        """FR-1d. Loading one would hand a deployment the placeholder values it was told to
        replace."""
        path = write(tmp_path, 'X = "placeholder"\n', name="rackstatus.toml.example")
        with pytest.raises(L.ConfigError, match="example"):
            L.load(path)


class TestTC2AnAbsentFileIsTheOrdinaryCase:
    """FR-2. The real file is gitignored, so every fresh clone and every existing deployment has
    none — this is the normal path, not an edge case."""

    def test_it_completes_and_records_why(self, tmp_path):
        result = L.load(tmp_path / "nope.toml")
        assert result.materialized == ()
        assert any("no " in r.message for r in result.records)

    def test_it_materializes_nothing(self, tmp_path):
        before = dict(os.environ)
        L.load(tmp_path / "nope.toml")
        assert dict(os.environ) == before


class TestTC19EmptyStringWins:
    """FR-10 / AC-10 — the case that discriminates `setdefault` from a falsy check."""

    def test_a_deliberately_empty_variable_is_not_overwritten(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_SINKS", "")
        L.load(write(tmp_path, 'NVIS_LOG_SINKS = ["file"]\n'))

        assert os.environ["NVIS_LOG_SINKS"] == "", (
            "an empty environment value was overwritten by the file; an `if not os.environ.get(k)` "
            "guard does this and `setdefault` does not"
        )

    def test_an_unset_variable_is_filled(self, tmp_path):
        L.load(write(tmp_path, 'NVIS_LOG_SINKS = ["file"]\n'))
        assert os.environ["NVIS_LOG_SINKS"] == "file"


class TestTheFileIsTheBaselineAndTheLaunchWins:
    def test_an_existing_value_is_declined_and_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RACK_SSH_HOST", "from-launch")
        result = L.load(write(tmp_path, 'RACK_SSH_HOST = "from-file"\n'))

        assert os.environ["RACK_SSH_HOST"] == "from-launch"
        assert result.declined == ("RACK_SSH_HOST",)
        assert result.origins["RACK_SSH_HOST"] == "env"

    def test_provenance_distinguishes_the_two_origins_at_one_rung(self, tmp_path, monkeypatch):
        """FR-25. `env` and `file` are two origins at the process-env rung, not a sixth rung —
        the ledger tells them apart so `_eget` does not have to learn one."""
        monkeypatch.setenv("RACK_SSH_HOST", "x")
        result = L.load(write(tmp_path, 'RACK_SSH_HOST = "y"\nGUNICORN_THREADS = 4\n'))

        assert result.origins == {"RACK_SSH_HOST": "env", "GUNICORN_THREADS": "file"}


class TestTypeRendering:
    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("X = true", "1"),
            ("X = false", "0"),
            ("X = 8", "8"),
            ("X = -3", "-3"),
            ('X = "plain"', "plain"),
            ('X = ["a", "b"]', "a,b"),
            ("X = [1, 2]", "1,2"),
            ("X = []", ""),
        ],
    )
    def test_each_type_renders_as_the_environment_requires(self, tmp_path, body, expected):
        L.load(write(tmp_path, body + "\n"))
        assert os.environ["X"] == expected

    def test_booleans_render_as_one_and_zero_not_true_and_false(self, tmp_path):
        """`core.truthy_env` and every shell script in `bin/scripts` accept `1`/`0`; `"True"` would
        be truthy to Python and meaningless to `[ "$X" = 1 ]`."""
        L.load(write(tmp_path, "X = true\n"))
        assert os.environ["X"] == "1"

    def test_arrays_join_without_spaces(self, tmp_path):
        """Matching `logging_config._csv`, which strips but would otherwise carry a space into a
        value comparison."""
        L.load(write(tmp_path, 'X = ["a", "b", "c"]\n'))
        assert " " not in os.environ["X"]

    def test_a_float_is_refused_with_the_fix_in_the_message(self, tmp_path):
        """Both float-shaped settings here are version strings: `nccl_allreduce.sbatch:114`
        compares one in awk by splitting on `.`, and `tls_server.py:60-66` silently keeps the TLS
        1.2 floor on an unrecognised one."""
        with pytest.raises(L.ConfigError, match="float"):
            L.load(write(tmp_path, "SOME_VERSION = 13.3\n"))


class TestTablesAreGroupingOnly:
    def test_a_leaf_key_is_the_variable_name(self, tmp_path):
        """FR-7a. Concatenating would invent a second name for every knob and put a translation
        table between the file and the 238 readers that name their variables literally."""
        L.load(write(tmp_path, '[auth]\nRACK_SSH_HOST = "h"\n'))

        assert os.environ.get("RACK_SSH_HOST") == "h"
        assert "AUTH_RACK_SSH_HOST" not in os.environ

    def test_the_same_leaf_in_two_tables_is_refused(self, tmp_path):
        """FR-7b. Valid TOML, but it means one setting declared twice with file order deciding."""
        with pytest.raises(L.ConfigError, match="more than one table"):
            L.load(write(tmp_path, "[a]\nX = 1\n\n[b]\nX = 2\n"))


class TestABrokenFileIsFatalNotPartial:
    """FR-5, FR-6. A partial load is worse than a refusal: the operator sees some of their
    settings applied and has no way to know which."""

    def test_malformed_toml_refuses(self, tmp_path):
        with pytest.raises(L.ConfigError, match="not valid TOML"):
            L.load(write(tmp_path, "this is = = not toml\n"))

    def test_invalid_utf8_is_its_own_diagnostic(self, tmp_path):
        """A third case, distinct from "cannot open" and "is not TOML". TOML v1.0.0 requires UTF-8,
        and without this branch the operator gets an unhandled traceback out of a module that runs
        before any log handler exists."""
        path = tmp_path / "rackstatus.toml"
        path.write_bytes(b'X = "\xff\xfe not utf 8"\n')

        with pytest.raises(L.ConfigError, match="UTF-8"):
            L.load(path)

    def test_nothing_is_materialized_when_a_later_key_fails(self, tmp_path):
        """The refusal must not leave half the file applied."""
        with pytest.raises(L.ConfigError):
            L.load(write(tmp_path, "[a]\nX = 1\n\n[b]\nX = 2\n"))
        assert "X" not in os.environ


class TestUndeclaredKeys:
    def test_an_undeclared_key_warns_but_still_loads(self, tmp_path):
        """FR-8/FR-8a. Refusing would make the file a rollback hazard for an operator mid-upgrade;
        the warning names the key so a typo is findable."""
        result = L.load(write(tmp_path, 'SOME_KEY = "v"\n'))

        assert os.environ["SOME_KEY"] == "v"
        assert any("SOME_KEY" in r.message and r.level == "warning" for r in result.records)


class TestNFR5Idempotency:
    def test_loading_twice_changes_nothing(self, tmp_path):
        path = write(tmp_path, 'RACK_SSH_HOST = "h"\nGUNICORN_THREADS = 4\n')
        L.load(path)
        snapshot = dict(os.environ)

        second = L.load(path)

        assert dict(os.environ) == snapshot
        assert second.materialized == (), "the second load re-applied values"
        assert set(second.declined) == {"RACK_SSH_HOST", "GUNICORN_THREADS"}


class TestTC22TheImportOrderInvariant:
    """FR-3a / AC-3a. The AST half; the behavioural half needs a full app boot and lives in
    `test_launch_config_wiring.py`."""

    #: The loader's own closure. FR-3a cannot mean "before *every* first-party import" literally —
    #: calling `config_launch.load()` requires importing it — so the exclusion set is named here
    #: rather than left for a reader to infer, which is what keeps the check from being widened
    #: later to hide a violation.
    CLOSURE: ClassVar[set[str]] = {
        "config_launch", "config_registry", "toml_compat", "logging_config", "core",
    }

    def _module_level_imports(self, path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names.add((node.module or "").split(".")[0])
        return names

    def test_the_loader_imports_nothing_outside_its_closure(self):
        stdlib_ok = {"os", "sys", "dataclasses", "pathlib", "__future__", "logging", "re", "typing"}
        imported = self._module_level_imports(BACKEND / "config_launch.py")
        assert not (imported - stdlib_ok - self.CLOSURE), (
            f"config_launch imports {sorted(imported - stdlib_ok - self.CLOSURE)} at module level"
        )

    def test_no_module_in_the_closure_reaches_config_env(self):
        """Transitive, and this is the half that matters. `paths.py:30` is
        `from config_env import _eget`, so reaching for the obvious `paths.REPO_ROOT` helper would
        violate DD-1 without the name `config_env` ever appearing in this module."""
        for name in sorted(self.CLOSURE):
            source = BACKEND / f"{name}.py"
            if not source.exists():
                continue
            reached = self._module_level_imports(source)
            assert "config_env" not in reached, f"{name} imports config_env at module level"
            assert "paths" not in reached, (
                f"{name} imports paths, which imports config_env (paths.py:30)"
            )

    def test_the_loader_runs_above_every_governed_first_party_import(self):
        """The ordering, through the AST rather than by `str.index`.

        The index version of this passed a mutation that inserted an extra import above the loader,
        because comparing text offsets says nothing about which *statements* execute first. Line
        numbers of real nodes do.
        """
        tree = ast.parse((BACKEND / "app.py").read_text())

        load_line = next(
            (
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "load"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "_config_launch"
            ),
            None,
        )
        assert load_line is not None, "app.py never calls the loader"

        # Every module-level first-party import, in execution order. `logging_config` is the one
        # that must come after: the file has to be able to set NVIS_LOG_LEVEL.
        governed: list[tuple[int, str]] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                governed += [(node.lineno, a.name.split(".")[0]) for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                governed.append((node.lineno, (node.module or "").split(".")[0]))

        first_party = [
            (line, name)
            for line, name in governed
            if (BACKEND / f"{name}.py").exists() or (BACKEND / name).is_dir()
        ]
        too_early = [
            (line, name)
            for line, name in first_party
            if line < load_line and name not in self.CLOSURE
        ]
        assert not too_early, (
            "these first-party modules are imported before the loader runs, so a value the file "
            f"supplies can be snapshotted or deleted before it lands: {too_early}"
        )
