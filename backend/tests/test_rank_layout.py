# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""An llmb job's ranks run rack by rack, and its rendezvous names the host rank 0 is on.

The 512-GPU Exemplar run on Nscale NC (2026-10-20) lost every MoE recipe on its first step and the
Nemotron-H recipe to a 20-minute hang. Slurm numbers tasks in its own node order, and NC hostnames
(gpuN-suM) put the racks at the same position in different SUs side by side -- Q7 and S7 alternated
node by node -- so a 64-rank expert group spanned NVLink domains and DeepEP's fabric-handle import
failed (Slurm 216666). The same hostnames do not compress into a Slurm range, so the NeMo 26.04.01
image's Megatron-Bridge took the whole comma-list SLURM_NODELIST as its master address (216664).

``lib/rank_layout.inc.sh`` writes the rank order itself from the placement Rack Status projects
(``NVIS_NODE_PLACEMENT``) and exports SLURM_HOSTFILE + SLURM_DISTRIBUTION=arbitrary + MASTER_ADDR
into the job; the backend projects the placement for the launches that run these launchers.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import node_targets
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "bin" / "scripts"
LIB = SCRIPTS / "lib" / "rank_layout.inc.sh"

# A stand-in `scontrol show hostnames` that expands one bracket range, as Slurm would.
_SCONTROL = r"""#!/usr/bin/env python3
import re, sys
assert sys.argv[1:3] == ["show", "hostnames"]
if sys.argv[3].count("[") != sys.argv[3].count("]"):
    sys.exit("scontrol: error: Invalid hostlist")
for part in re.findall(r"[^,\[]+(?:\[[^\]]*\])?[^,]*", sys.argv[3]):
    m = re.match(r"^(.*)\[(\d+)-(\d+)\](.*)$", part)
    if m:
        for i in range(int(m.group(2)), int(m.group(3)) + 1):
            print(f"{m.group(1)}{i}{m.group(4)}")
    elif part:
        print(part)
"""


def _q7(n: int) -> str:
    return f"sys1-gso1-p-phy-gpu{n}-su22"


def _s7(n: int) -> str:
    return f"sys1-gso1-p-phy-gpu{n}-su30"


def _layout(tmp_path: Path, nodes: str, per_node: int = 4, **env: str) -> tuple[dict[str, str], list[str], str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "scontrol").write_text(_SCONTROL)
    (bindir / "scontrol").chmod(0o755)
    script = (f'source "{LIB}"\nnvis_rank_layout test "{nodes}" {per_node} "{tmp_path / "logs"}"; rc=$?\n'
              'echo "RC=$rc"; echo "HOSTFILE=${SLURM_HOSTFILE:-}"; echo "DIST=${SLURM_DISTRIBUTION:-}"; '
              'echo "MASTER=${MASTER_ADDR:-}"; echo "FORCE=${NVIS_FORCE_EXPORT_VARS:-}"')
    full = {"PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}", **env}
    r = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, timeout=30, check=True)
    out = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    lines = Path(out["HOSTFILE"]).read_text().splitlines() if out.get("HOSTFILE") else []
    return out, lines, r.stderr


def test_racks_slurm_interleaves_are_laid_out_one_after_the_other(tmp_path):
    # The NC order Slurm gave job 216666: Q7 and S7 alternate because both are gpu91-94.
    interleaved = ",".join(h for n in range(91, 95) for h in (_q7(n), _s7(n)))
    placement = f"Q7={','.join(_q7(n) for n in range(91, 95))};S7={','.join(_s7(n) for n in range(91, 95))}"
    out, lines, _ = _layout(tmp_path, interleaved, NVIS_NODE_PLACEMENT=placement)
    assert out["RC"] == "0" and out["DIST"] == "arbitrary"
    assert len(lines) == 8 * 4
    assert lines[:16] == [_q7(n) for n in range(91, 95) for _ in range(4)]  # one rack, all its ranks
    assert lines[16:] == [_s7(n) for n in range(91, 95) for _ in range(4)]
    assert out["MASTER"] == _q7(91) == lines[0]
    assert {"SLURM_HOSTFILE", "SLURM_DISTRIBUTION", "MASTER_ADDR"} <= set(out["FORCE"].split())


