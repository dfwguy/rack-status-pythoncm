# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""The node gate: clean up after one job and check every node before the next.

Jobs run back to back on the same nodes inherited what the last one left (Nscale NC Exemplar run,
2026-10-20): Nemotron 3 ran out of GPU memory on GPUs still holding ~270 GiB, eight jobs in a row started
on a GPU that had just thrown an uncorrectable ECC error, and 127 of 128 nodes carried stale NCCL /dev/shm
segments. The gate is a validation preset (node, rack and multi-rack, from the tool list), and its per-node
script runs inside one exclusive Slurm job -- which is what makes killing leftover GPU processes safe.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import validation_presets
from api import validation_runs
from catalog import models, seed

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "bin" / "scripts"
GATE = SCRIPTS / "node_gate.sh"
SBATCH = SCRIPTS / "node_gate.sbatch"


def _bash4() -> str:
    for cand in ("/opt/homebrew/bin/bash", "/usr/local/bin/bash", "/bin/bash", "/usr/bin/bash"):
        if Path(cand).exists() and subprocess.run([cand, "-c", "((BASH_VERSINFO[0] >= 4))"], check=False).returncode == 0:
            return cand
    pytest.skip("needs bash 4+")


# ---------------------------------------------------------------- the recipe and the tool


def test_the_gate_is_a_preset_at_every_scope_with_no_container_and_a_20_minute_ceiling():
    row = {r["id"]: r for r in validation_presets.catalogue()}["node-gate"]
    assert row["requirements"]["scopes"] == ["node", "rack", "multirack"]
    assert row["defaults"]["container_profile"] is None and row["image"] is None
    assert row["defaults"]["max_runtime_minutes"] == 20
    gate = validation_presets.resolve_preset("node_gate")
    with pytest.raises(ValueError, match="max_runtime_minutes=20"):
        validation_presets.normalize_options({"scope": "rack", "max_runtime_minutes": 60}, gate)


def test_the_tool_list_offers_it_on_the_rack_node_and_multirack_boards():
    tool = models.Tool.from_dict(next(t for t in seed.SEED_TOOLS if t["id"] == "preset-node-gate"))
    assert tool.validate() == []
    assert {"rackDetail.validation", "multirack"} <= set(tool.surfaces)
    assert tool.launch.kind == "validation-preset" and tool.launch.preset_id == "node-gate"
    assert tool.launch.scopes == ["node", "rack", "multirack"]
    assert tool.category == "maintenance"


def test_every_preset_records_its_activity_under_a_named_tool():
    """An unlisted preset failed its launch with a KeyError, after the plan had been reviewed."""
    assert {p.id for p in validation_presets.PRESETS} <= set(validation_runs.PRESET_ACTIVITY_TOOL)


# ---------------------------------------------------------------- the dispatcher


def _dispatch(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "NVIS_PREPARED_NODE_COUNT": "2"}
    return subprocess.run([_bash4(), str(SCRIPTS / "run-validation-preset.sh"), "--preset", "node-gate",
                           "--nodelist", "n1,n2", "--log-dir", str(tmp_path), "--dry-run", *extra],
                          capture_output=True, text=True, env=env, check=False)


def test_the_dispatcher_holds_the_exact_nodes_exclusively_one_task_each(tmp_path):
    proc = _dispatch(tmp_path)
    assert proc.returncode == 0, proc.stderr
    plan = next(line for line in proc.stdout.splitlines() if line.startswith("PLAN: sbatch"))
    for part in ("--exclusive", "--time=00:20:00", "--nodes=2", "--nodelist=n1\\,n2", "--ntasks-per-node=1",
                 f"NVIS_NODE_GATE_STATE={tmp_path}/node-gate-state", "node_gate.sbatch"):
        assert part in plan, part
    assert "VALIDATION_PRESET_DRY_RUN preset=node-gate" in proc.stdout


def test_the_dispatcher_refuses_to_stage_a_container_for_it(tmp_path):
    proc = _dispatch(tmp_path, "--storage", "local")
    assert proc.returncode == 2 and "nothing to stage" in proc.stderr


