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
"""Local identity: users, groups, membership, and the principals they render as.

Phase 2 of the auth programme (``docs/auth/AUTH_PROGRAMME_SCOPE.md``). Requirements and design live in
``.plc/briefs/auth-identity-brief.md``; the resource taxonomy authorization will be written against
is ``docs/RESOURCE_MODEL.md``.

**This is a new first-class citizen**, in the sense ``.cursor/rules/core-designs.mdc`` uses: identity
is a concern no existing citizen owned. It deliberately does **not** live in the catalog, and that is
not a stylistic choice -- ``NVIS_CATALOG_FILE`` selects which catalog file a process reads, so users
in the catalog would mean an environment variable decides who can log in, which FR-12 forbids
outright. The catalog answers *what this deployment can run*; identity answers *who may use it*.

What it reuses rather than reimplements: ``catalog.store._AtomicJSONStorage`` (tmp + ``os.replace``
plus an ``fcntl`` flock), which is parameterised only by path. One implementation of
atomic-write-with-lock, two stores.
"""
