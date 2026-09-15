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
/**
 * DocumentationView — the Sphinx site, inside the app.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * It used to be `<a href="/docs/" target="_blank">`, which was wrong twice over: the desktop
 * shell has no browser tab to open, and `/docs/` was a gated route that a plain navigation could
 * not keep a session alive for.
 *
 * **`/docs/` is public now, and that is what makes this simple.** The documentation is the same
 * text anybody can read in the repository, so gating it bought nothing and cost two confusing
 * failures — a login screen an hour after signing in, and `not authorized` for administrators
 * whose policy predated the `docs` resource type. The route serves static files from a Sphinx
 * build and reads no state, so an unauthenticated reader reaches nothing the documentation does
 * not already describe (`web_auth.AUTH_BYPASS_ENDPOINTS` records the reasoning;
 * `test_docs_are_public.py` holds it).
 *
 * So this view frames the site and gets out of the way. The one thing still worth checking is
 * whether the documentation was *built* into this deployment — `static/docs` is a build artifact,
 * so a developer tree or a backend-only deployment can genuinely lack it, and framing that 503
 * would show a paragraph of plain text in a box instead of saying so.
 *
 * An iframe rather than fetching and rendering the HTML: Sphinx pages reference their own CSS,
 * search index and relative links, so the alternative is proxying a static site through the SPA.
 * Framing needs `frame-ancestors 'self'` on the docs responses, which `app._security_headers`
 * sets for `/docs` only — the default `'none'` blocks same-origin framing too, and that mistake
 * renders an empty box while every jsdom test passes.
 */
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Flex, Text } from "@nvidia/foundations-react-core";
import { ExternalLink, RotateCw } from "lucide-react";
import { apiFetch } from "../lib/apiFetch";
import { PageHeader } from "../components/layout/PageHeader";

/** Where the Sphinx build is served from. A server route, not an SPA one. */
const DOCS_URL = "/docs/";

export function DocumentationView() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);

  /**
   * Has the documentation been built into this deployment?
   *
   * `apiFetch` rather than a bare `fetch` only for its error handling; no credential is needed.
   * A 503 is the documented answer when `static/docs` is absent, and saying so beats framing a
   * page of plain text.
   */
  const probe = useCallback(async (): Promise<boolean> => {
    try {
      const res = await apiFetch(DOCS_URL, { headers: { Accept: "text/html" } });
      if (res.status === 503) {
        setError(
          "The documentation has not been built into this deployment. Build it with " +
            "`sphinx-build -W --keep-going -b html docs static/docs`, or rebuild the image.",
        );
        return false;
      }
      setError("");
      return true;
    } catch {
      // A probe that cannot run is not a reason to withhold the docs: frame them and let the
      // browser show whatever the server says. Refusing here would turn a transient blip into
      // "no documentation".
      return true;
    }
  }, []);

  useEffect(() => {
    let live = true;
    void probe().then((ok) => {
      if (live && ok) setReady(true);
    });
    return () => {
      live = false;
    };
  }, [probe]);

  const reload = () => {
    void probe().then((ok) => {
      if (ok) {
        setReady(true);
        setNonce((n) => n + 1);
      }
    });
  };

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", p: 0 }}>
      <Box sx={{ px: 4, pt: 4 }}>
        <PageHeader
          title="Documentation"
          description="The documentation for the version running here, served by this deployment."
          contentId="documentation"
          actions={
            <Flex gap="2">
              <Button kind="tertiary" size="small" onClick={reload}>
                <RotateCw size={14} style={{ marginRight: 6 }} />
                Reload
              </Button>
              {/* Kept, but no longer the only way in. An operator who wants the docs beside the
                  app rather than inside it still can; in the desktop shell this opens the
                  system browser, which is a deliberate choice rather than the default. */}
              <Button
                kind="tertiary"
                size="small"
                onClick={() => window.open(DOCS_URL, "_blank", "noopener,noreferrer")}
              >
                <ExternalLink size={14} style={{ marginRight: 6 }} />
                Open in a new window
              </Button>
            </Flex>
          }
        />
      </Box>

      {error ? (
        <Box sx={{ px: 4, pb: 2 }}>
          <Banner status="error" kind="inline">
            {error}
          </Banner>
        </Box>
      ) : null}

      <Box sx={{ flex: 1, minHeight: 0, px: 4, pb: 4 }}>
        {ready ? (
          <iframe
            key={nonce}
            src={DOCS_URL}
            title="Greenlight documentation"
            style={{
              width: "100%",
              height: "100%",
              minHeight: "70vh",
              border: "1px solid var(--kui-color-border-subtle, #333)",
              borderRadius: 4,
              background: "#fff",
            }}
          />
        ) : (
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            {error ? "" : "Loading documentation…"}
          </Text>
        )}
      </Box>
    </Box>
  );
}
