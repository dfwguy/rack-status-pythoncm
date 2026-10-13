# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""A value stated for a launch wins over the launcher's own tuning (lib/stated_env.inc.sh).

NeMo 194780 (U20+U23, Nscale NC 2026-10-13) received the cluster's multi-rack nccl_env --
NCCL_BUFFSIZE=1048576 reached all 144 ranks -- yet ran with NCCL_IB_QPS_PER_CONNECTION=2 on every
rank where the cluster said 1: the NeMo launcher exports 2 as part of its tuning, after the job's
environment arrived. The scripts hold defaults; the cluster record and the launch dialog override them.
Each launcher on a path that carries a cluster's nccl_env now captures the stated keys
(NVIS_EXTRA_ENV_KEYS) before its tuning and restores them after it. These tests run each launcher's
real tuning section, with the fabric probes stubbed, and read what the launch would get.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
LIB = SCRIPTS / "lib" / "stated_env.inc.sh"


def _bash(script: str, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(["bash", "-c", script], env=full, cwd=cwd, capture_output=True, text=True,
                          timeout=60, check=False)


def _values(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def _between(text: str, start: str, end: str) -> str:
    """The launcher text from the line holding *start* through the line holding *end*."""
    i = text.index(start)
    j = text.index(end, i)
    return text[text.rindex("\n", 0, i) + 1:text.index("\n", j) + 1]


class TestTheLibrary:
    def test_a_stated_value_survives_a_later_export_and_says_what_it_replaced(self):
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; export NCCL_IB_QPS_PER_CONNECTION=2; '
                  'nvis_stated_env_restore nemo; echo "Q=$NCCL_IB_QPS_PER_CONNECTION"',
                  {"NVIS_EXTRA_ENV_KEYS": "NCCL_IB_QPS_PER_CONNECTION", "NCCL_IB_QPS_PER_CONNECTION": "1"})
        assert _values(r.stdout)["Q"] == "1"
        assert "nemo: NCCL_IB_QPS_PER_CONNECTION=1 (stated for this launch; replaces the launcher's " \
               "NCCL_IB_QPS_PER_CONNECTION=2)" in r.stderr

    def test_an_unstated_variable_keeps_the_launchers_value(self):
        # A leftover from the submitting shell is not a statement: the script's tuning still applies.
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; export NCCL_MIN_CTAS=16; nvis_stated_env_restore; '
                  'echo "C=$NCCL_MIN_CTAS"', {"NVIS_EXTRA_ENV_KEYS": "NCCL_BUFFSIZE", "NCCL_MIN_CTAS": "4"})
        assert _values(r.stdout)["C"] == "16"

    def test_a_stated_value_the_launcher_unset_comes_back(self):
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; unset NCCL_MNNVL_ENABLE; nvis_stated_env_restore; '
                  'echo "M=${NCCL_MNNVL_ENABLE-gone}"',
                  {"NVIS_EXTRA_ENV_KEYS": "NCCL_MNNVL_ENABLE", "NCCL_MNNVL_ENABLE": "0"})
        assert _values(r.stdout)["M"] == "0"

    def test_nothing_stated_changes_nothing_and_says_nothing(self):
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; export UCX_TLS=rc; nvis_stated_env_restore; echo "T=$UCX_TLS"',
                  {})
        assert _values(r.stdout)["T"] == "rc" and r.stderr == ""

    def test_a_listed_name_that_is_not_set_or_not_a_name_is_ignored(self):
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; export UCX_TLS=rc; nvis_stated_env_restore; '
                  'echo "T=$UCX_TLS"; echo "N=${#_NVIS_STATED_ENV[@]}"',
                  {"NVIS_EXTRA_ENV_KEYS": "UCX_TLS $(id) 9BAD"})
        assert _values(r.stdout) == {"T": "rc", "N": "0"}

    def test_a_value_with_spaces_and_equals_signs_is_restored_whole(self):
        r = _bash(f'. "{LIB}"; nvis_stated_env_capture; export NVIS_X=other; nvis_stated_env_restore; '
                  'printf "X=%s\\n" "$NVIS_X"', {"NVIS_EXTRA_ENV_KEYS": "NVIS_X", "NVIS_X": "a=b c"})
        assert _values(r.stdout)["X"] == "a=b c"