# ---------------------------------------------------------------- one node's check, against stand-ins


_NVIDIA_SMI = r"""#!/usr/bin/env bash
case "$*" in
  *--query-compute-apps*) if [[ -f "$FAKE/killed" || ! -f "$FAKE/procs" ]]; then :; else cat "$FAKE/procs"; fi ;;
  -L) for i in $(seq 0 $((${FAKE_GPUS:-4} - 1))); do echo "GPU $i: NVIDIA GB300 (UUID: GPU-$i)"; done ;;
  *--query-gpu*) for i in $(seq 0 $((${FAKE_GPUS:-4} - 1))); do
                   echo "$i, ${FAKE_MEM:-0}, ${FAKE_ECC:-0}, ${FAKE_REASON:-0x0000000000000001}"; done ;;
  -q) for i in $(seq 0 $((${FAKE_GPUS:-4} - 1))); do
        echo "    Fabric"; echo "        State                             : ${FAKE_FABRIC:-Completed}"
        echo "        CliqueId                          : $([[ -n ${FAKE_SPLIT:-} && $i == 3 ]] && echo 9 || echo 32766)"; done ;;
esac
"""
_KILL_TRACKING_SUDO = r"""#!/usr/bin/env bash
[[ "$1" == -n ]] && shift
echo "$*" >> "$FAKE/sudo.log"
if [[ "$1" == kill ]]; then touch "$FAKE/killed"; exit 0; fi
exec "$@"
"""


def _node(tmp_path: Path, **fake: str) -> dict:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    state = tmp_path / "state"
    shm = tmp_path / "shm"
    shm.mkdir(exist_ok=True)
    stubs = {
        "nvidia-smi": _NVIDIA_SMI,
        "sudo": _KILL_TRACKING_SUDO,
        "dmesg": '#!/usr/bin/env bash\ncat "$FAKE/dmesg" 2>/dev/null\n',
        "enroot": '#!/usr/bin/env bash\n[[ $1 == list ]] && cat "$FAKE/containers" 2>/dev/null; exit 0\n',
        "systemctl": ('#!/usr/bin/env bash\ncase "$1" in\n'
                      '  list-unit-files) echo "${2} enabled" ;;\n'
                      '  is-active) [[ -f "$FAKE/down-${3}" ]] && exit 3; exit 0 ;;\n'
                      '  restart) [[ -f "$FAKE/stuck-${2}" ]] && exit 1; rm -f "$FAKE/down-${2}" ;;\nesac\n'),
        "hostname": "#!/usr/bin/env bash\necho node7\n",
        "sleep": "#!/usr/bin/env bash\nexit 0\n",
    }
    for name, body in stubs.items():
        (fakebin / name).write_text(body)
        (fakebin / name).chmod(0o755)
    env = {"PATH": f"{fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "FAKE": str(tmp_path),
           "NVIS_NODE_GATE_SHM_DIR": str(shm), "NVIS_NODE_GATE_KILL_WAIT": "0", **fake}
    r = subprocess.run([_bash4(), str(GATE), str(state)], capture_output=True, text=True, env=env, timeout=60,
                       check=True)
    line = next(x for x in r.stdout.splitlines() if x.startswith("NVIS_NODE_GATE_JSON="))
    return json.loads(line.split("=", 1)[1])


def test_a_clean_node_passes(tmp_path):
    v = _node(tmp_path)
    assert (v["node"], v["status"], v["codes"], v["actions"]) == ("node7", "pass", [], [])


def test_leftovers_are_cleaned_and_the_node_still_passes(tmp_path):
    (tmp_path / "procs").write_text("4242\n4243\n")
    (tmp_path / "containers").write_text("pyxis_old_job\n")
    (tmp_path / "shm").mkdir()
    (tmp_path / "shm" / "nccl-AbCd").write_text("x")
    (tmp_path / "down-nvidia-imex").touch()
    v = _node(tmp_path, RACK_NODE_SUDO="1")
    assert v["status"] == "pass", v
    assert "killed 2 leftover GPU process(es)" in v["actions"]
    assert any("removed 1 file(s)" in a for a in v["actions"]) and not (tmp_path / "shm" / "nccl-AbCd").exists()
    assert "restarted nvidia-imex" in v["actions"]
    assert "kill -9 4242 4243" in (tmp_path / "sudo.log").read_text()  # the cluster said: node commands via sudo


