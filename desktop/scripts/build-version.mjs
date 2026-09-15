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
 *
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Print the build version: the package.json version plus a UTC build stamp.
 *
 *     0.3.0  ->  0.3.0-260910183755
 *
 * So "which build am I running?" has an answer, and two builds on the same day are
 * distinguishable — which `0.3.0` alone cannot do, and which matters while iterating.
 *
 * **The stamp is a semver prerelease identifier, deliberately, and that has two consequences
 * worth knowing.** Ordering is correct: semver compares numeric prerelease identifiers
 * numerically, so `0.3.0-260910183755` < `0.3.0-260911090000` < `0.3.0`. And `electron-updater`
 * treats prereleases as eligible when the *running* version is itself a prerelease, so a stamped
 * build will offer a newer stamped build without any `allowPrerelease` configuration. A plain
 * `0.3.0` release would not offer a stamped build, since a prerelease ranks below the release —
 * which is the right way round, but means stamped and unstamped builds should not be mixed in one
 * update channel.
 *
 * **Not written into package.json.** Committing a timestamp would put a new diff in every build,
 * and the file is the input, not the output. It is injected into the packaged app instead, via
 * electron-builder's `--config.extraMetadata.version`, so the source stays clean and
 * `app.getVersion()` still reports the stamped value at runtime.
 *
 * UTC, not local time: a build stamp that shifts with the builder's timezone cannot be ordered
 * against one from a colleague in another region.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

/**
 * The version comes from the repo root's `version.py`, not from `desktop/package.json`.
 *
 * That file is where the product's version actually lives — release metadata the ci-toolkit job
 * bumps — and the release CI job already reads it directly to stamp a release build. Reading
 * package.json here made a second copy of the number, and it drifted immediately: the product
 * moved to 0.3.1 while package.json still said 0.3.0, so a local build produced `0.3.0-<stamp>`,
 * a *lower* semver than the release it was built after. Shipping that would have offered users an
 * "update" that ranks below what they already run.
 *
 * Parsed, never imported — same reason `backend/app_version.py` parses it: importing drags in a
 * Python dependency for one string, and this is a Node script.
 */
function productVersion() {
  const root = join(here, "..", "..", "version.py");
  try {
    const m = readFileSync(root, "utf8").match(/^VERSION\s*=\s*["']([^"']+)["']/m);
    if (m) return m[1];
    process.stderr.write(`build-version: ${root} has no VERSION assignment\n`);
  } catch {
    process.stderr.write(`build-version: cannot read ${root}\n`);
  }
  // The fallback exists for a desktop-only checkout, but it MUST announce itself. Silent, it
  // produced a wrong answer that looked right: the container build mounts `desktop/` as /project,
  // so version.py was out of reach and the build shipped 0.3.0 while the product was on 0.3.1 —
  // a lower semver than the release it followed, discovered only by reading a filename.
  const fallback = JSON.parse(readFileSync(join(here, "..", "package.json"), "utf8")).version;
  process.stderr.write(
    `build-version: falling back to package.json version ${fallback}; ` +
      `this is only correct in a checkout with no version.py\n`,
  );
  return fallback;
}

const version = productVersion();

// Reuse an existing stamp when one is passed in, so every step of a single release (build, sign,
// publish) agrees rather than each re-reading the clock and drifting by a second.
const override = process.env.BUILD_STAMP;
const d = new Date();
const two = (n) => String(n).padStart(2, "0");
const stamp =
  override ||
  [
    String(d.getUTCFullYear()).slice(2),
    two(d.getUTCMonth() + 1),
    two(d.getUTCDate()),
    two(d.getUTCHours()),
    two(d.getUTCMinutes()),
    two(d.getUTCSeconds()),
  ].join("");

process.stdout.write(`${version}-${stamp}\n`);
