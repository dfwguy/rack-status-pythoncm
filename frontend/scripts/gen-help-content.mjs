/**
 * Generates `help_content.json` at the repo root from the canonical TypeScript
 * knowledge registry (`src/content`). This mirror lets the Flask backend read
 * the exact same content the frontend uses (the "hybrid" storage model).
 *
 * Run via `npm run gen:help`. It is also invoked from `prebuild` so the JSON
 * stays in sync with the TS source on every production build.
 *
 * Bundles the type-only TS registry into an in-memory ESM module using whichever
 * bundler ships with the toolchain (rolldown for Vite 8, esbuild otherwise), so
 * no extra runtime dependency (tsx/ts-node) is required.
 */
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const entry = resolve(here, "../src/content/index.ts");
const outPath = resolve(here, "../../help_content.json");

/** Bundles `entry` to a single ESM string, trying rolldown then esbuild. */
async function bundleToEsm() {
  try {
    const { rolldown } = await import("rolldown");
    const bundle = await rolldown({ input: entry, logLevel: "silent" });
    const { output } = await bundle.generate({ format: "esm" });
    await bundle.close();
    return output[0].code;
  } catch (rolldownErr) {
    try {
      const { build } = await import("esbuild");
      const result = await build({
        entryPoints: [entry],
        bundle: true,
        write: false,
        format: "esm",
        platform: "neutral",
        logLevel: "silent",
      });
      return result.outputFiles[0].text;
    } catch (esbuildErr) {
      throw new AggregateError(
        [rolldownErr, esbuildErr],
        "Neither rolldown nor esbuild could bundle the help registry",
      );
    }
  }
}

async function main() {
  const code = await bundleToEsm();
  const moduleUrl =
    "data:text/javascript;base64," + Buffer.from(code).toString("base64");
  const mod = await import(moduleUrl);

  const entries = mod.ALL_CONTENT;
  if (!Array.isArray(entries) || entries.length === 0) {
    throw new Error("ALL_CONTENT is empty — refusing to write help_content.json");
  }

  const payload = {
    generated_by: "frontend/scripts/gen-help-content.mjs",
    count: entries.length,
    entries,
  };

  writeFileSync(outPath, JSON.stringify(payload, null, 2) + "\n", "utf-8");
  console.log(`Wrote ${entries.length} help entries to ${outPath}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
