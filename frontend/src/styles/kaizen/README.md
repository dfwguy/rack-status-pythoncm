# Vendored Kaizen UI Foundations CSS + fonts

`base.css` and `components.css` are vendored copies of the Kaizen UI Foundations
stylesheets, and `fonts/` holds the 16 NVIDIA Sans / JetBrains Mono `woff2` files they
reference. All of it is imported by `src/main.tsx`, so Vite bundles and hashes it with
the rest of the app assets (~1 MB in the repo).

The upstream `base.css` points its `@font-face` rules at
`brand-assets.cne.ngc.nvidia.com`; those URLs are rewritten here to relative
`./fonts/...` paths so Vite resolves them locally. **Re-apply that rewrite whenever you
refresh `base.css`** — otherwise the CSS is local but the fonts silently fall back to
system faces on any host that cannot reach the internet.

Everything is vendored rather than loaded from
`https://webassets.nvidia.com/kaizen-ui-foundations/<version>/` on purpose:

- **Offline / air-gapped deployments work.** Cluster operators reach the dashboard
  through an SSH tunnel; a browser that cannot reach the public CDN would render
  the app completely unstyled.
- **The build is reproducible and self-contained.** No network fetch at page load,
  and no deploy-time rewriting of `index.html` to point at locally-mounted copies
  (which is what the Nscale TX deployment used to do — see
  `docs/clusters/NSCALE_TX_DEPLOYMENT.md`).
- **Version is pinned with the code**, so a CDN publish cannot restyle a released
  build underneath us.

## Version

Pinned to **1.0.0**, matching `@nvidia/foundations-react-core` in
`frontend/package.json`. The npm package does not ship these stylesheets, which is
why they are copied here.

## Updating

Keep the CSS version and the package version in lockstep — bump both together:

```bash
V=1.0.0   # must match @nvidia/foundations-react-core
cd frontend/src/styles/kaizen

# 1. refresh the stylesheets
for f in base components; do
  curl -sSf -o "$f.css" "https://webassets.nvidia.com/kaizen-ui-foundations/$V/$f.css"
done

# 2. re-download any fonts base.css now asks for
mkdir -p fonts
for u in $(grep -ohE 'https://brand-assets[^")]*\.woff2' base.css | sort -u); do
  curl -sSf -o "fonts/$(basename "$u")" "$u"
done

# 3. repoint @font-face at the local copies
perl -pi -e 's{https://brand-assets\.cne\.ngc\.nvidia\.com/assets/fonts/[^/]+/[^/]+/}{./fonts/}g' base.css
```

Then rebuild and verify the output makes no external requests:

```bash
cd frontend && npm run build
grep -ohE 'https?://[^"'"'"' )]+' ../static/spa/index.html ../static/spa/assets/*.css \
  | grep -v www.w3.org | sort -u    # expect no output
```

(`www.w3.org` is excluded because it only appears as the SVG XML namespace inside inline
`data:` URIs — it is never fetched.) Apart from the fonts, the stylesheets are
self-contained: every other `url()` is an inline `data:` SVG and there are no `@import`s.
