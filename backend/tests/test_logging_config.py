# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# Author: Rafael M. Koike & Cursor Agent ("Ada")
"""TC-1 .. TC-5 of the Phase 1 test plan: the central logging configuration.

Traces to AC-1, AC-2, AC-3, AC-3a, AC-4, AC-5, AC-6, AC-6a, AC-25, AC-27.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import socket

import logging_config as lc
import pytest

FEATURE_ENV_PREFIXES = ("NVIS_LOG_", "NVIS_AUDIT_")


@pytest.fixture(autouse=True)
def _clean_logging(monkeypatch, tmp_path):
    """Every case starts from a cleared feature environment and its own log directory.

    Autouse because a leaked handler writing to a previous case's ``tmp_path`` is the kind of
    cross-test coupling that shows up much later as an unrelated flake.
    """
    for name in list(lc.SETTINGS):
        monkeypatch.delenv(name, raising=False)
    for name in [k for k in dict(__import__("os").environ) if k.startswith(FEATURE_ENV_PREFIXES)]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NVIS_LOG_FILE", str(tmp_path / "app.log"))
    lc.reset_for_tests()
    yield
    lc.reset_for_tests()


def _ours(root=None):
    return [h for h in (root or logging.getLogger()).handlers if lc.is_ours(h)]


# --------------------------------------------------------------------------- TC-1


class TestTC1SingleConfiguration:
    """One configuration, applied once, reaching an arbitrary module's logger (AC-1, AC-2)."""

    def test_every_module_logger_reaches_a_sink(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        assert lc.configure(force=True) is True

        logging.getLogger("backend.anything.at.all").info("hello from an arbitrary module")

        for h in _ours():
            h.flush()
        written = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert "hello from an arbitrary module" in written
        assert "backend.anything.at.all" in written

    def test_configure_is_idempotent(self, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        lc.configure(force=True)
        first = len(_ours())

        assert lc.configure() is False, "a second call without force should be a no-op"
        assert len(_ours()) == first

        lc.configure(force=True)
        assert len(_ours()) == first, "force must replace our handlers, not accumulate them"

    def test_foreign_handlers_survive(self, monkeypatch):
        """The 'scope the configuration' decision: never detach somebody else's handler.

        This is what keeps pytest's ``caplog`` working and is why NFR-6 holds — none of the 94
        existing test files needed a change.
        """
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        root = logging.getLogger()
        foreign = logging.handlers.MemoryHandler(capacity=10)
        root.addHandler(foreign)
        try:
            lc.configure(force=True)
            assert foreign in root.handlers
            lc.configure(force=True)
            assert foreign in root.handlers, "re-applying config must not evict a foreign handler"
        finally:
            root.removeHandler(foreign)

    def test_dictconfig_preserves_caplog_capture(self, caplog, monkeypatch):
        """The concrete form of the above, and the answer to the plan's Open Question 2.

        ``configure`` is called *inside* the test, after ``caplog`` is active, so the assertion
        observes a configuration competing with pytest's capture handler rather than one that ran
        at import time and can pass trivially. If this fails, the 94 existing test files would
        need a fixture; because it passes, NFR-6 holds and none of them changed.
        """
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        with caplog.at_level(logging.INFO):
            lc.configure(force=True)
            logging.getLogger("backend.some.module").info("still visible to caplog")
        assert "still visible to caplog" in caplog.text


# --------------------------------------------------------------------------- TC-2


class TestTC2SinkSelection:
    """Each sink individually selected, and the syslog-only refusal (AC-3, AC-3a)."""

    def test_stdout_sink(self, capsys, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_SINKS", "stdout")
        lc.configure(force=True)
        logging.getLogger("t").info("to stdout")
        for h in _ours():
            h.flush()
        assert "to stdout" in capsys.readouterr().out
        kinds = {type(h) for h in _ours()}
        assert kinds == {logging.StreamHandler}, "stdout alone means stdout only (AC-3)"

    def test_file_sink(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        lc.configure(force=True)
        logging.getLogger("t").info("to file")
        for h in _ours():
            h.flush()
        assert "to file" in (tmp_path / "app.log").read_text(encoding="utf-8")
        kinds = {type(h) for h in _ours()}
        assert kinds == {logging.handlers.RotatingFileHandler}, "file alone means file only (AC-3)"

    def test_syslog_sink_delivers_a_datagram(self, monkeypatch):
        """Bind a real UDP socket and read one datagram, rather than asserting on handler class."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(2.0)
        port = sock.getsockname()[1]
        try:
            monkeypatch.setenv("NVIS_LOG_SINKS", "syslog,file")
            monkeypatch.setenv("NVIS_LOG_SYSLOG_HOST", "127.0.0.1")
            monkeypatch.setenv("NVIS_LOG_SYSLOG_PORT", str(port))
            lc.configure(force=True)
            logging.getLogger("t").warning("over the wire")
            for h in _ours():
                h.flush()
            assert b"over the wire" in sock.recv(4096)
        finally:
            sock.close()

    def test_syslog_alone_is_refused(self, monkeypatch, caplog):
        """syslog-only cannot stand: it is UDP, it drops silently, FR-2a forbids it (AC-3a)."""
        monkeypatch.setenv("NVIS_LOG_SINKS", "syslog")
        monkeypatch.setenv("NVIS_LOG_SYSLOG_HOST", "127.0.0.1")
        # A port nothing listens on, but a valid one: UDP sendto(port 0) fails immediately, and
        # the resulting handler error would fire while the substitution note is being logged.
        monkeypatch.setenv("NVIS_LOG_SYSLOG_PORT", "9999")
        with caplog.at_level(logging.WARNING):
            lc.configure(force=True)
        assert any(
            isinstance(h, logging.handlers.RotatingFileHandler) for h in _ours()
        ), "the file sink must be added back"
        assert any("is lossy only" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- TC-3


class TestTC3FormatAndLevel:
    """Format and level, parametrised (AC-4, AC-5)."""

    @pytest.mark.parametrize(
        ("fmt", "should_parse"), [("json", True), ("text", False)], ids=["json", "text"]
    )
    def test_json_and_text_formats(self, tmp_path, monkeypatch, fmt, should_parse):
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        monkeypatch.setenv("NVIS_LOG_FORMAT", fmt)
        lc.configure(force=True)
        logging.getLogger("t").info("formatted")
        for h in _ours():
            h.flush()

        line = (tmp_path / "app.log").read_text(encoding="utf-8").strip().splitlines()[-1]
        if should_parse:
            payload = json.loads(line)
            assert payload["message"] == "formatted"
            assert payload["level"] == "INFO"
        else:
            with pytest.raises(json.JSONDecodeError):
                json.loads(line)

    @pytest.mark.parametrize(
        ("emit", "expected"), [("debug", False), ("warning", True)], ids=["below", "at-or-above"]
    )
    def test_level_filtering(self, tmp_path, monkeypatch, emit, expected):
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        monkeypatch.setenv("NVIS_LOG_LEVEL", "WARNING")
        lc.configure(force=True)
        getattr(logging.getLogger("t"), emit)("level probe")
        for h in _ours():
            h.flush()
        assert ("level probe" in (tmp_path / "app.log").read_text(encoding="utf-8")) is expected


# --------------------------------------------------------------------------- TC-4


class TestTC4Redaction:
    """A configured sensitive value reaches no sink (AC-6, AC-6a)."""

    SECRET = "sup3rs3cret-value-not-a-real-credential"

    def test_redaction_in_message_and_args(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("RACK_NVSW_ADMIN_PASSWORD", self.SECRET)
        monkeypatch.setenv("NVIS_LOG_SINKS", "stdout,file")
        lc.configure(force=True)

        log = logging.getLogger("t")
        log.info("inline %s", self.SECRET)
        log.info(f"interpolated {self.SECRET}")
        for h in _ours():
            h.flush()

        written = (tmp_path / "app.log").read_text(encoding="utf-8")
        assert self.SECRET not in written, "the raw value must not reach the file sink"
        assert self.SECRET not in capsys.readouterr().out, "nor the stdout sink"
        assert written.count("[REDACTED]") >= 2

    def test_short_values_are_not_redacted(self, tmp_path, monkeypatch):
        """A very short secret would match inside ordinary words and redact the whole log."""
        monkeypatch.setenv("RACK_TEST_TOKEN", "ab")
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        lc.configure(force=True)
        logging.getLogger("t").info("a stable observable string")
        for h in _ours():
            h.flush()
        assert "a stable observable string" in (tmp_path / "app.log").read_text(encoding="utf-8")

    def test_path_valued_key_names_are_not_treated_as_secrets(self, monkeypatch):
        """``RACK_SSH_KEY`` holds a path. Redacting it protects nothing and hurts readability."""
        monkeypatch.setenv("RACK_SSH_KEY", "/home/operator/.ssh/id_ed25519")
        assert "/home/operator/.ssh/id_ed25519" not in lc.sensitive_values()

    def test_longest_secret_redacted_first(self, monkeypatch):
        monkeypatch.setenv("RACK_A_TOKEN", "abcdefgh")
        monkeypatch.setenv("RACK_B_TOKEN", "abcdefghijklmnop")
        values = lc.sensitive_values()
        assert values.index("abcdefghijklmnop") < values.index("abcdefgh")


# --------------------------------------------------------------------------- TC-5


class TestTC5DefaultsAndNoNetwork:
    """Working defaults with a cleared environment, and no network handler (AC-25, AC-27)."""

    def test_defaults_need_no_env(self, monkeypatch, tmp_path):
        monkeypatch.delenv("NVIS_LOG_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        resolved = lc.resolve_settings()

        assert set(resolved) == set(lc.SETTINGS), "resolve_settings must cover the registry"
        for name, value in resolved.items():
            # "Usable" is not the same as "non-empty". RACK_WEB_TRUSTED_PROXIES defaults to the
            # empty string on purpose: an empty trust list means no forwarded header is ever
            # believed, which is the secure default rather than a missing one (FR-8). Asserting
            # non-emptiness here would push the wrong default onto a security setting.
            assert value is not None, f"{name} has no usable default"
            assert isinstance(value, type(lc.SETTINGS[name].default)), (
                f"{name} default type drifted"
            )

    def test_no_network_handler_by_default(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        lc.configure(force=True)
        assert not any(isinstance(h, logging.handlers.SysLogHandler) for h in _ours())
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in _ours())

    def test_default_sinks_are_stdout_and_file(self):
        assert lc.LOG_SINKS.default == (lc.SINK_STDOUT, lc.SINK_FILE)
        assert lc.SINK_SYSLOG not in lc.LOG_SINKS.default, "no network egress by default (NFR-9)"

    def test_unusable_value_falls_back_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("NVIS_LOG_MAX_BYTES", "not-a-number")
        assert lc.LOG_MAX_BYTES.resolve() == lc.LOG_MAX_BYTES.default

    def test_config_failure_does_not_block_startup(self, monkeypatch, capsys):
        """NFR-1 / AC-15a: an unusable sink configuration must not stop the app serving."""
        monkeypatch.setenv("NVIS_LOG_SINKS", "file")
        monkeypatch.setattr(lc, "_build_handlers", lambda _s: (_ for _ in ()).throw(OSError("nope")))
        assert lc.configure(force=True) is False
        assert "logging configuration failed" in capsys.readouterr().err
