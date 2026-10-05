# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for NVIS naming templates (Settings → Job environment)."""

from __future__ import annotations

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
    import native_status

    # Clear the cache the function under test actually reads, not just the naming one.
    # extract_rack_ids_from_cmsh_text resolves NVIS_RACK_ID through native_status's own
    # mtime-keyed `_je_subset`, which overrides the environment; clear_nvis_naming_cache() drops
    # the lru_cache but leaves that subset warm. So an earlier test that populated it with a
    # different NVIS_RACK_ID made this test read the wrong templates and fail — intermittently,
    # depending on suite order. invalidate_job_env_file_cache() clears the subset *and* the
    # naming cache, which is why it is the module's documented reset hook.
    native_status.invalidate_job_env_file_cache()

    text = (
        "CBR01-DH01-RK0102    DataHall     1              1              42\n"
        "CBR01-DH01-RK0104    DataHall     2              1              42\n"
    )
    ids = native_status.extract_rack_ids_from_cmsh_text(text)
    assert ids == ["CBR01-DH01-SVR-CN0102", "CBR01-DH01-SVR-CN0104"]


def test_extract_rack_ids_anchorless_template_ignores_member_hostname_substrings(monkeypatch):
    """An anchorless rack template must not invent racks from member-hostname substrings.

    Nscale NC's rack ids are a bare ``[A-Z][\\d+]`` (``U19``, ``Q26``) and the ``rack; list`` rows
    carry member hostnames like ``sys1-gso1-p-phy-gpu1-su19``. A whole-text scan of that output
    matched ``gso1``->``o1``, ``gpu1``->``u1``, ``su19``->``u19`` and produced ~150 phantom racks.
    The rack name is always the first column, so that column is authoritative and the scan is
    skipped when the template has no literal to anchor on.
    """
    monkeypatch.setenv("NVIS_RACK_ID", r"[A-Z][\d+]")
    monkeypatch.delenv("NVIS_CT_ID", raising=False)
    import native_status

    native_status.invalidate_job_env_file_cache()

    text = (
        "Q26   17 26 42   sys1-gso1-p-phy-gpu19-su36,sys1-gso1-p-phy-nvs10-su36\n"
        "R2    18  2 42   sys1-gso1-p-phy-gpu1-su26,sys1-gso1-p-phy-nvs1-su26\n"
        "U19   21 19 42   sys1-gso1-p-phy-gpu1-su19,sys1-gso1-p-phy-gpu9-su19\n"
    )
    ids = native_status.extract_rack_ids_from_cmsh_text(text)
    assert ids == ["Q26", "R2", "U19"]  # only the first-column rack names, no o1/u1/u19 phantoms


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


def test_rack_id_from_log_basename_attributes_single_rack_nccl():
    """An NCCL sweep naming exactly one rack is attributed to that rack.

    ``nccl`` entered the tool marker table when the basename predicate was widened
    to accept any plausible result log, so these are surfaced rather than dropped.
    Attribution stays safe because the caller still demands exactly one rack id,
    and the multirack / Llama basenames below are excluded before that check.
    """
    n = nid.NvisNaming.from_templates(nid.DEFAULT_NVIS_RACK_ID, nid.DEFAULT_NVIS_CT_ID)
    assert n.rack_id_from_log_basename("root-NCCL-p5-r03-ct12-N-22417.txt") == "p5-r03"
    assert n.rack_id_from_log_basename("root-NCCL-4Rack-p5-r03-N-1.txt") is None
    assert n.rack_id_from_log_basename("root-NCCL-p5-r03-p5-r04-N-1.txt") is None


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


# --- Row-letter racks (Boston Markley GB200): ``[A-Z]`` single-letter slot ----

_BOS_RACK = r"bos01-[A-Z][01-99]"
_BOS_CT = r"bos01-[A-Z][01-99]-[01-99]-s1-dgx-[01-99]-c[01-99]"
_BOS_NVSW = r"bos01-[A-Z][01-99]-[01-99]-s1-nvsw-[01-99]"


def test_alpha_slot_triplet_validates():
    assert nid.validate_nvis_naming_triplet(_BOS_RACK, _BOS_CT, _BOS_NVSW) is None


