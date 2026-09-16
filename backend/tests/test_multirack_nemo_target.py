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
"""Naming and exact-node regressions for the multirack NeMo launcher.

Regression: ``_prepare_run_nemo_xrack`` hardcoded ``f"{rack}-ct[{inner}]"``. That is only correct
for Bright naming (``p5-r03-ct12``). On Nscale TX the CT template is ``<rack>-<NN>``, so launching
h02+h03 produced ``ink50-dh01-h02-ct[11-18,28-37]`` — hosts in neither DNS nor Slurm. pdsh failed
ssh 255 on all 36 nodes and sbatch rejected the list with "Invalid node name specified".

The bug was invisible under the default templates, which is why these tests parametrise the
naming: a spec builder that passes only for Bright is exactly what shipped.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("RACK_STATUS_DEMO", "1")
os.environ.setdefault("RACK_WEB_AUTH_DISABLED", "1")
os.environ.setdefault("RACK_EXEC_MODE", "local")

import app as app_module  # noqa: E402
import naming as naming_mod  # noqa: E402
import nvis_id_templates as nid  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
SCRIPTS = BACKEND.parent / "bin" / "scripts"

# The two conventions that matter. Bright is the default; Nscale TX is the one that broke.
BRIGHT = ("p[\\d+]-r[\\d+]", "p[\\d+]-r[\\d+]-ct[\\d+]")
NSCALE_TX = ("ink50-dh01-[a-z][\\d+]", "ink50-dh01-[a-z][\\d+]-[\\d+]")


@pytest.fixture
def prepare():
    """Build the legacy rack tray expression through the shared naming contract.

    The API now replaces this expression with an exact campaign node list before
    launching NeMo. The helper contract remains important to scripts and older
    integrations, so these tests continue to prevent a literal ``-ct`` joiner.
    """

    def _run(rack_tmpl: str, ct_tmpl: str, racks: list[str], ct_range: str = "01-18") -> str:
        naming = nid.get_nvis_naming(rack_tmpl, ct_tmpl)
        return ",".join(naming.ct_blob(rack, ct_range) for rack in dict.fromkeys(racks))

    return _run


# --- the spec follows the template ---------------------------------------------------------


def test_bright_naming_keeps_the_ct_joiner(prepare):
    arg = prepare(*BRIGHT, ["p5-r01"])
    assert arg == "p5-r01-ct[01-18]"


def test_nscale_tx_naming_has_no_ct(prepare):
    """The exact failing case: h02+h03 with the 18-tray GB300 range."""
    arg = prepare(*NSCALE_TX, ["ink50-dh01-h02", "ink50-dh01-h03"], ct_range="11-18,28-37")
    assert arg == "ink50-dh01-h02-[11-18,28-37],ink50-dh01-h03-[11-18,28-37]"
    assert "-ct[" not in arg, "a literal -ct joiner is back"


def test_nscale_spec_matches_the_real_slurm_hostnames(prepare):
    """Sanity-check the spec expands to names Slurm actually has (ink50-dh01-h02-11, …)."""
    arg = prepare(*NSCALE_TX, ["ink50-dh01-h02"], ct_range="11-18,28-37")
    prefix, inner = arg.split("[", 1)
    assert prefix == "ink50-dh01-h02-"
    assert inner.rstrip("]") == "11-18,28-37"
    # Verified against `sinfo -N` on the cluster: nodes are <rack>-<NN>, no ct segment.
    assert re.fullmatch(r"ink50-dh01-h02-", prefix)


def test_multiple_racks_are_comma_joined(prepare):
    arg = prepare(*BRIGHT, ["p5-r01", "p5-r02", "p5-r03"])
    assert arg == "p5-r01-ct[01-18],p5-r02-ct[01-18],p5-r03-ct[01-18]"


def test_duplicate_racks_are_collapsed(prepare):
    arg = prepare(*BRIGHT, ["p5-r01", "p5-r01"])
    assert arg == "p5-r01-ct[01-18]"


def test_the_spec_agrees_with_ct_blob(prepare):
    """The builder must produce exactly what the shared naming helper produces."""
    for rack_tmpl, ct_tmpl, rack, rng in (
        (*BRIGHT, "p5-r01", "01-18"),
        (*NSCALE_TX, "ink50-dh01-h02", "11-18,28-37"),
    ):
        arg = prepare(rack_tmpl, ct_tmpl, [rack], ct_range=rng)
        naming = nid.get_nvis_naming(rack_tmpl, ct_tmpl)
        assert arg == naming.ct_blob(rack, rng)


# --- structural guards ---------------------------------------------------------------------


def test_prepare_uses_exact_campaign_nodes_without_hardcoded_ct_joiner():
    body = (BACKEND / "app.py").read_text(encoding="utf-8")
    m = re.search(r"def _prepare_run_nemo_xrack\(.*?\n(?=^def )", body, re.S | re.M)
    assert m, "_prepare_run_nemo_xrack not found"
    # Only executable lines: the docstring and comments legitimately mention the broken -ct form.
    code = [ln for ln in m.group(0).splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert not re.search(r'["\']\{?\w*\}?-ct\[', ln), f"the -ct literal is back: {ln.strip()}"
    joined = "\n".join(code)
    assert "exact_nodes" in joined
    assert "_multirack_campaign_snapshot" in joined


def test_xrack_script_derives_rack_ids_from_the_template():
    """Both rack-id derivations in run-nemo-xrack.sh must use nvis_nodes_rack_of.

    The hardcoded ``bos01-[a-z][0-9]+`` sed existed twice; only the rack COUNT was converted.
    The surviving one built the --output filename, so outside Boston it degraded to all N
    hostnames joined by '+' and blew the 255-byte limit sbatch enforces.
    """
    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    code = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert "bos01-[a-z]" not in ln, f"hardcoded Boston rack pattern in code: {ln.strip()}"
    assert body.count("nvis_nodes_rack_of") >= 2, "both derivations should use nvis_nodes_rack_of"


@pytest.mark.parametrize(
    "spec,expected",
    [
        # Bright: the joiner carries "ct"
        ("bos01-b05-ct[01-18]", "bos01-b05"),
        ("p5-r01-ct[01-18]", "p5-r01"),
        ("p5-r01-ct01", "p5-r01"),
        # Nscale TX: the joiner is just "-". Matching a literal "-ct" left the whole spec as the
        # rack id, so nvis_nodes_resolve found nothing and the intended path was skipped.
        ("ink50-dh01-h02-[11-18,28-37]", "ink50-dh01-h02"),
        ("ink50-dh01-h03-[11-18]", "ink50-dh01-h03"),
        # A real hostname must pass through untouched — stripping a bare trailing -<N> would
        # truncate it to a prefix instead of leaving it for the hostlist fallback.
        ("ink50-dh01-h02-11", "ink50-dh01-h02-11"),
        ("bos01-b05-11-s1-dgx-05-c01", "bos01-b05-11-s1-dgx-05-c01"),
    ],
)
def test_rack_id_strip_is_joiner_agnostic(spec: str, expected: str):
    """The sed in run-nemo-xrack.sh must derive the rack id under either naming."""
    import subprocess

    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    line = next((ln for ln in body.splitlines() if "racks+=(" in ln and "sed -E" in ln), None)
    assert line, "could not find the rack-id strip in run-nemo-xrack.sh"
    m = re.search(r"sed -E '([^']+)'", line)
    assert m, f"could not read the sed expression from: {line.strip()}"
    sed_expr = m.group(1)
    out = subprocess.run(
        ["bash", "-c", 'printf %s "$1" | sed -E "$2"', "_", spec, sed_expr],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.stdout.strip() == expected, out.stderr


@pytest.mark.parametrize(
    "spec_tag,expected",
    [
        # This site's range contains a comma, so a plain IFS=',' split tears it in half.
        (
            "ink50-dh01-h02-[11-18,28-37],ink50-dh01-h03-[11-18,28-37]",
            ["ink50-dh01-h02-[11-18,28-37]", "ink50-dh01-h03-[11-18,28-37]"],
        ),
        # Bright ranges have no inner comma — this is why the bug stayed hidden.
        ("bos01-b05-ct[01-18],bos01-b06-ct[01-18]", ["bos01-b05-ct[01-18]", "bos01-b06-ct[01-18]"]),
        ("p5-r01-ct[01-18]", ["p5-r01-ct[01-18]"]),
        # Plain hostnames, no brackets at all.
        ("ink50-dh01-h02-11,ink50-dh01-h02-12", ["ink50-dh01-h02-11", "ink50-dh01-h02-12"]),
        ("ink50-dh01-h02-[11-18,28-37]", ["ink50-dh01-h02-[11-18,28-37]"]),
    ],
)
def test_spec_split_keeps_bracketed_ranges_intact(spec_tag: str, expected: list[str]):
    """Commas inside [...] belong to the range, not to the spec list."""
    import subprocess

    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    # The whole split block, bounded by the `racks=()` that follows it — anchoring on the first
    # `_specs+=` would cut the loop in half and yield an unbalanced snippet.
    m = re.search(r"^(_specs=\(\)\n.*?)\nracks=\(\)$", body, re.S | re.M)
    assert m, "could not find the bracket-aware split in run-nemo-xrack.sh"
    snippet = m.group(1)
    script = f'spec_tag="$1"\n{snippet}\nprintf "%s\\n" "${{_specs[@]}}"\n'
    out = subprocess.run(
        ["bash", "-c", script, "_", spec_tag], capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0, out.stderr
    assert [ln for ln in out.stdout.splitlines() if ln] == expected, out.stdout


def test_staging_verify_runs_inside_an_exact_node_slurm_allocation():
    """The web container must not need pdsh or compute-node SSH credentials."""
    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    code = [ln for ln in body.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(re.search(r"(^|[ (])pdsh([ )]|$)", ln) for ln in code)
    assert "validation_container_stage.sbatch" in body
    assert "--parsable --wait --no-requeue" in body
    assert '--nodelist="$_hostcsv"' in body
    assert "NVIS_STAGE_RSYNC=0" in body
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1" in body
    assert "NVIS_STAGE_GPU_RECOVERY_CHECK=${_gpu_recovery_check}" in body
    assert "NVIS_SCRIPTS_DIR=${node_script_dir}" in body
    assert "NVIS_SCRIPTS_DIR=${SCRIPT_DIR}" not in body
    assert '"$node_script_dir" == /app/*' in body


def test_staging_verify_requires_positive_markers_from_every_node():
    """A successful sbatch reply alone cannot hide an absent node or skipped gate."""
    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    assert "grep -c '^IMAGE_STAGE_READY '" in body
    assert "grep -c '^GPU_RECOVERY_READY '" in body
    assert re.search(r"_image_ok != n", body)
    assert re.search(r'_gpu_ok" -ne "\$n', body)
    assert "aborting before NeMo workload submission" in body


def test_submit_dry_run_precedes_every_sbatch_execution():
    """Dry-run proof must not allocate preflight nodes or launch the workload."""
    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    dry_run = body.index('if [[ "${NVIS_NEMO_SUBMIT_DRY_RUN:-0}" == "1" ]]')
    stage_exec = body.index('_stage_reply="$(')
    workload_exec = body.index('sbatch "${slurm_submit_as[@]}" -N')
    assert dry_run < stage_exec < workload_exec


def test_allocation_gate_checks_gpu_recovery_fail_closed():
    stage = (SCRIPTS / "validation_container_stage.sbatch").read_text(encoding="utf-8")
    assert "NVIS_STAGE_GPU_RECOVERY_CHECK" in stage
    assert "gpu_recovery_action" in stage
    assert "GPU_RECOVERY_READY" in stage
    assert "GPU_RECOVERY_FAIL" in stage
    assert '--nodes="${nodes}" --ntasks="${nodes}" --ntasks-per-node=1' in stage
    assert "--kill-on-bad-exit=1" in stage


def test_nemo_provenance_includes_the_allocation_gate():
    import validation_provenance

    bundles, files = validation_provenance.PROFILES["nemo-xrack"]
    assert bundles == ("mnubergemm", "nemo")
    assert "validation_container_stage.sbatch" in files


def test_allocation_gate_prepares_imex_channel0_without_persistence():
    stage = (SCRIPTS / "validation_container_stage.sbatch").read_text(encoding="utf-8")
    assert "NVIS_STAGE_ENSURE_IMEX_CHANNEL0" in stage
    assert "IMEX_CHANNEL_READY" in stage
    assert "mknod" in stage and "os.O_RDWR" in stage
    assert "systemctl" not in stage and "udevadm" not in stage


def test_api_forwards_the_compute_visible_runtime_tree(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "_validation_provenance_root",
        lambda: "/cluster/OVA/scripts/nscaletx-16k-validation",
    )
    env = app_module._validation_runtime_env()
    assert env["NVIS_VALIDATION_NODE_SCRIPTS_DIR"] == (
        "/cluster/OVA/scripts/nscaletx-16k-validation/runtime/bin/scripts"
    )
    assert not env["NVIS_VALIDATION_NODE_SCRIPTS_DIR"].startswith("/app/")
