"""Unit tests for the composable transport layer."""
import base64
import os
import re
import subprocess
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
    fanout,
    fanout_errors,
    group_outputs,
    make_env_resolver,
    merge_env,
)
from transport.base import Connection


class _FakeBase(Connection):
    """Records the (ssh) command strings handed to it; returns canned stdout by node."""

    def __init__(self, outputs=None):
        self.calls: list[str] = []
        self.outputs = outputs or {}

    def is_remote(self) -> bool:
        return True

    def run(self, cmd, *, timeout=120, cwd=None, env=None, login_shell=True, env_extra=None):
        self.calls.append(cmd)
        for node, out in self.outputs.items():
            if node in cmd:
                return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def popen(self, cmd, *, cwd=None, env=None, env_extra=None):  # pragma: no cover
        raise NotImplementedError


def _decode_payload(ssh_str: str) -> str:
    m = re.search(r"printf %s ([A-Za-z0-9+/=]+) \| base64", ssh_str)
    assert m, f"no base64 payload in: {ssh_str}"
    return base64.b64decode(m.group(1)).decode("utf-8")


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


class TestHopChaining:
    def test_hop_runs_ssh_through_base_and_preserves_command(self):
        base = _FakeBase()
        base.hop("node1", user="cmods").run("nvidia-smi -L")
        ssh_str = base.calls[0]
        assert ssh_str.startswith("ssh ") and "cmods@node1" in ssh_str
        assert _decode_payload(ssh_str) == "nvidia-smi -L"

    def test_hop_is_remote(self):
        assert _FakeBase().hop("n").is_remote() is True

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
        class Boom(_FakeBase):
            def run(self, cmd, **k):
                if "bad" in cmd:
                    raise TimeoutError("boom")
                return subprocess.CompletedProcess(cmd, 0, "ok\n", "")

        results = fanout(Boom(), ["good", "bad"], "x")
        errs = fanout_errors(results)
        assert "bad:" in errs and "boom" in errs
        assert "ok" in group_outputs(results)
        assert next(r.returncode for r in results if r.node == "bad") == 255

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
        argv = SSHConnection(SSHConfig(host="h", user="u")).ssh_argv()
        joined = " ".join(argv)
        assert "ControlMaster=auto" in joined and "ControlPath=" in joined and "ControlPersist=" in joined

    def test_multiplex_can_be_disabled(self):
        argv = SSHConnection(SSHConfig(host="h", user="u", multiplex=False)).ssh_argv()
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
