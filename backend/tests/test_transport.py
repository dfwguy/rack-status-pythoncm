"""Unit tests for the composable transport layer."""
import os
from unittest import mock

import pytest

from transport import (
    EnvForwardingConnection,
    LocalConnection,
    RoutingConnection,
    SSHConfig,
    SSHConnection,
    SudoConnection,
    TransportConfig,
    build_connection,
    make_env_resolver,
    merge_env,
)


class TestLocalConnection:
    def test_run_string(self):
        r = LocalConnection().run("echo hi", timeout=5)
        assert r.returncode == 0 and "hi" in r.stdout

    def test_run_list(self):
        r = LocalConnection().run(["echo", "yo"], timeout=5)
        assert r.returncode == 0 and "yo" in r.stdout

    def test_login_shell_flag(self):
        # non-login shell still runs the command
        r = LocalConnection().run("echo x", timeout=5, login_shell=False)
        assert r.returncode == 0 and "x" in r.stdout

    def test_popen(self):
        p = LocalConnection().popen("echo popen")
        out, _ = p.communicate(timeout=5)
        assert p.returncode == 0 and "popen" in out

    def test_is_remote_false(self):
        assert LocalConnection().is_remote() is False

    def test_resolve_path_local_is_absolute(self, tmp_path):
        rp = LocalConnection().resolve_path(str(tmp_path))
        assert os.path.isabs(rp)


class TestMergeEnv:
    def test_none_none_inherits(self):
        assert merge_env(None, None) is None

    def test_explicit_env_preserved_without_extra(self):
        # No extra => pass the caller's env dict through unchanged (replace semantics).
        base = {"ONLY": "1"}
        assert merge_env(base, None) == base

    def test_extra_layers_on_environ_when_no_base(self):
        merged = merge_env(None, {"FOO": "bar"})
        assert merged["FOO"] == "bar"
        assert "PATH" in merged  # layered on os.environ

    def test_extra_overrides_base(self):
        merged = merge_env({"FOO": "old"}, {"FOO": "new"})
        assert merged["FOO"] == "new"


class TestSSHConnection:
    def _conn(self):
        return SSHConnection(SSHConfig(host="10.0.0.1", user="root", key="/nonexistent", port="22"))

    def test_ssh_argv_includes_host(self):
        argv = self._conn().ssh_argv()
        assert argv[0] == "ssh" and "root@10.0.0.1" in argv

    def test_ssh_argv_requires_host(self):
        with pytest.raises(RuntimeError, match="SSH host"):
            SSHConnection(SSHConfig(host="")).ssh_argv()

    def test_remote_cmd_env_prefix_before_cd(self):
        argv = self._conn().wrap_argv("run.sh", cwd="/tmp/x", env_extra={"A": "b"})
        remote = argv[-1]
        assert remote.startswith("export A=b; ")
        assert "cd /tmp/x && run.sh" in remote

    def test_connect_timeout_override(self):
        argv = self._conn().ssh_argv(connect_timeout="3")
        assert "ConnectTimeout=3" in argv

    def test_is_remote_true(self):
        assert self._conn().is_remote() is True


class TestEnvResolver:
    def test_forwards_allowlisted_prefixes(self):
        resolver = make_env_resolver(("RACK_",), ("CUDA_MAJ",))
        with mock.patch.dict(os.environ, {"RACK_X": "1", "OTHER": "2", "CUDA_MAJ": "12"}, clear=False):
            out = resolver()
        assert out["RACK_X"] == "1" and out["CUDA_MAJ"] == "12"
        assert "OTHER" not in out

    def test_overlay_lowest_precedence(self):
        resolver = make_env_resolver(("RACK_",), (), overlay_provider=lambda: {"RACK_X": "overlay"})
        with mock.patch.dict(os.environ, {"RACK_X": "environ"}, clear=False):
            out = resolver()
        assert out["RACK_X"] == "environ"  # environ beats overlay

    def test_env_extra_highest_precedence(self):
        resolver = make_env_resolver(("RACK_",), ())
        with mock.patch.dict(os.environ, {"RACK_X": "environ"}, clear=False):
            out = resolver({"RACK_X": "extra"})
        assert out["RACK_X"] == "extra"


