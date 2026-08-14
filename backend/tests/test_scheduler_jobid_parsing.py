# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Scheduler job-id extraction from wrapper stdout (``parse_scheduler_job_ids``).

The Tasks column in the validation-activities table is built from these ids, and the UI now keys
"Charts" off them. Only two scripts ever printed ``jobid=`` (llmb_autocollect.inc.sh,
run_mnubergemm_stress.sh); every launcher that just calls sbatch emits Slurm's own
``Submitted batch job N``. Missing that dialect left those activities with no tasks, which is what
forced the UI into rack-name guessing and made Charts open the wrong job's results.
"""
import validation_activities as va


def test_jobid_equals_dialect():
    """llmb-run / autocollect style."""
    assert va.parse_scheduler_job_ids("SUBMITTED: workload=nccl jobid=12345") == ["12345"]


def test_bare_jobid_line():
    assert va.parse_scheduler_job_ids("some preamble\njobid=999\ntrailing") == ["999"]


def test_sbatch_submitted_dialect():
    """Slurm's native reply — run-nemo-xrack.sh, run_nccl_mpi.sh, run_clusterkit.sh, run_singlerack.sh."""
    assert va.parse_scheduler_job_ids("Submitted batch job 131920") == ["131920"]


def test_sbatch_submitted_is_case_insensitive_and_tolerates_spacing():
    assert va.parse_scheduler_job_ids("submitted batch job    42") == ["42"]


def test_sbatch_submitted_amid_other_output():
    text = (
        "run-nemo-xrack: staging image to /raid/data on 72 nodes\n"
        "Submitted batch job 131920\n"
        "run-nemo-xrack: log -> root-NEMO-...-72N-131920.txt\n"
    )
    assert va.parse_scheduler_job_ids(text) == ["131920"]


def test_ids_are_returned_in_textual_order_across_dialects():
    """Order defines the nccl/nemo task labels, so it must follow the text, not the pattern."""
    text = "Submitted batch job 100\nlater stage\njobid=200\n"
    assert va.parse_scheduler_job_ids(text) == ["100", "200"]

    text_rev = "jobid=200\nlater stage\nSubmitted batch job 100\n"
    assert va.parse_scheduler_job_ids(text_rev) == ["200", "100"]


def test_duplicate_ids_collapse():
    text = "Submitted batch job 55\njobid=55\nSubmitted batch job 55\n"
    assert va.parse_scheduler_job_ids(text) == ["55"]


def test_combined_two_stage_run_yields_both_ids_in_order():
    """A combined NCCL+NEMO launch: first id is stage 1, second is stage 2."""
    text = (
        "=== stage 1/2: NCCL sweep ===\nSubmitted batch job 1001\n"
        "=== stage 2/2: NEMO pretrain ===\nSubmitted batch job 1002\n"
    )
    assert va.parse_scheduler_job_ids(text) == ["1001", "1002"]


def test_no_ids_and_empty_input():
    assert va.parse_scheduler_job_ids("") == []
    assert va.parse_scheduler_job_ids("nothing to see here") == []


def test_does_not_match_unrelated_numbers():
    assert va.parse_scheduler_job_ids("allocated 72 nodes, 288 GPUs, 4200 ms/iter") == []
