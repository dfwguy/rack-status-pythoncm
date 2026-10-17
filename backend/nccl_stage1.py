# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Which launcher rack Stage 1 (the "NCCL Benchmark (Stage-1)" tool) runs, and what its card says.

Stage 1 has two launchers, chosen by ``RACK_NCCL_FULLRACK_LAUNCHER``:

- ``llmb`` (default since 2026-07-28, at an operator's request to run Stage 1 through llmb-run):
  ``run_llmb_nccl.sh`` submits llmb-run's ``microbenchmark_nccl``, which runs the LLMB install's
  whole test set in one job. The stock set is 24 tests over all_reduce, all_gather, reduce_scatter,
  alltoall and sendrecv. The set is generated into the workload's ``launch.sh`` when LLMB is
  installed, so the per-launch rounds and NCCL-version options do not apply.
- ``mpi`` (also ``sbatch``, ``direct``, ``nccl_mpi``): ``run_nccl_mpi.sh`` runs host-mpirun
  all_reduce, ``--rounds`` times (default 5), up to ``--size-max``.

The card used to describe the ``mpi`` path whatever the setting said, so a rack Stage 1 billed as
"5 rounds all_reduce" ran 24 LLMB tests. The catalog now derives the card from this module when it
serves the tool, so the two cannot disagree.
"""
from __future__ import annotations

from typing import Any

from config_env import _eget

#: The launch endpoint that identifies the Stage-1 tool, whatever id a catalog gave it.
ENDPOINT = "/api/run-nccl-fullrack-async"

LLMB = "llmb"
MPI = "mpi"
_MPI_ALIASES = frozenset({"mpi", "sbatch", "direct", "nccl_mpi"})

#: Rounds the ``mpi`` path runs when a launch does not choose (``_nccl_rounds_from_body`` default).
DEFAULT_MPI_ROUNDS = 5


def launcher() -> str:
    """``llmb`` or ``mpi``: the launcher rack Stage 1 runs, from ``RACK_NCCL_FULLRACK_LAUNCHER``."""
    raw = (_eget("RACK_NCCL_FULLRACK_LAUNCHER") or LLMB).strip().lower()
    return MPI if raw in _MPI_ALIASES else LLMB


def card() -> dict[str, Any]:
    """Subtitle, description and run options for the Stage-1 card, matching :func:`launcher`."""
    if launcher() == LLMB:
        return {
            "subtitle": "LLMB NCCL test set (llmb-run)",
            "description": (
                "Stage-1 NCCL acceptance through llmb-run microbenchmark_nccl: one job runs the LLMB "
                "install's whole NCCL test set (the stock set is 24 tests across all_reduce, "
                "all_gather, reduce_scatter, alltoall and sendrecv). The set is fixed when LLMB is "
                "installed, so there are no per-launch rounds or NCCL version. Launcher: llmb "
                "(Settings, RACK_NCCL_FULLRACK_LAUNCHER)."
            ),
            "ai_context": (
                "Stage-1 NCCL acceptance via llmb-run microbenchmark_nccl (RACK_NCCL_FULLRACK_LAUNCHER="
                "llmb): one job runs the LLMB install's whole test set (stock: 24 tests over "
                "all_reduce, all_gather, reduce_scatter, alltoall, sendrecv). Rounds and NCCL version "
                "do not apply. It drops an NCCL-1Rack-<rack> log into log_dir."
            ),
            # The options dialog would offer rounds and an NCCL version that this path ignores.
            "body": {"show_iters": False, "show_nccl_version": False},
        }
    return {
        "subtitle": f"{DEFAULT_MPI_ROUNDS} rounds all_reduce, up to 16G",
        "description": (
            f"Stage-1 NCCL acceptance through host mpirun: all_reduce, {DEFAULT_MPI_ROUNDS} rounds by "
            "default (rounds and NCCL version can be chosen per launch), message sizes up to 16G. "
            "Launcher: mpi (Settings, RACK_NCCL_FULLRACK_LAUNCHER)."
        ),
        "ai_context": (
            f"Stage-1 NCCL acceptance via host mpirun (RACK_NCCL_FULLRACK_LAUNCHER=mpi): all_reduce, "
            f"{DEFAULT_MPI_ROUNDS} rounds by default, up to 16G; it drops an NCCL-1Rack-<rack> log into "
            "log_dir."
        ),
        "body": {"show_iters": True, "show_nccl_version": True},
    }


def apply_card(item: dict[str, Any]) -> None:
    """Rewrite a served Stage-1 tool *item* in place so its card describes the active launcher."""
    launch = item.get("launch")
    if not isinstance(launch, dict) or launch.get("endpoint") != ENDPOINT:
        return
    c = card()
    item["subtitle"] = c["subtitle"]
    item["description"] = c["description"]
    item["ai_context"] = c["ai_context"]
    body = launch.get("body")
    launch["body"] = {**(body if isinstance(body, dict) else {}), **c["body"]}
