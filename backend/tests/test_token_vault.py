# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Tests for encrypted NVFTP token vault."""

from __future__ import annotations

import json
import os

import pytest

os.environ["RACK_STATUS_DEMO"] = "1"
os.environ["RACK_EXEC_MODE"] = "local"

import token_vault
from app import app


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(token_vault, "VAULT_DIR", tmp_path)
    monkeypatch.setattr(token_vault, "NVFTP_VAULT_FILE", tmp_path / "nvftp_token.vault.json")
    monkeypatch.setattr(token_vault, "FLOCK_FILE", tmp_path / "nvftp_token.vault.flock")
    yield


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_store_and_retrieve_roundtrip():
    token_vault.store_nvftp_token("nvftp-secret-token-abc")
    assert token_vault.get_nvftp_token() == "nvftp-secret-token-abc"
    st = token_vault.nvftp_token_status()
    assert st["configured"] is True
    assert st["masked"] == "••••-abc"


def test_vault_file_uses_salt_and_ciphertext():
    token_vault.store_nvftp_token("x" * 40)
    raw = json.loads(token_vault.NVFTP_VAULT_FILE.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert "salt" in raw
    assert "ciphertext" in raw
    assert "x" * 40 not in json.dumps(raw)


def test_clear_token():
    token_vault.store_nvftp_token("t")
    token_vault.clear_nvftp_token()
    assert token_vault.get_nvftp_token() is None
    assert token_vault.nvftp_token_status()["configured"] is False


def test_api_put_and_get_status(client):
    r = client.put(
        "/api/vault/nvftp-token",
        json={"token": "api-test-token"},
        content_type="application/json",
    )
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["configured"] is True
    r2 = client.get("/api/vault/nvftp-token")
    assert r2.get_json()["configured"] is True
    assert token_vault.get_nvftp_token() == "api-test-token"
