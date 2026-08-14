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
"""What build is this? -- pinned, because the answer was silently wrong.

``RACK_WEB_VERSION`` was the only source and is set nowhere in the repo: not the Dockerfile, not
``rack-config.env``, not CI. So the Cluster Dashboard's "Dashboard build" tile read ``dev`` on
every deployment while ``version.py`` said 0.1.5. These tests pin a file-based answer instead, and
pin the precedence so a build stamp can never be shadowed by the release version or vice versa.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_version  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point the module at a scratch tree and drop its cache between tests.

    Both roots are patched: build-info.json is read from APP_DIR (it sits beside the code, under
    backend/, so a bind-mounted backend directory cannot hide it) while version.py is read from
    REPO_ROOT.
    """
    monkeypatch.setattr(app_version, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(app_version, "APP_DIR", tmp_path)
    app_version.reset_cache()
    yield
    app_version.reset_cache()


def _write_build_info(root: Path, **payload) -> None:
    (root / "build-info.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_version_py(root: Path, raw: str) -> None:
    (root / "version.py").write_text(raw, encoding="utf-8")


class TestBuildInfoWins:
    def test_build_stamp_is_reported_when_present(self, tmp_path):
        _write_version_py(tmp_path, 'VERSION = "0.1.5"\n')
        _write_build_info(tmp_path, version="0.1.5", build="20260814T1607.e8ea22e")
        assert app_version.app_version() == "0.1.5+20260814T1607.e8ea22e"

    def test_build_info_without_a_build_field_is_just_the_version(self, tmp_path):
        _write_build_info(tmp_path, version="0.2.0")
        assert app_version.app_version() == "0.2.0"

    def test_build_info_beats_version_py(self, tmp_path):
        """The image is authoritative about itself; version.py in the tree may be newer."""
        _write_version_py(tmp_path, 'VERSION = "9.9.9"\n')
        _write_build_info(tmp_path, version="0.1.5", build="abc")
        assert app_version.app_version() == "0.1.5+abc"


class TestVersionPyFallback:
    def test_release_version_is_marked_dev_without_a_build_stamp(self, tmp_path):
        """No build-info means nobody built this: a working tree, so say so."""
        _write_version_py(tmp_path, 'VERSION = "0.1.5"\n')
        assert app_version.app_version() == "0.1.5-dev"

    def test_prerelease_and_single_quotes_are_read(self, tmp_path):
        _write_version_py(tmp_path, "VERSION = '1.2.3-rc1'\n")
        assert app_version.app_version() == "1.2.3-rc1-dev"

    def test_version_py_is_parsed_not_imported(self, tmp_path):
        """Importing it would execute arbitrary module code at request time."""
        _write_version_py(tmp_path, 'raise SystemExit("must not be imported")\nVERSION = "3.0.0"\n')
        assert app_version.app_version() == "3.0.0-dev"


class TestUnknown:
    def test_no_files_at_all(self, tmp_path):
        assert app_version.app_version() == "unknown"

    def test_corrupt_build_info_falls_through_to_version_py(self, tmp_path):
        (tmp_path / "build-info.json").write_text("{not json", encoding="utf-8")
        _write_version_py(tmp_path, 'VERSION = "0.4.0"\n')
        assert app_version.app_version() == "0.4.0-dev"

    def test_version_py_without_a_version_assignment(self, tmp_path):
        _write_version_py(tmp_path, "# nothing here\n")
        assert app_version.app_version() == "unknown"

    def test_a_directory_where_build_info_should_be_is_survived(self, tmp_path):
        (tmp_path / "build-info.json").mkdir(parents=True)
        _write_version_py(tmp_path, 'VERSION = "0.5.0"\n')
        assert app_version.app_version() == "0.5.0-dev"


class TestNoEnvironmentDependence:
    def test_rack_web_version_is_ignored(self, tmp_path, monkeypatch):
        """Deliberate: the env var was the old source and was never set, so it lied."""
        monkeypatch.setenv("RACK_WEB_VERSION", "7.7.7-from-env")
        _write_version_py(tmp_path, 'VERSION = "0.1.5"\n')
        assert app_version.app_version() == "0.1.5-dev"


class TestCaching:
    def test_the_answer_is_cached_so_it_is_read_once(self, tmp_path):
        """Stamped into payloads on hot endpoints; it must not stat files per request."""
        _write_version_py(tmp_path, 'VERSION = "0.1.5"\n')
        first = app_version.app_version()
        (tmp_path / "version.py").unlink()
        assert app_version.app_version() == first

    def test_reset_cache_re_reads(self, tmp_path):
        _write_version_py(tmp_path, 'VERSION = "0.1.5"\n')
        assert app_version.app_version() == "0.1.5-dev"
        _write_version_py(tmp_path, 'VERSION = "0.2.0"\n')
        app_version.reset_cache()
        assert app_version.app_version() == "0.2.0-dev"
