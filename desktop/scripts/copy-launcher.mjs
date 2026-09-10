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
 * Copy the launcher's static assets next to their compiled script.
 *
 * `tsc` emits `src/launcher/launcher.ts` to `out/launcher/launcher.js` but knows nothing about the
 * `.html` and `.css` beside it, and `windows.ts` loads `out/launcher/index.html`. Without this the
 * app starts to a blank window with no error — the file simply is not there.
 */

import { copyFile, mkdir, readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const from = join(here, "..", "src", "launcher");
const to = join(here, "..", "out", "launcher");

await mkdir(to, { recursive: true });

const assets = (await readdir(from)).filter((name) => /\.(html|css)$/.test(name));
if (assets.length === 0) {
  throw new Error(`no launcher assets found in ${from}; the app would start to a blank window`);
}

for (const name of assets) {
  await copyFile(join(from, name), join(to, name));
  console.log(`copied launcher/${name}`);
}
