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
// Author: Rafael M. Koike
//
// The rack-staged stage ladder as an operator types it: "1, 50%, 100%". Each rung is a cumulative
// per-rack target -- a node count or a share of the rack -- and the ladder must rise and end at 100%
// so every node is reached. Mirrors backend catalog.models.validate_rollout_stages, so the editor
// flags a ladder before the save would.

/** The default ladder: one node per rack, then half of every rack, then the rest. */
export const DEFAULT_STAGES = ["1", "50%", "100%"];

const RUNG = /^([1-9][0-9]*)(%?)$/;

/** A ladder from typed text: comma-separated, spaces ignored, empty rungs dropped. */
export function parseStages(text: string): string[] {
  return text
    .split(",")
    .map((t) => t.replace(/\s+/g, ""))
    .filter(Boolean);
}

/** How the ladder reads back in an input. */
export function formatStages(stages: readonly string[]): string {
  return stages.join(", ");
}

/** Why a ladder cannot be used, or null when it can. An empty ladder is valid where it inherits. */
export function stagesProblem(stages: readonly string[], { allowEmpty = false } = {}): string | null {
  if (stages.length === 0) return allowEmpty ? null : "At least one stage is required.";
  const counts: number[] = [];
  const shares: number[] = [];
  for (const token of stages) {
    const m = RUNG.exec(token);
    if (!m) return `"${token}" is not a node count (e.g. 1) or a share (e.g. 50%).`;
    const value = Number(m[1]);
    if (m[2]) {
      if (value > 100) return `"${token}" is more than 100%.`;
      shares.push(value);
    } else {
      counts.push(value);
    }
  }
  const rising = (xs: number[]) => xs.every((x, i) => i === 0 || x > xs[i - 1]);
  if (!rising(counts) || !rising(shares)) return "Stages must rise.";
  if (stages[stages.length - 1] !== "100%") return "The last stage must be 100% so every node is reached.";
  return null;
}