def test_a_rack_placed_as_a_slurm_range_is_expanded(tmp_path):
    _, lines, _ = _layout(tmp_path, "rk2-ct[1-2],rk1-ct[1-2]", per_node=1,
                            NVIS_NODE_PLACEMENT="rk1=rk1-ct[1-2];rk2=rk2-ct[1-2]")
    assert lines == ["rk1-ct1", "rk1-ct2", "rk2-ct1", "rk2-ct2"]


def test_hosts_the_inventory_did_not_place_go_last_in_natural_order(tmp_path):
    _, lines, err = _layout(tmp_path, f"{_s7(92)},{_q7(10)},{_q7(9)},{_s7(91)}", per_node=1,
                              NVIS_NODE_PLACEMENT=f"S7={_s7(91)},{_s7(92)}")
    assert lines == [_s7(91), _s7(92), _q7(9), _q7(10)]
    assert "2 not placed by the inventory" in err


def test_without_placement_the_rendezvous_is_still_named(tmp_path):
    """The Nemotron-H hang needs only MASTER_ADDR; a single rack the inventory does not know still gets it."""
    out, lines, _ = _layout(tmp_path, f"{_q7(92)},{_q7(91)}", per_node=2)
    assert lines == [_q7(91), _q7(91), _q7(92), _q7(92)]
    assert out["MASTER"] == _q7(91)


def test_a_stated_hostfile_is_left_to_the_operator(tmp_path):
    mine = tmp_path / "mine.hosts"
    mine.write_text(f"{_q7(91)}\n")
    out, lines, err = _layout(tmp_path, _q7(91), NVIS_EXTRA_ENV_KEYS="SLURM_HOSTFILE", SLURM_HOSTFILE=str(mine))
    assert out["HOSTFILE"] == str(mine) and lines == [_q7(91)] and out["FORCE"] == "" and "stated" in err


def test_a_stated_master_address_is_kept(tmp_path):
    out, _, _ = _layout(tmp_path, f"{_q7(91)},{_q7(92)}", NVIS_EXTRA_ENV_KEYS="MASTER_ADDR",
                        MASTER_ADDR="10.0.0.1")
    assert out["MASTER"] == "10.0.0.1" and "MASTER_ADDR" not in out["FORCE"].split()


@pytest.mark.parametrize(("nodes", "per_node"), [("a[1-", 4), ("", 4), ("a1", 0)])
def test_nothing_is_exported_when_the_layout_cannot_be_written(tmp_path, nodes, per_node):
    out, _, _ = _layout(tmp_path, nodes, per_node=per_node)
    assert out["RC"] == "1" and out["HOSTFILE"] == "" and out["FORCE"] == ""


# ---------------------------------------------------------------- the launchers


def test_the_nemo_launcher_lays_out_ranks_after_its_nccl_stage_and_before_the_shim():
    text = (SCRIPTS / "run_llmb_nemo.sh").read_text(encoding="utf-8")
    layout = text.index('nvis_rank_layout run_llmb_nemo "${NODELIST}" "${GPUS_PER_NODE}"')
    assert text.index('bash "${_nccl_script}"') < layout < text.index('nvis_sbatch_pin_install "${NODELIST}"')


def test_the_nemo_launcher_can_ask_llmb_for_its_profiled_run():
    """One Exemplar run is captured with Nsight Systems: llmb-run -p, by flag or by env."""
    text = (SCRIPTS / "run_llmb_nemo.sh").read_text(encoding="utf-8")
    assert "--profile) PROFILE=1; shift ;;" in text
    assert '[[ "${NVIS_LLMB_NEMO_PROFILE:-0}" == "1" ]] && PROFILE=1' in text
    assert text.index('[[ "${PROFILE}" -eq 1 ]] && cmd+=( -p )') > text.index('cmd=( "${LLMB_RUN}" "${LLMB_SUBCMD}"')


