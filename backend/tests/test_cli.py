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
"""The `rackstatus` launcher.

The property worth most here is that **`check` changes nothing**. It is the command an operator
points at a live deployment's paths to find out what a restart would do, so a `check` that wrote
anything would be worse than not having it. The first implementation failed exactly that: opening
an `IdentityStore` calls `touch()`, so merely asking "is there an administrator" created the store
and then reported it as absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import cli

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def paths(tmp_path):
    """A deployment's worth of paths, none of which exist yet."""
    return {
        "identity": tmp_path / "identity" / "identity.json",
        "policy": tmp_path / "store" / "policies.json",
        # Its own catalog, because `RACK_SSH_HOST`'s requirement predicate keys off the *resolved*
        # connection of the active cluster rather than reading `RACK_EXEC_MODE` — deliberately, so a
        # cluster supplying the host through its overlay is not refused. Sharing the suite's catalog
        # made the fault tests depend on whatever an earlier module had left in it: they passed
        # alone and failed in a full run.
        "catalog": tmp_path / "catalog" / "catalog.json",
    }


def _run(paths, *extra, demo: str = "1", exec_mode: str = "local") -> subprocess.CompletedProcess:
    """Out of process, because the parser writes `os.environ` and the plan reads modules that cache
    it. In-process runs would leak a repointed identity path into the rest of the session.

    `exec_mode` is explicit because `conftest` exports `RACK_EXEC_MODE=local` for the whole suite,
    and that legitimately waives the `RACK_SSH_HOST` requirement — so a fault test inheriting it
    asserts against a condition that is correctly absent.
    """
    env = {
        **os.environ,
        "RACK_STATUS_DEMO": demo,
        "RACK_EXEC_MODE": exec_mode,
        "RACK_WEB_SECRET_KEY": "cli-test-secret",
    }
    # Cleared, not inherited. A full-suite run has RACK_SSH_HOST in `os.environ` from an earlier
    # module, which satisfies the requirement these tests assert is unmet — so they passed in
    # isolation and failed in the suite.
    for leaked in ("RACK_SSH_HOST", "NVIS_RACKSTATUS_CONFIG"):
        env.pop(leaked, None)
    return subprocess.run(
        [sys.executable, "backend/cli.py", "check",
         "--identity-file", str(paths["identity"]),
         "--policy-file", str(paths["policy"]),
         "--catalog-file", str(paths["catalog"]), *extra],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, check=False,
    )


class TestCheckChangesNothing:
    def test_it_creates_no_identity_store(self, paths):
        """The regression test for the bug this file's docstring describes."""
        _run(paths)
        assert not paths["identity"].exists()

    def test_it_creates_no_policy_file(self, paths):
        _run(paths)
        assert not paths["policy"].exists()

    def test_it_creates_no_parent_directories(self, paths):
        """`touch(create_dirs=True)` made the directory as well as the file, so the directory is
        the more sensitive signal — it survives even if the file write fails."""
        _run(paths)
        assert not paths["identity"].parent.exists()
        assert not paths["policy"].parent.exists()

    def test_the_catalog_is_the_only_thing_it_may_write(self, paths, tmp_path):
        """Pinned as a documented exception rather than left to be discovered.

        The preflight resolves the active cluster so its verdict accounts for a host supplied
        through a cluster overlay, and the catalog seeds on first access. That seed is idempotent
        and holds no secret. This test exists so the exception cannot silently grow: if a future
        `check` starts writing anything else, it fails here.
        """
        _run(paths)
        written = {p.name for p in tmp_path.rglob("*") if p.is_file()}
        assert written <= {"catalog.json"}, f"check wrote {sorted(written)}"

    def test_the_banner_does_not_overclaim(self, paths):
        """It used to say "nothing has been changed", which the catalog seed made untrue."""
        out = _run(paths).stdout
        assert "nothing has been changed" not in out
        assert "no administrator, policy or credential has been created" in out


