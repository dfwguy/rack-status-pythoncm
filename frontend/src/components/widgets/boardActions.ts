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
// boardActions — the one shape a board card renders, whatever it came from.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// A board used to hold catalog `Tool` documents only. It now also holds **workflows**, which
// are not catalog documents at all: they live in the workflow engine, are authored in the
// designer, and carry their own name, description and step count. Rather than require an
// operator to mint a Tool document to wrap each flow they build — friction, and two records
// to keep in step — both are adapted into this view model at the edge.
//
// Everything downstream (placement, persistence, the card, the details modal) works on
// `BoardAction` and never asks what kind of thing it is, except the one place that has to: the
// launcher. Tools go through their catalog launch binding, workflows through
// `POST /api/workflows/<id>/run`.
import type { Tool } from "../../types/catalog";
import type { WorkflowSummary } from "../../services/workflowService";

export type ActionKind = "tool" | "workflow";

/** Synthetic category for workflows, so they get their own badge colour and banner. */
export const WORKFLOW_CATEGORY = "workflow";

/**
 * Workflows sort after the catalog's tools by default.
 *
 * The seeded tools occupy 10..90, so a flow an operator adds lands at the end of the board
 * rather than in the middle of the validation sequence, where order is operational.
 */
export const WORKFLOW_ORDER = 500;

export interface BoardAction {
  /**
   * Stable, namespaced identity — `tool:nvloom`, `workflow:3f2a`.
   *
   * Namespaced because the two id spaces are independent: a workflow the operator named
   * `clusterkit` must not collide with the catalog tool of the same id, and a stored layout
   * has to survive either one being renamed without silently re-pointing at the other.
   */
  key: string;
  kind: ActionKind;
  /** The underlying tool or workflow id, without the namespace. */
  id: string;
  name: string;
  subtitle: string;
  description: string;
  /** Drives the badge and the banner artwork. `workflow` for flows. */
  category: string;
  buttonLabel: string;
  destructive: boolean;
  order: number;
  /** Surfaces this appears on by default. Always empty for workflows — see `fromWorkflow`. */
  surfaces: string[];
  /** Set when `kind === "tool"`. */
  tool?: Tool;
  /** Set when `kind === "workflow"`. */
  workflow?: WorkflowSummary;
}

export function actionKey(kind: ActionKind, id: string): string {
  return `${kind}:${id}`;
}

/** Split a key, tolerating an id that itself contains a colon. */
export function parseActionKey(key: string): { kind: ActionKind; id: string } | null {
  const at = key.indexOf(":");
  if (at <= 0) return null;
  const kind = key.slice(0, at);
  const id = key.slice(at + 1);
  if (!id) return null;
  if (kind !== "tool" && kind !== "workflow") return null;
  return { kind, id };
}

/** A catalog tool is launchable only once the catalog gave it a launch binding. */
export function toolIsLaunchable(tool: Tool): boolean {
  return !!tool.launch && tool.launch.kind !== "none";
}

export function fromTool(tool: Tool): BoardAction {
  return {
    key: actionKey("tool", tool.id),
    kind: "tool",
    id: tool.id,
    name: tool.name,
    subtitle: tool.subtitle || "",
    description: tool.description || "",
    category: tool.category,
    buttonLabel: tool.button_label || "Run",
    destructive: !!tool.destructive,
    order: tool.order ?? 100,
    surfaces: tool.surfaces ?? [],
    tool,
  };
}

/**
 * Adapt a workflow.
 *
 * `surfaces: []` on purpose: a flow someone authored is never on a *default* board. The
 * catalog's seeded tools are curated and shipped, whereas a workflow is one operator's work in
 * progress — appearing unasked on everybody's rack board would be a surprise, and possibly a
 * destructive one. It is offered under "Add action" and appears once chosen.
 */
export function fromWorkflow(workflow: WorkflowSummary): BoardAction {
  const steps = workflow.step_count === 1 ? "1 step" : `${workflow.step_count} steps`;
  return {
    key: actionKey("workflow", workflow.id),
    kind: "workflow",
    id: workflow.id,
    name: workflow.name,
    subtitle: `${steps} · v${workflow.version}`,
    description: workflow.description || "Custom workflow.",
    category: WORKFLOW_CATEGORY,
    buttonLabel: "Run flow",
    destructive: false,
    order: WORKFLOW_ORDER,
    surfaces: [],
    workflow,
  };
}

/** Every action the operator could put on a board: launchable tools plus every workflow. */
export function buildActions(tools: Tool[], workflows: WorkflowSummary[]): BoardAction[] {
  return [...tools.filter(toolIsLaunchable).map(fromTool), ...workflows.map(fromWorkflow)];
}
