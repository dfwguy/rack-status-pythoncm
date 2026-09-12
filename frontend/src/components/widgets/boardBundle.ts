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
// boardBundle — a board, as a file one Greenlight instance can hand another.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The point is sharing hard-won setup: somebody works out that a script needs a particular
// set of parameters to get a rack through bring-up, and their peers get the same buttons
// instead of rediscovering it. So a bundle carries two separable things, and the export panel
// makes the operator choose:
//
//   "layout"  which actions are on the board and where, by id. Tiny, and it only works on an
//             instance that already has those tools — the normal case between two deployments
//             of the same release.
//   "full"    the same, plus the actual Tool documents and Workflow definitions, so an
//             instance that has never seen them can recreate them. This is what makes a
//             bundle portable, and also what makes it dangerous: importing it WRITES to the
//             shared catalog, which is why import stages a per-item review instead of
//             applying silently.
//
// **Nothing here executes anything, and nothing here writes.** A bundle is data; applying it
// goes through the existing authorized endpoints (`POST /api/catalog/tools`,
// `POST /api/workflows`), so the server's own validation — including the `script` shape check
// that stops a shared file smuggling a shell payload — is the real gate. This module's job is
// to describe, diff and refuse, never to trust.
import type { Tool } from "../../types/catalog";
import type { Workflow, WorkflowSummary } from "../../services/workflowService";
import type { BoardAction } from "./boardActions";
import { parseActionKey } from "./boardActions";

/** Marks the file as ours. Checked before anything else is read. */
export const BUNDLE_KIND = "greenlight.board";

/**
 * Bump when the shape changes incompatibly; an unknown version is refused, not guessed at.
 *
 * 2 dropped `board.layout`: the board is a CSS grid whose column count the browser picks, so a
 * coordinate written on one machine means nothing on another — and `board.keys` was already
 * "in board order", which is the whole of what a shared arrangement can portably say. A v1
 * bundle is therefore **accepted**, not refused: ignoring its positions loses nothing that was
 * ever honoured.
 */
export const BUNDLE_VERSION = 2;

/** Versions we can read. v1 differs only by a field we now ignore. */
export const READABLE_BUNDLE_VERSIONS = [1, 2];

export type BundleScope = "layout" | "full";

export interface BundleSource {
  /** Product version that wrote it, for a human reading the file. */
  product_version: string;
  /** Informational: which cluster and platform it was built against. */
  cluster: string;
  platform: string;
  customer: string;
}

export interface BoardBundle {
  kind: typeof BUNDLE_KIND;
  v: number;
  scope: BundleScope;
  exported_at: string;
  source: BundleSource;
  /** The surface the board belongs to, e.g. `rackDetail.validation`. */
  surface: string;
  /** Action keys in board order. The order *is* the arrangement — see `BUNDLE_VERSION`. */
  board: {
    keys: string[];
  };
  /** Present only when `scope === "full"`. */
  tools?: Tool[];
  workflows?: Workflow[];
}

export interface BuildBundleArgs {
  surface: string;
  /** The resolved board, in order. */
  board: BoardAction[];
  scope: BundleScope;
  source: BundleSource;
  /** Full definitions for the workflows on the board; only needed for `scope: "full"`. */
  workflows?: Workflow[];
  now?: () => Date;
}

/**
 * Build a bundle for one surface's board.
 *
 * Only the documents for actions actually on the board are embedded. Exporting the whole
 * catalog would make every shared board a catalog migration, which is not what somebody
 * sharing "my bring-up screen" is asking for.
 */
export function buildBundle(args: BuildBundleArgs): BoardBundle {
  const { surface, board, scope, source } = args;
  const keys = board.map((a) => a.key);
  const bundle: BoardBundle = {
    kind: BUNDLE_KIND,
    v: BUNDLE_VERSION,
    scope,
    exported_at: (args.now?.() ?? new Date()).toISOString(),
    source,
    surface,
    board: { keys },
  };
  if (scope === "full") {
    bundle.tools = board
      .filter((a) => a.kind === "tool" && a.tool)
      .map((a) => a.tool as Tool);
    bundle.workflows = args.workflows ?? [];
  }
  return bundle;
}

/** A suggested filename. Surface and date, so a folder of these is readable. */
export function bundleFilename(bundle: BoardBundle): string {
  const surface = bundle.surface.replace(/[^A-Za-z0-9]+/g, "-").toLowerCase();
  const day = bundle.exported_at.slice(0, 10);
  return `greenlight-board-${surface}-${bundle.scope}-${day}.json`;
}

