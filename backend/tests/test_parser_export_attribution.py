# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
from pathlib import PurePosixPath
from types import SimpleNamespace
import io

from openpyxl import load_workbook

import app as app_module


def _parsed(job_id="46752"):
    return {
        "nccl_peak_out_of_place_busbw_gbps": [
            {"rack": "?", "job_id": job_id, "all_reduce_gbps": "381.87"}
        ],
        "nemotron_5h_56b_train_window": [
            {"rack": "?", "job_id": job_id, "step_s": "4.55"}
        ],
    }


def _mnu_parsed(job_id="55599", rack="?"):
    return {
        "mnubergemm_stress": [{
            "rack": rack,
            "job_id": job_id,
            "gflops_max": "792700",
            "gflops_median": "741200",
        }]
    }


def _nscale_naming():
    def resolve(node):
        parts = str(node).rsplit("-", 1)
        if len(parts) != 2 or not parts[1].isdigit() or not parts[0].startswith("ink50-dh"):
            return None
        return parts[0], str(node)

    return SimpleNamespace(sinfo_rack_from_host=resolve)


def test_export_attribution_uses_exact_campaign_path(monkeypatch):
    parsed = _parsed()
    path = PurePosixPath(
        "/cluster/OVA/logs/SU5/rack01/multiNodeNeMo/nemo-26.04.01-46752.out"
    )
    monkeypatch.setattr(app_module.validation_activities, "list_activities", lambda **_kwargs: [])

    app_module._annotate_parser_export_racks(
        parsed, [path], PurePosixPath("/cluster/OVA/logs/SU5/rack01/multiNodeNeMo")
    )

    assert parsed["nccl_peak_out_of_place_busbw_gbps"][0]["rack"] == "ink50-dh02-e01"
    assert parsed["nemotron_5h_56b_train_window"][0]["rack"] == "ink50-dh02-e01"


def test_export_attribution_uses_unique_activity_job(monkeypatch):
    parsed = _parsed("55587")
    path = PurePosixPath(
        "/cluster/OVA/logs/nscaletx-16k-validation/nemo-26.04.01-55587.out"
    )
    monkeypatch.setattr(
        app_module.validation_activities,
        "list_activities",
        lambda **_kwargs: [{
            "rack": "ink50-dh02-f08",
            "racks": ["ink50-dh02-f08"],
            "tasks": [{"scheduler_ref": "55587", "stdout_path": str(path)}],
        }],
    )

    app_module._annotate_parser_export_racks(
        parsed, [path], PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation")
    )

    assert parsed["nemotron_5h_56b_train_window"][0]["rack"] == "ink50-dh02-f08"


def test_export_attribution_rejects_ambiguous_activity(monkeypatch):
    parsed = _parsed("55593")
    path = PurePosixPath(
        "/cluster/OVA/logs/nscaletx-16k-validation/nvftp-nccl-five-collective-55593.out"
    )
    monkeypatch.setattr(
        app_module.validation_activities,
        "list_activities",
        lambda **_kwargs: [{
            "rack": "ink50-dh02-e02",
            "racks": ["ink50-dh02-e02", "ink50-dh02-i03"],
            "tasks": [{"scheduler_ref": "55593"}],
        }],
    )

    app_module._annotate_parser_export_racks(
        parsed, [path], PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation")
    )

    assert parsed["nccl_peak_out_of_place_busbw_gbps"][0]["rack"] == "?"


