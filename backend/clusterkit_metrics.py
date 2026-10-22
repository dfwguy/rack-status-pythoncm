# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Shared ClusterKit triangular matrix access for legacy and modular results."""


def get_cell(links: list[object], i: int, j: int) -> float | None:
    """Upper-triangular storage: same convention as clusterkit ``bwResultAnalyzer.getCell``."""
    if i == j:
        return 0.0
    try:
        if i < j:
            row = links[i]
            if not isinstance(row, list) or j - i >= len(row):
                return None
            return float(row[j - i])
        row = links[j]
        if not isinstance(row, list) or i - j >= len(row):
            return None
        return float(row[i - j])
    except (IndexError, TypeError, ValueError):
        return None
