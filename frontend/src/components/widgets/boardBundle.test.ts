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
import { describe, expect, it } from "vitest";
import type { Tool } from "../../types/catalog";
import type { Workflow, WorkflowSummary } from "../../services/workflowService";
import { fromTool, fromWorkflow } from "./boardActions";
import {
  BUNDLE_KIND,
  BUNDLE_VERSION,
  buildBundle,
  bundleFilename,
  diffBundle,
  bundleToCustomization,
  parseBundle,
  platformMismatch,
} from "./boardBundle";

const SURFACE = "rackDetail.validation";
const SOURCE = {
  product_version: "0.3.0",
  cluster: "nscale-tx-gb300",
  platform: "gb300-nvl72",
  customer: "NscaleTX",
};

function tool(id: string, over: Partial<Tool> = {}): Tool {
  return {
    id,
    name: id,
    category: "validation",
    script: `${id}.sh`,
    env_key: "",
    params: [],
    compatible: { match: "all", capabilities: [], platform_ids: [] },
    surfaces: [SURFACE],
    execution: { mode: "async", target: "rack" },
    description: `${id} description`,
    ai_context: "",
    status: "ga",
    source: "seed",
    subtitle: "",
    button_label: "Run",
    icon: "",
    image: "",
    destructive: false,
    order: 100,
    launch: {
      kind: "async-job",
      endpoint: `/api/run-${id}-async`,
      preset_id: "",
      dialog: "",
      scopes: [],
      body: {},
      confirm: false,
      min_nodes: 0,
      max_nodes: 0,
    },
    ...over,
  };
}

const wfSummary = (id: string, over: Partial<WorkflowSummary> = {}): WorkflowSummary => ({
  id,
  name: id,
  description: "",
  version: 1,
  step_count: 2,
  ...over,
});

const wfFull = (id: string, over: Partial<Workflow> = {}): Workflow => ({
  id,
  name: id,
  description: "",
  steps: [],
  ...over,
});

// --- building --------------------------------------------------------------------------

describe("buildBundle", () => {
  it("records the board's actions, in board order", () => {
    const board = [fromTool(tool("a")), fromTool(tool("b"))];
    const bundle = buildBundle({
      surface: SURFACE,
      board,
      scope: "layout",
      source: SOURCE,
      now: () => new Date("2026-09-11T18:00:00Z"),
    });
    expect(bundle.kind).toBe(BUNDLE_KIND);
    expect(bundle.v).toBe(BUNDLE_VERSION);
    expect(bundle.board.keys).toEqual(["tool:a", "tool:b"]);
    expect(bundle.exported_at).toBe("2026-09-11T18:00:00.000Z");
  });

  it("omits definitions at layout scope, so the file stays a reference", () => {
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a"))],
      scope: "layout",
      source: SOURCE,
    });
    expect(bundle.tools).toBeUndefined();
    expect(bundle.workflows).toBeUndefined();
  });

  it("embeds the definitions of the actions on the board at full scope", () => {
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a")), fromWorkflow(wfSummary("wf1"))],
      scope: "full",
      source: SOURCE,
      workflows: [wfFull("wf1")],
    });
    expect(bundle.tools?.map((t) => t.id)).toEqual(["a"]);
    expect(bundle.workflows?.map((w) => w.id)).toEqual(["wf1"]);
  });

  it("carries the arrangement as key order, with no coordinates at all", () => {
    // v2 dropped board.layout: a coordinate written on one machine means nothing on another,
    // because the browser picks the column count. Key order is the whole portable arrangement.
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("b")), fromTool(tool("a"))],
      scope: "layout",
      source: SOURCE,
    });
    expect(bundle.board.keys).toEqual(["tool:b", "tool:a"]);
    expect("layout" in bundle.board).toBe(false);
  });

  it("names the file after the surface, scope and day", () => {
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a"))],
      scope: "full",
      source: SOURCE,
      now: () => new Date("2026-09-11T18:00:00Z"),
    });
    expect(bundleFilename(bundle)).toBe(
      "greenlight-board-rackdetail-validation-full-2026-09-11.json",
    );
  });
});

