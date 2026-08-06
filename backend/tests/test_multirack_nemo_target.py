# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""The multirack NeMo launch must build its tray spec from NVIS_CT_ID, not a literal "-ct".

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
import nvis_id_templates as nid  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
SCRIPTS = BACKEND.parent / "bin" / "scripts"

# The two conventions that matter. Bright is the default; Nscale TX is the one that broke.
BRIGHT = ("p[\\d+]-r[\\d+]", "p[\\d+]-r[\\d+]-ct[\\d+]")
NSCALE_TX = ("ink50-dh01-[a-z][\\d+]", "ink50-dh01-[a-z][\\d+]-[\\d+]")


@pytest.fixture
def prepare(monkeypatch, tmp_path):
    """Call _prepare_run_nemo_xrack under a chosen naming, returning the built argv string.

    Naming and the tray range are injected by patching the two accessors rather than by setting
    NVIS_* env vars. ``_eget`` consults the catalog active-context, which re-materializes
    ``os.environ`` from the baseline captured when ``app`` was imported — so an env var set after
    import is silently reverted mid-call. (Not a production concern: there they are container env
    vars, present at import and therefore part of that baseline.)
    """

    def _run(rack_tmpl: str, ct_tmpl: str, racks: list[str], ct_range: str = "01-18") -> str:
        naming = nid.get_nvis_naming(rack_tmpl, ct_tmpl)
        monkeypatch.setattr(app_module, "_nvis_naming", lambda: naming)
        monkeypatch.setattr(app_module, "_validation_ct_inner", lambda: ct_range)
        # The script lives on the exec host in SSH mode; here just let the path check pass.
        monkeypatch.setattr(app_module, "_script_exists", lambda _p: True)
        prepared, err = app_module._prepare_run_nemo_xrack(
            {"racks": racks, "log_dir": str(tmp_path)}
        )
        assert err is None, f"prepare returned an error: {err}"
        assert prepared is not None
        return str(prepared["arg"])

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


def test_prepare_does_not_hardcode_the_ct_joiner():
    body = (BACKEND / "app.py").read_text(encoding="utf-8")
    m = re.search(r"def _prepare_run_nemo_xrack\(.*?\n(?=^def )", body, re.S | re.M)
    assert m, "_prepare_run_nemo_xrack not found"
    # Only executable lines: the docstring and comments legitimately mention the broken -ct form.
    code = [ln for ln in m.group(0).splitlines() if not ln.lstrip().startswith("#")]
    for ln in code:
        assert not re.search(r'["\']\{?\w*\}?-ct\[', ln), f"the -ct literal is back: {ln.strip()}"
    assert "ct_blob" in "\n".join(code), "the spec must come from the naming helper"


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


def test_staging_verify_requires_a_positive_ok_per_node():
    """Staging must count confirmations, not failures.

    Counting ``BAD`` replies passed when a node was unreachable, because an unreachable node
    never ran the remote test and so never replied at all — a run where ssh failed on all 36
    nodes still printed "staged OK on all 36 nodes" and submitted.
    """
    body = (SCRIPTS / "run-nemo-xrack.sh").read_text(encoding="utf-8")
    assert "STAGED_OK" in body, "verify should count positive per-node confirmations"
    assert 'grep -c BAD' not in body, "the failure-counting verify is back"
    # and the count must be compared against the node total
    assert re.search(r'_ok.*-ne.*\$\{#hosts\[@\]\}', body), "OK count must equal the node count"