@pytest.mark.parametrize(("fake", "code"), [
    ({"FAKE_MEM": "270000"}, "gpu-memory-held"),
    ({"FAKE_ECC": "1"}, "ecc-uncorrected"),
    ({"FAKE_REASON": "0x0000000000000088"}, "power-brake"),
    ({"FAKE_FABRIC": "In Progress"}, "fabric-not-completed"),
    ({"FAKE_SPLIT": "1"}, "clique-split"),
    ({"FAKE_GPUS": "3", "NVIS_NODE_GATE_GPUS": "4"}, "gpu-missing"),
])
def test_a_node_unfit_for_the_next_job_fails_with_its_reason(tmp_path, fake, code):
    v = _node(tmp_path, **fake)
    assert v["status"] == "fail" and code in v["codes"], v


def test_a_service_that_will_not_come_back_fails_the_node(tmp_path):
    (tmp_path / "down-nvidia-dcgm").touch()
    (tmp_path / "stuck-nvidia-dcgm").touch()
    v = _node(tmp_path)
    assert v["status"] == "fail" and "service-down" in v["codes"]


def test_a_hardware_xid_warns_on_the_first_gate_and_fails_when_it_is_new(tmp_path):
    xid = "NVRM: Xid (PCI:0008:06:00): 48, An uncorrectable double bit error (DBE)\n"
    (tmp_path / "dmesg").write_text(xid)
    first = _node(tmp_path)
    assert first["status"] == "warning" and "since boot" in first["warnings"][0]
    assert _node(tmp_path)["status"] == "pass"  # the same line again: nothing new
    (tmp_path / "dmesg").write_text(xid + xid.replace("48,", "95,"))
    again = _node(tmp_path)
    assert again["status"] == "fail" and "new-hw-xid" in again["codes"]


def test_an_application_xid_is_not_a_hardware_fault(tmp_path):
    (tmp_path / "dmesg").write_text("NVRM: Xid (PCI:0008:06:00): 31, MMU Fault: ENGINE GRAPH\n")
    assert _node(tmp_path)["status"] == "pass"


# ---------------------------------------------------------------- the Slurm job


def test_a_node_that_never_reports_is_failed_by_name(tmp_path):
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "srun").write_text(
        '#!/usr/bin/env bash\necho \'NVIS_NODE_GATE_JSON={"node":"n1","status":"pass","codes":[],"reason":"",'
        '"warnings":[],"actions":[]}\'\n')
    (fakebin / "scontrol").write_text("#!/usr/bin/env bash\necho n1; echo n2\n")
    for f in ("srun", "scontrol"):
        (fakebin / f).chmod(0o755)
    results = tmp_path / "out.results"
    env = {"PATH": f"{fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}", "SLURM_JOB_NUM_NODES": "2",
           "SLURM_JOB_NODELIST": "n[1-2]", "NVIS_NODE_GATE_RESULTS": str(results),
           "NVIS_NODE_GATE_STATE": str(tmp_path / "st"), "NVIS_BIN_SCRIPTS": str(SCRIPTS)}
    r = subprocess.run([_bash4(), str(SBATCH)], env=env, capture_output=True, text=True, timeout=30, check=False)
    rows = [json.loads(line.split("=", 1)[1]) for line in results.read_text().splitlines()]
    assert r.returncode == 3
    assert {(row["node"], row["status"]) for row in rows} == {("n1", "pass"), ("n2", "fail")}
    assert next(row for row in rows if row["node"] == "n2")["codes"] == ["no-answer"]


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_the_gate_scripts_are_shellcheck_clean():
    r = subprocess.run(["shellcheck", "-S", "warning", str(GATE), str(SBATCH)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout
