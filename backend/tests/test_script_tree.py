# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which copy of a script runs, and the composer that creates a project's copy.

Author: Rafael M. Koike & Cursor Agent ("Ada")

Two failures are worth more than the happy path here.

**An existing deployment must not change behaviour.** Every site today runs from one flat
``bin/scripts``, and a resolver that started pointing at a directory nobody created would break
all of them at once. So the layered answer applies only when the tree exists, and the legacy
answer is asserted, not assumed.

**A project's own edit must survive the second bootstrap.** The per-project copy exists so one
customer's fix cannot break another's run; silently restoring the default over it would undo the
only thing the directory is for. The composer's first version got this wrong in the other
direction as well - ``--ignore-existing`` protected the edit *and* skipped the platform override
it should have installed - so both directions are pinned.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import script_tree

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "bin" / "bootstrap-project-scripts.sh"


@pytest.fixture(autouse=True)
def _clear_cache():
    script_tree.clear_cache()
    yield
    script_tree.clear_cache()


def _env(monkeypatch, **kw):
    """Point script_tree at a local tree and make the "execution host" this machine.

    `host_exec.invalidate_connection()` as well as `script_tree.clear_cache()`, because
    `legacy_dir()` asks `host_exec.role_root("scripts")` and that answer is cached behind its
    own init. Leaving it out made this file pass alone and fail after any other test that set a
    base path -- and chasing that is what turned up the product bug: a cluster switch did not
    invalidate it either, so scripts resolved against the previous cluster's paths.
    """
    import host_exec

    monkeypatch.setenv("RACK_EXEC_MODE", "local")
    for k, v in kw.items():
        monkeypatch.setenv(k, str(v))
    host_exec.invalidate_connection()
    script_tree.clear_cache()


# --- resolution ---------------------------------------------------------------------------------


def test_without_a_tree_every_answer_is_the_legacy_directory(monkeypatch, tmp_path):
    """The migration is "create the tree", not "upgrade and hope"."""
    base = tmp_path / "deploy"
    (base / "bin" / "scripts").mkdir(parents=True)
    (base / "bin" / "scripts" / "sysinfo.sh").write_text("flat\n")
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    assert script_tree.layers() == [f"{base}/bin/scripts"]
    assert script_tree.scripts_dir() == f"{base}/bin/scripts"
    assert script_tree.resolve("sysinfo.sh") == f"{base}/bin/scripts/sysinfo.sh"


def test_the_project_layer_wins_over_platform_and_defaults(monkeypatch, tmp_path):
    base = tmp_path / "deploy"
    tree = base / "scripts"
    for layer, body in (
        ("multi-platform", "default"),
        ("gb300-nvl72", "platform"),
        ("project-nscale-tx", "project"),
    ):
        (tree / layer).mkdir(parents=True)
        (tree / layer / "sysinfo.sh").write_text(body + "\n")
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    # Ids passed explicitly. `NVIS_PROJECT_ID` in the process environment would NOT take
    # effect, and that is the production contract, not a test workaround: `_eget` ranks the
    # catalog's high overlay above the environment, so the active project is whatever the
    # catalog says it is. `test_the_active_ids_come_from_the_config_ladder` pins that.
    resolved = script_tree.resolve("sysinfo.sh", "nscale-tx", "gb300-nvl72")
    assert resolved == f"{tree}/project-nscale-tx/sysinfo.sh"
    assert Path(resolved).read_text().strip() == "project"
    # Most specific first, so a reader can see the precedence rather than infer it. `custom/`
    # sits above the shipped layers: an operator can shadow a shipped script without editing
    # it, which is the same relationship a project directory has, one scope wider.
    assert script_tree.layers("nscale-tx", "gb300-nvl72") == [
        f"{tree}/project-nscale-tx",
        f"{tree}/custom",
        f"{tree}/gb300-nvl72",
        f"{tree}/multi-platform",
    ]


def test_a_script_only_the_defaults_have_still_resolves(monkeypatch, tmp_path):
    """A project directory need not be complete for the search path to find a script."""
    base = tmp_path / "deploy"
    tree = base / "scripts"
    (tree / "multi-platform").mkdir(parents=True)
    (tree / "multi-platform" / "parser.sh").write_text("default\n")
    (tree / "project-p1").mkdir(parents=True)
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    assert script_tree.resolve("parser.sh", "p1") == f"{tree}/multi-platform/parser.sh"


