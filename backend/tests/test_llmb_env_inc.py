"""bin/scripts/lib/llmb_env.inc.sh — which UCX values reach llmb-run's in-container ranks.

Host pins exported by the fabric probe are dropped (they kill every container rank), but a value
the launch STATED -- the cluster's nccl_env or the operator's dialog, listed by Rack Status in
NVIS_EXTRA_ENV_KEYS -- is kept and forced into the job's --export. That is how a site pins MPI's
own transport for a rack whose nodes disagree on their RoCE VFs (Nscale NC U6/V6/R24).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "bin" / "scripts" / "lib" / "llmb_env.inc.sh"


def _run(env: dict[str, str]) -> tuple[dict[str, str], str]:
    script = (
        f'source "{LIB}"\n'
        "nvis_llmb_drop_host_ucx_pins\n"
        "nvis_llmb_force_export_stated\n"
        'printf "UCX_TLS=%s\\n" "${UCX_TLS-<unset>}"\n'
        'printf "UCX_NET_DEVICES=%s\\n" "${UCX_NET_DEVICES-<unset>}"\n'
        'printf "NVIS_FORCE_EXPORT_VARS=%s\\n" "${NVIS_FORCE_EXPORT_VARS-<unset>}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=True,
    )
    out = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    return out, proc.stderr


def test_host_pins_are_dropped_when_nothing_was_stated():
    out, err = _run({"UCX_TLS": "^tcp", "UCX_NET_DEVICES": "mlx5_0:1"})
    assert out["UCX_TLS"] == "<unset>"
    assert out["UCX_NET_DEVICES"] == "<unset>"
    assert "unsetting UCX_TLS=^tcp" in err
    # Nothing stated, nothing to force: the variable is not invented.
    assert out["NVIS_FORCE_EXPORT_VARS"] == "<unset>"


def test_stated_values_survive_and_are_force_exported():
    out, err = _run({
        "UCX_TLS": "^tcp",
        "UCX_NET_DEVICES": "enP22s22f0np0",
        "OMPI_MCA_pml": "ucx",
        "NVIS_EXTRA_ENV_KEYS": "OMPI_MCA_pml UCX_NET_DEVICES",
        "NVIS_FORCE_EXPORT_VARS": "RUN_CONF_IMAGE PMIX_MCA_gds MELLANOX_VISIBLE_DEVICES",
    })
    # Stated -> kept; the unstated host pin beside it still goes.
    assert out["UCX_NET_DEVICES"] == "enP22s22f0np0"
    assert out["UCX_TLS"] == "<unset>"
    assert "keeping UCX_NET_DEVICES=enP22s22f0np0" in err
    assert out["NVIS_FORCE_EXPORT_VARS"].split() == [
        "RUN_CONF_IMAGE",
        "PMIX_MCA_gds",
        "MELLANOX_VISIBLE_DEVICES",
        "OMPI_MCA_pml",
        "UCX_NET_DEVICES",
    ]


def test_force_export_does_not_duplicate_and_starts_clean():
    out, _ = _run({"NVIS_EXTRA_ENV_KEYS": "A B", "NVIS_FORCE_EXPORT_VARS": "B"})
    assert out["NVIS_FORCE_EXPORT_VARS"].split() == ["B", "A"]
    out, _ = _run({"NVIS_EXTRA_ENV_KEYS": "A"})
    assert out["NVIS_FORCE_EXPORT_VARS"] == "A"


def test_keep_ucx_env_keeps_host_pins_for_host_runs():
    out, _ = _run({"UCX_TLS": "^tcp", "NVIS_LLMB_KEEP_UCX_ENV": "1"})
    assert out["UCX_TLS"] == "^tcp"


@pytest.mark.parametrize("launcher", ["run_llmb_nccl", "run_llmb_nemo"])
def test_both_llmb_launchers_source_the_lib(launcher):
    text = (LIB.parent.parent / f"{launcher}.sh").read_text()
    assert 'source "${_SCRIPT_DIR}/lib/llmb_env.inc.sh"' in text
    assert f"nvis_llmb_container_wiring {launcher}" in text


def test_the_wiring_drops_host_pins_and_forces_the_stated_keys():
    body = (LIB.read_text())
    wiring = body[body.index("nvis_llmb_container_wiring() {"):]
    assert "nvis_llmb_drop_host_ucx_pins" in wiring and "nvis_llmb_force_export_stated" in wiring
