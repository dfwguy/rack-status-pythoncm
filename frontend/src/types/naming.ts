/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
/** Naming hints from GET /api/status ``naming`` (Settings → NVIS_* templates). */
export interface NvisNamingSchema {
  rack_id: string;
  ct_id: string;
  nvsw_id: string;
  /** Literal between rack id and ``[tray-ranges]`` (e.g. ``-ct``); empty for CBR-style. */
  ct_joiner: string;
  node_id_mode: "rack_ct_suffix" | "full_hostname";
}
