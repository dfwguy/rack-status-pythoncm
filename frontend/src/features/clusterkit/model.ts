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
import type { InventoryNode, RunStatus } from "./types";

export function nodeAvailability(
  node: InventoryNode,
  partition?: string,
): "idle" | "busy" | "unavailable" {
  if (
    !partition ||
    !node.partitions.includes(partition) ||
    !node.states.length ||
    node.states.some((state) => !["IDLE", "ALLOCATED", "MIXED"].includes(state))
  )
    return "unavailable";
  return node.states.every((state) => state === "IDLE") ? "idle" : "busy";
}
export function terminal(run: RunStatus): boolean {
  return (
    ["done", "error", "timeout"].includes(run.phase) &&
    run.work_phase !== "submission_unknown"
  );
}
export function functionalStatus(run: RunStatus): string {
  if (run.summary_error) return "INVALID EVIDENCE";
  if (["error", "timeout"].includes(run.phase)) return "FAIL";
  if (run.summary?.functional_status === "FAIL") return "FAIL";
  if (
    run.phase === "done" &&
    run.scheduler_state === "COMPLETED" &&
    run.summary?.functional_status
  )
    return run.summary.functional_status;
  return terminal(run) ? "FAIL" : "PENDING";
}
export function dateTime(value?: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
