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
"""The operator audit trail.

* ``actions`` — the declared action vocabulary and the registry that validates it. Present.
* ``sink`` — the bounded rotating JSONL writer. Work item 4.
* ``emit`` and the event schema and flood guard. Work item 4.

Kept deliberately import-light: ``actions`` is imported by every module under ``backend/api/``,
so anything expensive here would be paid 38 times at startup.
"""

from __future__ import annotations

from audit.actions import action, build_registry, registry, resolve

__all__ = ["action", "build_registry", "registry", "resolve"]