export interface ParseResult {
  bundle: BoardBundle | null;
  /** Why it was refused, for display. Empty when `bundle` is set. */
  error: string;
}

/**
 * Parse an uploaded file, refusing anything that is not a bundle we understand.
 *
 * Deliberately talkative on failure: the likeliest causes are a file from a newer Greenlight
 * and a file that is simply something else, and those want different responses from the
 * operator. "Invalid JSON" alone sends them looking in the wrong place.
 */
export function parseBundle(text: string): ParseResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { bundle: null, error: "That file is not valid JSON." };
  }
  if (!raw || typeof raw !== "object") {
    return { bundle: null, error: "That file does not contain a board bundle." };
  }
  const b = raw as Partial<BoardBundle>;
  if (b.kind !== BUNDLE_KIND) {
    return {
      bundle: null,
      error: `That file is not a Greenlight board bundle (expected kind "${BUNDLE_KIND}").`,
    };
  }
  if (typeof b.v !== "number" || !READABLE_BUNDLE_VERSIONS.includes(b.v)) {
    return {
      bundle: null,
      error:
        `This bundle is version ${String(b.v)}; this build understands version ` +
        `${READABLE_BUNDLE_VERSIONS.join(" and ")}. It was probably exported by a newer ` +
        `Greenlight.`,
    };
  }
  if (typeof b.surface !== "string" || !b.surface) {
    return { bundle: null, error: "This bundle does not say which board it is for." };
  }
  const keys = Array.isArray(b.board?.keys)
    ? b.board!.keys.filter((k): k is string => typeof k === "string" && !!parseActionKey(k))
    : [];
  if (keys.length === 0) {
    return { bundle: null, error: "This bundle contains no actions." };
  }
  const scope: BundleScope = b.scope === "full" ? "full" : "layout";
  return {
    bundle: {
      kind: BUNDLE_KIND,
      v: BUNDLE_VERSION,
      scope,
      exported_at: typeof b.exported_at === "string" ? b.exported_at : "",
      source: {
        product_version: str(b.source?.product_version),
        cluster: str(b.source?.cluster),
        platform: str(b.source?.platform),
        customer: str(b.source?.customer),
      },
      surface: b.surface,
      // A v1 bundle's `board.layout` is read and discarded here: `keys` already carries the
      // order it encoded, so there is nothing to migrate.
      board: { keys },
      tools: scope === "full" && Array.isArray(b.tools) ? b.tools.filter(isToolish) : undefined,
      workflows:
        scope === "full" && Array.isArray(b.workflows)
          ? b.workflows.filter(isWorkflowish)
          : undefined,
    },
    error: "",
  };
}

export type ItemStatus = "new" | "changed" | "identical";

export interface BundleItem {
  key: string;
  kind: "tool" | "workflow";
  id: string;
  name: string;
  status: ItemStatus;
  /** For `changed`: which fields differ from the local copy. */
  changedFields: string[];
  /** Present when the bundle embedded a definition (scope "full"). */
  tool?: Tool;
  workflow?: Workflow;
  /** True when the action is referenced but this instance has no such tool/workflow. */
  missingLocally: boolean;
}

/** Fields compared when deciding whether an incoming tool differs from the local one. */
const COMPARED_TOOL_FIELDS = [
  "name",
  "category",
  "script",
  "env_key",
  "subtitle",
  "button_label",
  "icon",
  "image",
  "description",
  "destructive",
  "order",
  "surfaces",
  "execution",
  "compatible",
  "params",
  "launch",
] as const;

/**
 * Work out, per item in the bundle, what importing it would do here.
 *
 * `source` deliberately does not participate: a document that arrives from a peer is by
 * definition "user" on their instance and would otherwise always read as changed. Nor does it
 * decide anything — `source` is provenance, and the importer's own copy keeps its own.
 */
