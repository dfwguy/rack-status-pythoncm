"""Tests for host_exec module."""
import os
import subprocess
from unittest import mock

import pytest

os.environ.setdefault("RACK_EXEC_MODE", "local")

import host_exec


class TestIsRemote:
    def test_local_mode(self):
        with mock.patch.dict(os.environ, {"RACK_EXEC_MODE": "local"}, clear=False):
            host_exec._EXEC_MODE = None
            host_exec._ensure_init()
            assert host_exec.is_remote() is False

    def test_ssh_mode(self):
        with mock.patch.dict(os.environ, {"RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "10.0.0.1"}, clear=False):
            host_exec._EXEC_MODE = None
            host_exec._ensure_init()
            assert host_exec.is_remote() is True


class TestRunOnHostLocal:
    def setup_method(self):
        host_exec._EXEC_MODE = "local"

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
        host_exec._EXEC_MODE = "local"

    def test_popen_communicate(self):
        hp = host_exec.popen_on_host("echo popen_test")
        out, err = hp.communicate(timeout=5)
        assert hp.returncode == 0
        assert "popen_test" in out

    def test_popen_kill(self):
        hp = host_exec.popen_on_host("sleep 60")
        hp.kill()
        hp.communicate(timeout=5)
        assert hp.returncode is not None


class TestEnvForwarding:
    def test_build_env_exports_includes_rack_vars(self):
        with mock.patch.dict(os.environ, {"RACK_TEST_VAR": "abc", "NEMO_SQSH_SRC": "/path/to/sqsh"}, clear=False):
            exports = host_exec._build_env_exports()
            assert "RACK_TEST_VAR" in exports
            assert "NEMO_SQSH_SRC" in exports
            assert "abc" in exports

    def test_build_env_exports_excludes_unrelated(self):
        with mock.patch.dict(os.environ, {"HOME": "/root", "PATH": "/usr/bin"}, clear=False):
            exports = host_exec._build_env_exports()
            assert "HOME=" not in exports
            assert "PATH=" not in exports


class TestSshArgvBuilding:
    def test_ssh_base_argv_requires_host(self):
        host_exec._EXEC_MODE = "ssh"
        host_exec._SSH_HOST = ""
        with pytest.raises(RuntimeError, match="RACK_SSH_HOST"):
            host_exec._ssh_base_argv()

    def test_ssh_base_argv_includes_host(self):
        host_exec._EXEC_MODE = "ssh"
        host_exec._SSH_HOST = "10.0.0.1"
        host_exec._SSH_USER = "root"
        host_exec._SSH_PORT = "22"
        host_exec._SSH_KEY = "/nonexistent"
        host_exec._SSH_EXTRA_OPTS = []
        argv = host_exec._ssh_base_argv()
        assert "ssh" in argv
        assert "root@10.0.0.1" in argv
