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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Keeps the heavy dependencies out of the initial bundle — the code every operator downloads
// before the dashboard paints, over an SSH tunnel, on a cluster with no internet.
//
// This is a *static* check on the import graph rather than a size assertion on the built
// output, for three reasons: it needs no `npm run build`, so it runs in the existing
// `frontend-test` job in milliseconds; a byte threshold drifts and gets raised until it means
// nothing; and when it fails it can name the exact import chain that caused it, which is the
// only part anybody needs.
//
// It exists because the policy it enforces was written in a comment and then quietly broken.
// `App.tsx` has code-split every route since 2026-08, but the comment spoke only of routes,
// and three heavy libraries walked back in through **dialogs**: a conditionally *rendered*
// component is still statically *imported*, and the bundler does not care that React will
// never mount it. chart.js came in twice (`RackTable` and `RackDetailView` → parser charts),
// xterm twice (`Toolbar` and `RackDetailView` → terminal), react-flow once (`ValidationTab` →
// `WorkflowRunPanel`, added the same week this test was written). Together, 1.2 MB of source
// and 45% of the initial chunk.
import { describe, expect, it } from "vitest";

/**
 * Every module under `src/`, as text.
 *
 * `import.meta.glob` rather than `node:fs` because `tsconfig.app.json` pins
 * `types: ["vite/client"]` — node APIs are deliberately not available to code under `src/`,
 * and weakening that to let one test read files would open the door for browser code to do the
 * same. Vite resolves this at transform time, so it also works in the jsdom environment.
 */
const SOURCES = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Keyed without the leading `./`, so paths compose like the import specifiers do. */
const FILES: Record<string, string> = Object.fromEntries(
  Object.entries(SOURCES).map(([k, v]) => [k.replace(/^\.\//, ""), v]),
);

/** Entry points the browser loads before anything is rendered. */
const ROOTS = ["main.tsx", "App.tsx"];

/**
 * Packages whose weight is worth a chunk of their own, and the surface that should own them.
 *
 * A package earns a place here by being large *and* optional — needed by one screen or one
 * dialog rather than by the shell. Adding one is cheap; the test then tells the next person
 * which import chain to break.
 */
const HEAVY: Record<string, string> = {
  "chart.js": "parser / analytics charts",
  "react-chartjs-2": "parser / analytics charts",
  "@xterm/xterm": "the web terminal",
  "@xterm/addon-webgl": "the web terminal",
  "@xterm/addon-fit": "the web terminal",
  reactflow: "the workflow diagram",
  "@reactflow/core": "the workflow diagram",
  dagre: "the workflow diagram",
  "monaco-editor": "the policy editor",
  "@monaco-editor/react": "the policy editor",
  mermaid: "the workflow editor",
  cytoscape: "the topology graph",
  katex: "markdown maths",
};

/**
 * Static imports only.
 *
 * `import(...)` never matches — the line it appears on starts with `const`/`return`, and the
 * parenthesis is not a quote — which is the whole point: a dynamic import is a chunk boundary,
 * so everything behind one is on demand by construction and this walk should stop there.
 *
 * `import type` is skipped because it is erased before the bundler sees it, so counting it
 * would report a package that ships no bytes.
 */
const STATIC_IMPORT =
  /^[ \t]*(?:import|export)\s(?!type\s)[^;]*?from\s*["']([^"']+)["']|^[ \t]*import\s*["']([^"']+)["']/gm;

/** `a/b/../c` -> `a/c`. No path module available here (see SOURCES above). */
function normalizePath(path: string): string {
  const out: string[] = [];
  for (const part of path.split("/")) {
    if (part === "." || part === "") continue;
    if (part === "..") out.pop();
    else out.push(part);
  }
  return out.join("/");
}

function resolve(spec: string, importer: string): string | null {
  if (!spec.startsWith(".")) return null; // a bare package, not one of our modules
  const dir = importer.includes("/") ? importer.slice(0, importer.lastIndexOf("/")) : "";
  const base = normalizePath(`${dir}/${spec}`);
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) {
    if (candidate in FILES) return candidate;
  }
  return null;
}

/** Every module the browser parses before the first paint, with how each was reached. */
function walkEagerGraph(): { seen: Set<string>; parent: Map<string, string>; heavy: Map<string, string[]> } {
  const seen = new Set<string>(ROOTS);
  const parent = new Map<string, string>();
  const heavy = new Map<string, string[]>();
  const queue = [...ROOTS];

  while (queue.length) {
    const file = queue.shift()!;
    const text = FILES[file];
    if (text === undefined) continue;
    for (const match of text.matchAll(STATIC_IMPORT)) {
      const spec = match[1] ?? match[2];
      if (!spec) continue;
      const pkg = spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];
      if (spec in HEAVY || pkg in HEAVY) {
        const chain: string[] = [];
        for (let cur: string | undefined = file; cur && chain.length < 12; cur = parent.get(cur)) {
          chain.push(cur.slice(cur.lastIndexOf("/") + 1));
        }
        heavy.set(spec in HEAVY ? spec : pkg, chain);
        continue;
      }
      const next = resolve(spec, file);
      if (next && !seen.has(next)) {
        seen.add(next);
        parent.set(next, file);
        queue.push(next);
      }
    }
  }
  return { seen, parent, heavy };
}

describe("the initial bundle", () => {
  it("can actually see the source tree", () => {
    // Fails loudly if the glob stops matching, rather than reporting a clean graph because it
    // read nothing. A check that cannot run must not look like a check that passed.
    expect(Object.keys(FILES).length).toBeGreaterThan(200);
    for (const root of ROOTS) expect(root in FILES, `${root} not in the glob`).toBe(true);
    expect(walkEagerGraph().seen.size).toBeGreaterThan(50);
  });

  it("does not statically import any heavy dependency", () => {
    const { heavy } = walkEagerGraph();
    const report = [...heavy.entries()].map(
      ([pkg, chain]) =>
        `  ${pkg} (belongs to ${HEAVY[pkg]})\n      reached by: ${chain.join(" <- ")}`,
    );
    expect(
      report.join("\n"),
      report.length
        ? "A heavy dependency is in the initial bundle — every operator now downloads it before " +
            "the dashboard paints.\n\n" +
            report.join("\n") +
            "\n\nFix the *first* file in each chain: make the component `lazy()` AND render it only " +
            "when it is open (`{flag ? <Suspense>…</Suspense> : null}`). A lazy component that is " +
            "rendered with `open={false}` still fetches its chunk immediately, so both halves are " +
            "required. See the note above the route imports in App.tsx."
        : undefined,
    ).toBe("");
  });
});