def _mbridge_lines() -> str:
    """The launcher's own lines that collect Megatron-Bridge arguments and add them to llmb-run."""
    text = (SCRIPTS / "run_llmb_nemo.sh").read_text(encoding="utf-8")
    keep = [line for line in text.splitlines()
            if line.startswith(("MBRIDGE_ARGS=()", "read -r -a MBRIDGE_ARGS", 'for _mb in "${MBRIDGE_ARGS[@]}"'))]
    assert len(keep) == 3, keep
    return "\n".join(keep)


@pytest.mark.parametrize("stated,expected", [
    ("", []),
    ("--moe_expert_rank_capacity_factor=1.5", ["--mbridge-arg=--moe_expert_rank_capacity_factor=1.5"]),
    ("model.a=1  model.b=2", ["--mbridge-arg=model.a=1", "--mbridge-arg=model.b=2"]),
])
def test_the_nemo_launcher_hands_stated_mbridge_args_to_llmb_run(stated, expected):
    """NVIS_LLMB_NEMO_MBRIDGE_ARGS becomes one llmb-run --mbridge-arg per word, after the recipe flags."""
    script = f'set -u\ncmd=( llmb-run submit )\n{_mbridge_lines()}\nprintf "%s\\n" "${{cmd[@]:2}}"'
    out = subprocess.run(["bash", "-c", script], env={**os.environ, "NVIS_LLMB_NEMO_MBRIDGE_ARGS": stated},
                         capture_output=True, text=True, check=True).stdout
    assert out.split() == expected


def test_the_nemo_launcher_takes_mbridge_args_as_a_repeatable_option():
    text = (SCRIPTS / "run_llmb_nemo.sh").read_text(encoding="utf-8")
    assert '--mbridge-arg) MBRIDGE_ARGS+=( "${2:?--mbridge-arg needs a value}" ); shift 2 ;;' in text
    assert '--mbridge-arg=*) MBRIDGE_ARGS+=( "${1#*=}" ); shift ;;' in text
    assert text.index('for _mb in "${MBRIDGE_ARGS[@]}"') > text.index('cmd=( "${LLMB_RUN}" "${LLMB_SUBCMD}"')


def test_the_exemplar_llama_wrapper_lays_out_ranks_before_its_shim():
    text = (SCRIPTS / "run-exemplar-llama.sh").read_text(encoding="utf-8")
    assert text.index("nvis_rank_layout run-exemplar-llama") < text.index('nvis_sbatch_pin_install "$nodelist"')


# ---------------------------------------------------------------- the placement Rack Status projects


def test_the_target_is_grouped_by_the_rack_the_inventory_placed_each_host_in(monkeypatch):
    import native_status

    placed = {_q7(91): "Q7", _s7(91): "S7", _q7(92): "Q7"}
    monkeypatch.setattr(native_status, "placed_racks_of", lambda hosts: {h: placed[h] for h in hosts if h in placed})
    env = node_targets.node_placement_env(f"{_q7(91)},{_s7(91)},{_q7(92)},{_s7(99)}")
    assert env == {"NVIS_NODE_PLACEMENT": f"Q7={_q7(91)},{_q7(92)};S7={_s7(91)}"}


def test_a_template_or_an_unbuilt_inventory_projects_nothing(monkeypatch):
    import native_status

    monkeypatch.setattr(native_status, "placed_racks_of", lambda hosts: None)
    assert node_targets.node_placement_env(f"{_q7(91)},{_q7(92)}") == {}
    assert node_targets.node_placement_env("U22-ct[01-18]") == {}


def test_a_group_the_launcher_could_not_parse_is_dropped():
    assert node_targets.placement_env({"U2": "a,b", "bad;rack": "c", "U3": "d=e", "": "f"}) == {
        "NVIS_NODE_PLACEMENT": "U2=a,b"}