// --- parsing: refuse anything that is not ours -----------------------------------------

describe("parseBundle", () => {
  const good = () =>
    buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a"))],
      scope: "full",
      source: SOURCE,
    });

  it("round-trips a bundle it wrote", () => {
    const result = parseBundle(JSON.stringify(good()));
    expect(result.error).toBe("");
    expect(result.bundle?.board.keys).toEqual(["tool:a"]);
    expect(result.bundle?.source.platform).toBe("gb300-nvl72");
  });

  it("refuses a file that is not JSON", () => {
    expect(parseBundle("{nope").error).toMatch(/not valid JSON/);
  });

  it("refuses a JSON file that is not a bundle", () => {
    expect(parseBundle(JSON.stringify({ hello: "world" })).error).toMatch(/not a Greenlight/);
  });

  it("refuses a newer bundle version rather than guessing at it", () => {
    // The likeliest cause is a peer on a newer release, and that wants a different response
    // from the operator than a corrupt file.
    const err = parseBundle(JSON.stringify({ ...good(), v: 99 })).error;
    expect(err).toMatch(/version 99/);
    expect(err).toMatch(/newer Greenlight/);
  });

  it("refuses a bundle with no surface, and one with no actions", () => {
    expect(parseBundle(JSON.stringify({ ...good(), surface: "" })).error).toMatch(/which board/);
    const empty = { ...good(), board: { keys: [] } };
    expect(parseBundle(JSON.stringify(empty)).error).toMatch(/no actions/);
  });

  it("discards malformed keys instead of failing the whole file", () => {
    const messy = {
      ...good(),
      board: { keys: ["tool:a", "", "nonsense", 42, "workflow:w"] },
    };
    const parsed = parseBundle(JSON.stringify(messy)).bundle;
    expect(parsed?.board.keys).toEqual(["tool:a", "workflow:w"]);
  });

  it("reads a v1 bundle, ignoring the positions it carried", () => {
    // v1 stored board.layout beside the keys. The keys already encoded the order, so there is
    // nothing to migrate and no reason to refuse the file — refusing it would strand every
    // board anyone has already shared.
    const v1 = {
      ...good(),
      v: 1,
      board: { keys: ["tool:a"], layout: [{ i: "tool:a", x: 2, y: 1, w: 1, h: 1 }] },
    };
    const parsed = parseBundle(JSON.stringify(v1)).bundle;
    expect(parsed?.board.keys).toEqual(["tool:a"]);
    expect(parsed?.v).toBe(BUNDLE_VERSION);
  });

  it("still refuses a version from the future", () => {
    expect(parseBundle(JSON.stringify({ ...good(), v: 99 })).error).toMatch(/version 99/);
  });

  it("ignores definitions on a layout-scope bundle", () => {
    // Belt and braces: scope is what the UI shows, so a file claiming "layout" must not
    // smuggle documents past the review that full scope triggers.
    const sneaky = { ...good(), scope: "layout", tools: [tool("evil")] };
    expect(parseBundle(JSON.stringify(sneaky)).bundle?.tools).toBeUndefined();
  });
});

// --- the diff that drives the review ---------------------------------------------------