class TestEnvForwardingDecorator:
    def test_injects_resolved_env(self):
        resolver = make_env_resolver(("RACK_",), ())
        conn = EnvForwardingConnection(
            SSHConnection(SSHConfig(host="h", user="u")), resolver
        )
        with mock.patch.dict(os.environ, {"RACK_Y": "z"}, clear=False):
            argv = conn.wrap_argv("cmd")
        assert "export RACK_Y=z" in argv[-1]


class TestSudoConnection:
    def test_wrap_list_non_interactive(self):
        conn = SudoConnection(LocalConnection())
        argv = conn.wrap_argv(["bash", "sysinfo.sh", "rack01"])
        assert argv[:3] == ["sudo", "-n", "-E"]
        assert argv[3:] == ["bash", "sysinfo.sh", "rack01"]

    def test_no_preserve_env(self):
        conn = SudoConnection(LocalConnection(), preserve_env=False)
        argv = conn.wrap_argv(["id"])
        assert "-E" not in argv and argv[:2] == ["sudo", "-n"]

    def test_password_mode_uses_stdin(self):
        conn = SudoConnection(
            SSHConnection(SSHConfig(host="h", user="cmods")), password="s3cret"
        )
        argv = conn.wrap_argv(["bash", "sysinfo.sh"])
        remote = argv[-1]
        assert "sudo -S" in remote and "s3cret" in remote

    def test_sudo_over_ssh_prepends_before_cmd_after_cd(self):
        # export…; cd …; sudo -n -E <cmd>
        inner = SSHConnection(SSHConfig(host="h", user="cmods"))
        conn = SudoConnection(inner)
        remote = conn.wrap_argv("sysinfo.sh", cwd="/work")[-1]
        assert "cd /work && sudo -n -E sysinfo.sh" in remote


class TestRoutingConnection:
    def test_select_and_default(self):
        a, b = LocalConnection(), LocalConnection()
        r = RoutingConnection({"control": a, "script": b}, default="control")
        assert r.select("script") is b
        assert r.select("control") is a
        assert r.select("unknown") is a  # falls back to default


class TestBuildConnection:
    def test_local(self):
        conn = build_connection(TransportConfig(exec_mode="local"))
        assert conn.is_remote() is False
        assert conn.run("echo z", timeout=5).stdout.strip() == "z"

    def test_ssh_composition(self):
        cfg = TransportConfig(exec_mode="ssh", control=SSHConfig(host="10.0.0.1", user="root"))
        conn = build_connection(cfg)
        assert conn.is_remote() is True
        with mock.patch.dict(os.environ, {"RACK_W": "q"}, clear=False):
            argv = conn.wrap_argv(["true"], profile="control")
        assert "root@10.0.0.1" in argv and "export RACK_W=q" in argv[-1]

    def test_from_env(self):
        with mock.patch.dict(
            os.environ, {"RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "h", "RACK_SSH_USER": "u"}, clear=False
        ):
            cfg = TransportConfig.from_env()
        assert cfg.exec_mode == "ssh" and cfg.control.host == "h" and cfg.control.user == "u"

    def test_script_sudo_wraps_only_privileged_route(self):
        with mock.patch.dict(
            os.environ,
            {"RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "h", "RACK_SSH_USER": "cmods",
             "RACK_SCRIPT_SUDO": "1"},
            clear=False,
        ):
            cfg = TransportConfig.from_env()
            conn = build_connection(cfg)
        assert cfg.script_sudo is True
        # privileged route escalates, control route does not
        script_argv = conn.wrap_argv(["bash", "sysinfo.sh"], profile="script")
        control_argv = conn.wrap_argv(["bash", "sysinfo.sh"], profile="control")
        assert "sudo -n -E bash sysinfo.sh" in script_argv[-1]
        assert "sudo" not in control_argv[-1]

    def test_sudo_password_enables_sudo(self):
        with mock.patch.dict(
            os.environ,
            {"RACK_EXEC_MODE": "local", "RACK_SUDO_PASSWORD": "pw"},
            clear=False,
        ):
            cfg = TransportConfig.from_env()
        assert cfg.script_sudo is True and cfg.sudo_password == "pw"
