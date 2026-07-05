"""Tests for the host_exec facade (now delegating to the transport layer).

These drive behaviour through the public surface + environment (the real
contract) rather than private globals, so they survive the transport refactor.
"""
import os
import subprocess
from unittest import mock

import pytest

os.environ.setdefault("RACK_EXEC_MODE", "local")

import host_exec


def _reset() -> None:
    """Force host_exec to rebuild its connection from the current environment."""
    host_exec._CONN = None
    host_exec._CONTROL_SSH = None
    host_exec._CONFIG = None
    host_exec._EXEC_MODE = None


def _init(**env: str) -> None:
    _reset()
    with mock.patch.dict(os.environ, env, clear=False):
        host_exec._ensure_init()


class TestIsRemote:
    def teardown_method(self):
        _reset()

    def test_local_mode(self):
        with mock.patch.dict(os.environ, {"RACK_EXEC_MODE": "local"}, clear=False):
            _reset()
            host_exec._ensure_init()
            assert host_exec.is_remote() is False

    def test_ssh_mode(self):
        with mock.patch.dict(
            os.environ, {"RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "10.0.0.1"}, clear=False
        ):
            _reset()
            host_exec._ensure_init()
            assert host_exec.is_remote() is True
        _reset()


class TestRunOnHostLocal:
    def setup_method(self):
        _init(RACK_EXEC_MODE="local")

    def teardown_method(self):
        _reset()

    def test_run_string_command(self):
        result = host_exec.run_on_host("echo hello", timeout=5)
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_run_list_command(self):
        result = host_exec.run_on_host(["echo", "world"], timeout=5)
        assert result.returncode == 0
        assert "world" in result.stdout

    def test_run_with_cwd(self, tmp_path):
        result = host_exec.run_on_host("pwd", cwd=str(tmp_path), timeout=5)
        assert result.returncode == 0
        assert str(tmp_path) in result.stdout

    def test_timeout_raises(self):
        with pytest.raises(subprocess.TimeoutExpired):
            host_exec.run_on_host("sleep 10", timeout=1)

    def test_failing_command(self):
        result = host_exec.run_on_host("exit 42", timeout=5)
        assert result.returncode == 42


class TestPopenOnHostLocal:
    def setup_method(self):
        _init(RACK_EXEC_MODE="local")

    def teardown_method(self):
        _reset()

    def test_popen_communicate(self):
        hp = host_exec.popen_on_host("echo popen_test")
        out, _err = hp.communicate(timeout=5)
        assert hp.returncode == 0
        assert "popen_test" in out

    def test_popen_kill(self):
        hp = host_exec.popen_on_host("sleep 60")
        hp.kill()
        hp.communicate(timeout=5)
        assert hp.returncode is not None


class TestEnvForwardingViaFacade:
    """Env forwarding is observable in the SSH-wrapped command string."""

    def teardown_method(self):
        _reset()

    def test_wrap_forwards_rack_vars(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="10.0.0.1", RACK_SSH_USER="root")
        with mock.patch.dict(os.environ, {"RACK_TEST_VAR": "abc"}, clear=False):
            argv = host_exec.wrap_for_host(["true"])
        assert "export RACK_TEST_VAR=abc" in argv[-1]

    def test_wrap_excludes_unrelated(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="10.0.0.1", RACK_SSH_USER="root")
        argv = host_exec.wrap_for_host(["true"])
        assert "export HOME=" not in argv[-1]
        assert "export PATH=" not in argv[-1]

    def test_wrap_forwards_env_extra(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="10.0.0.1", RACK_SSH_USER="root")
        argv = host_exec.wrap_for_host(
            ["bash", "tool.sh"], env_extra={"NVIS_INSTALL_CUDA_TOOLKIT": "1"}
        )
        assert "export NVIS_INSTALL_CUDA_TOOLKIT=1" in argv[-1]

    def test_env_extra_overrides_environ(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="10.0.0.1", RACK_SSH_USER="root")
        with mock.patch.dict(os.environ, {"NVIS_BUILD_NCCL": "0"}, clear=False):
            argv = host_exec.wrap_for_host(["true"], env_extra={"NVIS_BUILD_NCCL": "1"})
        assert "export NVIS_BUILD_NCCL=1" in argv[-1]
        assert "export NVIS_BUILD_NCCL=0" not in argv[-1]


class TestSshArgvViaFacade:
    def teardown_method(self):
        _reset()

    def test_ssh_requires_host(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="")
        with pytest.raises(RuntimeError, match="SSH host"):
            host_exec.wrap_for_host(["true"])

    def test_ssh_includes_host(self):
        _init(RACK_EXEC_MODE="ssh", RACK_SSH_HOST="10.0.0.1", RACK_SSH_USER="root")
        argv = host_exec.wrap_for_host(["true"])
        assert "ssh" in argv
        assert "root@10.0.0.1" in argv


class TestScriptProfileRouting:
    """Privileged 'script' profile routes to RACK_SCRIPT_SSH_* when configured."""

    def teardown_method(self):
        _reset()

    def _init_targets(self, *, script_host: str) -> None:
        env = {
            "RACK_EXEC_MODE": "ssh",
            "RACK_SSH_HOST": "head.cluster",
            "RACK_SSH_USER": "cmods",
            "RACK_SCRIPT_SSH_USER": "root",
        }
        if script_host:
            env["RACK_SCRIPT_SSH_HOST"] = script_host
        _reset()
        # Clear any inherited script host first so the empty case is honoured.
        with mock.patch.dict(os.environ, env, clear=False):
            if not script_host:
                os.environ.pop("RACK_SCRIPT_SSH_HOST", None)
            host_exec._ensure_init()

    def test_control_profile_uses_head(self):
        self._init_targets(script_host="127.0.0.1")
        argv = host_exec.wrap_for_host(["true"], profile="control")
        assert "cmods@head.cluster" in argv
        assert "root@127.0.0.1" not in argv

    def test_script_profile_uses_privileged_target(self):
        self._init_targets(script_host="127.0.0.1")
        argv = host_exec.wrap_for_host(["true"], profile="script")
        assert "root@127.0.0.1" in argv
        assert "cmods@head.cluster" not in argv

    def test_script_profile_falls_back_to_control_when_unset(self):
        self._init_targets(script_host="")
        argv = host_exec.wrap_for_host(["true"], profile="script")
        assert "cmods@head.cluster" in argv

    def test_script_target_configured_flag(self):
        self._init_targets(script_host="127.0.0.1")
        assert host_exec.script_target_configured() is True
        self._init_targets(script_host="")
        assert host_exec.script_target_configured() is False

    def test_wrap_for_host_script_profile(self):
        self._init_targets(script_host="127.0.0.1")
        argv = host_exec.wrap_for_host(["bash", "sysinfo.sh", "rack01"], profile="script")
        assert "root@127.0.0.1" in argv
        assert argv[-1].endswith("bash sysinfo.sh rack01")
