# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""How Rack Status runs commands on compute nodes is a property of the cluster.

On Nscale NC (2026-10-10) a services restart for a node with "GPU requires reset" reported done
with rc 0 and changed nothing: Rack Status connected to the head as an operator account, pdsh used
its rsh default (``rcmd: socket: Permission denied`` on every node), the command would have needed
root anyway, and pdsh's exit code hid all of it. Two cluster switches now decide the route --
straight from the app server or through the head node (``connection.node_exec_direct``) -- and
whether node commands run under ``sudo -n`` (``connection.node_sudo``); both default to the
original behaviour. Node actions count success per node, and a per-node Slurm reboot joins drain
and resume.
"""
from __future__ import annotations

import os
import stat
import subprocess
import types

import host_exec
import pytest
from api import rack_power
from catalog import context, models
from platforms.bcm import BCMProvider
from transport.compose import SudoExecutor
from transport.fanout import NodeResult

NODE = "sys1-gso1-p-phy-gpu105-su21"


def _cluster(**connection):
    return models.Cluster.from_dict({"id": "c1", "name": "C1", "connection": connection})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("RACK_NODE_SUDO", "RACK_DIRECT_NODE_SSH", "RACK_NODE_SSH_USER", "RACK_NODE_SSH_KEY",
              "RACK_GPU_RESET_SCRIPT", "RACK_GPU_RESET_DRY_RUN", "RACK_SERVICES_RESTART_DRY_RUN",
              "RACK_NODE_REBOOT_DRY_RUN", "RACK_NODE_REBOOT_DISABLED"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- the settings


class TestTheClusterSwitches:
    def test_both_default_to_the_original_behaviour(self):
        c = _cluster().connection
        assert (c.node_sudo, c.node_exec_direct) == (False, False)

    def test_they_round_trip(self):
        c = _cluster(node_sudo=True, node_exec_direct=True)
        d = c.to_dict()["connection"]
        assert (d["node_sudo"], d["node_exec_direct"]) == (True, True)

    def test_the_overlay_publishes_them(self):
        env = context.high_overlay(None, _cluster(node_sudo=True, node_exec_direct=True), None)
        assert (env["RACK_NODE_SUDO"], env["RACK_DIRECT_NODE_SSH"]) == ("1", "1")

    def test_off_publishes_nothing(self):
        env = context.high_overlay(None, _cluster(), None)
        assert "RACK_NODE_SUDO" not in env and "RACK_DIRECT_NODE_SSH" not in env


# ---------------------------------------------------------------- the route to a node


class _Ctrl:
    user, key, port, connect_timeout = "rkoike", "/run/secrets/ssh_key", "22", "15"


def test_a_direct_leg_uses_the_connection_account_and_its_key(monkeypatch):
    monkeypatch.setattr(host_exec, "_ensure_init", lambda: None)
    monkeypatch.setattr(host_exec, "_CONFIG", types.SimpleNamespace(control=_Ctrl()))
    monkeypatch.setenv("RACK_DIRECT_NODE_SSH", "1")
    conn = host_exec.node_connection(NODE)
    cfg = conn.executor.config
    assert (cfg.host, cfg.user, cfg.key) == (NODE, "rkoike", "/run/secrets/ssh_key")
    assert not isinstance(conn.executor, SudoExecutor)


def test_a_direct_leg_with_sudo_wraps_every_command(monkeypatch):
    monkeypatch.setattr(host_exec, "_ensure_init", lambda: None)
    monkeypatch.setattr(host_exec, "_CONFIG", types.SimpleNamespace(control=_Ctrl()))
    monkeypatch.setenv("RACK_DIRECT_NODE_SSH", "1")
    conn = host_exec.node_connection(NODE, sudo=True)
    assert isinstance(conn.executor, SudoExecutor)
    assert conn.executor._wrap(["bash", "-c", "a; b"]) == ["sudo", "-n", "-E", "bash", "-c", "a; b"]


def test_a_relayed_leg_asks_the_head_hop_for_sudo(monkeypatch):
    seen = {}

    class _Head:
        def select(self, _role):
            return self

        def hop(self, node, **kw):
            seen.update(node=node, **kw)
            return "hop"

    monkeypatch.setattr(host_exec, "_ensure_init", lambda: None)
    monkeypatch.setattr(host_exec, "_CONFIG", types.SimpleNamespace(control=_Ctrl()))
    monkeypatch.setattr(host_exec, "_conn", lambda: _Head())
    assert host_exec.node_connection(NODE, sudo=True) == "hop"
    assert seen["node"] == NODE and seen["sudo"] is True


def test_run_on_nodes_runs_the_whole_command_under_the_setting(monkeypatch):
    calls = []

    def fake_fanout(connect, nodes, cmd, **kw):
        calls.append((nodes, cmd, kw["warm"]))
        return [NodeResult(n, "ok", "", 0) for n in nodes]

    monkeypatch.setattr(host_exec, "fanout_via", fake_fanout)
    monkeypatch.setenv("RACK_DIRECT_NODE_SSH", "1")
    out = host_exec.run_on_nodes([NODE, NODE, ""], "systemctl restart a; nvidia-smi -r")
    assert [r.node for r in out] == [NODE]
    assert calls == [([NODE], ["bash", "-c", "systemctl restart a; nvidia-smi -r"], None)]


def test_a_node_that_fails_is_never_folded_into_success(monkeypatch):
    monkeypatch.setenv("RACK_NODE_SUDO", "1")
    results = [
        NodeResult("a", "restarted\n", "", 0),
        NodeResult("b", "", "rcmd: socket: Permission denied", 255),
    ]
    s = host_exec.node_results_summary(results, "true")
    assert s["returncode"] == 1 and s["failed_nodes"] == ["b"] and s["nodes_ok"] == ["a"]
    assert s["stdout"] == "a: restarted" and s["stderr"] == "b: rcmd: socket: Permission denied"
    assert s["command_display"].startswith("[via head node, sudo -n] on 2 node(s)")
    assert host_exec.node_results_summary([], "true")["returncode"] == 1, "no node reached is not success"


# ---------------------------------------------------------------- the node actions


def test_services_restart_reports_the_nodes_it_could_not_reach(monkeypatch):
    monkeypatch.setattr(rack_power, "_expand_pdsh_w_slurm_nodenames", lambda w: w)
    monkeypatch.setattr(host_exec, "run_on_nodes",
                        lambda hosts, cmd, **kw: [NodeResult(h, "", "Permission denied", 255) for h in hosts])
    jid = "job-1"
    rack_power._reset_jobs[jid] = {"job_id": jid, "phase": "starting"}
    monkeypatch.setattr(rack_power, "_lake_push_job", lambda *_a: None)
    monkeypatch.setattr(rack_power, "_reset_job_purge_later", lambda *_a: None)
    rack_power._services_restart_worker(jid, f"{NODE},sys1-gso1-p-phy-gpu30-su22")
    row = rack_power._reset_jobs.pop(jid)
    assert row["phase"] == "error" and row["ok"] is False
    assert row["failed_nodes"] == [NODE, "sys1-gso1-p-phy-gpu30-su22"]
    assert row["error"].startswith("services restart failed on 2 node(s)")


def test_gpu_reset_runs_the_platform_command_on_each_node(monkeypatch):
    monkeypatch.setattr(rack_power, "_expand_pdsh_w_slurm_nodenames", lambda w: w)
    seen = {}

    def fake(hosts, cmd, **kw):
        seen.update(hosts=hosts, cmd=cmd)
        return [NodeResult(h, "GPU reset", "", 0) for h in hosts]

    monkeypatch.setattr(host_exec, "run_on_nodes", fake)
    out = rack_power.run_gpu_reset(NODE)
    assert out["returncode"] == 0 and seen["hosts"] == [NODE]
    assert "nvidia-smi -r" in seen["cmd"] and "nvidia-persistenced" in seen["cmd"]


def test_a_site_gpu_reset_script_still_runs_as_before(monkeypatch, tmp_path):
    script = tmp_path / "my_reset.sh"
    script.write_text("#!/bin/bash\n")
    monkeypatch.setenv("RACK_GPU_RESET_SCRIPT", str(script))
    monkeypatch.setattr(rack_power, "_expand_pdsh_w_slurm_nodenames", lambda w: w)
    monkeypatch.setattr(rack_power, "_script_exists", lambda _p: True)
    monkeypatch.setattr(host_exec, "run_on_nodes", lambda *a, **k: pytest.fail("override must win"))
    ran = []
    monkeypatch.setattr(host_exec, "run_on_host",
                        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert rack_power.run_gpu_reset(NODE)["returncode"] == 0
    assert ran and ran[0][-1] == NODE and ran[0][1].endswith("my_reset.sh")


def test_pdsh_always_uses_ssh():
    assert BCMProvider().pdsh_cmd("n[1-2]", "uptime", fanout=4)[:5] == ["pdsh", "-R", "ssh", "-f", "4"]


def test_the_gpu_reset_command_stops_only_what_runs_and_restarts_it(tmp_path):
    """Run the real command against fake systemctl / nvidia-smi and read what it did."""
    log = tmp_path / "log"
    # nvidia-dcgm is mid-restart: the state that let its nv-hostengine hold the GPUs on NC.
    states = {"cmd": "active", "slurmd": "active", "nvidia-dcgm": "activating", "dcgm-exporter": "active",
              "nvidia-imex": "active", "nvidia-persistenced": "active"}
    running = set(states)
    cases = " ".join(f'{u}) echo {st}; exit 0;;' for u, st in states.items())
    (tmp_path / "systemctl").write_text(
        "#!/bin/bash\n"
        f'echo "systemctl $*" >> {log}\n'
        f'if [ "$1" = is-active ]; then case "$2" in {cases} *) echo inactive; exit 3;; esac; fi\n'
    )
    (tmp_path / "nvidia-smi").write_text(f'#!/bin/bash\necho "nvidia-smi $*" >> {log}\nexit 0\n')
    (tmp_path / "sleep").write_text("#!/bin/bash\n")
    for f in ("systemctl", "nvidia-smi", "sleep"):
        (tmp_path / f).chmod((tmp_path / f).stat().st_mode | stat.S_IEXEC)

    cmd = BCMProvider().gpu_reset_remote_cmd()
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=30, check=False)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in log.read_text().splitlines() if not ln.startswith("systemctl is-active")]
    stop = lines[0].split()[2:]
    assert lines[0].startswith("systemctl stop") and set(stop) == running, "cuda-dcgm is not up here; a restarting DCGM is"
    assert lines[1] == "nvidia-smi -r"
    started = [ln.split()[2] for ln in lines[2:]]
    assert set(started) == running and started[0] == "nvidia-persistenced"


# ---------------------------------------------------------------- the reboot


def test_the_reboot_is_slurms_own_and_returns_the_node_to_service(monkeypatch):
    import slurm_admin

    ran = []
    monkeypatch.setattr(slurm_admin.host_exec, "run_slurm_on_host",
                        lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    out = slurm_admin._run_scontrol_reboot(NODE, "nvis-nvlink-fix: reboot")
    assert out["ok"] is True
    assert "scontrol reboot ASAP nextstate=RESUME 'reason=nvis-nvlink-fix: reboot' " + NODE in ran[0]


def test_the_reboot_route_targets_the_named_nodes(monkeypatch):
    import app as app_module
    from api import slurm_ops

    seen = {}
    monkeypatch.setattr(slurm_ops, "validation_target_for_rack", lambda rack, ov: ov)
    monkeypatch.setattr(slurm_ops, "_run_scontrol_reboot",
                        lambda target, reason: seen.update(target=target, reason=reason) or {"ok": True, "returncode": 0})
    r = app_module.app.test_client().post("/api/scheduler-reboot",
                                          json={"target": NODE, "reason": "nvis-nvlink-fix: reboot"})
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert seen == {"target": NODE, "reason": "nvis-nvlink-fix: reboot"}


# ---------------------------------------------------------------- the node-side reboot


def _reboot_rig(monkeypatch, *, busy_out="", squeue_rc=0):
    import slurm_admin

    calls = {"scontrol": [], "nodes": []}
    monkeypatch.setattr(rack_power, "_expand_pdsh_w_slurm_nodenames", lambda w: w)
    monkeypatch.setattr(host_exec, "run_slurm_on_host",
                        lambda cmd, **kw: types.SimpleNamespace(returncode=squeue_rc, stdout=busy_out, stderr="boom"))
    monkeypatch.setattr(slurm_admin, "_run_scontrol",
                        lambda args: calls["scontrol"].append(args) or {"ok": True})

    def fake_nodes(hosts, cmd, **kw):
        calls["nodes"].append((hosts, cmd))
        return [NodeResult(h, "", "", 0) for h in hosts]

    monkeypatch.setattr(host_exec, "run_on_nodes", fake_nodes)
    return calls


def test_a_node_slurm_cannot_reboot_is_rebooted_from_the_node(monkeypatch):
    calls = _reboot_rig(monkeypatch)
    out = rack_power.run_node_reboot(NODE)
    assert out["returncode"] == 0 and out["skipped_nodes"] == []
    assert calls["scontrol"] == [["cancel_reboot", NODE]], "a pending Slurm reboot must not fire a second time"
    assert calls["nodes"] == [([NODE], "systemctl --no-block reboot")]


def test_a_node_running_a_job_is_never_rebooted(monkeypatch):
    other = "sys1-gso1-p-phy-gpu30-su22"
    calls = _reboot_rig(monkeypatch, busy_out="sys1-gso1-p-phy-gpu30-su22\n")
    out = rack_power.run_node_reboot(f"{NODE},{other}")
    assert calls["nodes"] == [([NODE], "systemctl --no-block reboot")]
    assert out["skipped_nodes"] == [other] and out["returncode"] == 1
    assert "skipped 1 node(s) running a job" in rack_power._node_job_error("node reboot", out)


def test_when_slurm_cannot_be_asked_nothing_is_rebooted(monkeypatch):
    calls = _reboot_rig(monkeypatch, squeue_rc=1)
    with pytest.raises(OSError, match="squeue failed"):
        rack_power.run_node_reboot(NODE)
    assert calls["nodes"] == [] and calls["scontrol"] == []


def test_the_reboot_route_takes_hostnames_only(monkeypatch):
    import app as app_module

    started = []
    monkeypatch.setattr(rack_power, "_node_reboot_worker", lambda jid, w: started.append(w))
    client = app_module.app.test_client()
    r = client.post("/api/node-reboot-async", json={"specs": [NODE]})
    if r.status_code in (401, 403):
        pytest.skip("route requires a session in this app configuration")
    assert r.status_code == 200 and r.get_json()["kind"] == "node-reboot"
    assert client.post("/api/node-reboot-async", json={"specs": ["a;reboot"]}).status_code == 400
    assert client.post("/api/node-reboot-async", json={"specs": []}).status_code == 400
