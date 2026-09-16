# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary

from auth_token import auth_token


def test_file_token_takes_precedence_over_legacy_environment(tmp_path):
    token_file = tmp_path / "web-token"
    token_file.write_text("file-secret\n", encoding="utf-8")

    assert auth_token(
        {
            "RACK_WEB_AUTH_TOKEN_FILE": str(token_file),
            "RACK_WEB_AUTH_TOKEN": "legacy-secret",
        }
    ) == "file-secret"


def test_missing_or_empty_file_preserves_legacy_compatibility(tmp_path):
    missing = tmp_path / "missing"
    empty = tmp_path / "empty"
    empty.write_text("\n", encoding="utf-8")

    for path in (missing, empty):
        assert auth_token(
            {
                "RACK_WEB_AUTH_TOKEN_FILE": str(path),
                "RACK_WEB_AUTH_TOKEN": "legacy-secret",
            }
        ) == "legacy-secret"


def test_invalid_existing_file_fails_closed_instead_of_using_legacy(tmp_path):
    directory = tmp_path / "not-a-token"
    directory.mkdir()
    symlink = tmp_path / "token-link"
    symlink.symlink_to(directory)

    for path in (directory, symlink):
        assert auth_token(
            {
                "RACK_WEB_AUTH_TOKEN_FILE": str(path),
                "RACK_WEB_AUTH_TOKEN": "legacy-secret",
            }
        ) is None


def test_token_is_absent_when_neither_source_is_configured():
    assert auth_token({}) is None


def test_app_auth_uses_file_backed_token(monkeypatch, tmp_path):
    import app as app_module

    token_file = tmp_path / "web-token"
    token_file.write_text("integrated-secret\n", encoding="utf-8")
    monkeypatch.setenv("RACK_WEB_AUTH_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("RACK_WEB_AUTH_TOKEN", "legacy-secret")

    assert app_module._auth_token() == "integrated-secret"
    assert app_module._token_verify("integrated-secret") is True
    assert app_module._token_verify("legacy-secret") is False
