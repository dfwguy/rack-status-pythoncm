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
// validationStages.ts — shared helpers for the staged per-rack validation model.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
import type { RackRow, StageState } from "../types/api";

export type StageKey = "stage1" | "stage2";

export interface StageMeta {
  key: StageKey;
  /** Short badge label, e.g. "S1". */
  short: string;
  /** Human label, e.g. "Stage 1". */
  label: string;
  /** Underlying tool/test for the stage. */
  tool: string;
}

/**
 * The per-rack validation stages. Stage 3 (cross-rack) is a *multi-rack* concern
 * surfaced on the Multi-rack tab, not a per-rack column — a rack is considered
 * individually validated once stage 1 & stage 2 are both done.
 */
export const VALIDATION_STAGES: StageMeta[] = [
  { key: "stage1", short: "S1", label: "Stage 1", tool: "NCCL" },
  { key: "stage2", short: "S2", label: "Stage 2", tool: "NeMo" },
];

/**
 * The four buckets a validation status collapses to. Filter controls should offer these rather
 * than raw ``nemo`` strings, because the running form carries an elapsed time (``ongoing [12:34]``)
 * and would never match an equality test.
 */
export const VALIDATION_CATEGORIES: StageState[] = ["done", "fail", "ongoing", "pending"];

/** Collapse an ``ongoing [MM:SS]`` label (or any status) to a bare category. */
export function nemoCategory(nemo: string): StageState {
  const n = (nemo || "").trim();
  if (n === "done") return "done";
  if (n === "fail") return "fail";
  if (n.startsWith("ongoing")) return "ongoing";
  return "pending";
}

/**
 * Resolve stage 1 / stage 2 states for a row. When the backend omits stage fields
 * (legacy shell table, multirack rows), fall back to the combined ``nemo`` category
 * so the UI still renders something sensible.
 */
export function stageStates(row: Pick<RackRow, "nemo" | "stage1" | "stage2">): {
  stage1: StageState;
  stage2: StageState;
} {
  if (row.stage1 && row.stage2) {
    return { stage1: row.stage1, stage2: row.stage2 };
  }
  const cat = nemoCategory(row.nemo);
  return { stage1: row.stage1 ?? cat, stage2: row.stage2 ?? cat };
}

export type StageTagColor = "green" | "red" | "yellow" | "blue" | "gray";

export function stageStateColor(state: StageState): StageTagColor {
  switch (state) {
    case "done":
      return "green";
    case "fail":
      return "red";
    case "ongoing":
      return "yellow";
    case "pending":
      return "blue";
    default:
      return "gray";
  }
}

const STAGE_STATE_LABEL: Record<StageState, string> = {
  done: "pass",
  fail: "fail",
  ongoing: "running",
  pending: "not started",
};

export function stageStateLabel(state: StageState): string {
  return STAGE_STATE_LABEL[state] ?? state;
}
