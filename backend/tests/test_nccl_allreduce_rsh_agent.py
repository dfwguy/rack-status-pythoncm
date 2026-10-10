# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The MPI daemons of a multi-node NCCL run log in as the job's user, whatever its ssh config says.

nccl_allreduce.sbatch launches orted over ssh. On Nscale NC (job 136390, 270 nodes) the submitter's
NFS-shared ~/.ssh/config carried a P2P block -- ``Host sys1-*-su*`` / ``User nvis-p2p`` / a service
IdentityFile -- so every daemon tried to log in as the service account and was refused. The agent now
ignores that file; ``NVIS_MPI_RSH_AGENT`` still overrides it.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SBATCH = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "nccl_allreduce.sbatch"
NC_P2P_BLOCK = "Host sys1-*-su*\n  User nvis-p2p\n  IdentityFile ~/.ssh/id_nvis_p2p_node\n"


def _default_agent() -> list[str]:
    m = re.search(r'^RSH_AGENT="\$\{NVIS_MPI_RSH_AGENT:-([^}]*)\}"$', SBATCH.read_text(), re.MULTILINE)
    assert m, "nccl_allreduce.sbatch must define RSH_AGENT with an NVIS_MPI_RSH_AGENT override"
    return shlex.split(m.group(1))


def test_mpirun_uses_the_agent_and_the_agent_skips_the_user_config():
    text = SBATCH.read_text()
    assert '--mca plm_rsh_agent "${RSH_AGENT}"' in text
    agent = _default_agent()
    assert agent[0] == "ssh"
    assert agent[agent.index("-F") + 1] == "/dev/null"
    assert "BatchMode=yes" in agent


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs an ssh client to resolve the config")
def test_a_remapping_user_config_does_not_reach_the_daemon_login(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(NC_P2P_BLOCK)
    env = {**os.environ, "HOME": str(home)}
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()

    def resolved(cmd: list[str]) -> dict[str, list[str]]:
        out = subprocess.run([*cmd, "-G", "sys1-gso1-p-phy-gpu1-su19"], capture_output=True, text=True,
                             env=env, check=True).stdout
        conf: dict[str, list[str]] = {}
        for line in out.splitlines():
            key, _, value = line.partition(" ")
            conf.setdefault(key, []).append(value)
        return conf

    # Plain ssh, the old agent: the P2P block wins and the daemon logs in as the service account.
    assert resolved(["ssh", "-F", str(home / ".ssh" / "config")])["user"] == ["nvis-p2p"]
    # The agent the sbatch now uses: the job's own user, and no service key forced on it.
    conf = resolved(_default_agent())
    assert conf["user"] == [me]
    assert not any("id_nvis_p2p_node" in f for f in conf.get("identityfile", []))
