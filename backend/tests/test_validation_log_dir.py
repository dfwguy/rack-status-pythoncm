# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Validation launchers must write logs where the cluster says, never a baked-in absolute path.

A validation preset run drained on the NC GB300 cluster with ``mkdir: cannot create directory
'/cluster': Permission denied``: ``run-validation-preset.sh`` received ``--log-dir`` but never handed
it to the ``run_nccl_mpi.sh`` it chains to, and that launcher fell back to a hard-coded
``/cluster/benchmark/harish/rack_status/logs`` -- a personal path that does not exist on the
execution host and cannot exist on any other cluster. Two invariants keep that from coming back:

* no job launcher carries the old absolute default (it was a cross-cluster no-go); the last-resort
  default is ``$PWD``, which the app sets to the cluster's configured ``log_dir``; and
* the preset dispatcher exports ``RACK_JOB_LOG_DIR`` so every child launcher it invokes inherits the
  resolved directory instead of guessing.
"""

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"

#: Every launcher/collector that resolves a job log directory. The env chain is the contract the
#: backend drives (``RACK_JOB_LOG_DIR`` per-launch, ``RACK_STATUS_LOG_DIR`` per-deployment); the
#: innermost default must be the launch cwd, not an absolute site path.
LOG_DIR_SCRIPTS = (
    "nccl_fullrack_rounds.sh",
    "run_nccl_mpi.sh",
    "collect_llmb_nemo_log.sh",
    "collect_llmb_nccl_log.sh",
    "nccl_campaign.sh",
    "run_singlerack.sh",
    "run-nemo-xrack.sh",
    "run_nvloom_singlerack.sh",
    "run-llama.sh",
    "run_clusterkit.sh",
    "run_llmb_nemo.sh",
    "run-exemplar-llama.sh",
    "run_mnubergemm_stress.sh",
    "run_llmb_nccl.sh",
)

OLD_HARDCODED_DEFAULT = "/cluster/benchmark/harish/rack_status/logs"


def test_no_launcher_keeps_the_hardcoded_cluster_log_default():
    offenders = [
        path.name
        for path in SCRIPTS.glob("*")
        if path.is_file() and OLD_HARDCODED_DEFAULT in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} still default the log dir to {OLD_HARDCODED_DEFAULT!r}; that path does not "
        "exist on the execution host and is not portable across clusters"
    )


def test_job_launchers_default_log_dir_to_the_launch_cwd():
    for name in LOG_DIR_SCRIPTS:
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-$PWD}}" in source, (
            f"{name} must resolve its log dir as RACK_JOB_LOG_DIR > RACK_STATUS_LOG_DIR > $PWD"
        )


def test_preset_dispatcher_forwards_its_log_dir_to_children():
    source = (SCRIPTS / "run-validation-preset.sh").read_text(encoding="utf-8")
    # The dispatcher owns --log-dir and chains to host launchers (run_nccl_mpi.sh,
    # run-exemplar-system-info.sh) that read it only from the environment. It must export it.
    assert 'export RACK_JOB_LOG_DIR="$log_dir"' in source, (
        "run-validation-preset.sh must export RACK_JOB_LOG_DIR so chained launchers inherit --log-dir"
    )
