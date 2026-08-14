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
"""The materialize hook must actually be registered -- silence here is invisible.

Materialising Settings overrides into ``os.environ`` strips Settings-managed keys, so the active
catalog overlay has to be re-applied straight after or platform-derived values (RACK_VALIDATION_
CT_RANGE and friends) quietly disappear. ``config_env`` cannot import that re-apply -- it lives in
``app.py`` and reaches the rack-status cache -- so ``app.py`` injects it.

If that injection were ever dropped, nothing would raise. ``_materialize_hook`` would stay ``None``,
the ``callable()`` guard would skip it, and the overlay would simply stop being re-applied: wrong
values on some clusters, no error anywhere. The recursion-guard suite would still pass, because
with no hook there is no re-entry to guard against -- so it cannot catch this either.

Hence a test whose only job is to assert the wire exists.
"""
from __future__ import annotations

import app
import config_env


def test_app_registers_its_catalog_reapply_as_the_hook():
    assert config_env._materialize_hook is app._apply_catalog_active_to_environ


def test_materializing_invokes_the_hook():
    calls: list[int] = []
    previous = config_env._materialize_hook
    try:
        config_env.set_materialize_hook(lambda: calls.append(1))
        config_env._materialize_job_env_overrides_into_os_environ({})
        assert calls == [1]
    finally:
        config_env.set_materialize_hook(previous)


def test_a_raising_hook_does_not_break_materialization():
    """A broken catalog must not take down every configuration read in the product."""
    previous = config_env._materialize_hook

    def boom():
        raise RuntimeError("catalog on fire")

    try:
        config_env.set_materialize_hook(boom)
        config_env._materialize_job_env_overrides_into_os_environ({})  # must not raise
    finally:
        config_env.set_materialize_hook(previous)


def test_no_hook_is_tolerated():
    """Import order is not guaranteed; a read before registration must still work."""
    previous = config_env._materialize_hook
    try:
        config_env.set_materialize_hook(None)
        config_env._materialize_job_env_overrides_into_os_environ({})  # must not raise
    finally:
        config_env.set_materialize_hook(previous)
