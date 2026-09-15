#!/usr/bin/env node
// window-open-check.mjs — the window-open policy, asserted without launching Electron.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: LicenseRef-NvidiaProprietary
//
// `setWindowOpenHandler` decides whether a page may open a window in this process, hand a URL to
// the operator's browser, or neither. It is the shell's most security-relevant branch and the
// hardest to exercise in the smoke test, which needs a live deployment to connect to — so the
// decision lives in `windowOpenPolicy.ts` with no Electron import and this runs the table.
//
// Follows `menu-check.mjs`: a bespoke node check rather than a test framework, because that is
// what this package already has.

import assert from "node:assert/strict";

const { windowOpenDecision, sameOrigin } = await import("../out/main/windowOpenPolicy.js");

const ORIGIN = "https://cluster.example:5056";

const cases = [
  // The deployment's own pages open here. This is what "Open in a new window" on the
  // documentation needs, and what used to be handed to Safari.
  [`${ORIGIN}/docs/`, ORIGIN, "open-window"],
  [`${ORIGIN}/docs/reference/identity/`, ORIGIN, "open-window"],
  [`${ORIGIN}/`, ORIGIN, "open-window"],

  // Anything else http(s) goes to the real browser, which has the address bar an Electron popup
  // does not: Grafana, a vendor runbook, the repository.
  ["https://grafana.example:3000/d/abc", ORIGIN, "open-external"],
  ["https://gitlab-master.nvidia.com/nvis/nvis-rack-status", ORIGIN, "open-external"],
  ["http://cluster.example:5056/docs/", ORIGIN, "open-external"], // scheme differs => not same origin

  // The cases that make `sameOrigin` a parsed comparison rather than a `startsWith`. Both
  // *begin* with the allowed origin as a string.
  //
  // The userinfo form is the one to keep in mind: it parses, and its origin is `evil.example`.
  // A prefix check would have called it ours and opened it in-process.
  ["https://cluster.example:5056@evil.example/docs/", ORIGIN, "open-external"],
  ["https://cluster.example:5057/docs/", ORIGIN, "open-external"], // port differs
  // And this one is not a valid URL at all — letters in the port — so it is refused outright
  // rather than handed to the browser. Asserted because the first draft of this table expected
  // `open-external` and the check corrected it.
  [`${ORIGIN}.evil.example/docs/`, ORIGIN, "block"],

  // Never handed to the operating system.
  ["file:///etc/passwd", ORIGIN, "block"],
  ["data:text/html,<h1>hi", ORIGIN, "block"],
  ["javascript:alert(1)", ORIGIN, "block"],
  ["ms-msdt:/id", ORIGIN, "block"],
  ["not a url", ORIGIN, "block"],

  // The launcher window has no allowed origin, so nothing is same-origin for it.
  [`${ORIGIN}/docs/`, null, "open-external"],
];

let passed = 0;
for (const [url, origin, expected] of cases) {
  const actual = windowOpenDecision(url, origin);
  assert.equal(actual, expected, `${url} (origin ${origin}) => ${actual}, expected ${expected}`);
  passed += 1;
}

assert.equal(sameOrigin("https://cluster.example:5056@evil.example/x", ORIGIN), false);
assert.equal(sameOrigin(`${ORIGIN}.evil.example/x`, ORIGIN), false);
assert.equal(sameOrigin(`${ORIGIN}/x`, ORIGIN), true);

console.log(`window-open policy: ${passed} cases passed`);
