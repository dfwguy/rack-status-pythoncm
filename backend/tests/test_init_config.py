# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""`cli.py init-config`: the minimal `config/rackstatus.toml` a deployment starts from.

Two promises are pinned. The minimal file is *enough* — it carries every setting a fresh,
non-demo deployment is required to have, so a new required setting cannot be added without this
failing. And converting an old `rack-config.env` loses nothing silently: every key is either
carried or named with the reason it was not.
"""
from __future__ import annotations

import os

import config_launch
import config_registry
import pytest

import cli

#: Settings with a `required_when` that a fresh, non-demo deployment does NOT need in its minimal
#: file, each with why. Adding a required setting means either writing it in `minimal_config()` or
#: adding it here with a reason — the test below refuses a third option.
CONDITIONALLY_REQUIRED = {
    "RACK_WEB_TLS_KEY": "required only when RACK_WEB_TLS_CERT is set; TLS is settled in config/tls/",
}


@pytest.fixture()
def isolated_env(monkeypatch, tmp_path):
    """`config_launch.load` writes `os.environ`; keep it from leaking into other tests."""
    monkeypatch.setattr(os, "environ", os.environ.copy())
    target = tmp_path / "config" / "rackstatus.toml"
    monkeypatch.setenv("NVIS_RACKSTATUS_CONFIG", str(target))
    for name in config_registry.declared():
        monkeypatch.delenv(name, raising=False)
    return target


class TestTheMinimalFileIsEnough:
    def test_every_required_setting_is_written_or_explained(self):
        written = {
            line.split("=", 1)[0].strip()
            for line in cli.minimal_config("10.0.0.9").splitlines()
            if "=" in line and not line.startswith("#")
        }
        carrying = {s.name for s in config_registry.declared().values() if s.required_when}
        unexplained = sorted(carrying - written - set(CONDITIONALLY_REQUIRED))
        assert not unexplained, (
            f"required settings the minimal config neither writes nor explains: {unexplained}"
        )

    def test_it_loads_and_sets_the_head_node(self, isolated_env):
        isolated_env.parent.mkdir(parents=True)
        isolated_env.write_text(cli.minimal_config("10.0.0.9"))
        result = config_launch.load()
        assert os.environ["RACK_SSH_HOST"] == "10.0.0.9"
        assert not [r for r in result.records if r.level == "warning"]

    def test_without_a_host_the_line_is_inert_and_says_so(self, isolated_env):
        text = cli.minimal_config()
        assert "# RACK_SSH_HOST =" in text and "set this" in text
        isolated_env.parent.mkdir(parents=True)
        isolated_env.write_text(text)
        config_launch.load()
        assert "RACK_SSH_HOST" not in os.environ

    def test_it_is_not_the_example(self):
        """The example lists everything commented out; this lists only what is needed, live."""
        text = cli.minimal_config("h")
        assert len([ln for ln in text.splitlines() if ln and not ln.startswith("#")]) == 2


class TestTheCommand:
    def test_it_writes_the_file(self, isolated_env, capsys):
        assert cli.main(["init-config", "--ssh-host", "head-01"]) == cli.EXIT_OK
        assert 'RACK_SSH_HOST = "head-01"' in isolated_env.read_text()
        assert str(isolated_env) in capsys.readouterr().out

    def test_it_never_replaces_an_operators_file_by_default(self, isolated_env):
        isolated_env.parent.mkdir(parents=True)
        isolated_env.write_text("# mine\n")
        assert cli.main(["init-config", "--ssh-host", "h"]) == cli.EXIT_FAULT
        assert isolated_env.read_text() == "# mine\n"

    def test_force_replaces_it(self, isolated_env):
        isolated_env.parent.mkdir(parents=True)
        isolated_env.write_text("# mine\n")
        assert cli.main(["init-config", "--ssh-host", "h", "--force"]) == cli.EXIT_OK
        assert 'RACK_SSH_HOST = "h"' in isolated_env.read_text()

    @pytest.mark.parametrize("bad", ["", "   ", "a\nb"])
    def test_a_blank_or_multiline_host_is_refused(self, isolated_env, bad):
        assert cli.main(["init-config", "--ssh-host", bad]) == cli.EXIT_CONFIG
        assert not isolated_env.exists()


OLD_RACK_CONFIG = """\
# a comment
RACK_REMOTE_BASE_PATH=/home/cmsupport/nvis-rack-status
RACK_EXEC_MODE=ssh
RACK_PLATFORM=bcm
RACK_SSH_USER=root
RACK_SSH_PORT=22
export RACK_SLURM_SKIP_MODULE_LOAD=1
NEMO_SQSH_SRC="/cluster/containers/nemo.sqsh"
RACK_AI_API_KEY=nvapi-secret
RACK_SITE_ONLY_KNOB=yes
PORT=5000
RACK_WEB_TLS_CERT=/etc/x.crt
"""


class TestConvertingAnOldEnvFile:
    def test_parsing_handles_comments_export_and_quotes(self):
        values = cli.parse_env_file(OLD_RACK_CONFIG)
        assert values["NEMO_SQSH_SRC"] == "/cluster/containers/nemo.sqsh"
        assert values["RACK_SLURM_SKIP_MODULE_LOAD"] == "1"
        assert "# a comment" not in values

    def test_every_key_is_carried_or_named(self):
        values = cli.parse_env_file(OLD_RACK_CONFIG)
        kept, notes = cli.sort_env_values(values)
        named = {note.split(":", 1)[0] for note in notes}
        assert set(kept) | named == set(values)
        assert not set(kept) & named

    def test_secrets_are_refused_not_copied(self):
        kept, notes = cli.sort_env_values(cli.parse_env_file(OLD_RACK_CONFIG))
        assert "RACK_AI_API_KEY" not in kept
        assert any(n.startswith("RACK_AI_API_KEY:") and "secret" in n for n in notes)

    @pytest.mark.parametrize("key", ["PORT", "RACK_EXEC_MODE", "RACK_WEB_TLS_CERT"])
    def test_keys_that_cannot_take_effect_from_the_file_are_left_out(self, key):
        kept, notes = cli.sort_env_values(cli.parse_env_file(OLD_RACK_CONFIG))
        assert key not in kept
        assert any(n.startswith(f"{key}:") for n in notes)

    def test_the_converted_file_loads_with_the_site_values(self, isolated_env, tmp_path):
        old = tmp_path / "rack-config.env"
        old.write_text(OLD_RACK_CONFIG)
        assert cli.main(["init-config", "--ssh-host", "h", "--from-env-file", str(old)]) == 0
        text = isolated_env.read_text()
        assert "RACK_SSH_PORT = 22" in text, "declared integers are written as integers"
        assert "RACK_SLURM_SKIP_MODULE_LOAD = true" in text, "declared booleans as booleans"
        assert "[site]" in text and "RACK_SITE_ONLY_KNOB" in text

        config_launch.load()
        assert os.environ["RACK_REMOTE_BASE_PATH"] == "/home/cmsupport/nvis-rack-status"
        assert os.environ["RACK_SLURM_SKIP_MODULE_LOAD"] == "1"
        assert os.environ["RACK_SSH_PORT"] == "22"
        assert os.environ["RACK_SITE_ONLY_KNOB"] == "yes"
        assert "RACK_AI_API_KEY" not in os.environ

    def test_a_host_in_the_old_file_is_carried_when_none_is_given(self, isolated_env, tmp_path):
        old = tmp_path / "rack-config.env"
        old.write_text("RACK_SSH_HOST=10.9.9.9\n")
        assert cli.main(["init-config", "--from-env-file", str(old)]) == 0
        assert 'RACK_SSH_HOST = "10.9.9.9"' in isolated_env.read_text()

    def test_a_missing_env_file_is_refused(self, isolated_env, tmp_path):
        assert cli.main(["init-config", "--from-env-file", str(tmp_path / "nope.env")]) == 78
        assert not isolated_env.exists()


def test_it_runs_without_a_toml_parser(tmp_path):
    """Operators run this on the Docker host, whose Python may predate `tomllib` (macOS ships 3.9).
    Writing the file needs no parser, so a module-scope TOML import on this path is a regression."""
    import subprocess
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    target = tmp_path / "rackstatus.toml"
    code = (
        "import sys; sys.modules['tomllib'] = None; sys.modules['tomli'] = None; "
        f"sys.path.insert(0, {str(backend)!r}); import cli; "
        f"raise SystemExit(cli.main(['init-config', '--config', {str(target)!r}, '--ssh-host', 'h']))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert 'RACK_SSH_HOST = "h"' in target.read_text()