export function diffBundle(
  bundle: BoardBundle,
  localTools: Tool[],
  localWorkflows: WorkflowSummary[],
): BundleItem[] {
  const toolsById = new Map(localTools.map((t) => [t.id, t]));
  const workflowsById = new Map(localWorkflows.map((w) => [w.id, w]));
  const incomingTools = new Map((bundle.tools ?? []).map((t) => [t.id, t]));
  const incomingWorkflows = new Map((bundle.workflows ?? []).map((w) => [w.id, w]));

  return bundle.board.keys.flatMap((key): BundleItem[] => {
    const parsed = parseActionKey(key);
    if (!parsed) return [];
    const { kind, id } = parsed;

    if (kind === "tool") {
      const incoming = incomingTools.get(id);
      const local = toolsById.get(id);
      if (!incoming) {
        // Layout-only bundle, or a full one that omitted this document.
        return [{
          key, kind, id,
          name: local?.name ?? id,
          status: local ? "identical" : "new",
          changedFields: [],
          missingLocally: !local,
        }];
      }
      if (!local) {
        return [{ key, kind, id, name: incoming.name, status: "new", changedFields: [], tool: incoming, missingLocally: true }];
      }
      const changedFields = COMPARED_TOOL_FIELDS.filter(
        (f) => stable(incoming[f]) !== stable(local[f]),
      );
      return [{
        key, kind, id,
        name: incoming.name,
        status: changedFields.length ? "changed" : "identical",
        changedFields: [...changedFields],
        tool: incoming,
        missingLocally: false,
      }];
    }

    const incoming = incomingWorkflows.get(id);
    const local = workflowsById.get(id);
    if (!incoming) {
      return [{
        key, kind, id,
        name: local?.name ?? id,
        status: local ? "identical" : "new",
        changedFields: [],
        missingLocally: !local,
      }];
    }
    if (!local) {
      return [{ key, kind, id, name: incoming.name, status: "new", changedFields: [], workflow: incoming, missingLocally: true }];
    }
    const changedFields = ["name", "description", "steps"].filter(
      (f) => stable((incoming as unknown as Record<string, unknown>)[f]) !==
        stable((local as unknown as Record<string, unknown>)[f]),
    );
    return [{
      key, kind, id,
      name: incoming.name,
      status: changedFields.length ? "changed" : "identical",
      changedFields,
      workflow: incoming,
      missingLocally: false,
    }];
  });
}

/**
 * Express an imported board as a customisation diff against the catalog default.
 *
 * Stored as a diff, not as the absolute list the bundle contains, for the same reason every
 * other customisation is: an absolute list freezes the board, so a tool the catalog gains
 * later would never appear for anyone who had imported a board. The bundle says "these
 * actions"; we record "these added, those hidden", and the default keeps flowing through.
 */
export function bundleToCustomization(
  bundle: BoardBundle,
  defaultKeys: string[],
): { added: string[]; removed: string[]; order: string[] } {
  const wanted = bundle.board.keys;
  return {
    added: wanted.filter((k) => !defaultKeys.includes(k)),
    removed: defaultKeys.filter((k) => !wanted.includes(k)),
    // The bundle's key order is the shared arrangement, so it becomes the stored order
    // verbatim. Keys the local catalog does not have are filtered out later by `resolveBoard`,
    // which is also what keeps a partial order safe.
    order: [...wanted],
  };
}

/**
 * Whether the bundle was built against a different platform.
 *
 * A warning, never a refusal: the catalog's own `compatible` rules already hide a tool that
 * cannot run here, so importing a GB300 board onto a B200 cluster yields a board with some
 * cards missing rather than something broken. Saying so up front beats letting the operator
 * wonder where half their buttons went.
 */
export function platformMismatch(bundle: BoardBundle, localPlatform: string | undefined): string {
  const theirs = bundle.source.platform;
  if (!theirs || !localPlatform || theirs === localPlatform) return "";
  return `This board was built for ${theirs}; this cluster is ${localPlatform}. Actions that are not compatible with your platform will not appear.`;
}

// --- narrowing helpers ----------------------------------------------------------------

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}


function isToolish(t: unknown): t is Tool {
  const q = t as Partial<Tool>;
  return !!q && typeof q.id === "string" && !!q.id && typeof q.name === "string";
}

function isWorkflowish(w: unknown): w is Workflow {
  const q = w as Partial<Workflow>;
  return !!q && typeof q.id === "string" && !!q.id && typeof q.name === "string";
}

/** Order-insensitive-enough comparison for diffing. */
function stable(v: unknown): string {
  if (v === undefined || v === null) return "";
  if (Array.isArray(v)) return JSON.stringify([...v].map(stable).sort());
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>)
      .map(([k, val]) => [k, stable(val)] as const)
      .sort(([a], [b]) => a.localeCompare(b));
    return JSON.stringify(entries);
  }
  return JSON.stringify(v);
}