class TestCheckReportsWhatServeWouldDo:
    def test_a_brand_new_deployment_would_get_both(self, paths):
        got = _run(paths)
        assert "create the `admin` account" in got.stdout
        assert "write the `administrators` policy" in got.stdout

    def test_it_names_the_paths_it_inspected(self, paths):
        """An operator running this against the wrong deployment is the mistake it should make
        impossible, so the resolved paths are part of the report rather than assumed."""
        got = _run(paths)
        assert str(paths["identity"]) in got.stdout
        assert str(paths["policy"]) in got.stdout

    def test_a_provisioned_deployment_would_get_nothing(self, paths, tmp_path):
        paths["identity"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-c",
             ("import sys; sys.path.insert(0, 'backend');"
             "from identity.store import IdentityStore;"
             "import identity.bootstrap as b;"
             "s = IdentityStore(sys.argv[1]);"
             "b.ensure_admin(s, emit=False); b.ensure_admin_policy(s); s.close()"),
             str(paths["identity"])],
            cwd=REPO_ROOT, check=True, capture_output=True,
            env={**os.environ, "RACK_WEB_POLICY_FILE": str(paths["policy"]),
                 "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )

        got = _run(paths)
        assert "create nothing" in got.stdout
        assert "ENFORCING" in got.stdout

    def test_it_says_whether_authorization_is_enforcing(self, paths):
        """The gap this closes: with no policy file the product runs allow-all and nothing in the
        UI or the logs an operator reads says so."""
        assert "permissive (allow-all)" in _run(paths).stdout


class TestExitCodes:
    def test_a_clean_check_exits_zero(self, paths):
        assert _run(paths).returncode == 0

    def test_a_required_setting_fault_exits_78(self, paths):
        """`EX_CONFIG`, so a supervisor reads it as "the configuration is wrong" and does not
        restart-loop a deployment that cannot come up."""
        got = _run(paths, demo="0", exec_mode="ssh")
        assert got.returncode == cli.EXIT_CONFIG
        assert "RACK_SSH_HOST" in got.stdout

    def test_a_fault_names_the_setting_and_the_condition(self, paths):
        """The first version printed a repr of a list of dataclasses, which named nothing."""
        got = _run(paths, demo="0", exec_mode="ssh")
        assert "RACK_SSH_HOST: " in got.stdout
        assert "Fault(" not in got.stdout


class TestTheJsonReportIsMachineReadable:
    def test_it_parses_and_carries_the_verdict(self, paths):
        got = _run(paths, "--json")
        payload = json.loads(got.stdout)
        assert payload["would_create_admin"] is True
        assert payload["would_create_policy"] is True
        assert payload["enforcing"] is False

    def test_it_records_which_flags_were_applied(self, paths):
        """Provenance, so a deploy gate's log shows which paths the verdict is about."""
        payload = json.loads(_run(paths, "--json").stdout)
        assert payload["applied"]["NVIS_IDENTITY_FILE"] == str(paths["identity"])


class TestResetAdminPassword:
    """The deliberate recovery path for a one-time password missed on first boot. It re-issues the
    credential — the one thing allowed to replace a live one — and, unlike a serve-time flag, carries
    no standing state that would re-fire on the next restart."""

    def _seed_admin(self, paths):
        paths["identity"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-c",
             ("import sys; sys.path.insert(0, 'backend');"
              "from identity.store import IdentityStore;"
              "import identity.bootstrap as b;"
              "s = IdentityStore(sys.argv[1]); b.ensure_admin(s, emit=False); s.close()"),
             str(paths["identity"])],
            cwd=REPO_ROOT, check=True, capture_output=True,
            env={**os.environ, "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )

    def _reset(self, paths):
        return subprocess.run(
            [sys.executable, "backend/cli.py", "reset-admin-password",
             "--identity-file", str(paths["identity"])],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False,
            env={**os.environ, "RACK_STATUS_DEMO": "1", "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )

    def test_it_reissues_for_an_existing_admin(self, paths):
        self._seed_admin(paths)
        got = self._reset(paths)
        assert got.returncode == 0, got.stderr
        assert "password:" in got.stdout
        assert "no longer works" in got.stdout

    def test_two_resets_issue_different_passwords(self, paths):
        """Each reset is a fresh credential from the CSPRNG; a bug that reprinted a cached value
        would show as two identical passwords."""
        import re

        self._seed_admin(paths)
        # Anchored to line start, or it matches the "reset-admin-password:" prefix line too.
        pattern = r"(?m)^\s*password:\s+(\S+)\s*$"
        first = re.search(pattern, self._reset(paths).stdout)
        second = re.search(pattern, self._reset(paths).stdout)
        assert first and second and first.group(1) != second.group(1)

    def test_it_refuses_and_creates_nothing_without_a_store(self, paths):
        """No store means no deployment to recover a login for. `serve` creates the administrator;
        this must not mint one into a store it just brought into being."""
        got = self._reset(paths)
        assert got.returncode == cli.EXIT_FAULT
        assert "no identity store" in got.stderr
        assert not paths["identity"].exists()


class TestAttachAdminPolicy:
    """The separate repair for a policy-attachment lockout — distinct from a password reset. The
    `administrators` policy exists (persisted on the state volume) but is attached to nobody, so the
    freshly bootstrapped admin authenticates and is then refused everything under default-deny."""

    def _seed_orphan(self, paths):
        """An admin plus an `administrators` policy that is attached to no one — a reset identity
        store beside a persisted policy store."""
        paths["identity"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-c",
             ("import sys; sys.path.insert(0, 'backend');"
              "from identity.store import IdentityStore;"
              "import identity.bootstrap as b; from authz import store as ps;"
              "s = IdentityStore(sys.argv[1]); b.ensure_admin(s, emit=False);"
              "ps.create(b.admin_policy_document()); s.close()"),
             str(paths["identity"])],
            cwd=REPO_ROOT, check=True, capture_output=True,
            env={**os.environ, "RACK_WEB_POLICY_FILE": str(paths["policy"]),
                 "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )

    def _attach(self, paths):
        return subprocess.run(
            [sys.executable, "backend/cli.py", "attach-admin-policy",
             "--identity-file", str(paths["identity"]), "--policy-file", str(paths["policy"])],
            capture_output=True, text=True, cwd=REPO_ROOT, check=False,
            env={**os.environ, "RACK_STATUS_DEMO": "1", "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )

    def _attached_subjects(self, paths):
        out = subprocess.run(
            [sys.executable, "-c",
             ("import sys; sys.path.insert(0, 'backend');"
              "from identity.store import IdentityStore; import identity.bootstrap as b;"
              "s = IdentityStore(sys.argv[1]);"
              "print(s.subjects_with_policy(b.ADMIN_POLICY_ID)); s.close()"),
             str(paths["identity"])],
            cwd=REPO_ROOT, check=True, capture_output=True, text=True,
            env={**os.environ, "RACK_WEB_POLICY_FILE": str(paths["policy"]),
                 "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )
        return out.stdout.strip()

    def test_it_attaches_an_orphaned_policy(self, paths):
        self._seed_orphan(paths)
        assert "user:admin" not in self._attached_subjects(paths)

        got = self._attach(paths)
        assert got.returncode == 0, got.stderr
        assert "attached" in got.stdout
        assert "user:admin" in self._attached_subjects(paths)

    def test_it_is_idempotent(self, paths):
        self._seed_orphan(paths)
        self._attach(paths)
        again = self._attach(paths)
        assert again.returncode == 0
        assert "already attached" in again.stdout

    def test_it_refuses_without_a_store(self, paths):
        got = self._attach(paths)
        assert got.returncode == cli.EXIT_FAULT
        assert "no identity store" in got.stderr
        assert not paths["identity"].exists()

    def test_it_refuses_without_the_policy(self, paths):
        """Admin present, but no administrators policy to attach — a different problem, and this
        command must not invent a policy (that is bootstrap's job)."""
        paths["identity"].parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [sys.executable, "-c",
             ("import sys; sys.path.insert(0, 'backend');"
              "from identity.store import IdentityStore; import identity.bootstrap as b;"
              "s = IdentityStore(sys.argv[1]); b.ensure_admin(s, emit=False); s.close()"),
             str(paths["identity"])],
            cwd=REPO_ROOT, check=True, capture_output=True,
            env={**os.environ, "RACK_WEB_POLICY_FILE": str(paths["policy"]),
                 "RACK_WEB_SECRET_KEY": "cli-test-secret"},
        )
        got = self._attach(paths)
        assert got.returncode == cli.EXIT_FAULT
        assert "no 'administrators' policy" in got.stderr


class TestTheParser:
    def test_a_subcommand_is_required(self):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    @pytest.mark.parametrize("command", ["serve", "check"])
    def test_both_commands_accept_every_path_override(self, command):
        args = cli.build_parser().parse_args([
            command, "--config", "/c.toml", "--identity-file", "/i.json",
            "--policy-file", "/p.json", "--catalog-file", "/cat.json",
            "--signing-key-dir", "/keys", "--tls-cert", "/c.pem", "--tls-key", "/k.pem",
        ])
        assert args.identity_file == "/i.json"
        assert args.tls_key == "/k.pem"

    def test_every_path_flag_maps_to_a_declared_setting(self):
        """`PATH_SETTINGS` is what `_apply` iterates, so a flag missing from it parses and is then
        silently ignored — the worst outcome available, because the run looks configured."""
        parsed = cli.build_parser().parse_args(["check"])
        for attr in cli.PATH_SETTINGS:
            assert hasattr(parsed, attr), attr

    def test_applying_flags_resolves_paths_absolutely(self, monkeypatch, tmp_path):
        """Several of these are read long after startup, by code with no memory of the working
        directory the server was launched from.

        Every settable key goes through `monkeypatch.setenv` first so teardown restores it. `_apply`
        writes `os.environ` directly — that is its job — and without this the temp path leaks into
        the session, where `test_identity_startup_wiring`'s isolation guard rightly fails the run
        for pointing the suite at a store outside the temp directory.
        """
        for env_name in cli.PATH_SETTINGS.values():
            monkeypatch.setenv(env_name, os.environ.get(env_name, ""))
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rel.json").write_text("{}")
        args = cli.build_parser().parse_args(["check", "--identity-file", "rel.json"])
        applied = cli._apply(args)
        assert applied["NVIS_IDENTITY_FILE"] == str((tmp_path / "rel.json").resolve())


class TestItImportsNoFirstPartyModuleAtImportTime:
    """The ordering constraint that makes the whole file work.

    `config_launch.load()` runs during `import app`, and `config_env` snapshots `os.environ` when it
    is first imported. A first-party import at this module's scope would read the environment
    *before* the flags were applied, so a `--policy-file` would parse, be stored, and then be
    ignored by a module that had already cached the old value.
    """

    def test_the_module_imports_only_the_standard_library(self):
        import ast

        source = (REPO_ROOT / "backend" / "cli.py").read_text()
        tree = ast.parse(source)
        first_party = {"app", "config_env", "config_launch", "config_preflight", "identity",
                       "authz", "web_auth", "paths", "core"}

        top_level: set[str] = set()
        for node in tree.body:  # module scope only; function-level imports are the point
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])

        assert not (top_level & first_party), (
            f"{sorted(top_level & first_party)} imported at module scope; move it into the "
            "function that needs it, after _apply() has written the environment"
        )
