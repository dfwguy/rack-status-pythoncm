# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""Which RDMA fabric is a compute node actually on?

Classifies a node's interfaces as compute rails versus the storage / NS plane. Worth having as an
endpoint because getting this wrong is a class of failure that looks like bad hardware: a collective
pinned to the storage plane is slow and lossy rather than broken, so it shows up as flaky benchmark
numbers instead of an error.
"""
from __future__ import annotations

import json
import logging

import host_exec
import script_tree
from audit.actions import action
from config_env import _env_int
from flask import Blueprint, jsonify, request
from platforms import get_platform as _get_platform

_logger = logging.getLogger(__name__)

# Empty prefix: routes keep the paths they had in app.py, so the move cannot change a URL.
bp = Blueprint("fabric_probe", __name__, url_prefix="")


@bp.route("/api/fabric-probe", methods=["GET"])
@action(get="fabric:read")
def api_fabric_probe():
    """Classify a compute node's RDMA fabric (compute rails vs storage/NS plane).

    Runs ``bin/scripts/fabric_probe.sh`` against a representative compute node and
    returns the detected plane (link tech, per-rail rate, compute rails, the NCCL
    ``NCCL_IB_HCA`` / ``NCCL_SOCKET_IFNAME`` it would use) plus any detected-vs-expected
    discrepancies. Expected values come from the active platform's fabric profile
    (forwarded as ``NVIS_FABRIC_EXPECT_*``). ``?node=`` targets a specific host;
    otherwise the first Slurm node is used (homogeneous cluster assumption).
    """
    node = (request.args.get("node") or "").strip()
    try:
        if not node:
            proc = host_exec.run_slurm_on_host(
                _get_platform().sinfo_cmd("-N -h -o '%N' | head -1"),
                timeout=30,
            )
            lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
            node = lines[0] if lines else ""
        if not node:
            return jsonify({"ok": False, "error": "no compute node found; pass ?node=<hostname>"}), 400
        script = script_tree.resolve("fabric_probe.sh")
        proc = host_exec.run_on_host(
            ["bash", script, node],
            timeout=max(30, _env_int("RACK_FABRIC_PROBE_TIMEOUT", 60)),
        )
        out = (proc.stdout or "").strip()
        json_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("{")]
        if not json_lines:
            return jsonify({
                "ok": False,
                "error": "fabric probe returned no JSON",
                "node": node,
                "stderr": (proc.stderr or "")[-500:],
            }), 502
        data = json.loads(json_lines[-1])
        return jsonify({"ok": True, "node": node, "fabric": data})
    except Exception as e:
        _logger.debug("api_fabric_probe: ignoring error", exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500
