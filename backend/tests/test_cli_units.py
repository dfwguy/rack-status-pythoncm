# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""`backend/cli.py` in process — the half `test_cli.py` cannot measure.

`test_cli.py` drives the CLI through **fifteen subprocess runs**, and for a good reason it states:
the parser writes `os.environ` and the plan reads modules that cache it, so an in-process run would
leak a repointed identity path into the rest of the session. That gives excellent behavioural
coverage and **zero measured coverage**, because `coverage.py` instruments the parent process only.
The per-file floor therefore read `backend/cli.py` at 32.5% for a file whose behaviour is
well tested — the number was measuring the wrong thing.

Rather than re-plumb the whole repo's coverage for subprocesses, this file tests the parts that are
pure functions of their inputs, in process, with `monkeypatch` owning every environment key touched
(the same isolation the earlier `_apply` test needed). Two of them are worth having regardless of
coverage:

* **`_serve` builds a gunicorn argv with `--workers 1`, and that is a correctness requirement**, not
  a tuning choice. SRP challenge state is in-process, so a second worker splits the table and
  roughly half of all logins fail — a failure that presents as an intermittent client bug. Nothing
  asserted it, and it is one edit away from being "tuned".
* **`_report`'s exit code**, because a `check` that prints faults and exits 0 is a deploy gate that
  passes a broken deployment.

`os.execv` is stubbed, so `_serve` is inspected rather than executed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cli


@pytest.fixture(autouse=True)
def _own_every_key_we_touch(monkeypatch):
    """Hand `monkeypatch` every name these functions write, so it restores them.

    `apply_runtime_defaults` and `_apply` write `os.environ` directly. `monkeypatch.setenv` records
    the pre-test value and restores it on teardown even when the product overwrites it in between,
    which is what keeps a repointed identity path from leaking into the rest of the session — the
    hazard that made `test_cli.py` use subprocesses in the first place.
    """
    for name in (
        *cli.PATH_SETTINGS.values(),
        *cli.CLUSTER_DEFAULTS,
        "RACK_STATUS_DEMO",
        "RACK_WEB_BIND_ADDR",
        "RACK_WEB_PORT",
        "GUNICORN_THREADS",
        "GUNICORN_TIMEOUT",
    ):
        monkeypatch.setenv(name, os.environ.get(name, ""))


def _args(**kw) -> argparse.Namespace:
    base = dict.fromkeys(cli.PATH_SETTINGS, None)
    base.update({"host": None, "port": None, "command": "check", "as_json": False,
                 "no_bootstrap": False})
    base.update(kw)
    return argparse.Namespace(**base)


