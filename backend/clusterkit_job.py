# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""One pure resource/argv contract for ClusterKit preview and submission."""
from pathlib import PurePosixPath

from schedulers.base import JobSpec


def job_spec(plan):
    resources = plan["resources"]
    wall = resources["wall_seconds"]
    wall_text = f"{wall // 3600:02d}:{wall % 3600 // 60:02d}:{wall % 60:02d}"
    return JobSpec(
        name="ck-" + plan["attempt_id"],
        nodes=plan["node_count"],
        nodelist=",".join(plan["nodes"]),
        ntasks_per_node=1,
        script=plan["entrypoint"],
        script_args=[str(PurePosixPath(plan["results_directory"]) / "plan.json"), plan["scripts_directory"]],
        output=str(PurePosixPath(plan["results_directory"]) / "slurm-%j.out"),
        extra_args=[f"--partition={plan['effective_profile']['partition']}",
                    f"--ntasks={plan['node_count']}", "--cpus-per-task=1", "--mem=1G",
                    "--gres=none", "--no-requeue", f"--time={wall_text}",
                    "--comment=rack-clusterkit:" + plan["attempt_id"]],
        timeout=wall + 120,
    )
