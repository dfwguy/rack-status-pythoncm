# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike
"""Native workflow steps for the NVIS-P2P reconcile.

Every step of the reconcile runs on the **P2P app server**, which is a host in its own right and,
on a cluster like NC, not the cluster's control connection (that is the BCM head). So the steps do
not use the workflow engine's default connection -- they build a direct connection to the app server
from the settings the gateway passes as run params (``P2P_APPSERVER_*``) and run there. Two native
step kinds cover it:

* ``p2p_stage_assets`` -- copy the deploy/p2p tree to the app server (a Python primitive,
  ``host_exec.copy_tree_to_host``, not a remote command).
* ``p2p_run_on_appserver`` -- run a step's ``run:`` script on the app server. This is what the
  preflight, secret generation, render/validate and bring-up steps use, so their scripts stay
  readable in the workflow YAML while executing on the right host.

Registering them as native steps keeps the reconcile inside one durable run with the engine's
progress and failure handling (citizen 4), rather than a second job path beside it.
"""

from __future__ import annotations

import base64
import logging

import host_exec
import paths
import workflow_engine

_logger = logging.getLogger("rack_web.p2p_provision")

#: The asset tree baked into the image (deploy/p2p): scripts, compose, config templates, catalog
#: manifest and, when a release build staged them, the pinned Dragonfly binaries under dist/.
_ASSET_DIR = paths.REPO_ROOT / "deploy" / "p2p"


def _params(run: dict, step: dict) -> dict:
    """Merged params a step sees: earlier outputs, then step params, then run params (run wins)."""
    return {**run.get("outputs", {}), **(step.get("parameters") or {}), **run.get("params", {})}


def _appserver_conn(params: dict):
    """A direct connection to the P2P app server, from the params the gateway injected."""
    return host_exec.connection_to(
        params.get("P2P_APPSERVER_HOST", ""),
        user=params.get("P2P_APPSERVER_USER", ""),
        key=params.get("P2P_APPSERVER_KEY", ""),
    )


def _stage_assets(run: dict, step: dict) -> tuple[bool, str]:
    """Copy the P2P asset tree to ``{NVIS_INSTALL_ROOT}/src`` on the app server."""
    params = _params(run, step)
    host = (params.get("P2P_APPSERVER_HOST") or "").strip()
    if not host:
        return False, "[stage-assets] no P2P app server host in run params (P2P_APPSERVER_HOST)"
    install_root = (params.get("NVIS_INSTALL_ROOT") or "/cluster/nvis-p2p").rstrip("/")
    # `current` is where the rendered compose expects the tree (render rewrites the upstream path to
    # {install_root}/current), so stage there rather than a separate src/ that nothing would read.
    remote = f"{install_root}/current"
    if not _ASSET_DIR.is_dir():
        return False, (
            f"[stage-assets] the P2P asset tree is missing from this image ({_ASSET_DIR}). "
            "This is a text-only build; rebuild after deploy/p2p/bin/stage-artifacts.sh."
        )
    _logger.info("p2p stage-assets: %s -> %s:%s", _ASSET_DIR, host, remote)
    err = host_exec.copy_tree_to_host(_ASSET_DIR, remote, conn=_appserver_conn(params))
    if err:
        return False, f"[stage-assets] copy to {host}:{remote} failed: {err}"
    return True, f"[stage-assets] staged the P2P asset tree to {host}:{remote}"


def _run_on_appserver(run: dict, step: dict) -> tuple[bool, str]:
    """Run the step's ``run:`` script on the P2P app server. Success is exit 0.

    ``${param}`` is substituted first, then the script is base64-piped to ``bash`` so quoting
    survives the SSH hop -- the same shape the engine's inline-run uses, but on the app-server
    connection rather than the default control one.
    """
    params = _params(run, step)
    host = (params.get("P2P_APPSERVER_HOST") or "").strip()
    if not host:
        return False, "[p2p] no P2P app server host in run params (P2P_APPSERVER_HOST)"
    script = str(step.get("run") or step.get("script") or "")
    if not script.strip():
        return False, "[p2p] native app-server step has no run: script"
    for key, val in params.items():
        script = script.replace(f"${{{key}}}", str(val))
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    cmd = ["sh", "-c", f"printf %s {b64} | base64 -d | bash"]
    timeout = int(step.get("timeout", 600))
    try:
        proc = _appserver_conn(params).run(cmd, timeout=timeout)
    except Exception as exc:
        _logger.exception("p2p app-server step %s failed", step.get("id"))
        return False, f"[p2p run on {host}] {type(exc).__name__}: {exc}"
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n--- stderr ---\n" + proc.stderr
    out += f"\n--- exit {proc.returncode} ---\n"
    return proc.returncode == 0, out or f"[completed with exit {proc.returncode}]"


workflow_engine.register_native_step("p2p_stage_assets", _stage_assets)
workflow_engine.register_native_step("p2p_run_on_appserver", _run_on_appserver)
