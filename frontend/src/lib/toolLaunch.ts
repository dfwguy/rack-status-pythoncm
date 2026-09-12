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
// toolLaunch — turn a catalog Tool's launch binding into something a view can act on.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// A widget does not launch anything itself. It produces a plan, and the view hands that plan
// to the launch machinery it already owns — the preset dialog, the launch-options dialog, the
// command store. That split is deliberate: a widget that POSTed on its own would be a second
// launch path beside the reviewed one, and reviewed-target drift is exactly what the plan_hash
// on the preset path exists to catch.
import type { Tool } from "../types/catalog";

export type ToolLaunchPlan =
  | { kind: "validation-preset"; presetId: string; scopes: string[]; label: string }
  | {
      kind: "async-job";
      endpoint: string;
      /** Options dialog to show first; empty means launch straight away. */
      dialog: string;
      body: Record<string, unknown>;
      label: string;
      confirm: boolean;
    }
  | { kind: "stream"; endpoint: string; body: Record<string, unknown>; label: string }
  | { kind: "dialog"; dialog: string; label: string }
  | { kind: "unsupported"; reason: string; label: string };

/**
 * Read a tool's binding, or say why it cannot be honoured.
 *
 * Every failure returns `unsupported` with a reason rather than throwing or returning null,
 * because the caller renders it: a widget whose binding the running frontend does not
 * understand must say so on the card. A button that silently does nothing when pressed is the
 * worst outcome available here — an operator cannot tell it apart from a cluster that is slow,
 * and the deployment that produced it is usually a frontend older than its catalog.
 */
export function planToolLaunch(tool: Tool): ToolLaunchPlan {
  const label = tool.name || tool.id;
  const launch = tool.launch;
  if (!launch || launch.kind === "none") {
    return { kind: "unsupported", reason: "this tool has no launch binding", label };
  }
  const body = launch.body ?? {};
  switch (launch.kind) {
    case "validation-preset":
      if (!launch.preset_id) {
        return { kind: "unsupported", reason: "preset binding names no recipe", label };
      }
      return {
        kind: "validation-preset",
        presetId: launch.preset_id,
        scopes: launch.scopes ?? [],
        label,
      };
    case "async-job":
      if (!isOwnApiPath(launch.endpoint)) {
        return { kind: "unsupported", reason: `endpoint is not ours: ${launch.endpoint}`, label };
      }
      return {
        kind: "async-job",
        endpoint: launch.endpoint,
        dialog: launch.dialog ?? "",
        body,
        label,
        confirm: !!launch.confirm || !!tool.destructive,
      };
    case "stream":
      if (!isOwnApiPath(launch.endpoint)) {
        return { kind: "unsupported", reason: `endpoint is not ours: ${launch.endpoint}`, label };
      }
      return { kind: "stream", endpoint: launch.endpoint, body, label };
    case "dialog":
      if (!launch.dialog) {
        return { kind: "unsupported", reason: "dialog binding names no dialog", label };
      }
      return { kind: "dialog", dialog: launch.dialog, label };
    default:
      // A kind this build does not know. The catalog is served by a backend that may be newer
      // than the bundle in the browser, so this is a real state, not a defensive branch.
      return {
        kind: "unsupported",
        reason: `unknown launch kind "${String(launch.kind)}" — this build may be older than the catalog`,
        label,
      };
  }
}

/**
 * Why the current node selection cannot run this widget, or null if it can.
 *
 * The bounds come from the document, so a per-surface rule like "GPT-OSS is single-node on
 * the rack tab" is catalog data rather than an `if` in a component. The message names the
 * requirement: a button that is disabled without saying why sends the operator hunting.
 */
export function nodeSelectionProblem(tool: Tool, nodeCount: number): string | null {
  const min = tool.launch?.min_nodes ?? 0;
  const max = tool.launch?.max_nodes ?? 0;
  if (min && max && min === max && nodeCount !== min) {
    return `Select exactly ${min} node${min === 1 ? "" : "s"} (${nodeCount} selected).`;
  }
  if (min && nodeCount < min) {
    return `Select at least ${min} node${min === 1 ? "" : "s"} (${nodeCount} selected).`;
  }
  if (max && nodeCount > max) {
    return `Select at most ${max} node${max === 1 ? "" : "s"} (${nodeCount} selected).`;
  }
  return null;
}

/**
 * The poll URL for an async launch endpoint.
 *
 * There are two conventions in the backend, not one, and the split is not cosmetic:
 *
 *  - the `run-*` job family has a dedicated `-status` route per tool
 *    (`/api/run-nvloom-async` -> `/api/run-nvloom-status`), because those carry per-tool
 *    payloads — parsed results, task lists, Slurm ids;
 *  - every other async action (preflight, resets, power, IMEX, NVSwitch) shares the generic
 *    `/api/reset-job-status`, which is just phase and output.
 *
 * Deriving this from the launch endpoint is what lets a catalog document carry one URL
 * instead of two that could disagree. `test_async_launch_endpoints_have_a_status_route`
 * checks the mapping against the tracked route snapshot, so adding a tool whose poll route
 * does not exist fails in CI rather than at the first press.
 */
export const GENERIC_JOB_STATUS = "/api/reset-job-status";

export function statusUrlFor(endpoint: string, jobId: string): string {
  const stem = /^\/api\/run-[a-z0-9-]+-async$/.test(endpoint)
    ? endpoint.replace(/-async$/, "-status")
    : GENERIC_JOB_STATUS;
  return `${stem}?job_id=${encodeURIComponent(jobId)}`;
}

/**
 * Same-origin `/api/...` paths only, re-checked here.
 *
 * The backend validates this when a document is written, which is the real gate. This is the
 * second half: a store can be edited by hand on the box, and a value that reached the browser
 * has already skipped the write path. Cheap to check, and the failure mode it prevents —
 * pointing a button at another origin — is not one worth trusting a single check with.
 */
function isOwnApiPath(endpoint: string): boolean {
  return (
    typeof endpoint === "string" &&
    /^\/api\/[A-Za-z0-9][A-Za-z0-9._~/-]*$/.test(endpoint) &&
    !endpoint.includes("..")
  );
}
