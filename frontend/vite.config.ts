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
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 3000,
    proxy: {
      // The backend serves HTTPS only, usually with a self-signed certificate, hence `secure: false`
      // for this dev-only proxy (it verifies nothing a browser would not also warn about).
      "/api": {
        target: "https://127.0.0.1:5050",
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: "wss://127.0.0.1:5050",
        changeOrigin: true,
        secure: false,
        ws: true,
      },
    },
  },
  base: "/",
  build: {
    outDir: "../static/spa",
    emptyOutDir: true,
    /*
     * Raised from the 500 kB default because the warning was crying wolf and had stopped being
     * read. It fires per chunk with no idea whether a chunk is in the initial graph, so the three
     * loudest offenders were Monaco (2.7 MB) and its JSON language mode (1.2 MB) — which are
     * *already* on demand, loaded only when someone opens the policy editor. Three lines of alarm
     * on every build about code that is correctly split, which is how a real regression in the
     * initial chunk went unnoticed for weeks.
     *
     * 2800 clears the known on-demand giants and still fires if one of them doubles. What guards
     * the number that actually matters — the initial chunk — is `src/initialBundle.test.ts`,
     * which fails when a heavy dependency becomes statically reachable from `App.tsx`. That is a
     * cause, not a symptom, so it can say which import to break.
     */
    chunkSizeWarningLimit: 2800,
  },
});
