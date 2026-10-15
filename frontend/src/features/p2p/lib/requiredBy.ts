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
// A catalog image's Required by names the launches that run it; a launch finds its image by that
// name, so a typo there is silent until the launch is refused. The editor picks from the names the
// backend reports (GET /api/p2p/catalog -> consumers) instead of taking free text.
import type { ImageConsumer } from "../services/fleetService";

/**
 * Required by options: every launch that stages the image naming it, plus any name already on an
 * image that this build does not know (kept visible so saving never drops it silently).
 */
export function requiredByItems(consumers: ImageConsumer[], selected: string[]) {
  const known = new Set(consumers.map((c) => c.id));
  return [
    ...consumers.map((c) => ({ label: `${c.label} (${c.id})`, value: c.id })),
    ...selected.filter((id) => !known.has(id)).map((id) => ({ label: `${id} (unknown launch)`, value: id })),
  ];
}
