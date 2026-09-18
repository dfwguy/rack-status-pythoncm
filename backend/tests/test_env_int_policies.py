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
"""One integer-knob reader, three validity rules, stated per caller.

Four modules each carried their own ``_env_int`` with three different policies, so the same knob
validated differently depending on which module read it. They now share ``config_env.env_int`` and
declare a rule via ``min_valid``; these tests pin each module's rule to what it had before, because
the interesting failure is silent -- a knob that starts accepting 0 makes ``NVIS_UP_HEAD_BYTES=0``
read nothing instead of the normal 2 MB.

``min_valid`` deliberately falls back to the DEFAULT rather than clamping to the bound: a typo should
produce documented behaviour, not a different wrong number.
"""
from __future__ import annotations

import config_env
import native_status
import pytest
import rack_locks
import universal_parser

DEFAULT = 100

# (env value, config_env, native_status, universal_parser, rack_locks)
MATRIX = [
    ("", DEFAULT, DEFAULT, DEFAULT, DEFAULT),
    ("   ", DEFAULT, DEFAULT, DEFAULT, DEFAULT),
    ("abc", DEFAULT, DEFAULT, DEFAULT, DEFAULT),
    ("120", 120, 120, 120, 120),
    ("9999", 9999, 9999, 9999, 9999),
    # zero: meaningless as a read size, fine as a TTL
    ("0", 0, 0, DEFAULT, 0),
    # negative: operator error where a rule is declared
    ("-5", -5, DEFAULT, DEFAULT, -5),
]


@pytest.mark.parametrize(("raw", "generic", "native", "universal", "locks"), MATRIX)
def test_each_module_keeps_its_own_rule(monkeypatch, raw, generic, native, universal, locks):
    monkeypatch.setenv("ZZ_ENV_INT_PROBE", raw)
    assert config_env._env_int("ZZ_ENV_INT_PROBE", DEFAULT) == generic
    assert native_status._env_int("ZZ_ENV_INT_PROBE", DEFAULT) == native
    assert universal_parser._env_int("ZZ_ENV_INT_PROBE", DEFAULT) == universal
    assert rack_locks._env_int("ZZ_ENV_INT_PROBE", DEFAULT) == locks


def test_an_unset_knob_returns_the_default(monkeypatch):
    monkeypatch.delenv("ZZ_ENV_INT_PROBE", raising=False)
    assert config_env.env_int("ZZ_ENV_INT_PROBE", DEFAULT) == DEFAULT


def test_below_min_valid_returns_the_default_not_the_bound(monkeypatch):
    """The distinction that matters: fall back to what is documented, do not clamp."""
    monkeypatch.setenv("ZZ_ENV_INT_PROBE", "-1")
    assert config_env.env_int("ZZ_ENV_INT_PROBE", DEFAULT, min_valid=10) == DEFAULT


def test_min_valid_is_inclusive(monkeypatch):
    monkeypatch.setenv("ZZ_ENV_INT_PROBE", "10")
    assert config_env.env_int("ZZ_ENV_INT_PROBE", DEFAULT, min_valid=10) == 10


def test_it_reads_the_process_environment_not_the_overlays(monkeypatch):
    """env_int is the plain reader; _eget_int is the one that follows the active cluster.

    Keeping them distinct matters: a knob resolved through the overlays changes per cluster, and a
    timeout that silently varies by site is very hard to explain from a log.
    """
    monkeypatch.setenv("ZZ_ENV_INT_PROBE", "7")
    assert config_env.env_int("ZZ_ENV_INT_PROBE", DEFAULT) == 7
