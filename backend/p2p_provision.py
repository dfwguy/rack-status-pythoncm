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

Most of the reconcile is bash on the app server -- preflight, render, check, bring-up -- which the
workflow engine runs as `run:` steps. One step is not: staging the deploy/p2p asset tree onto the
app server is a Python primitive (`host_exec.copy_tree_to_host`), not a remote command, because the
assets live in the Rack Status image and have to cross the (possibly multi-hop) connection to get
there. This module registers that step as a *native* step so it runs inside the same durable run,
progress and failure handling as every other -- the engine owning the capability rather than a
second job path beside it (citizen 4).
"""

from __future__ import annotations

import logging

import host_exec
import paths
import workflow_engine

_logger = logging.getLogger("rack_web.p2p_provision")

#: The asset tree baked into the image (deploy/p2p) -- scripts, compose, config templates, catalog
#: manifest and, when a release build staged them, the pinned binaries under dist/.
_ASSET_DIR = paths.REPO_ROOT / "deploy" / "p2p"


def _stage_assets(run: dict, step: dict) -> tuple[bool, str]:
    """Copy the P2P asset tree to ``{NVIS_INSTALL_ROOT}/src`` on the app server."""
    params = {**run.get("params", {}), **(step.get("parameters") or {})}
    install_root = (params.get("NVIS_INSTALL_ROOT") or "/cluster/nvis-p2p").rstrip("/")
    remote = f"{install_root}/src"

    if not _ASSET_DIR.is_dir():
        return False, (
            f"[stage-assets] the P2P asset tree is missing from this image ({_ASSET_DIR}). "
            "This is a text-only build; rebuild after deploy/p2p/bin/stage-artifacts.sh so the "
            "Dragonfly binaries and assets are present."
        )
    _logger.info("p2p stage-assets: %s -> %s", _ASSET_DIR, remote)
    err = host_exec.copy_tree_to_host(_ASSET_DIR, remote)
    if err:
        return False, f"[stage-assets] copy to {remote} failed: {err}"
    return True, f"[stage-assets] staged the P2P asset tree to {remote}"


workflow_engine.register_native_step("p2p_stage_assets", _stage_assets)
