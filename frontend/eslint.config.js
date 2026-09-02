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
import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // A WARNING, not an error, and deliberately so (2026-08-04).
      //
      // Every remaining hit is the same fetch-on-mount idiom: a `useCallback` loader that flips a
      // loading flag and then awaits, called from `useEffect(() => { void load() }, [load])`. The
      // rule rejects any setState synchronously REACHABLE from an effect, so the surgical fixes do
      // not satisfy it — tried and measured: making the flag conditional and passing
      // `showSpinner: false` from the mount path (where `loading` already initialises to true) is
      // still reported, because the checker cannot prove the branch is not taken.
      //
      // Reaching zero means separating fetching from state application in ~30 views so every
      // setState lands after an await. That is worth doing for its own reason — most of those
      // effects have no unmount guard and can set state on an unmounted component — but it is a
      // rewrite of the data loading on nearly every screen, not a lint fix, and it should land as
      // its own reviewable change rather than block the gate below.
      //
      // Every OTHER rule in this config is at zero, so `npm run lint` failing on any error is a
      // meaningful CI gate today. Keep it that way: if you are tempted to downgrade a second rule,
      // fix the code instead.
      'react-hooks/set-state-in-effect': 'warn',

      // ---- Comment and whitespace hygiene (added 2026-09-02) --------------------------------
      //
      // The Python side gets this from ruff (`extend-select = ["E", "W"]` with `preview = true`,
      // so E1/E2/E3 and the W warnings gate `backend/`). There was no equivalent here, which
      // meant trailing whitespace, stray blank lines at EOF and commented-out code were checked
      // on one half of the product and not the other. These are the closest core-ESLint
      // equivalents. All five were brought to zero in the same commit that enabled them, so they
      // gate honestly on day one rather than being aspirational.
      //
      // Still core rules in ESLint 10.3 (verified against node_modules/eslint/lib/rules) — the
      // v9 formatting-rule deprecation did not remove them, so this needs no @stylistic plugin.
      'no-trailing-spaces': 'error',                                    // ruff W291 / W293
      'eol-last': ['error', 'always'],                                  // ruff W292 / W391
      'no-multiple-empty-lines': ['error', { max: 2, maxEOF: 0, maxBOF: 0 }], // ruff E303
      'spaced-comment': ['error', 'always', { markers: ['/', '!'], block: { balanced: true } }],

      // `ignoreEOLComments` is load-bearing, not laziness. Several files align trailing comments
      // into a column on purpose (see the StepFanout fields in services/workflowService.ts);
      // without this option the rule would demand that alignment be flattened, which is a
      // readability regression dressed as a lint fix. Runs of spaces inside CODE are still caught.
      'no-multi-spaces': ['error', { ignoreEOLComments: true }],

      // This repo has ZERO TODO/FIXME/XXX/HACK markers today — measured, not assumed. That is
      // unusual and worth keeping: a marker is a comment that documents work nobody scheduled,
      // and it rots exactly like the stale comments this rule set exists to prevent. Track the
      // work in GitLab and link the issue instead. A warning, not an error, so an in-progress
      // branch is not blocked from building.
      'no-warning-comments': ['warn', { terms: ['todo', 'fixme', 'xxx', 'hack'], location: 'anywhere' }],
    },
  },
])
