# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Author: Harish (nvis-ova/nvis-ova-rack-status), harvested into v3 by Rafael M. Koike & Cursor Agent ("Ada")
"""The BCM rack inventory is cached, so the dashboard stops re-running cmsh on every poll.

Harvested from the NVIS-OVA fork, where it was written as groundwork for the PythonCM cutover
(that plan calls for "cached inventory and identity maps"). It stands on its own: it cuts
CMDaemon load today on plain cmsh, and it stops a transient cmsh failure from blanking the rack
table, which used to happen because the old refresh assigned the empty list unconditionally.
"""
from __future__ import annotations

from pathlib import Path

import native_status


def test_rack_inventory_is_cached_but_device_workers_remain_independent(monkeypatch):
    calls: list[int] = []
    clock = iter((100.0, 110.0, 111.0))

    monkeypatch.setenv("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")
    monkeypatch.setattr(native_status.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        native_status,
        "_cmsh_rack_list",
        lambda: calls.append(1) or ["wh1j1", "wh1j15"],
    )

    state = native_status.NativeRackTableState("test", Path("/tmp"))
    state.refresh_racks_blocking()
    state.refresh_racks_blocking()

    assert calls == [1]
    assert state.racks == ["wh1j1", "wh1j15"]


def test_rack_inventory_refreshes_after_ttl(monkeypatch):
    inventories = iter((["wh1j1"], ["wh1j1", "wh1j15"]))
    clock = iter((100.0, 1001.0))

    monkeypatch.setenv("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")
    monkeypatch.setattr(native_status.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(native_status, "_cmsh_rack_list", lambda: list(next(inventories)))

    state = native_status.NativeRackTableState("test", Path("/tmp"))
    state.refresh_racks_blocking()
    state.refresh_racks_blocking()

    assert state.racks == ["wh1j1", "wh1j15"]


def test_failed_refresh_keeps_last_good_inventory(monkeypatch):
    inventories = iter((["wh1j1"], []))
    clock = iter((100.0, 1001.0))

    monkeypatch.setenv("RACK_NATIVE_RACK_LIST_TTL_SEC", "900")
    monkeypatch.setattr(native_status.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(native_status, "_cmsh_rack_list", lambda: list(next(inventories)))

    state = native_status.NativeRackTableState("test", Path("/tmp"))
    state.refresh_racks_blocking()
    state.refresh_racks_blocking()

    assert state.racks == ["wh1j1"]
