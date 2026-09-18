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
"""`checker_args` validation — operator-supplied flags that reach a `pdsh` fan-out.

These three helpers decide what an operator may put in the hw-check "custom arguments" box, and
whatever survives them is passed after `--` to `checker.sh` on every node in the target. That makes
them the narrowest security boundary in `api/diagnostics.py`, and until now **nothing exercised
them at all** — the whole `_hw_check_*` family was uncovered, which the per-file coverage floor
surfaced when the gate finally ran after five days of being blocked behind an unrelated failure.

The allowlist is deliberately *narrower* than "no shell metacharacters": `^[-a-zA-Z0-9_=.,]+$`
admits no `/`, so a token cannot name a path, and no whitespace, so a token cannot smuggle a second
argument. Both are asserted below, because both read as accidental omissions and would be tempting
to "fix" — a reviewer widening the class to allow `/` would silently permit `--script=/tmp/mine.sh`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.diagnostics import (
    _HW_CHECK_ARGS_MAX_LEN,
    _HW_CHECK_ARGS_MAX_TOKENS,
    _HW_CHECK_PRESETS,
    _hw_check_argv_for_preset,
    _hw_check_argv_from_custom_string,
    _hw_check_validate_argv_tokens,
)


class TestTheTokenAllowlist:
    """`_hw_check_validate_argv_tokens` — the last thing between a text box and a fan-out."""

    def test_ordinary_checker_flags_pass(self):
        """The control. A guard that refused everything would satisfy every refusal case below."""
        argv = ["-e", "1", "-L", "3", "--gpu-count=8", "-x", "0"]
        assert _hw_check_validate_argv_tokens(argv) == argv

    @pytest.mark.parametrize("token", [
        ";", "&&", "|", "$(id)", "`id`", "$HOME", ">out", "<in", "\n", "\t", " ",
        "a;b", "a|b", "a b", "--flag=$(id)", "*", "?", "~", "'", '"', "\\", "(", ")",
    ])
    def test_a_shell_metacharacter_is_refused(self, token):
        """Every one of these is inert in an argv *list* and dangerous the moment any layer
        re-serialises it into a shell string, which is exactly what a `pdsh` fan-out does."""
        with pytest.raises(ValueError, match="disallowed character"):
            _hw_check_validate_argv_tokens([token])

    def test_a_path_separator_is_refused(self):
        """`/` is absent from the allowlist **on purpose**, and it looks like an oversight.

        With it, `--checker=/tmp/mine.sh` becomes expressible and the operator chooses which script
        runs on every node. Asserted so that widening the class has to argue with this test first.
        """
        for token in ("/etc/passwd", "--script=/tmp/x.sh", "./x", "../x"):
            with pytest.raises(ValueError, match="disallowed character"):
                _hw_check_validate_argv_tokens([token])

    def test_whitespace_cannot_smuggle_a_second_argument(self):
        """A token containing a space is two arguments wearing one token's clothing."""
        with pytest.raises(ValueError, match="disallowed character"):
            _hw_check_validate_argv_tokens(["-e 1 -L 3"])

    def test_an_empty_token_is_refused(self):
        """An empty argv member is not harmless: it shifts positional interpretation for
        everything after it, so a flag can end up reading the wrong value."""
        with pytest.raises(ValueError, match="empty checker_args token"):
            _hw_check_validate_argv_tokens(["-e", "", "1"])

    def test_the_token_count_is_capped(self):
        with pytest.raises(ValueError, match="too many checker_args tokens"):
            _hw_check_validate_argv_tokens(["-e"] * (_HW_CHECK_ARGS_MAX_TOKENS + 1))

    def test_the_cap_itself_is_permitted(self):
        """Off-by-one on a boundary check is the commonest way a limit is wrong, and it fails in
        the direction that refuses legitimate input rather than admitting bad input."""
        exactly = ["-e"] * _HW_CHECK_ARGS_MAX_TOKENS
        assert _hw_check_validate_argv_tokens(exactly) == exactly

    def test_it_returns_a_copy(self):
        """The caller passes this to a worker thread. Returning the same list would let a later
        mutation by either side change what the other one runs."""
        original = ["-e", "1"]
        returned = _hw_check_validate_argv_tokens(original)
        returned.append("-L")
        assert original == ["-e", "1"]