class TestRuntimeDefaults:
    """Demo-vs-cluster inference. Only ever sets a *default*."""

    def test_macos_defaults_to_demo(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "")
        assert cli.apply_runtime_defaults(platform="darwin")["RACK_STATUS_DEMO"] == "1"

    def test_linux_defaults_to_cluster_mode(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "")
        assert cli.apply_runtime_defaults(platform="linux")["RACK_STATUS_DEMO"] == "0"

    def test_an_explicit_choice_is_honoured_on_a_mac(self, monkeypatch):
        """`RACK_STATUS_DEMO=0` on a Mac is how somebody tests against a real cluster over SSH
        from their desk. Inference must not overrule it."""
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        applied = cli.apply_runtime_defaults(platform="darwin")
        assert "RACK_STATUS_DEMO" not in applied
        assert os.environ["RACK_STATUS_DEMO"] == "0"

    def test_cluster_defaults_are_withheld_in_demo_mode(self, monkeypatch):
        """They name absolute paths on a head node's filesystem, and a demo run has no head node
        for them to be wrong about."""
        monkeypatch.setenv("RACK_STATUS_DEMO", "1")
        applied = cli.apply_runtime_defaults(platform="darwin")
        for name in cli.CLUSTER_DEFAULTS:
            assert name not in applied

    def test_cluster_defaults_are_applied_outside_demo_mode(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        for name in cli.CLUSTER_DEFAULTS:
            monkeypatch.setenv(name, "")
        applied = cli.apply_runtime_defaults(platform="linux")
        assert applied == {**cli.CLUSTER_DEFAULTS}

    def test_an_existing_cluster_setting_is_never_overwritten(self, monkeypatch):
        monkeypatch.setenv("RACK_STATUS_DEMO", "0")
        monkeypatch.setenv("VALIDATION_TEST", "mine")
        cli.apply_runtime_defaults(platform="linux")
        assert os.environ["VALIDATION_TEST"] == "mine"


class TestApply:
    """Flags to environment. The CLI resolves no precedence — that has one owner."""

    def test_a_path_is_made_absolute(self, monkeypatch, tmp_path):
        """The server may be started from anywhere, and several of these are read long after
        startup by code with no memory of the original working directory."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "id.json").touch()
        cli._apply(_args(identity_file="id.json"))
        assert os.environ["NVIS_IDENTITY_FILE"] == str((tmp_path / "id.json").resolve())

    def test_a_user_path_is_expanded(self, monkeypatch):
        cli._apply(_args(policy_file="~/policies.json"))
        assert os.environ["RACK_WEB_POLICY_FILE"] == str(
            (Path.home() / "policies.json").resolve()
        )

    def test_host_and_port_are_set(self):
        applied = cli._apply(_args(host="127.0.0.1", port=5051))
        assert applied["RACK_WEB_BIND_ADDR"] == "127.0.0.1"
        assert applied["RACK_WEB_PORT"] == "5051"

    def test_every_path_flag_maps_to_its_setting(self, tmp_path):
        """Driven from `PATH_SETTINGS`, so a flag added without a mapping fails here."""
        supplied = {attr: str(tmp_path / attr) for attr in cli.PATH_SETTINGS}
        applied = cli._apply(_args(**supplied))
        for attr, env in cli.PATH_SETTINGS.items():
            assert applied[env] == str((tmp_path / attr).resolve())

    def test_an_unsupplied_flag_sets_nothing(self):
        applied = cli._apply(_args())
        for env in cli.PATH_SETTINGS.values():
            assert env not in applied


class TestDescribe:
    """One line per fault, naming the setting and why it is required."""

    def test_it_names_the_setting_and_the_condition(self):
        fault = argparse.Namespace(name="RACK_SSH_HOST", condition="exec mode is ssh", detail="")
        assert cli._describe(fault) == "RACK_SSH_HOST: exec mode is ssh"

    def test_the_detail_is_appended_when_present(self):
        fault = argparse.Namespace(name="X", condition="because", detail="extra")
        assert cli._describe(fault) == "X: because (extra)"

    def test_something_without_a_name_falls_back_to_its_string(self):
        assert cli._describe("just a string") == "just a string"


def _plan(**over) -> dict:
    base = {
        "config_file": None, "config_settings_from_file": 0, "warnings": [],
        "identity_file": "/tmp/identity.json", "policy_file": "/tmp/policies.json",
        "would_create_admin": False, "would_create_policy": False,
        "admin_exists": True, "policy_count": 1, "enforcing": True, "tls": True,
        "faults": [],
    }
    base.update(over)
    return base


class TestReport:
    """The `check` output, and — the part that matters for a deploy gate — its exit code."""

    def test_a_clean_plan_exits_zero(self, capsys):
        assert cli._report(_plan(), {}, as_json=False) == cli.EXIT_OK

    def test_a_fault_exits_78(self, capsys):
        """78 is `EX_CONFIG`. A `check` that prints faults and exits 0 is a gate that passes a
        broken deployment, which is the whole reason this function returns a code at all."""
        assert cli._report(_plan(faults=["X: required"]), {}, as_json=False) == cli.EXIT_CONFIG
        assert "1 fault(s) would refuse the launch" in capsys.readouterr().out

    def test_json_mode_carries_the_same_code(self, capsys):
        """A machine-readable mode that always exits 0 would be useless as a gate, and the
        divergence would be invisible to anyone reading the JSON."""
        assert cli._report(_plan(faults=["X"]), {}, as_json=True) == cli.EXIT_CONFIG
        assert cli._report(_plan(), {}, as_json=True) == cli.EXIT_OK

    def test_json_mode_is_valid_json_including_what_was_applied(self, capsys):
        cli._report(_plan(), {"RACK_WEB_PORT": "5051"}, as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["applied"] == {"RACK_WEB_PORT": "5051"}
        assert payload["admin_exists"] is True

    def test_it_says_what_a_serve_would_create(self, capsys):
        cli._report(_plan(would_create_admin=True, would_create_policy=True,
                          admin_exists=False, policy_count=0, enforcing=False), {}, as_json=False)
        out = capsys.readouterr().out
        assert "create the `admin` account" in out
        assert "switching to default-deny" in out

    def test_it_says_when_it_would_create_nothing(self, capsys):
        cli._report(_plan(), {}, as_json=False)
        assert "create nothing" in capsys.readouterr().out

    def test_warnings_are_reported_without_refusing(self, capsys):
        code = cli._report(_plan(warnings=["Y: undetermined"]), {}, as_json=False)
        assert code == cli.EXIT_OK, "a warning must not refuse a launch; only a fault does"
        assert "could not be determined" in capsys.readouterr().out

    def test_an_unreadable_policy_set_is_said_so_rather_than_shown_as_zero(self, capsys):
        """`None` means "could not read", `0` means "read it, and it is empty". Rendering the
        first as the second would report an unreadable policy file as a permissive deployment."""
        cli._report(_plan(policy_count=None), {}, as_json=False)
        assert "unreadable" in capsys.readouterr().out


class TestServeBuildsTheGunicornCommand:
    """`_serve` without serving: `os.execv` is stubbed so the argv can be inspected."""

    @pytest.fixture
    def execv(self, monkeypatch):
        captured: dict[str, list[str]] = {}

        def _stub(path, argv):
            captured["path"] = path
            captured["argv"] = list(argv)

        monkeypatch.setattr(cli.os, "execv", _stub)
        monkeypatch.setattr("startup_gate.prepare", lambda **_kw: [])
        return captured

    def test_one_worker_is_not_negotiable(self, execv):
        """SRP challenge state is in-process. A second worker splits the table, so roughly half of
        all logins fail — and it presents as an intermittent client bug, not as a config error.
        `assert_single_worker_safe()` refuses it at startup; this stops the argv drifting there.
        """
        cli._serve(_args(command="serve"))
        argv = execv["argv"]
        assert "--workers" in argv
        assert argv[argv.index("--workers") + 1] == "1"

    def test_it_runs_gunicorn_with_the_shared_config(self, execv):
        """Not the Werkzeug development server: over TLS it races its own shutdown against chunked
        responses. Reusing `gunicorn.conf.py` is what keeps a local run on production's TLS floor.
        """
        cli._serve(_args(command="serve"))
        argv = execv["argv"]
        assert argv[1:4] == ["-m", "gunicorn", "-c"]
        assert argv[4] == "gunicorn.conf.py"
        assert argv[-1] == "app:app"

    def test_the_certificate_is_left_to_gunicorn_conf(self, execv, monkeypatch):
        """gunicorn.conf.py sets certfile/keyfile from `startup_gate.ensure_tls()`. A CLI flag too
        would be a second source for the same decision."""
        monkeypatch.setenv("RACK_WEB_TLS_CERT", "/tmp/cert.pem")
        monkeypatch.setenv("RACK_WEB_TLS_KEY", "/tmp/key.pem")
        cli._serve(_args(command="serve"))
        argv = execv["argv"]
        assert "--certfile" not in argv and "--keyfile" not in argv

    def test_half_configured_tls_is_not_passed(self, execv, monkeypatch):
        """A cert with no key would make gunicorn fail to bind at all. Passing neither leaves the
        startup gate to report the real problem, which names the fix."""
        monkeypatch.setenv("RACK_WEB_TLS_CERT", "/tmp/cert.pem")
        monkeypatch.setenv("RACK_WEB_TLS_KEY", "")
        cli._serve(_args(command="serve"))
        assert "--certfile" not in execv["argv"]

    def test_the_bind_address_comes_from_the_environment(self, execv, monkeypatch):
        monkeypatch.setenv("RACK_WEB_BIND_ADDR", "127.0.0.1")
        monkeypatch.setenv("RACK_WEB_PORT", "5051")
        cli._serve(_args(command="serve"))
        argv = execv["argv"]
        assert argv[argv.index("--bind") + 1] == "127.0.0.1:5051"

    def test_it_binds_all_interfaces_on_5050_by_default(self, execv, monkeypatch):
        monkeypatch.setenv("RACK_WEB_BIND_ADDR", "")
        monkeypatch.setenv("RACK_WEB_PORT", "")
        cli._serve(_args(command="serve"))
        argv = execv["argv"]
        assert argv[argv.index("--bind") + 1] == "0.0.0.0:5050"

    def test_the_gate_runs_before_the_handoff(self, monkeypatch):
        """Ordering, not presence. `execv` replaces the process, so a gate called afterwards is a
        gate that never runs — and the refusal must come from the command the operator typed.
        """
        order: list[str] = []
        monkeypatch.setattr("startup_gate.prepare", lambda **_kw: order.append("gate") or [])
        monkeypatch.setattr(cli.os, "execv", lambda *_a: order.append("execv"))
        cli._serve(_args(command="serve"))
        assert order == ["gate", "execv"]

    def test_no_bootstrap_is_forwarded_to_the_gate(self, monkeypatch):
        """`--no-bootstrap` is the flag that turns "create what is missing" into "refuse". Dropping
        it on the floor would silently create an administrator on a deployment that asked not to.
        """
        seen: dict[str, object] = {}
        monkeypatch.setattr("startup_gate.prepare",
                            lambda **kw: seen.update(kw) or [])
        monkeypatch.setattr(cli.os, "execv", lambda *_a: None)
        cli._serve(_args(command="serve", no_bootstrap=True))
        assert seen == {"bootstrap": False}


class TestMainDispatch:
    def test_check_reports_and_does_not_serve(self, monkeypatch, capsys):
        served: list[bool] = []
        monkeypatch.setattr(cli, "_plan", lambda: _plan())
        monkeypatch.setattr(cli, "_serve", lambda _a: served.append(True))
        assert cli.main(["check"]) == cli.EXIT_OK
        assert not served, "`check` must never start a server"

    def test_serve_does_not_report(self, monkeypatch):
        monkeypatch.setattr(cli, "_serve", lambda _a: cli.EXIT_OK)
        monkeypatch.setattr(cli, "_plan", lambda: pytest.fail("`serve` must not run the plan"))
        assert cli.main(["serve"]) == cli.EXIT_OK

    def test_a_missing_subcommand_is_refused(self):
        """`required=True` on the subparser. Without it, a bare `rackstatus` would fall through to
        an argv with no `command` attribute."""
        with pytest.raises(SystemExit):
            cli.main([])