describe("diffBundle", () => {
  const bundleWith = (tools: Tool[], keys = tools.map((t) => `tool:${t.id}`)) =>
    parseBundle(
      JSON.stringify({
        ...buildBundle({
          surface: SURFACE,
          board: tools.map((t) => fromTool(t)),
          scope: "full",
          source: SOURCE,
        }),
        board: { keys },
        tools,
      }),
    ).bundle!;

  it("calls a tool this instance does not have new", () => {
    const items = diffBundle(bundleWith([tool("fresh")]), [], []);
    expect(items[0]).toMatchObject({ status: "new", missingLocally: true });
  });

  it("calls an identical tool identical, so nothing is offered to overwrite", () => {
    const t = tool("same");
    const items = diffBundle(bundleWith([t]), [t], []);
    expect(items[0].status).toBe("identical");
    expect(items[0].changedFields).toEqual([]);
  });

  it("names the fields that differ, so an overwrite is an informed choice", () => {
    const local = tool("x", { subtitle: "NCCL 2.30.7", order: 20 });
    const incoming = tool("x", { subtitle: "NCCL 2.31.2", order: 20 });
    const items = diffBundle(bundleWith([incoming]), [local], []);
    expect(items[0].status).toBe("changed");
    expect(items[0].changedFields).toEqual(["subtitle"]);
  });

  it("ignores provenance when diffing", () => {
    // A document from a peer is "user" on their instance and "seed" here; treating that as a
    // difference would make every shared tool read as an overwrite.
    const local = tool("x", { source: "seed" });
    const incoming = tool("x", { source: "user" });
    expect(diffBundle(bundleWith([incoming]), [local], [])[0].status).toBe("identical");
  });

  it("reports a referenced-but-absent action in a layout-only bundle", () => {
    const bundle = parseBundle(
      JSON.stringify(
        buildBundle({
          surface: SURFACE,
          board: [fromTool(tool("absent"))],
          scope: "layout",
          source: SOURCE,
        }),
      ),
    ).bundle!;
    const items = diffBundle(bundle, [], []);
    expect(items[0]).toMatchObject({ status: "new", missingLocally: true });
    expect(items[0].tool).toBeUndefined(); // nothing to write: it is a reference
  });

  it("diffs workflows as well as tools", () => {
    const bundle = parseBundle(
      JSON.stringify({
        ...buildBundle({
          surface: SURFACE,
          board: [fromWorkflow(wfSummary("wf1"))],
          scope: "full",
          source: SOURCE,
          workflows: [wfFull("wf1", { name: "Theirs" })],
        }),
      }),
    ).bundle!;
    const items = diffBundle(bundle, [], [wfSummary("wf1", { name: "Mine" })]);
    expect(items[0]).toMatchObject({ kind: "workflow", status: "changed" });
    expect(items[0].changedFields).toContain("name");
  });
});

describe("platformMismatch", () => {
  it("warns when the platforms differ, and says what it means", () => {
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a"))],
      scope: "layout",
      source: SOURCE,
    });
    const msg = platformMismatch(bundle, "b200-hgx-intel");
    expect(msg).toMatch(/gb300-nvl72/);
    expect(msg).toMatch(/will not appear/);
  });

  it("says nothing when they match, or when either is unknown", () => {
    const bundle = buildBundle({
      surface: SURFACE,
      board: [fromTool(tool("a"))],
      scope: "layout",
      source: SOURCE,
    });
    expect(platformMismatch(bundle, "gb300-nvl72")).toBe("");
    expect(platformMismatch(bundle, undefined)).toBe("");
  });
});

describe("bundleToCustomization", () => {
  const bundleFor = (keys: string[]) =>
    parseBundle(
      JSON.stringify({
        kind: BUNDLE_KIND,
        v: BUNDLE_VERSION,
        scope: "layout",
        exported_at: "",
        source: SOURCE,
        surface: SURFACE,
        board: { keys },
      }),
    ).bundle!;

  it("records what the bundle adds and what it hides, not an absolute list", () => {
    // An absolute list would freeze the board: a tool the catalog gains later would never
    // appear for anyone who had imported a board. Same trap `resolveBoard` avoids.
    const custom = bundleToCustomization(bundleFor(["tool:a", "tool:extra"]), [
      "tool:a",
      "tool:b",
    ]);
    expect(custom.added).toEqual(["tool:extra"]);
    expect(custom.removed).toEqual(["tool:b"]);
  });

  it("adds and removes nothing when the bundle matches the default board", () => {
    const custom = bundleToCustomization(bundleFor(["tool:a", "tool:b"]), ["tool:a", "tool:b"]);
    expect(custom.added).toEqual([]);
    expect(custom.removed).toEqual([]);
  });

  it("carries the imported positions through", () => {
    const custom = bundleToCustomization(bundleFor(["tool:a"]), ["tool:a"]);
    expect(custom.order).toEqual(["tool:a"]);
  });
});
