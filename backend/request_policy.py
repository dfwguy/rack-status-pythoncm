# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
"""Pure request classification shared by auth bootstrap and action capture."""


def is_print_only_request(path, method):
    return (method in {"GET", "HEAD"} and path in {
        "/api/clusterkit/readiness", "/api/clusterkit/inventory",
    }) or (method == "POST" and path == "/api/clusterkit/preview")
