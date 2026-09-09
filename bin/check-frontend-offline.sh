#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
#
# check-frontend-offline.sh — the built SPA must fetch nothing from the internet.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
#
# WHY THIS IS A SCRIPT AND NOT A NOTE (AC-15i / FR-19b)
#
# Operators reach this dashboard over an SSH tunnel, and several deployments are air-gapped. A
# single external reference in the bundle does not fail loudly there — it hangs until the browser
# gives up, so the symptom is "the page is slow" or a control that silently never appears. The
# Kaizen CSS is vendored for exactly this reason, and the Monaco editor was moved off its CDN for
# it too.
#
# The check existed as a `grep` recipe in `frontend/src/styles/kaizen/README.md` under the words
# "verify the output makes no external requests" — which is a check nobody runs, on the one axis
# where a regression is invisible in every screenshot and on every developer's machine, because a
# workstation simply fetches the asset. Two separate pieces of work (the CSS vendoring and the
# Monaco de-CDN-ing) each relied on somebody remembering it.
#
# It must run against a BUILT bundle. Vite rewrites and inlines, so a dependency can introduce a
# CDN URL that appears nowhere in our source, and conversely our source contains documentation URLs
# that never reach the output. Checking the source would be both unsound and noisy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SPA="${1:-$ROOT/static/spa}"

say() { printf '[frontend-offline] %s\n' "$*"; }

# `local-vs-ci-tree.mdc` §4: the job image is not your shell. This runs in `node:24-slim`, so
# check for the tools rather than discovering their absence as a confusing failure.
for tool in grep sort; do
  command -v "$tool" >/dev/null 2>&1 || { say "FAIL: '$tool' is not on PATH"; exit 2; }
done

if [ ! -f "$SPA/index.html" ]; then
  say "FAIL: no build at $SPA — run 'npm run build' in frontend/ first."
  say "      (A missing bundle must not pass: that is how this check would quietly stop running.)"
  exit 2
fi

# Allowed hosts, each for a stated reason. Keep this list short and justify every addition.
#
#   www.w3.org      — the SVG/XML namespace inside inline `data:` URIs. A namespace is an
#                     identifier, never fetched. Excluded in the original README recipe too.
#   schemas.*       — same shape: XML namespace declarations.
#   localhost/127.* — a same-origin dev reference is not an external origin.
ALLOWED='www\.w3\.org|schemas\.openxmlformats\.org|schemas\.microsoft\.com|localhost|127\.0\.0\.1'

# ── Two scopes, deliberately different, because a blanket scan is unusable ───────────────────
#
# HTML and CSS are checked STRICTLY: any external origin fails. Those files are where a `<link>`,
# a `<script src>`, an `@import`, a `url()` or an `@font-face` lives, so a URL there is a fetch
# with very few exceptions.
#
# JS is checked against a DENYLIST of asset CDNs instead. Measured on the current bundle, a
# blanket scan of `assets/*.js` returns 148 hits and **not one is fetched** — they are error
# messages and documentation links (`reactflow.dev/error#001`, `json-schema.org`, `chevrotain.io`,
# `code.visualstudio.com`) plus XML namespaces and template literals like `https://${t}`. A check
# that fires 148 times on a clean tree is a check that gets commented out in a week, and would be
# strictly worse than the README recipe it replaces.
#
# The denylist keeps the two regressions this repo has actually had — the Kaizen CSS being served
# from `webassets.nvidia.com` and the Monaco editor defaulting to `jsdelivr` — while staying quiet
# on prose. It is not exhaustive, and it is not meant to be: HTML/CSS is the sound half, and this
# is a targeted net for the failure mode that has bitten twice.
ASSET_CDNS='webassets\.nvidia\.com|brand-assets\.cne\.ngc\.nvidia\.com|cdn\.jsdelivr\.net|unpkg\.com|cdnjs\.cloudflare\.com|fonts\.googleapis\.com|fonts\.gstatic\.com|ajax\.googleapis\.com|esm\.sh|skypack\.dev'

# `-oh` so only the URL is printed and filenames do not pollute the comparison.
strict="$(
  grep -ohE 'https?://[^"'"'"' )]+' "$SPA/index.html" "$SPA"/assets/*.css 2>/dev/null \
  | grep -vE "$ALLOWED" | sort -u || true
)"

cdn="$(
  grep -ohE "https?://($ASSET_CDNS)[^\"'\` )]*" "$SPA"/assets/*.js 2>/dev/null | sort -u || true
)"

found="$(printf '%s\n%s\n' "$strict" "$cdn" | grep -v '^$' | sort -u || true)"

if [ -n "$found" ]; then
  say "FAIL: the built bundle references external origin(s):"
  printf '  %s\n' $found
  cat <<'EOF'

An air-gapped or tunnelled deployment cannot reach these, and the failure is silent: the
browser hangs on the request rather than reporting it, so the page looks slow and a control
may simply never render.

Vendor the asset instead (see frontend/src/styles/kaizen/README.md for the CSS precedent),
or, if the reference is genuinely never fetched -- an XML namespace, for instance -- add it to
ALLOWED in this script WITH the reason.
EOF
  exit 1
fi

say "PASS: no external origins in $(basename "$SPA")"