class TestTheNemoLauncher:
    """nemo-nemotron5h_56b-mbridge-fp8.sh: single-rack and cross-rack NeMo."""

    TEXT = (SCRIPTS / "nemo-nemotron5h_56b-mbridge-fp8.sh").read_text()

    def test_the_cluster_wins_over_the_tuning_block_for_the_preflight(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        shutil.copy(LIB, lib / "stated_env.inc.sh")
        (lib / "fabric.inc.sh").write_text("")  # no probes: the tuning block's own exports are the point
        fake = tmp_path / "bin"
        fake.mkdir()
        (fake / "srun").write_text("#!/bin/bash\nexit 0\n")
        (fake / "srun").chmod(0o755)
        snippet = _between(self.TEXT, "# What this launch states", "nvis_stated_env_restore nemo")
        r = _bash(f'_nvis_lib="{lib}/fabric.inc.sh"; single_rack=0; ns_nic=bond0\n' + snippet
                  + 'echo "Q=$NCCL_IB_QPS_PER_CONNECTION"; echo "C=$NCCL_MIN_CTAS"; echo "B=$NCCL_BUFFSIZE"',
                  {"PATH": f"{fake}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                   "NVIS_EXTRA_ENV_KEYS": "NCCL_BUFFSIZE NCCL_IB_QPS_PER_CONNECTION",
                   "NCCL_IB_QPS_PER_CONNECTION": "1", "NCCL_BUFFSIZE": "1048576"}, cwd=tmp_path)
        values = _values(r.stdout)
        assert values["Q"] == "1", r.stderr
        assert values["C"] == "16"  # not stated: the launcher's default stands
        assert values["B"] == "1048576"

    def test_both_launches_restore_after_their_own_defaults(self):
        t = self.TEXT
        capture = t.index("nvis_stated_env_capture")
        assert capture < t.index("export NCCL_IB_QPS_PER_CONNECTION=2")
        first, second = (i for i in (t.index("nvis_stated_env_restore nemo"),
                                     t.index("nvis_stated_env_restore nemo", t.index("nvis_stated_env_restore nemo") + 1)))
        assert t.index('export NCCL_DEBUG="${NVIS_NCCL_DEBUG:-WARN}"') < first < t.index("all_reduce_perf_mpi")
        assert t.index('export NCCL_DEBUG="${NVIS_NCCL_DEBUG:-INFO}"') < second < t.index("train_rc=0")


class TestTheNcclLauncher:
    """nccl_allreduce.sbatch: the direct NCCL sweep, single-rack and cross-rack."""

    def test_the_cluster_wins_over_the_fabric_probe(self, tmp_path):
        lib = tmp_path / "lib"
        lib.mkdir()
        shutil.copy(LIB, lib / "stated_env.inc.sh")
        (lib / "fabric.inc.sh").write_text(
            "nvis_fabric_export() { export NCCL_IB_HCA==mlx5_0 NCCL_SOCKET_IFNAME=bond0; }\n")
        text = (SCRIPTS / "nccl_allreduce.sbatch").read_text()
        snippet = _between(text, "# What this launch states", "nvis_stated_env_restore nccl_allreduce")
        r = _bash(f'_D="{tmp_path}"\n' + snippet + 'echo "H=$NCCL_IB_HCA"; echo "S=$NCCL_SOCKET_IFNAME"',
                  {"NVIS_EXTRA_ENV_KEYS": "NCCL_IB_HCA", "NCCL_IB_HCA": "=roce_vf_r1,roce_vf_r2"})
        assert _values(r.stdout) == {"H": "=roce_vf_r1,roce_vf_r2", "S": "bond0"}, r.stderr

    def test_the_restore_comes_before_the_forwarding_scan(self):
        # The -x list is built from the environment; it must see the stated values, not the probe's.
        text = (SCRIPTS / "nccl_allreduce.sbatch").read_text()
        assert text.index("nvis_stated_env_restore nccl_allreduce") < text.index("EXTRA_X=()")


class TestTheLlmbLauncher:
    """run_llmb_nccl.sh: the in-container LLMB sweep."""

    def test_a_stated_netdev_survives_the_container_transport_pick(self, tmp_path):
        # NC's rack list states UCX_NET_DEVICES but not UCX_TLS; the cross-rack transport pick used to
        # replace the netdev with the bootstrap one.
        lib = tmp_path / "lib"
        lib.mkdir()
        shutil.copy(LIB, lib / "stated_env.inc.sh")
        shutil.copy(SCRIPTS / "lib" / "llmb_env.inc.sh", lib / "llmb_env.inc.sh")
        stubs = (
            'nvis_nodes_first() { echo n1; }\n'
            'nvis_fabric_export_from_node() { export UCX_NET_DEVICES=mlx5_0:1; }\n'
            'nvis_fabric_container_env() { NVIS_FABRIC_CONTAINER_EXPORTS="MELLANOX_VISIBLE_DEVICES"; }\n'
            'nvis_fabric_mpi_xrack_transport() { export UCX_TLS=tcp UCX_NET_DEVICES=bond0; }\n'
        )
        text = (SCRIPTS / "run_llmb_nccl.sh").read_text()
        snippet = _between(text, "# What this launch states", "nvis_stated_env_restore run_llmb_nccl")
        r = _bash(f'_SCRIPT_DIR="{tmp_path}"\n' + stubs + snippet
                  + 'echo "D=$UCX_NET_DEVICES"; echo "T=$UCX_TLS"; echo "F=$NVIS_FORCE_EXPORT_VARS"',
                  {"NVIS_EXTRA_ENV_KEYS": "UCX_NET_DEVICES", "UCX_NET_DEVICES": "enP22s22f0np0"})
        values = _values(r.stdout)
        assert values["D"] == "enP22s22f0np0", r.stderr
        assert values["T"] == "tcp"  # not stated: the fabric's pick stands
        assert "UCX_NET_DEVICES" in values["F"].split()