class TestTheCustomStringIsSplitSafely:
    """`_hw_check_argv_from_custom_string` — shlex first, then the allowlist."""

    def test_a_quoted_string_splits_into_tokens(self):
        assert _hw_check_argv_from_custom_string("  -e 1   -L 3  ") == ["-e", "1", "-L", "3"]

    def test_quoting_does_not_defeat_the_allowlist(self):
        """The important interaction, and the reason the order matters. `shlex` happily *removes*
        the quotes, so `'a;b'` arrives at the allowlist as the bare token `a;b`. If validation ran
        before splitting — or not at all on split output — quoting would be a bypass.
        """
        for raw in ("'a;b'", '"$(id)"', "'--flag=/tmp/x'", "'a b'"):
            with pytest.raises(ValueError, match="disallowed character"):
                _hw_check_argv_from_custom_string(raw)

    def test_an_empty_string_is_refused(self):
        for raw in ("", "   ", "\t\n"):
            with pytest.raises(ValueError, match="empty checker_args"):
                _hw_check_argv_from_custom_string(raw)

    def test_the_length_is_capped_before_splitting(self):
        """Capped on the raw string, so a pathological input cannot cost `shlex` anything."""
        with pytest.raises(ValueError, match="too long"):
            _hw_check_argv_from_custom_string("-e " * _HW_CHECK_ARGS_MAX_LEN)

    def test_unbalanced_quoting_is_a_clear_error(self):
        """`shlex` raises for an unterminated quote; the message is rewritten so an operator sees
        "invalid checker_args quoting" rather than a bare Python exception."""
        with pytest.raises(ValueError, match="invalid checker_args quoting"):
            _hw_check_argv_from_custom_string("-e 'unterminated")


class TestThePresets:
    """`_hw_check_argv_for_preset` — the named argument sets."""

    def test_every_shipped_preset_resolves_and_survives_its_own_validator(self):
        """Driven from the table, so a preset added later is checked automatically. A shipped
        preset that its own allowlist would refuse is a feature that fails only when selected.
        """
        assert _HW_CHECK_PRESETS, "no presets defined; this asserts nothing"
        for name in _HW_CHECK_PRESETS:
            argv = _hw_check_argv_for_preset(name)
            assert _hw_check_validate_argv_tokens(argv) == argv

    @pytest.mark.parametrize("spelling", ["", "   ", None])
    def test_an_unspecified_preset_is_the_default(self, spelling):
        assert _hw_check_argv_for_preset(spelling) == _HW_CHECK_PRESETS["default"]

    def test_the_name_is_case_and_whitespace_insensitive(self):
        name = next(iter(_HW_CHECK_PRESETS))
        assert _hw_check_argv_for_preset(f"  {name.upper()}  ") == _HW_CHECK_PRESETS[name]

    def test_an_unknown_preset_names_the_valid_ones(self):
        """The refusal has to be actionable: an operator who mistypes needs the list, not a
        rejection."""
        with pytest.raises(ValueError, match="unknown preset") as caught:
            _hw_check_argv_for_preset("does-not-exist")
        for name in _HW_CHECK_PRESETS:
            assert name in str(caught.value)

    def test_it_returns_a_copy_of_the_preset(self):
        """`_HW_CHECK_PRESETS` is module state. Handing out the live list would let one request's
        mutation change what every later request runs — for the whole process, silently.
        """
        name = next(iter(_HW_CHECK_PRESETS))
        before = list(_HW_CHECK_PRESETS[name])
        _hw_check_argv_for_preset(name).append("--injected")
        assert _HW_CHECK_PRESETS[name] == before