def test_a_missing_script_names_where_it_was_expected(monkeypatch, tmp_path):
    """An empty string would make the error message useless, so the base layer is returned."""
    base = tmp_path / "deploy"
    (base / "scripts" / "multi-platform").mkdir(parents=True)
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    assert script_tree.resolve("nope.sh", "p1").endswith("/multi-platform/nope.sh")


def test_switching_project_switches_the_script(monkeypatch, tmp_path):
    """The cache is keyed per path, and `clear_cache` is what the catalog hook calls.

    Without the hook a switch would keep running the previous project's copy for up to the cache
    TTL - which is the kind of bug that looks like the wrong script being deployed.
    """
    base = tmp_path / "deploy"
    tree = base / "scripts"
    (tree / "multi-platform").mkdir(parents=True)
    (tree / "multi-platform" / "s.sh").write_text("default\n")
    for pid in ("a", "b"):
        (tree / f"project-{pid}").mkdir(parents=True)
        (tree / f"project-{pid}" / "s.sh").write_text(pid + "\n")

    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)
    monkeypatch.setattr(script_tree, "active_project_id", lambda: "a")
    assert script_tree.resolve("s.sh").endswith("project-a/s.sh")

    monkeypatch.setattr(script_tree, "active_project_id", lambda: "b")
    script_tree.clear_cache()
    assert script_tree.resolve("s.sh").endswith("project-b/s.sh")


def test_the_active_ids_come_from_the_config_ladder(monkeypatch):
    """Not `os.environ` directly - the catalog's high overlay has to be able to win.

    `catalog.context.high_overlay` writes `NVIS_PROJECT_ID` / `NVIS_PLATFORM_ID`, and the whole
    point is that switching the active cluster switches which project's scripts run. Reading the
    process environment instead would pin the answer to whatever the container started with.
    """
    seen: list[str] = []

    def fake_eget(key, default=""):
        seen.append(key)
        return {"NVIS_PROJECT_ID": "from-catalog", "NVIS_PLATFORM_ID": "gb200-nvl72"}.get(key, default)

    monkeypatch.setattr(script_tree, "_eget", fake_eget)
    assert script_tree.active_project_id() == "from-catalog"
    assert script_tree.active_platform_id() == "gb200-nvl72"
    assert "NVIS_PROJECT_ID" in seen and "NVIS_PLATFORM_ID" in seen


def test_var_dir_is_not_derived_from_the_script_directory(monkeypatch, tmp_path):
    """The relative `../../var` escape is correct for one layout only - see script_tree."""
    base = tmp_path / "deploy"
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)
    assert script_tree.var_dir() == f"{base}/var"

    monkeypatch.setenv("NVIS_VAR_DIR", "/elsewhere/var")
    assert script_tree.var_dir() == "/elsewhere/var"


def test_script_env_carries_what_a_spooled_batch_script_cannot_derive(monkeypatch, tmp_path):
    """Slurm copies the batch script away from the tree, so both roots must be exported."""
    base = tmp_path / "deploy"
    (base / "scripts" / "multi-platform").mkdir(parents=True)
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    env = script_tree.script_env("p1")
    assert env["NVIS_SCRIPTS_DIR"] == env["NVIS_BIN_SCRIPTS"]
    assert env["NVIS_VAR_DIR"] == f"{base}/var"


# --- the composer -------------------------------------------------------------------------------


