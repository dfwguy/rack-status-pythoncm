# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Rack ids, normalised through the cluster's naming templates.

Author: Rafael M. Koike & Cursor Agent ("Ada")

A rack id is not a hostname. ``NVIS_RACK_ID`` names the dashboard/`cmsh` grouping key; the real
compute hostnames come from Slurm and are a different scheme entirely (see
``.cursor/rules/cluster-naming-conventions.mdc`` -- deriving one from the other has produced pdsh
targets that do not resolve). Everything here is about the first scheme only.

``nvis_id_templates`` caches the compiled templates, and the cache is cleared when the active
cluster changes, so always go through ``_nvis_naming()`` rather than holding onto a naming object.
"""

from __future__ import annotations

import nvis_id_templates


def _nvis_naming() -> nvis_id_templates.NvisNaming:
    return nvis_id_templates.get_nvis_naming()


def _normalize_rack_id(rack: str) -> str:
    return _nvis_naming().normalize_rack_id(rack)
