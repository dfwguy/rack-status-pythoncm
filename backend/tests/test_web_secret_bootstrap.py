# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for auto-generated RACK_WEB_SECRET_KEY on first start."""

from __future__ import annotations

import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import app as app_module


@pytest.fixture(autouse=True)
def isolated_secret_file(tmp_path, monkeypatch):
    secret_file = tmp_path / ".rack_web_secret_key"
    monkeypatch.setattr(app_module, "_RACK_WEB_SECRET_FILE", secret_file)
    monkeypatch.delenv("RACK_WEB_SECRET_KEY", raising=False)
    yield


def test_bootstrap_creates_persistent_secret():
    app_module._bootstrap_rack_web_secret_key()
    key1 = os.environ.get("RACK_WEB_SECRET_KEY", "")
    assert len(key1) >= 32
    assert app_module._RACK_WEB_SECRET_FILE.is_file()
    os.environ.pop("RACK_WEB_SECRET_KEY", None)
    app_module._bootstrap_rack_web_secret_key()
    key2 = os.environ.get("RACK_WEB_SECRET_KEY", "")
    assert key1 == key2


def test_bootstrap_respects_env_override(monkeypatch):
    monkeypatch.setenv("RACK_WEB_SECRET_KEY", "fixed-key-from-env")
    app_module._bootstrap_rack_web_secret_key()
    assert os.environ["RACK_WEB_SECRET_KEY"] == "fixed-key-from-env"
    assert not app_module._RACK_WEB_SECRET_FILE.is_file()
