"""Tests for NVIS naming templates (Settings → Job environment)."""

from __future__ import annotations

import pytest

import nvis_id_templates as nid


def test_bright_defaults_match_tray_hostname():
    m = nid.naming_matcher_from_env(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    hm = m.match_hostname("p5-r03-ct12")
    assert hm is not None
    assert hm.rack_id == "p5-r03"
    assert hm.tray == "12"


def test_bright_defaults_also_match_padded_cmsh_spelling():
    m = nid.naming_matcher_from_env(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    hm = m.match_hostname("p05-r01-ct01")
    assert hm is not None
    assert hm.rack_id == "p05-r01"
    assert hm.tray == "01"


def test_cbr_template_match():
    m = nid.naming_matcher_from_env(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    hm = m.match_hostname("CBR01-DH01-SVR-CN010213")
    assert hm is not None
    assert hm.rack_id == "CBR01-DH01-SVR-CN0102"
    assert hm.hostname == "CBR01-DH01-SVR-CN010213"
    assert hm.tray == "13"


def test_validate_rejects_rack_longer_than_ct():
    err = nid.validate_nvis_naming_triplet(
        "p[01-99]-r[01-99]-ct[01-99]",
        "p[01-99]-r[01-99]",
        "",
    )
    assert err is not None


def test_validate_accepts_empty_nvsw():
    assert nid.validate_nvis_naming_triplet(
        nid.DEFAULT_NVIS_RACK_ID,
        nid.DEFAULT_NVIS_CT_ID,
        "",
    ) is None


def test_find_in_ds_line():
    m = nid.naming_matcher_from_env(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    found = m.find_in_line("[   UP   ] CBR01-DH01-SVR-CN010211 extra")
    assert len(found) == 1
    assert found[0].tray == "11"


def test_nvis_naming_validation_target_bright_default():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.build_validation_target("p5-r03", "9-12", None) == "p5-r03-ct[9-12]"
    assert n.ct_joiner == "-ct"


def test_nvis_naming_validation_target_bright_padded_legacy():
    n = nid.NvisNaming.from_templates(nid.PADDED_NVIS_RACK_ID, nid.PADDED_NVIS_CT_ID)
    assert n.build_validation_target("p5-r1", "01-18", None) == "p05-r01-ct[01-18]"


def test_nvis_naming_cbr_ct_joiner_empty():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    assert n.ct_joiner == ""
    blob = n.ct_blob("cbr01-dh01-svr-cn0102", "11-18")
    assert blob == "CBR01-DH01-SVR-CN0102[11-18]"


def test_canonical_rack_id_bright_default_preserves_cmsh():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.canonical_rack_id("p5-r03") == "p5-r03"
    assert n.canonical_rack_id("p05-r01") == "p05-r01"


def test_canonical_rack_id_bright_padded_legacy():
    n = nid.NvisNaming.from_templates(nid.PADDED_NVIS_RACK_ID, nid.PADDED_NVIS_CT_ID)
    assert n.canonical_rack_id("p5-r1") == "p05-r01"


def test_canonical_rack_id_cbr_preserves_template_literals():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    assert n.canonical_rack_id("cbr01-dh01-svr-cn0102") == "CBR01-DH01-SVR-CN0102"
    assert (
        n.build_validation_target("cbr01-dh01-svr-cn0102", "11-18,28-37", None)
        == "CBR01-DH01-SVR-CN0102[11-18,28-37]"
    )


def test_map_cmsh_rack_list_token_rk_to_svr_cn():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    assert n.map_cmsh_rack_list_token("CBR01-DH01-RK0102") == "CBR01-DH01-SVR-CN0102"
    assert n.map_cmsh_rack_list_token("CBR01-DH01-RK0104") == "CBR01-DH01-SVR-CN0104"
    assert n.map_cmsh_rack_list_token("CBR01-DH01-SVR-CN0102") == "CBR01-DH01-SVR-CN0102"


def test_extract_rack_ids_from_cmsh_text_cbr_rk_lines(monkeypatch):
    monkeypatch.setenv("NVIS_RACK_ID", nid.CBR_NVIS_RACK_ID)
    monkeypatch.setenv("NVIS_CT_ID", nid.CBR_NVIS_CT_ID)
    nid.clear_nvis_naming_cache()
    import native_status

    text = (
        "CBR01-DH01-RK0102    DataHall     1              1              42\n"
        "CBR01-DH01-RK0104    DataHall     2              1              42\n"
    )
    ids = native_status.extract_rack_ids_from_cmsh_text(text)
    assert ids == ["CBR01-DH01-SVR-CN0102", "CBR01-DH01-SVR-CN0104"]


def test_rack_id_from_log_basename_clusterkit_root_ck():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    assert n.rack_id_from_log_basename("root-CK-CBR01-DH01-SVR-CN0102-829.txt") == "CBR01-DH01-SVR-CN0102"
    assert (
        n.rack_id_from_log_basename("root-CK-CBR01-DH01-SVR-CN0102-foo-CBR01-DH01-SVR-CN0104-1.txt") is None
    )


def test_rack_id_from_log_basename_still_matches_1rack():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    bn = "1Rack-CBR01-DH01-SVR-CN0102-[11-18]-18N-12345.txt"
    assert n.rack_id_from_log_basename(bn) == "CBR01-DH01-SVR-CN0102"


def test_rack_id_from_nemo_1rack_root_prefix_cbr_bracket():
    """run_singlerack / NEMO logs may use ``root-NEMO-1Rack-<rack>[trays]-…`` (no ``-`` before ``[``)."""
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    bn = "root-NEMO-1Rack-CBR01-DH01-SVR-CN0102[11-18,28-37]-18N-833.txt"
    assert n.rack_id_from_nemo_1rack_filename(bn) == "CBR01-DH01-SVR-CN0102"
    assert n.rack_id_from_log_basename(bn) == "CBR01-DH01-SVR-CN0102"


def test_rack_id_from_nemo_1rack_bright_ct_after_hyphen():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    bn = "root-NEMO-1Rack-p5-r03-ct[9-12]-18N-99.txt"
    assert n.rack_id_from_nemo_1rack_filename(bn) == "p5-r03"


def test_sinfo_and_ds_match_cmsh_bright_hostname():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.sinfo_rack_from_host("p5-r03-ct12") == ("p5-r03", "p5-r03-ct12")
    m = n.matcher()
    found = m.find_in_line("p5-r03-ct12 .............. [ CLOSED ] (DOWN)")
    assert len(found) == 1
    assert found[0].rack_id == "p5-r03"
    assert found[0].tray == "12"


def test_rack_id_from_log_basename_bright():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.rack_id_from_log_basename("root-NEMO-1Rack-p5-r03-14N-23806.txt") == "p5-r03"
    assert n.rack_id_from_log_basename("root-NCCL-p5-r03-ct12-N-22417.txt") is None


def test_rack_id_from_llama_slurm_basename_bright():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    bn = "cmsupport-NEMO-LLAMA3_8B-1N-p5-r03-ct07-99.txt"
    assert n.rack_id_from_llama_slurm_basename(bn) == "p5-r03"
    assert n.rack_id_from_log_basename(bn) is None


def test_nvsw_single_digit_default():
    n = nid.NvisNaming.from_templates(
        nid.DEFAULT_NVIS_RACK_ID,
        nid.DEFAULT_NVIS_CT_ID,
        nid.DEFAULT_NVIS_NVSW_ID,
    )
    assert n.is_nvsw_hostname("p03-r13-nvsw-9")
    assert n.normalize_nvsw_host("p03-r13", "p03-r13-nvsw-9") == "p03-r13-nvsw-9"


def test_multirack_expand_unpadded_rack_default():
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.expand_pod_r_inner("p5", "3,13") == ["p5-r3", "p5-r13"]


def test_parse_template_accepts_mixed_slot_syntax():
    p = nid.parse_template(r"p[\d+]-r[01-99]-ct[\d+]")
    assert p.slot_count == 3
    assert p.slot_kind(0) == "digits"
    assert p.slot_kind(1) == "range2"
    assert p.slot_kind(2) == "digits"


def test_rack_id_from_llama_slurm_basename_cbr():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    bn = "cmsupport-NEMO-LLAMA3_8B-1N-CBR01-DH01-SVR-CN010211-833.txt"
    assert n.rack_id_from_llama_slurm_basename(bn) == "CBR01-DH01-SVR-CN0102"
    assert n.rack_id_from_log_basename(bn) is None


def test_sn_llama_log_basename_matches_cbr_rack_casefold():
    """Parser merge scans sn-llama/*.txt with canonical rack ids (mixed case) vs lowercased basenames."""
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    rack = "CBR01-DH01-SVR-CN0102"
    fname = "cmsupport-NEMO-LLAMA3_8B-1N-CBR01-DH01-SVR-CN010211-833.txt"
    needles = list(n.find_rack_ids_in_string(rack))
    assert needles
    nl = fname.casefold()
    assert any(needle.casefold() in nl for needle in needles)
    assert not any(needle in fname.lower() for needle in needles)
