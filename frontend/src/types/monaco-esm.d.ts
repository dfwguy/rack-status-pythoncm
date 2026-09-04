/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * Types for Monaco's deep ESM entry points.
 *
 * `monaco-editor` declares no root `types` field, so TypeScript cannot resolve the `.d.ts` beside
 * `esm/vs/editor/editor.api.js` even though the package's `exports` map permits the subpath. The
 * deep imports are not optional: importing the package root pulls in the TypeScript, CSS and HTML
 * language workers and took this SPA from 8.1 MB to 22 MB, for an editor that only opens JSON.
 *
 * These declarations re-export the real types rather than widening to `any`, so a rename in a
 * future Monaco still surfaces as a type error — which is how the 0.56 move of the JSON language
 * service was caught rather than shipped as an editor that silently never validated.
 */

declare module 'monaco-editor/editor/editor.api.js' {
  export * from 'monaco-editor';
}


declare module 'monaco-editor/language/json/monaco.contribution.js' {
  /** The JSON language service defaults. Only the slice this product configures. */
  export const jsonDefaults: {
    setDiagnosticsOptions: (options: {
      validate: boolean;
      /** Kept false: a `$ref` fetch would silently stop validating on an air-gapped host. */
      enableSchemaRequest: boolean;
      schemas: { uri: string; fileMatch: string[]; schema: object }[];
    }) => void;
  };
}

declare module 'monaco-editor/language/json/json.worker.js?worker' {
  const WorkerFactory: new () => Worker;
  export default WorkerFactory;
}
