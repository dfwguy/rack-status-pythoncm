# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Assemble a composite :class:`Connection` from config (env or catalog)."""
from __future__ import annotations

import logging
import os
import shlex
import threading
from collections.abc import Callable
from dataclasses import dataclass

from transport.base import Connection, Executor
from transport.compose import (
    EnvForwardingExecutor,
    RoutingConnection,
    SudoExecutor,
    make_env_resolver,
)
from transport.local import LocalExecutor
from transport.ssh import SSHConfig, SSHExecutor

_logger = logging.getLogger(__name__)

#: Every connection is remote unless explicitly told otherwise. The app is
#: designed to run anywhere (container, laptop, head node) and drive the cluster
#: over the transport, so SSH is the honest default; ``local`` is the special case
#: of the app already sitting on the target.
DEFAULT_EXEC_MODE = "ssh"

VALID_EXEC_MODES = ("local", "ssh")

_warn_lock = threading.Lock()
_warned_deprecated = False


def _warn_exec_mode_deprecated(raw: str) -> None:
    """Emit the ``RACK_EXEC_MODE`` deprecation notice once per process."""
    global _warned_deprecated
    with _warn_lock:
        if _warned_deprecated:
            return
        _warned_deprecated = True
    _logger.warning(
        "RACK_EXEC_MODE=%s is DEPRECATED and will be removed in a future release. "
        "Execution is selected by the connection's executor, not by a global mode "
        "flag; connections now default to %s. Configure the target with "
        "RACK_SSH_HOST/RACK_SSH_USER/RACK_SSH_KEY instead. Set RACK_EXEC_MODE=local "
        "only while the app runs directly on the target host.",
        raw,
        DEFAULT_EXEC_MODE,
    )


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

    exec_mode: str = DEFAULT_EXEC_MODE  # local | ssh (deprecated; see DEFAULT_EXEC_MODE)
    control: SSHConfig | None = None
    script: SSHConfig | None = None  # privileged "script" profile (optional)
    # Sudo escalation for the privileged ("script") route (root-requiring node tools).
    script_sudo: bool = False
    sudo_password: str = ""
    sudo_preserve_env: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TransportConfig:
        e = env if env is not None else os.environ
        raw_mode = (e.get("RACK_EXEC_MODE") or "").strip()
        if raw_mode:
            _warn_exec_mode_deprecated(raw_mode)
            exec_mode = raw_mode.lower()
            if exec_mode not in VALID_EXEC_MODES:
                # Previously an unrecognised value fell through to local, silently
                # running cluster commands inside the container. Fail loudly-ish:
                # log and use the documented default instead.
                _logger.error(
                    "RACK_EXEC_MODE=%r is not one of %s; using %r.",
                    raw_mode, VALID_EXEC_MODES, DEFAULT_EXEC_MODE,
                )
                exec_mode = DEFAULT_EXEC_MODE
        else:
            exec_mode = DEFAULT_EXEC_MODE
        raw_opts = (e.get("RACK_SSH_OPTS") or "").strip()
        extra_opts = shlex.split(raw_opts) if raw_opts else []
        connect_timeout = (e.get("RACK_SSH_CONNECT_TIMEOUT") or "10").strip() or "10"
        alive_interval = (e.get("RACK_SSH_ALIVE_INTERVAL") or "5").strip() or "5"
        alive_count = (e.get("RACK_SSH_ALIVE_COUNT") or "2").strip() or "2"
        key = (e.get("RACK_SSH_KEY") or "/run/secrets/ssh_key").strip()
        # Connection multiplexing on by default (fixes fan-out MaxStartups resets);
        # opt out with RACK_SSH_MULTIPLEX=0.
        mux_raw = e.get("RACK_SSH_MULTIPLEX")
        multiplex = True if mux_raw is None else _truthy(mux_raw)
        mux_persist = (e.get("RACK_SSH_MUX_PERSIST") or "60").strip() or "60"
        control = SSHConfig(
            host=(e.get("RACK_SSH_HOST") or "").strip(),
            user=(e.get("RACK_SSH_USER") or "root").strip() or "root",
            key=key,
            port=(e.get("RACK_SSH_PORT") or "22").strip() or "22",
            extra_opts=extra_opts,
            connect_timeout=connect_timeout,
            alive_interval=alive_interval,
            alive_count=alive_count,
            multiplex=multiplex,
            mux_persist=mux_persist,
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
                multiplex=multiplex,
                mux_persist=mux_persist,
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

    The composition happens at the *executor* level — leaf (local or ssh) wrapped
    by env forwarding, optionally wrapped by sudo — and each composed executor is
    then handed to one plain :class:`Connection`. Local mode differs from SSH mode
    only in which leaf executor is built.
    """
    resolver = make_env_resolver(FORWARD_ENV_PREFIXES, FORWARD_ENV_NAMES, overlay_provider)

    def _maybe_sudo(ex: Executor) -> Executor:
        """Wrap the privileged route in sudo escalation when configured."""
        if not config.script_sudo:
            return ex
        return SudoExecutor(
            ex,
            password=config.sudo_password or None,
            preserve_env=config.sudo_preserve_env,
        )

    if config.exec_mode == "ssh":
        control_ex: Executor = EnvForwardingExecutor(
            SSHExecutor(config.control or SSHConfig(host="")), resolver
        )
        script_base: Executor = (
            EnvForwardingExecutor(SSHExecutor(config.script), resolver)
            if config.script is not None
            else control_ex
        )
        return RoutingConnection(
            {
                "control": Connection(control_ex),
                "script": Connection(_maybe_sudo(script_base)),
            },
            default="control",
        )

    local_ex: Executor = LocalExecutor()
    return RoutingConnection(
        {
            "control": Connection(local_ex),
            "script": Connection(_maybe_sudo(local_ex)),
        },
        default="control",
    )