def _bootstrap(tmp_path, *args):
    """Run the composer and return its JSON summary."""
    proc = subprocess.run(
        ["bash", str(BOOTSTRAP), "--json", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "RACK_REMOTE_BASE_PATH": ""},
    check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_bootstrap_installs_the_platform_override_over_the_default(tmp_path):
    """The bug the first version had: `--ignore-existing` skipped the override it had to install.

    Defaults and platform layer are composed in a staging directory precisely so "platform wins"
    and "do not clobber the operator" stop competing.
    """
    flat = tmp_path / "flat"
    (flat / "lib").mkdir(parents=True)
    (flat / "sysinfo.sh").write_text("default\n")
    (flat / "lib" / "x.inc.sh").write_text("lib\n")
    tree = tmp_path / "tree"
    (tree / "gb300-nvl72").mkdir(parents=True)
    (tree / "gb300-nvl72" / "sysinfo.sh").write_text("platform\n")

    out = _bootstrap(
        tmp_path,
        "--project", "p1",
        "--platform", "gb300-nvl72",
        "--tree", str(tree),
        "--source", str(flat),
    )
    dest = Path(out["dest"])
    assert dest == tree / "project-p1"
    assert (dest / "sysinfo.sh").read_text().strip() == "platform"
    # The whole default set arrives, not only the overridden file - `lib/` is what a script
    # sources, so a partial copy fails at run time rather than at bootstrap.
    assert (dest / "lib" / "x.inc.sh").is_file()


def test_bootstrap_is_idempotent(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "a.sh").write_text("a\n")
    tree = tmp_path / "tree"

    first = _bootstrap(tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat))
    second = _bootstrap(tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat))
    assert first["copied"] >= 1
    assert second["copied"] == 0
    assert second["differs_from_defaults"] == 0


def test_bootstrap_protects_an_operator_edit_unless_forced(tmp_path):
    """The reason the per-project directory exists at all."""
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "a.sh").write_text("default\n")
    tree = tmp_path / "tree"
    _bootstrap(tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat))

    edited = tree / "project-p1" / "a.sh"
    edited.write_text("operator fix\n")

    again = _bootstrap(tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat))
    assert edited.read_text().strip() == "operator fix"
    # ...and the drift is reported, so the caller can say what a force would discard.
    assert again["differs_from_defaults"] == 1

    forced = _bootstrap(
        tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat), "--force"
    )
    assert edited.read_text().strip() == "default"
    assert forced["copied"] == 1


def test_bootstrap_dry_run_writes_nothing(tmp_path):
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "a.sh").write_text("a\n")
    tree = tmp_path / "tree"

    out = _bootstrap(
        tmp_path, "--project", "p1", "--tree", str(tree), "--source", str(flat), "--dry-run"
    )
    assert out["dry_run"] == 1
    assert not (tree / "project-p1").exists()


def test_bootstrap_refuses_a_project_id_that_is_a_path(tmp_path):
    """The id lands in a directory path, so traversal is refused here as well as in the catalog."""
    tree = tmp_path / "tree"
    for bad in ("../escape", "p1/../..", "Upper", "with space"):
        proc = subprocess.run(
            ["bash", str(BOOTSTRAP), "--project", bad, "--tree", str(tree)],
            capture_output=True,
            text=True,
        check=False)
        assert proc.returncode == 2, f"{bad!r} was accepted"
        assert "invalid project id" in proc.stderr


def test_a_custom_script_shadows_a_shipped_one_without_editing_it(monkeypatch, tmp_path):
    """The layer that makes registering a Tool possible without writing a product directory.

    Registering a Tool records a *basename*; the file has to exist somewhere. Before `custom/`
    the only writable place was a project directory, which would make a genuinely
    cross-platform script the property of whichever project happened to be active.
    """
    base = tmp_path / "deploy"
    tree = base / "scripts"
    for layer, body in (("multi-platform", "shipped"), ("custom", "operator's")):
        (tree / layer).mkdir(parents=True)
        (tree / layer / "probe.sh").write_text(body + "\n")
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    resolved = script_tree.resolve("probe.sh")
    assert resolved == f"{tree}/custom/probe.sh"
    assert Path(resolved).read_text().strip() == "operator's"


def test_a_project_copy_still_beats_a_custom_script(monkeypatch, tmp_path):
    """Order matters both ways: a site's own edit stays the most specific answer."""
    base = tmp_path / "deploy"
    tree = base / "scripts"
    for layer, body in (
        ("multi-platform", "shipped"),
        ("custom", "operator's"),
        ("project-p1", "this project's"),
    ):
        (tree / layer).mkdir(parents=True)
        (tree / layer / "probe.sh").write_text(body + "\n")
    _env(monkeypatch, RACK_REMOTE_BASE_PATH=base)

    assert Path(script_tree.resolve("probe.sh", "p1")).read_text().strip() == "this project's"
