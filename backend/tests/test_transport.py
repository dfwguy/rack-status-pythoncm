# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Unit tests for the composable transport layer.

The layer is composition-based: an :class:`Executor` owns the mechanism (run a
command, inspect a file) for one target, decorators add policy (env forwarding,
sudo, ssh hops), and a single concrete :class:`Connection` holds whichever
executor was composed. These tests therefore assemble executors directly and
assert on the composed behaviour, rather than on a connection subclass.
"""
import base64
import gzip
import os
import re
import subprocess
from unittest import mock

import pytest
from transport import (
    Connection,
    EnvForwardingExecutor,
    Executor,
    FileStat,
    LocalConnection,
    LocalExecutor,
    RoutingConnection,
    ShellFilesystem,
    SSHConfig,
    SSHExecutor,
    SudoExecutor,
    TransportConfig,
    build_connection,
    fanout,
    fanout_errors,
    group_outputs,
    make_env_resolver,
    merge_env,
)
from transport import factory as factory_mod


class _FakeExecutor(Executor):
    """Records the (ssh) command strings handed to it; returns canned stdout by node."""

    kind = "ssh"

    def __init__(self, outputs=None):
        self.calls: list[str] = []
        self.outputs = outputs or {}

    def run(self, cmd, *, timeout=120, cwd=None, env=None, login_shell=True, env_extra=None):
        self.calls.append(cmd)
        for node, out in self.outputs.items():
            if node in cmd:
                return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None):  # pragma: no cover
        raise NotImplementedError

    # Filesystem: inert. These cases exercise execution and composition only, but
    # the Executor contract is total, so answer neutrally instead of raising.
    def resolve(self, path):
        return str(path)

    def stat(self, path):
        return None

    def stat_dir(self, base, *, patterns=None, maxdepth=1):
        return {}

    def makedirs(self, path):
        return None

    def write_bytes(self, path, data):
        return None

    def read_text(self, path, **kwargs):
        return "", {}

    def which(self, name):
        return None

    def _test_flag(self, path, flag):
        return False


class _FakeBase(Connection):
    """A Connection over a recording executor — what hop/fanout compose against."""

    def __init__(self, outputs=None, executor: Executor | None = None):
        super().__init__(executor if executor is not None else _FakeExecutor(outputs))

    @property
    def calls(self) -> list[str]:
        return self.executor.calls


def _decode_payload(ssh_str: str) -> str:
    m = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64", ssh_str)
    assert m, f"no base64 payload in: {ssh_str}"
    return base64.b64decode(m.group(1)).decode("utf-8")


class TestLocalExecutor:
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

    def test_kind_is_local(self):
        assert LocalConnection().kind == "local"

    def test_resolve_path_local_is_absolute(self, tmp_path):
        rp = LocalConnection().resolve_path(str(tmp_path))
        assert os.path.isabs(rp)


class TestLocalFilesystem:
    """The filesystem half of the Executor contract, on the local leaf."""

    def test_resolve_collapses_dotdot(self, tmp_path):
        messy = str(tmp_path / "sub" / ".." / "file.log")
        assert LocalExecutor().resolve(messy) == str(tmp_path / "file.log")

    def test_stat_returns_size_and_mtime(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("12345")
        st = LocalExecutor().stat(p)
        assert isinstance(st, FileStat) and st.size == 5 and st.mtime > 0

    def test_stat_missing_is_none(self, tmp_path):
        assert LocalExecutor().stat(tmp_path / "nope") is None

    def test_is_file_and_is_dir(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("x")
        ex = LocalExecutor()
        assert ex.is_file(p) is True and ex.is_dir(p) is False
        assert ex.is_dir(tmp_path) is True

    def test_stat_dir_filters_by_pattern(self, tmp_path):
        (tmp_path / "keep.txt").write_text("a")
        (tmp_path / "drop.bin").write_text("b")
        found = LocalExecutor().stat_dir(tmp_path, patterns=("*.txt",))
        assert [os.path.basename(k) for k in found] == ["keep.txt"]

    def test_stat_dir_respects_maxdepth(self, tmp_path):
        (tmp_path / "top.txt").write_text("a")
        nested = tmp_path / "sub"
        nested.mkdir()
        (nested / "deep.txt").write_text("b")
        ex = LocalExecutor()
        shallow = ex.stat_dir(tmp_path, maxdepth=1)
        deep = ex.stat_dir(tmp_path, maxdepth=2)
        assert {os.path.basename(k) for k in shallow} == {"top.txt"}
        assert {os.path.basename(k) for k in deep} == {"top.txt", "deep.txt"}

    def test_read_text_whole_small_file(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("hello")
        text, meta = LocalExecutor().read_text(p)
        assert text == "hello"
        assert meta["bytes"] == 5 and meta["truncated"] is False and meta["target"] == "local"

    def test_read_text_caps_large_file(self, tmp_path):
        p = tmp_path / "big.log"
        p.write_bytes(b"A" * 500 + b"B" * 500)
        text, meta = LocalExecutor().read_text(p, head_bytes=10, tail_bytes=10)
        assert meta["truncated"] is True and meta["bytes"] == 1000
        assert text.startswith("A" * 10) and text.endswith("B" * 10)
        assert "bytes elided" in text

    def test_read_text_gzip(self, tmp_path):
        p = tmp_path / "a.log.gz"
        with gzip.open(p, "wb") as fh:
            fh.write(b"compressed payload")
        text, meta = LocalExecutor().read_text(p)
        assert text == "compressed payload" and meta["gzip"] is True

    def test_read_text_missing_reports_error(self, tmp_path):
        text, meta = LocalExecutor().read_text(tmp_path / "nope")
        assert text == "" and meta["read_error"]

    def test_which(self):
        ex = LocalExecutor()
        assert ex.which("sh")
        assert ex.which("nvis-definitely-not-installed") is None

    def test_makedirs_creates_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        ex = LocalExecutor()
        assert ex.makedirs(target) is None
        assert ex.is_dir(target) is True


class _ScriptedExecutor(ShellFilesystem, Executor):
    """ShellFilesystem over a canned ``run`` — asserts the commands it builds."""

    kind = "ssh"

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.responses = list(responses or [])

    def run(self, cmd, *, timeout=120, cwd=None, env=None, login_shell=True, env_extra=None):
        self.calls.append(cmd)
        if self.responses:
            rc, out, err = self.responses.pop(0)
            return subprocess.CompletedProcess(cmd, rc, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None):  # pragma: no cover
        raise NotImplementedError


class TestShellFilesystem:
    """Remote filesystem access, expressed as shell commands."""

    def test_resolve_is_textual_and_offline(self):
        ex = _ScriptedExecutor()
        # Must not shell out: this runs on nearly every request, and a local
        # realpath() would rewrite the path through the container's filesystem.
        assert ex.resolve("/data/logs/../logs/a.txt") == "/data/logs/a.txt"
        assert ex.calls == []

    def test_stat_parses_mtime_and_size(self):
        ex = _ScriptedExecutor([(0, "1700000000 4096\n", "")])
        st = ex.stat("/var/log/a.log")
        assert st == FileStat(mtime=1700000000.0, size=4096)
        assert ex.calls[0][:3] == ["stat", "-c", "%Y %s"]

    def test_stat_missing_is_none(self):
        assert _ScriptedExecutor([(1, "", "No such file")]).stat("/nope") is None

    def test_stat_garbage_output_is_none(self):
        assert _ScriptedExecutor([(0, "not-a-stat\n", "")]).stat("/x") is None

    def test_stat_dir_parses_find_printf(self):
        out = "/logs/a.txt\t10\t1700000000\n/logs/b.txt\t20\t1700000001\n"
        ex = _ScriptedExecutor([(0, out, "")])
        found = ex.stat_dir("/logs", patterns=("*.txt",), maxdepth=2)
        assert found["/logs/a.txt"] == FileStat(mtime=1700000000.0, size=10)
        assert found["/logs/b.txt"].size == 20
        argv = ex.calls[0]
        assert argv[:2] == ["find", "/logs"]
        assert "-maxdepth" in argv and argv[argv.index("-maxdepth") + 1] == "2"
        assert "-name" in argv and "*.txt" in argv

    def test_stat_dir_unreachable_is_empty(self):
        assert _ScriptedExecutor([(255, "", "ssh: connect failed")]).stat_dir("/logs") == {}

    def test_read_text_decodes_base64_payload(self):
        payload = b"hello from the head node"
        blob = base64.b64encode(payload).decode()
        ex = _ScriptedExecutor([(0, f"{len(payload)}\n{blob}", "")])
        text, meta = ex.read_text("/var/log/a.log")
        assert text == "hello from the head node"
        assert meta["bytes"] == len(payload)
        assert meta["truncated"] is False and meta["target"] == "ssh"

    def test_read_text_flags_truncation(self):
        blob = base64.b64encode(b"partial").decode()
        ex = _ScriptedExecutor([(0, f"999999\n{blob}", "")])
        _text, meta = ex.read_text("/var/log/big.log", head_bytes=10, tail_bytes=10)
        assert meta["truncated"] is True and meta["bytes"] == 999999

    def test_read_text_missing_reports_error(self):
        ex = _ScriptedExecutor([(3, "-1\n", "stat: No such file")])
        text, meta = ex.read_text("/nope")
        assert text == "" and "No such file" in str(meta["read_error"])

    def test_read_text_uses_zcat_for_gzip(self):
        blob = base64.b64encode(b"x").decode()
        ex = _ScriptedExecutor([(0, f"1\n{blob}", "")])
        _text, meta = ex.read_text("/var/log/a.log.gz")
        assert meta["gzip"] is True
        assert "zcat" in " ".join(ex.calls[0])

    def test_which_returns_first_line(self):
        ex = _ScriptedExecutor([(0, "/usr/bin/cmsh\n", "")])
        assert ex.which("cmsh") == "/usr/bin/cmsh"
        assert ex.calls[0] == ["command", "-v", "cmsh"]

    def test_which_absent_is_none(self):
        assert _ScriptedExecutor([(1, "", "")]).which("cmsh") is None

    def test_is_dir_uses_test_flag(self):
        ex = _ScriptedExecutor([(0, "", "")])
        assert ex.is_dir("/data") is True
        assert ex.calls[0] == ["test", "-d", "/data"]

    def test_makedirs_reports_error(self):
        ex = _ScriptedExecutor([(1, "", "mkdir: Permission denied")])
        assert "Permission denied" in str(ex.makedirs("/root/x"))

    def test_makedirs_success_is_none(self):
        ex = _ScriptedExecutor([(0, "", "")])
        assert ex.makedirs("/tmp/x") is None
        assert ex.calls[0] == ["mkdir", "-p", "/tmp/x"]


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


class TestSSHExecutor:
    def _exec(self):
        return SSHExecutor(SSHConfig(host="10.0.0.1", user="root", key="/nonexistent", port="22"))

    def test_ssh_argv_includes_host(self):
        argv = self._exec().ssh_argv()
        assert argv[0] == "ssh" and "root@10.0.0.1" in argv

    def test_ssh_argv_requires_host(self):
        with pytest.raises(RuntimeError, match="SSH host"):
            SSHExecutor(SSHConfig(host="")).ssh_argv()

    def test_remote_cmd_env_prefix_before_cd(self):
        argv = self._exec().wrap_argv("run.sh", cwd="/tmp/x", env_extra={"A": "b"})
        remote = argv[-1]
        assert remote.startswith("export A=b; ")
        assert "cd /tmp/x && run.sh" in remote

    def test_connect_timeout_override(self):
        argv = self._exec().ssh_argv(connect_timeout="3")
        assert "ConnectTimeout=3" in argv

    def test_kind_is_ssh(self):
        assert self._exec().kind == "ssh"


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
        ex = EnvForwardingExecutor(SSHExecutor(SSHConfig(host="h", user="u")), resolver)
        with mock.patch.dict(os.environ, {"RACK_Y": "z"}, clear=False):
            argv = ex.wrap_argv("cmd")
        assert "export RACK_Y=z" in argv[-1]

    def test_filesystem_passes_through_to_inner(self, tmp_path):
        p = tmp_path / "a.log"
        p.write_text("data")
        # Env forwarding changes how a command runs, never which host owns files.
        ex = EnvForwardingExecutor(LocalExecutor(), make_env_resolver(("RACK_",), ()))
        assert ex.is_file(p) is True
        assert ex.stat(p).size == 4
        assert ex.kind == "local"


class TestSudoExecutor:
    def test_wrap_list_non_interactive(self):
        ex = SudoExecutor(LocalExecutor())
        argv = ex.wrap_argv(["bash", "sysinfo.sh", "rack01"])
        assert argv[:3] == ["sudo", "-n", "-E"]
        assert argv[3:] == ["bash", "sysinfo.sh", "rack01"]

    def test_no_preserve_env(self):
        ex = SudoExecutor(LocalExecutor(), preserve_env=False)
        argv = ex.wrap_argv(["id"])
        assert "-E" not in argv and argv[:2] == ["sudo", "-n"]

    def test_password_mode_uses_stdin(self):
        ex = SudoExecutor(SSHExecutor(SSHConfig(host="h", user="cmods")), password="s3cret")
        argv = ex.wrap_argv(["bash", "sysinfo.sh"])
        remote = argv[-1]
        assert "sudo -S" in remote and "s3cret" in remote

    def test_sudo_over_ssh_prepends_before_cmd_after_cd(self):
        # export…; cd …; sudo -n -E <cmd>
        inner = SSHExecutor(SSHConfig(host="h", user="cmods"))
        ex = SudoExecutor(inner)
        remote = ex.wrap_argv("sysinfo.sh", cwd="/work")[-1]
        assert "cd /work && sudo -n -E sysinfo.sh" in remote

    def test_filesystem_passes_through_to_inner(self, tmp_path):
        # Escalation is about privilege to run, not about which host owns files.
        ex = SudoExecutor(LocalExecutor())
        assert ex.is_dir(tmp_path) is True
        assert ex.kind == "local"


class TestRoutingConnection:
    def test_select_and_default(self):
        a, b = LocalConnection(), LocalConnection()
        r = RoutingConnection({"control": a, "script": b}, default="control")
        assert r.select("script") is b
        assert r.select("control") is a
        assert r.select("unknown") is a  # falls back to default

    def test_rejects_unknown_default(self):
        with pytest.raises(ValueError, match="default profile"):
            RoutingConnection({"control": LocalConnection()}, default="script")

    def test_is_a_connection_over_the_default_profile(self, tmp_path):
        r = RoutingConnection({"control": LocalConnection()}, default="control")
        # Filesystem access always belongs to the control target.
        assert r.kind == "local"
        assert r.is_dir(tmp_path) is True


class TestExecModeDeprecation:
    """``RACK_EXEC_MODE`` is on the way out; SSH is the default."""

    def setup_method(self):
        # The notice is emitted once per process; re-arm it for each case.
        factory_mod._warned_deprecated = False

    def test_unset_defaults_to_ssh(self):
        assert TransportConfig.from_env({}).exec_mode == "ssh"
        assert TransportConfig().exec_mode == "ssh"

    def test_explicit_mode_still_honoured(self):
        assert TransportConfig.from_env({"RACK_EXEC_MODE": "local"}).exec_mode == "local"

    def test_unknown_mode_falls_back_to_ssh(self, caplog):
        # Previously an unrecognised value silently ran commands inside the container.
        with caplog.at_level("ERROR", logger="transport.factory"):
            cfg = TransportConfig.from_env({"RACK_EXEC_MODE": "hybrid"})
        assert cfg.exec_mode == "ssh"
        assert "hybrid" in caplog.text

    def test_warns_once(self, caplog):
        with caplog.at_level("WARNING", logger="transport.factory"):
            TransportConfig.from_env({"RACK_EXEC_MODE": "local"})
            TransportConfig.from_env({"RACK_EXEC_MODE": "local"})
        assert caplog.text.count("DEPRECATED") == 1


class TestBuildConnection:
    def test_local(self):
        conn = build_connection(TransportConfig(exec_mode="local"))
        assert conn.kind == "local"
        assert conn.run("echo z", timeout=5).stdout.strip() == "z"

    def test_ssh_composition(self):
        cfg = TransportConfig(exec_mode="ssh", control=SSHConfig(host="10.0.0.1", user="root"))
        conn = build_connection(cfg)
        assert conn.kind == "ssh"
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


class TestHopChaining:
    def test_hop_runs_ssh_through_base_and_preserves_command(self):
        base = _FakeBase()
        base.hop("node1", user="cmods").run("nvidia-smi -L")
        ssh_str = base.calls[0]
        assert ssh_str.startswith("ssh ") and "cmods@node1" in ssh_str
        assert _decode_payload(ssh_str) == "nvidia-smi -L"

    def test_hop_kind_is_ssh(self):
        assert _FakeBase().hop("n").kind == "ssh"

    def test_hop_applies_cwd_and_env(self):
        base = _FakeBase()
        base.hop("n").run("do-it", cwd="/w", env_extra={"K": "v"})
        payload = _decode_payload(base.calls[0])
        assert "export K=v" in payload and "cd /w && do-it" in payload

    def test_nested_hop_two_legs(self):
        # control.hop(head).hop(node): base receives ONE `ssh head ...`; its payload
        # is an `ssh node ...`; whose payload is the original command.
        base = _FakeBase()
        base.hop("head").hop("node").run("hostname")
        assert len(base.calls) == 1
        head_payload = _decode_payload(base.calls[0])
        assert head_payload.startswith("ssh ") and "node" in head_payload
        assert _decode_payload(head_payload) == "hostname"

    def test_hop_filesystem_targets_the_hopped_host(self):
        # The hop owns the files too: a stat must travel the extra leg, not be
        # answered by the base host.
        base = _FakeBase()
        base.hop("node1").stat("/var/log/a.log")
        assert "node1" in base.calls[0]
        assert _decode_payload(base.calls[0]).startswith("stat -c")


class TestFanout:
    def test_runs_all_nodes_and_groups(self):
        base = _FakeBase(outputs={"n1": "same\n", "n2": "same\n", "n3": "diff\n"})
        results = fanout(base, ["n1", "n2", "n3"], "echo x")
        assert {r.node for r in results} == {"n1", "n2", "n3"}
        grouped = group_outputs(results)
        assert "n1,n2" in grouped and "same" in grouped and "diff" in grouped

    def test_silent_nodes_omitted(self):
        base = _FakeBase(outputs={"n1": "", "n2": ""})
        results = fanout(base, ["n1", "n2"], "true")
        assert group_outputs(results) == ""  # "expect no output" stays empty

    def test_errors_captured_separately_from_stdout(self):
        class _BoomExecutor(_FakeExecutor):
            def run(self, cmd, **k):
                if "bad" in cmd:
                    raise TimeoutError("boom")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

        results = fanout(_FakeBase(executor=_BoomExecutor()), ["good", "bad"], "x")
        errs = fanout_errors(results)
        assert "bad:" in errs and "boom" in errs
        assert "ok" in group_outputs(results)
        assert next(r.returncode for r in results if r.node == "bad") == 255

    def test_retries_silent_ssh_255_until_command_starts(self):
        class _Silent255ThenOk(_FakeExecutor):
            def run(self, cmd, **k):
                self.calls.append(cmd)
                if len(self.calls) < 3:
                    return subprocess.CompletedProcess(cmd, 255, "", "")
                return subprocess.CompletedProcess(cmd, 0, "ready\n", "")

        base = _FakeBase(executor=_Silent255ThenOk())
        results = fanout(base, ["n1"], "audit", retries=3)
        assert len(base.calls) == 3
        assert results[0].returncode == 0
        assert results[0].stdout == "ready\n"

    def test_empty_nodes(self):
        assert fanout(_FakeBase(), [], "x") == []

    def test_per_node_sudo_wraps_payload_runner(self):
        base = _FakeBase()
        fanout(base, ["n1"], "dmidecode -t memory", sudo=True)
        # calls[0] is the ControlMaster warm-up ("true"); the sudo hop is a later call
        assert any("sudo -n -E bash" in c for c in base.calls)

    def test_warms_control_master_before_parallel(self):
        base = _FakeBase()
        fanout(base, ["n1", "n2", "n3"], "echo x")
        # first call is the serial warm-up (`true`), before the per-node hops
        assert base.calls[0].strip() == "true"

    def test_no_warmup_for_single_node(self):
        base = _FakeBase()
        fanout(base, ["only"], "echo x")
        assert all(c.strip() != "true" for c in base.calls)


class TestSSHMultiplex:
    def test_control_master_opts_present_by_default(self):
        argv = SSHExecutor(SSHConfig(host="h", user="u")).ssh_argv()
        joined = " ".join(argv)
        assert "ControlMaster=auto" in joined and "ControlPath=" in joined and "ControlPersist=" in joined

    def test_multiplex_can_be_disabled(self):
        argv = SSHExecutor(SSHConfig(host="h", user="u", multiplex=False)).ssh_argv()
        assert "ControlMaster=auto" not in " ".join(argv)

    def test_from_env_opt_out(self):
        with mock.patch.dict(os.environ, {"RACK_EXEC_MODE": "ssh", "RACK_SSH_HOST": "h",
                                          "RACK_SSH_MULTIPLEX": "0"}, clear=False):
            cfg = TransportConfig.from_env()
        assert cfg.control.multiplex is False


class TestHopSudo:
    def test_sudo_runs_payload_under_sudo(self):
        base = _FakeBase()
        base.hop("n", sudo=True).run("ibstatus | grep state")
        assert "sudo -n -E bash" in base.calls[0]
        assert _decode_payload(base.calls[0]) == "ibstatus | grep state"

    def test_no_sudo_plain_bash(self):
        base = _FakeBase()
        base.hop("n").run("echo hi")
        assert "sudo" not in base.calls[0] and "base64 -d | bash" in base.calls[0]


class TestBuildChain:
    def test_folds_ssh_hops_into_nested_connection(self):
        from transport import build_chain
        base = _FakeBase()
        conn = build_chain(
            [{"kind": "ssh", "host": "head", "user": "u"},
             {"kind": "ssh", "host": "node", "user": "c", "sudo": True}],
            base=base,
        )
        conn.run("hostname")
        head_payload = _decode_payload(base.calls[0])
        assert "head" in base.calls[0] and head_payload.startswith("ssh ")
        assert "node" in head_payload and "sudo -n -E bash" in head_payload
        assert _decode_payload(head_payload) == "hostname"

    def test_local_hop_is_passthrough(self):
        from transport import build_chain
        base = _FakeBase()
        conn = build_chain([{"kind": "local"}], base=base)
        assert conn is base

    def test_vpn_not_implemented(self):
        from transport import build_chain
        with pytest.raises(NotImplementedError):
            build_chain([{"kind": "vpn", "host": "x"}], base=_FakeBase())

    def test_hopspec_from_dict(self):
        from transport import HopSpec
        s = HopSpec.from_dict({"kind": "SSH", "host": "h", "sudo": True})
        assert s.kind == "ssh" and s.host == "h" and s.sudo is True
