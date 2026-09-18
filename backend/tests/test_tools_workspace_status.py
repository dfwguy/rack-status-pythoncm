# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
from pathlib import PurePosixPath

import app as app_module

# The Tools helpers moved to api/tools.py; patch and call the module that OWNS them, not app's
# view of them (see backend-api.mdc on the aliasing trap).
from api import tools as tools_module


def _remote_files(workspace: PurePosixPath) -> dict[str, object]:
    paths = [
        workspace / "cuda13" / "bin" / "nvcc",
        workspace / "hpcx-v2.25.1-gcc-doca_ofed-ubuntu24.04-cuda13-aarch64" / "hpcx-init.sh",
        workspace / "nccl" / "bins" / "all_reduce_perf",
        workspace / "nvloom" / "nvloom_cli",
        workspace / "cmake-3.28.1-linux-aarch64" / "bin" / "cmake",
    ]
    return {str(path): object() for path in paths}


def test_workspace_payload_probes_execution_host_for_pure_posix_path(monkeypatch):
    workspace = PurePosixPath("/home/cmods/workspace")
    calls = []

    def stat_dir(base, *, patterns, maxdepth, role="control"):
        calls.append((base, patterns, maxdepth, role))
        return _remote_files(workspace)

    monkeypatch.setattr(app_module.host_exec, "stat_dir", stat_dir)

    payload = tools_module._workspace_toolchain_status_payload(workspace, "13")

    assert payload["all_ready"] is True
    assert payload["nvloom_cli"] is True
    assert payload["cmake_nvloom_bundle"] is True
    assert calls == [
        (
            workspace,
            (
                "all_reduce_perf",
                "cmake",
                "hpcx-init.sh",
                "hpcx-mt-init-ompi.sh",
                "nvcc",
                "nvloom_cli",
            ),
            5,
            "control",
        )
    ]


def test_workspace_toolchain_endpoint_never_stats_the_container(monkeypatch):
    workspace = PurePosixPath("/home/cmods/workspace")
    monkeypatch.setattr(tools_module, "_resolve_tools_destination", lambda _raw: workspace)
    monkeypatch.setattr(
        app_module.host_exec,
        "stat_dir",
        lambda *_args, **_kwargs: _remote_files(workspace),
    )

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        response = client.get(
            "/api/tools/workspace-toolchain-status",
            query_string={"workspace": str(workspace), "cuda_major": "13"},
        )

    assert response.status_code == 200
    assert response.get_json()["all_ready"] is True


def test_container_status_accepts_a_remote_symlink_to_directory(monkeypatch):
    destination = PurePosixPath("/home/cmods/workspace/llmb/images")
    # The path belongs to the EXECUTION host, so resolution is textual -- that is what a connection
    # does, and the function under test documents it. Pinned here because the test suite runs with
    # RACK_EXEC_MODE=local, where resolve() legitimately consults the local filesystem: on macOS
    # /home is a firmlink to /System/Volumes/Data/home, so an unpatched resolve rewrote this path and
    # failed the assertion for a reason that has nothing to do with symlink handling (and could not
    # happen on the Linux host this models).
    monkeypatch.setattr(app_module.host_exec, "resolve", lambda path, **_kw: str(path))
    monkeypatch.setattr(app_module.host_exec, "is_dir", lambda path: path == destination)
    monkeypatch.setattr(app_module.host_exec, "is_file", lambda path: path == destination)

    resolved = tools_module._tools_destination_dir_for_status_probe(str(destination))

    assert resolved == destination


def test_sqsh_nonempty_is_checked_on_execution_host(monkeypatch):
    calls = []

    class Result:
        returncode = 0

    def run_on_host(argv, *, timeout):
        calls.append((argv, timeout))
        return Result()

    monkeypatch.setattr(app_module.host_exec, "run_on_host", run_on_host)
    path = PurePosixPath("/cluster/llmb/images/nemo.sqsh")

    assert tools_module._tools_sqsh_nonempty(path) is True
    assert calls == [(["test", "-s", str(path)], 30)]