def test_alpha_slot_matches_compute_hostname_both_rows():
    m = nid.NvisNaming.from_templates(_BOS_RACK, _BOS_CT, _BOS_NVSW).matcher()
    a = m.match_hostname("bos01-a05-11-s1-dgx-01-c01")
    assert a is not None and a.rack_id == "bos01-a05" and a.tray == "01"
    b = m.match_hostname("bos01-b10-37-s1-dgx-12-c18")
    assert b is not None and b.rack_id == "bos01-b10" and b.tray == "18"


def test_alpha_slot_canonical_and_nvsw_and_sort():
    n = nid.NvisNaming.from_templates(_BOS_RACK, _BOS_CT, _BOS_NVSW)
    assert n.canonical_rack_id("bos01-a05") == "bos01-a05"
    assert n.is_nvsw_hostname("bos01-b07-25-s1-nvsw-09")
    assert not n.is_nvsw_hostname("bos01-b07-25-s1-dgx-01-c01")
    racks = ["bos01-b05", "bos01-a10", "bos01-a05", "bos01-b10"]
    assert sorted(racks, key=n.rack_sort_key) == [
        "bos01-a05",
        "bos01-a10",
        "bos01-b05",
        "bos01-b10",
    ]


def test_alpha_slot_extracts_from_rack_list_text():
    n = nid.NvisNaming.from_templates(_BOS_RACK, _BOS_CT, _BOS_NVSW)
    text = "bos01-a05  4  0  48\nbos01-b10  20 0 48\nbos01-a06 5 0 48\n"
    assert n.extract_rack_ids_from_text(text) == ["bos01-a05", "bos01-a06", "bos01-b10"]


# --- Hierarchy (section -> row -> rack): single source of truth for layouts -----


def test_hierarchy_boston_groups_rows_a_and_b():
    """Boston ``bos01-[A-Z][01-99]`` collapses into two row-letter rows, no section."""
    n = nid.NvisNaming.from_templates(_BOS_RACK, _BOS_CT, _BOS_NVSW)
    ids = [f"bos01-{r}{i:02d}" for r in ("b", "a") for i in (11, 1, 5)]
    h = n.build_hierarchy(ids)
    assert h["levels"] == ["row", "rack"]
    assert len(h["sections"]) == 1
    rows = h["sections"][0]["rows"]
    assert [r["label"] for r in rows] == ["a", "b"]  # rows ordered by letter ordinal
    # racks within a row sorted numerically (1, 5, 11 -> bos01-a01, a05, a11)
    assert [r["rack"] for r in rows[0]["racks"]] == ["bos01-a01", "bos01-a05", "bos01-a11"]
    assert h["unmatched"] == []


def test_hierarchy_bright_pod_rows():
    n = nid.NvisNaming.from_templates(r"p[\d+]-r[\d+]")
    h = n.build_hierarchy(["p6-r1", "p5-r10", "p5-r2", "p5-r1"])
    assert h["levels"] == ["row", "rack"]
    rows = h["sections"][0]["rows"]
    assert [r["label"] for r in rows] == ["5", "6"]
    assert [r["rack"] for r in rows[0]["racks"]] == ["p5-r1", "p5-r2", "p5-r10"]


def test_hierarchy_cbr_section_row_rack():
    n = nid.NvisNaming.from_templates(nid.CBR_NVIS_RACK_ID, nid.CBR_NVIS_CT_ID)
    h = n.build_hierarchy(
        ["CBR1-DH22-SVR-CN0201", "CBR1-DH22-SVR-CN0101", "CBR1-DH22-SVR-CN0102"]
    )
    assert h["levels"] == ["section", "row", "rack"]
    sec = h["sections"][0]
    assert sec["key"] == "CBR1-DH22"
    assert [row["label"] for row in sec["rows"]] == ["01", "02"]
    assert [r["rack"] for r in sec["rows"][0]["racks"]] == [
        "CBR1-DH22-SVR-CN0101",
        "CBR1-DH22-SVR-CN0102",
    ]


def test_hierarchy_unmatched_racks_are_preserved():
    n = nid.NvisNaming.from_templates(_BOS_RACK, _BOS_CT, _BOS_NVSW)
    h = n.build_hierarchy(["bos01-a05", "not-a-rack", "1"])
    assert "not-a-rack" in h["unmatched"]
    assert any(
        r["rack"] == "bos01-a05" for s in h["sections"] for row in s["rows"] for r in row["racks"]
    )