def test_export_attribution_preserves_parsed_rack_and_no_match(monkeypatch):
    parsed = _parsed("99999")
    parsed["nccl_peak_out_of_place_busbw_gbps"][0]["rack"] = "ink50-dh02-j04"
    monkeypatch.setattr(app_module.validation_activities, "list_activities", lambda **_kwargs: [])

    app_module._annotate_parser_export_racks(
        parsed,
        [PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation/unknown-99999.out")],
        PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation"),
    )

    assert parsed["nccl_peak_out_of_place_busbw_gbps"][0]["rack"] == "ink50-dh02-j04"
    assert parsed["nemotron_5h_56b_train_window"][0]["rack"] == "?"


def test_mnubergemm_attribution_uses_validated_hostlist(monkeypatch):
    parsed = _mnu_parsed()
    path = PurePosixPath(
        "/cluster/OVA/logs/nscaletx-16k-validation/"
        "mnubergemm-26.03-v1.17-g001-55599.out"
    )
    monkeypatch.setattr(app_module, "_nvis_naming", _nscale_naming)
    monkeypatch.setattr(
        app_module.host_exec,
        "read_text",
        lambda *_args, **_kwargs: (
            "HOSTLIST: ink50-dh02-e02-[11-12]\n",
            {"ok": True},
        ),
    )
    monkeypatch.setattr(app_module.validation_activities, "list_activities", lambda **_kwargs: [])

    app_module._annotate_parser_result_racks(parsed, [path], path.parent)

    assert parsed["mnubergemm_stress"][0]["rack"] == "ink50-dh02-e02"


def test_mnubergemm_attribution_rejects_mixed_rack_hostlist(monkeypatch):
    parsed = _mnu_parsed()
    path = PurePosixPath("/logs/mnubergemm-26.03-v1.17-g001-55599.out")
    monkeypatch.setattr(app_module, "_nvis_naming", _nscale_naming)
    monkeypatch.setattr(
        app_module.host_exec,
        "read_text",
        lambda *_args, **_kwargs: (
            "HOSTLIST: ink50-dh02-e02-11,ink50-dh02-j08-11\n",
            {"ok": True},
        ),
    )
    monkeypatch.setattr(app_module.validation_activities, "list_activities", lambda **_kwargs: [])

    app_module._annotate_parser_result_racks(parsed, [path], path.parent)

    assert parsed["mnubergemm_stress"][0]["rack"] == "?"


def test_mnubergemm_attribution_accepts_unique_task_nodes_in_multirack_activity(monkeypatch):
    parsed = _mnu_parsed("55600")
    path = PurePosixPath("/logs/mnubergemm-26.03-v1.17-g002-55600.out")
    monkeypatch.setattr(app_module, "_nvis_naming", _nscale_naming)
    monkeypatch.setattr(
        app_module.host_exec,
        "read_text",
        lambda *_args, **_kwargs: ("no hostlist here\n", {"ok": True}),
    )
    monkeypatch.setattr(
        app_module.validation_activities,
        "list_activities",
        lambda **_kwargs: [{
            "racks": ["ink50-dh02-e02", "ink50-dh02-j08"],
            "tasks": [{
                "scheduler_ref": "55600",
                "nodes": ["ink50-dh02-j08-11", "ink50-dh02-j08-12"],
            }],
        }],
    )

    app_module._annotate_parser_result_racks(parsed, [path], path.parent)

    assert parsed["mnubergemm_stress"][0]["rack"] == "ink50-dh02-j08"


def test_mnubergemm_attribution_fails_closed_on_conflicting_sources(monkeypatch):
    parsed = _mnu_parsed()
    path = PurePosixPath("/logs/mnubergemm-26.03-v1.17-g001-55599.out")
    monkeypatch.setattr(app_module, "_nvis_naming", _nscale_naming)
    monkeypatch.setattr(
        app_module.host_exec,
        "read_text",
        lambda *_args, **_kwargs: (
            "HOSTLIST: ink50-dh02-e02-11,ink50-dh02-e02-12\n",
            {"ok": True},
        ),
    )
    monkeypatch.setattr(
        app_module.validation_activities,
        "list_activities",
        lambda **_kwargs: [{
            "racks": ["ink50-dh02-j08"],
            "tasks": [{"scheduler_ref": "55599"}],
        }],
    )

    app_module._annotate_parser_result_racks(parsed, [path], path.parent)

    assert parsed["mnubergemm_stress"][0]["rack"] == "?"


def test_mnubergemm_attribution_never_overwrites_parser_rack(monkeypatch):
    parsed = _mnu_parsed(rack="ink50-dh02-e02")
    monkeypatch.setattr(
        app_module.host_exec,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read")),
    )

    app_module._annotate_parser_result_racks(
        parsed,
        [PurePosixPath("/logs/mnubergemm-26.03-v1.17-g001-55599.out")],
        PurePosixPath("/logs"),
    )

    assert parsed["mnubergemm_stress"][0]["rack"] == "ink50-dh02-e02"


def test_workbook_exports_attributed_nscale_racks_and_mnubergemm_sheet():
    parsed = {
        "nccl_peak_out_of_place_busbw_gbps": [{
            "rack": "ink50-dh02-e02",
            "job_id": "55597",
            "all_reduce_gbps": "381.87",
        }],
        "mnubergemm_stress": [
            {
                "rack": "ink50-dh02-e02",
                "job_id": "55599",
                "nodes": "2",
                "gpus": 8,
                "ranks": "8",
                "rank_samples": "3",
                "qualified_ranks": "8",
                "gflops_max": "7.927e+05",
                "gflops_median": "7.412e+05",
                "verdict": "DONE",
            },
            {
                "rack": "ink50-dh02-j08",
                "job_id": "55600",
                "nodes": "2",
                "gpus": 8,
                "ranks": "8",
                "rank_samples": "3",
                "qualified_ranks": "8",
                "gflops_max": "7.524e+05",
                "gflops_median": "7.509e+05",
                "verdict": "QUAL_OK",
            },
        ],
    }

    workbook = load_workbook(io.BytesIO(app_module._workbook_bytes_from_parser_export(parsed)))

    assert workbook.sheetnames == [
        "nccl all_reduce",
        "nccl all_to_all",
        "nccl all_gather",
        "llm",
        "mnubergemm",
    ]
    assert workbook["nccl all_reduce"]["A3"].value == "ink50-dh02-e02"
    assert list(workbook["mnubergemm"].iter_rows(min_row=3, values_only=True)) == [
        ("ink50-dh02-e02", 2, 8, 8, 3, 8, 792700, 741200, "DONE"),
        ("ink50-dh02-j08", 2, 8, 8, 3, 8, 752400, 750900, "QUAL_OK"),
    ]


def test_workbook_never_exports_all_zero_completed_mnubergemm_as_pass():
    parsed = {
        "mnubergemm_stress": [{
            "rack": "ink50-dh02-e02",
            "job_id": "55599",
            "nodes": "2",
            "gpus": 8,
            "ranks": "8",
            "rank_samples": "3",
            "qualified_ranks": "8",
            "gflops_max": "0",
            "gflops_median": "0",
            "verdict": "DONE",
        }]
    }

    workbook = load_workbook(io.BytesIO(app_module._workbook_bytes_from_parser_export(parsed)))

    assert workbook["mnubergemm"]["I3"].value == "FAIL"


def test_explicit_campaign_path_adoption_uses_exact_parent_and_rack(monkeypatch):
    path = PurePosixPath(
        "/cluster/OVA/logs/SU11/rack17/multiNodeMNU/"
        "mnubergemm-26.03-v1.17-g001-55952.out"
    )
    monkeypatch.setattr(
        app_module, "_racks_from_validated_hostlist", lambda _path: {"ink50-dh02-k17"}
    )

    log_dir, rack = app_module._validation_adoption_metadata(
        path,
        PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation"),
        "",
        explicit_path=True,
    )

    assert log_dir == path.parent
    assert rack == "ink50-dh02-k17"


def test_explicit_path_adoption_abstains_on_conflicting_rack_evidence(monkeypatch):
    path = PurePosixPath(
        "/cluster/OVA/logs/SU11/rack17/multiNodeMNU/"
        "mnubergemm-26.03-v1.17-g001-55952.out"
    )
    monkeypatch.setattr(
        app_module, "_racks_from_validated_hostlist", lambda _path: {"ink50-dh02-k17"}
    )
    monkeypatch.setattr(app_module, "_normalize_rack_id", lambda rack: rack)

    log_dir, rack = app_module._validation_adoption_metadata(
        path,
        PurePosixPath("/cluster/OVA/logs/nscaletx-16k-validation"),
        "ink50-dh02-i21",
        explicit_path=True,
    )

    assert log_dir == path.parent
    assert rack == ""
