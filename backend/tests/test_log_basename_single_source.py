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
"""Log-basename classification must have exactly one implementation.

``app.py`` and ``native_status.py`` each used to carry their own copy of four predicates
(NEMO/NCCL membership, single-rack, multirack, stage kind) plus a verbatim duplicate of
``_MULTIRACK_N_RACK_RE``, and there were three separate job-id extractors with different
match order. These decide which table a log appears in, so a drifted copy shows a rack in
one view and not the other.

The logic now lives in ``nvis_id_templates``; the module-level names in ``naming.py`` (moved
there from ``app.py`` so every domain shares one set) and ``native_status.py`` are binding
wrappers only. These tests pin the behaviour and guard the
structure, because a second copy is the only way the drift returns.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import naming
import native_status
import nvis_id_templates as nid
import validation_activities

BACKEND = Path(__file__).resolve().parent.parent

# A corpus spanning every naming shape the fleet actually produces.
CORPUS = [
    "cmods-NCCL-1Rack-p05-r01-18N-127326.txt",
    "cmods-NEMO-1Rack-p05-r01-18N-127327.txt",
    "cmods-NCCL-1Rack-ink50-dh01-h01-18N-127318.txt",
    "root-NEMO-1Rack-bos01-b05-18N-294.txt",
    "root-NEMO-p5-r13-ct01,p5-r15-ct01-72N-22403.txt",
    "root-NEMO-4Rack-p05-r01-72N-5.txt",
    "cmods-NEMO-36Rack-p05-r01-648N-9.txt",
    "nccl-mpi_ink50-dh01-h18+ink50-dh01-h19_127207.out",
    "llmb-nccl_nodes_127318.out",
    "llmb-autocollect_127326.log",
    "llmb-nemo-autocollect_127327.log",
    "cmods-NEMO-LLAMA3_8B-1N-node11-77.txt",
    "cmods-MNUBERGEMM-p05-r01-4242.txt",
    "root-CK-p05-r01-888.txt",
    "cmods-NVLOOM-p05-r01-999.txt",
    "sysinfo.txt",
    "app.py.bak.20260728",
    ".hidden.log",
    "notes.md",
]


# --- behaviour: the two module wrappers must agree with the canonical implementation ------


@pytest.mark.parametrize("name", CORPUS)
def test_nemo_nccl_membership_agrees(name):
    want = nid.nemo_nccl_in_log_basename(name)
    assert naming._nemo_nccl_in_log_basename(name) == want
    assert native_status._nemo_nccl_bn(name) == want


@pytest.mark.parametrize("name", CORPUS)
def test_single_rack_membership_agrees(name):
    want = nid.nemo_single_rack_log_basename(name)
    assert naming._nemo_single_rack_log_basename(name) == want
    assert native_status._nemo_single_rack_bn(name) == want


@pytest.mark.parametrize("name", CORPUS)
def test_multirack_classification_agrees(name):
    want = nid.is_multirack_log_basename(name)
    assert naming._is_multirack_log_basename(name) == want
    assert native_status._is_multirack_bn(name) == want


@pytest.mark.parametrize("name", CORPUS)
def test_job_id_agrees_across_all_three_modules(name):
    want = nid.job_id_from_log_basename(name)
    assert naming._jobid_from_nemo_log_basename(name) == want
    assert native_status._jobid_from_log_name(name) == want
    assert validation_activities.job_id_from_log_basename(name) == want


@pytest.mark.parametrize("name", CORPUS)
def test_node_count_suffix_agrees(name):
    want = nid.node_count_suffix_from_basename(name)
    assert naming._multirack_node_count_suffix(name) == want
    assert native_status._multirack_node_count_suffix_from_basename(name) == want


# --- behaviour: the classification itself --------------------------------------------------


def test_stage_kind_splits_the_two_stages():
    assert nid.stage_kind_of_log_basename("cmods-NCCL-1Rack-p05-r01-18N-1.txt") == "nccl"
    assert nid.stage_kind_of_log_basename("cmods-NEMO-1Rack-p05-r01-18N-2.txt") == "nemo"
    # A combined or multirack log carries no single-rack stage evidence.
    assert nid.stage_kind_of_log_basename("root-NEMO-4Rack-p05-r01-72N-3.txt") is None
    assert nid.stage_kind_of_log_basename("sysinfo.txt") is None


def test_n_rack_marker_makes_a_log_multirack():
    assert nid.is_multirack_log_basename("root-NEMO-4Rack-p05-r01-72N-5.txt") is True
    assert nid.multirack_n_rack_marker("cmods-NEMO-36Rack-x-9.txt") == "36Rack"
    # 1Rack is single-rack, not a multirack marker.
    assert nid.multirack_n_rack_marker("cmods-NEMO-1Rack-p05-r01-18N-9.txt") is None


def test_llama_job_id_wins_over_the_generic_tail():
    """A hostname ending in N would let the generic ``N-<jid>`` pattern match too."""
    assert nid.job_id_from_log_basename("cmods-NEMO-LLAMA3_8B-1N-nodeN-77.txt") == "77"
    assert nid.job_id_from_log_basename("cmods-NEMO-1Rack-p05-r01-18N-127327.txt") == "127327"
    assert nid.job_id_from_log_basename("sysinfo.txt") is None


def test_node_count_reads_the_n_field():
    assert nid.node_count_from_log_basename("cmods-NEMO-1Rack-p05-r01-18N-1.txt") == 18
    assert nid.node_count_from_log_basename("cmods-NCCL-1Rack-p05-r01-1N-2.txt") == 1
    assert nid.node_count_from_log_basename("no-node-count.txt") is None


def test_job_id_ignores_directories_in_the_path():
    """Callers pass basenames, but a full path must not change the answer."""
    assert nid.job_id_from_log_basename("/logs/failed/cmods-NEMO-1Rack-r01-18N-5.txt") == "5"


# --- structure: the copies must not come back ---------------------------------------------


@pytest.mark.parametrize("module", ["app.py", "native_status.py"])
def test_module_does_not_redefine_the_multirack_regex(module):
    body = (BACKEND / module).read_text(encoding="utf-8")
    assert "_MULTIRACK_N_RACK_RE = re.compile" not in body, (
        f"{module} redefines the N-rack regex; nvis_id_templates owns it"
    )


@pytest.mark.parametrize(
    "module,func",
    [
        ("naming.py", "_nemo_nccl_in_log_basename"),
        ("naming.py", "_nemo_single_rack_log_basename"),
        ("naming.py", "_is_multirack_log_basename"),
        ("naming.py", "_jobid_from_nemo_log_basename"),
        ("naming.py", "_multirack_node_count_suffix"),
        ("native_status.py", "_nemo_nccl_bn"),
        ("native_status.py", "_nemo_single_rack_bn"),
        ("native_status.py", "_is_multirack_bn"),
        ("native_status.py", "_stage_kind_of_log_bn"),
        ("native_status.py", "_jobid_from_log_name"),
        ("native_status.py", "_multirack_node_count_suffix_from_basename"),
    ],
)
def test_wrapper_carries_no_classification_logic(module, func):
    """A wrapper may bind a naming instance and return; it may not re-implement the rule.

    Guards against the specific regression: someone "fixes" a classification bug in one
    module's wrapper and the other view keeps the old behaviour.
    """
    body = (BACKEND / module).read_text(encoding="utf-8")
    m = re.search(rf"^def {re.escape(func)}\(.*?\n(?=^\S)", body, re.DOTALL | re.MULTILINE)
    assert m, f"{func} not found in {module}"
    src = m.group(0)
    assert "nvis_id_templates." in src or "_nvis_ids." in src, (
        f"{module}:{func} must delegate to nvis_id_templates"
    )
    for banned in ('re.search(', 're.compile(', '"nemo" in', "'nemo' in", '"1rack" in'):
        assert banned not in src, f"{module}:{func} re-implements the rule ({banned})"


def test_validation_activities_delegates_job_id():
    body = (BACKEND / "validation_activities.py").read_text(encoding="utf-8")
    assert "_SLURM_JID_BASENAME_RE" not in body, "the local job-id regexes should be gone"
    assert "_NEMO_LLAMA_JID_RE" not in body
