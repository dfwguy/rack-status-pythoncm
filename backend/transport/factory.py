"""Assemble a composite :class:`Connection` from config (env or catalog)."""
from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass

from transport.base import Connection
from transport.compose import (
    EnvForwardingConnection,
    RoutingConnection,
    SudoConnection,
    make_env_resolver,
)
from transport.local import LocalConnection
from transport.ssh import SSHConfig, SSHConnection


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")

# Historical forwarding allowlist (moved from host_exec so the transport layer
# is self-contained). Vars matching a prefix, or named exactly, are exported to
# the remote host so cluster jobs see the active generation/site environment.
FORWARD_ENV_PREFIXES = ("RACK_", "NEMO_", "VALIDATION_", "SQUEUE_", "NVIS_", "PDSH_", "NGC_", "LLMB_")
FORWARD_ENV_NAMES = (
    "CUDA_MAJ",
    "GPU_MODEL_FLAG",
    "MNNVL_TOPOLOGY",
    "CORE_TO_HCA_SCRIPT",
    # Fabric wireup overrides (lib/fabric.inc.sh) — forwarded so a Settings/platform
    # override of the auto-detected plane reaches remote jobs.
    "GPUS_PER_NODE",
    "NCCL_IB_HCA",
    "NCCL_SOCKET_IFNAME",
    "UCX_NET_DEVICES",
    "UCX_TLS",
    "OMPI_MCA_coll_ucc_enable",
    "OMPI_MCA_btl_tcp_if_include",
    "OMPI_MCA_oob_tcp_if_include",
)


@dataclass
class TransportConfig:
    """Resolved transport settings for the control + optional privileged targets."""

    exec_mode: str = "local"  # local | ssh
    control: SSHConfig | None = None
    script: SSHConfig | None = None  # privileged "script" profile (optional)
    # Sudo escalation for the privileged ("script") route (root-requiring node tools).
    script_sudo: bool = False
    sudo_password: str = ""
    sudo_preserve_env: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "TransportConfig":
        e = env if env is not None else os.environ
        exec_mode = (e.get("RACK_EXEC_MODE") or "local").strip().lower()
        raw_opts = (e.get("RACK_SSH_OPTS") or "").strip()
        extra_opts = shlex.split(raw_opts) if raw_opts else []
        connect_timeout = (e.get("RACK_SSH_CONNECT_TIMEOUT") or "10").strip() or "10"
        alive_interval = (e.get("RACK_SSH_ALIVE_INTERVAL") or "5").strip() or "5"
        alive_count = (e.get("RACK_SSH_ALIVE_COUNT") or "2").strip() or "2"
        key = (e.get("RACK_SSH_KEY") or "/run/secrets/ssh_key").strip()
        control = SSHConfig(
            host=(e.get("RACK_SSH_HOST") or "").strip(),
            user=(e.get("RACK_SSH_USER") or "root").strip() or "root",
            key=key,
            port=(e.get("RACK_SSH_PORT") or "22").strip() or "22",
            extra_opts=extra_opts,
            connect_timeout=connect_timeout,
            alive_interval=alive_interval,
            alive_count=alive_count,
        )
        script_host = (e.get("RACK_SCRIPT_SSH_HOST") or "").strip()
        script = None
        if script_host:
            script = SSHConfig(
                host=script_host,
                user=(e.get("RACK_SCRIPT_SSH_USER") or "root").strip() or "root",
                key=(e.get("RACK_SCRIPT_SSH_KEY") or key or "").strip(),
                port=(e.get("RACK_SSH_PORT") or "22").strip() or "22",
                extra_opts=extra_opts,
                connect_timeout=connect_timeout,
                alive_interval=alive_interval,
                alive_count=alive_count,
            )
        sudo_password = (e.get("RACK_SUDO_PASSWORD") or "").strip()
        # Enable sudo on the privileged route when explicitly asked, or implicitly
        # when a sudo password is supplied.
        script_sudo = _truthy(e.get("RACK_SCRIPT_SUDO", "")) or bool(sudo_password)
        # Preserve env across sudo by default (carries the forwarded platform env);
        # sites whose sudoers reject `sudo -E` can set RACK_SUDO_PRESERVE_ENV=0.
        preserve_raw = e.get("RACK_SUDO_PRESERVE_ENV")
        sudo_preserve_env = True if preserve_raw is None else _truthy(preserve_raw)
        return cls(
            exec_mode=exec_mode,
            control=control,
            script=script,
            script_sudo=script_sudo,
            sudo_password=sudo_password,
            sudo_preserve_env=sudo_preserve_env,
        )


def build_connection(
    config: TransportConfig,
    *,
    overlay_provider: Callable[[], dict[str, str]] | None = None,
) -> RoutingConnection:
    """Compose the transport: RoutingConnection{control, privileged}.

    Local mode: both profiles are the same :class:`LocalConnection`.
    SSH mode: each profile is an SSH leaf wrapped by an
    :class:`EnvForwardingConnection`; the privileged ("script") profile falls
    back to the control target when unconfigured.
    """
    resolver = make_env_resolver(FORWARD_ENV_PREFIXES, FORWARD_ENV_NAMES, overlay_provider)

    def _maybe_sudo(conn: Connection) -> Connection:
        """Wrap the privileged route in sudo escalation when configured."""
        if not config.script_sudo:
            return conn
        return SudoConnection(
            conn,
            password=config.sudo_password or None,
            preserve_env=config.sudo_preserve_env,
        )

    if config.exec_mode == "ssh":
        control_ssh = SSHConnection(config.control or SSHConfig(host=""))
        control: Connection = EnvForwardingConnection(control_ssh, resolver)
        script_base: Connection = (
            EnvForwardingConnection(SSHConnection(config.script), resolver)
            if config.script is not None
            else control
        )
        script = _maybe_sudo(script_base)
        return RoutingConnection({"control": control, "script": script}, default="control")

    local = LocalConnection()
    return RoutingConnection({"control": local, "script": _maybe_sudo(local)}, default="control")
