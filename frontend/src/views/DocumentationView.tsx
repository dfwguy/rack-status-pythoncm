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
import { useLocation, useNavigate } from "react-router-dom";
import Box from "@mui/material/Box";
import { Banner, Button, Flex, Text } from "@nvidia/foundations-react-core";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { apiFetch } from "../lib/apiFetch";
import { DEFAULT_PATH } from "../routes";

/** Where the Sphinx build is served from. A server route, not an SPA one. */
const DOCS_URL = "/docs/";

/**
 * A page inside the docs site, for a caller that wants to land somewhere specific.
 *
 * Validated even though the value arrives in router *state* rather than in the URL, so it is not
 * attacker-supplied today. The check is what keeps that true: the moment somebody wires this to a
 * query parameter -- the obvious next step, and the reason a deep link is wanted at all -- an
 * unvalidated value would frame any origin inside our own page. Relative, one of our own HTML
 * pages, and no traversal.
 */
const SAFE_DOC_PAGE = /^[A-Za-z0-9][A-Za-z0-9._/-]*\.html$/;

function docSrc(page: unknown): string {
  if (typeof page !== "string" || !page || page.includes("..")) return DOCS_URL;
  return SAFE_DOC_PAGE.test(page) ? `${DOCS_URL}${page}` : DOCS_URL;
}

export function DocumentationView() {
  const navigate = useNavigate();
  const location = useLocation();
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [version, setVersion] = useState("");

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

  /**
   * The build this documentation describes, named rather than alluded to.
   *
   * The bar used to read "for the version running here", which states the guarantee without
   * answering the question it raises — and that question is the entire reason the docs are served
   * locally instead of linked: *are these the docs for what I am running?*
   *
   * **`/api/about`, not `/api/app-stats`.** The stats endpoint carries the same version and is
   * gated, so reading it from here made a public page depend on a session. It worked, because
   * this view only renders for a signed-in operator — which is exactly what made it easy to miss.
   * `/api/about` is public and returns product and version only; both endpoints get the string
   * from `app_version`, so there is still one answer to "what build is this".
   *
   * Failure is silent on purpose: an unreachable endpoint is not a reason to withhold the
   * documentation, and a bar with no version reads as "no version" rather than as an error.
   */
  useEffect(() => {
    let live = true;
    void apiFetch("/api/about")
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (live && data?.version) setVersion(String(data.version));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  /**
   * Where Back goes.
   *
   * The route we came from, carried in router state by `handleNavigate`. **Not** browser
   * history: same-origin iframe navigations push entries onto the *parent's* session history,
   * so after reading five pages `navigate(-1)` walks back through documentation pages rather
   * than leaving the documentation — the operator presses Back and appears not to move.
   *
   * A deep link or a reload has no state, so it falls back to the dashboard rather than trapping
   * the reader.
   */
  const cameFrom = (location.state as { from?: string } | null)?.from;

  return (
    <Box
      sx={{
        position: "fixed",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        bgcolor: "#0d0d0d",
        zIndex: 1250,
      }}
    >
      {/* One slim bar, holding only what the documentation does not provide itself: a way out,
          and a way to detach it. No Reload (the browser reloads), no search or theme toggle
          (the Sphinx site has both), no Ask AI or Help — the page you are on *is* the help. */}
      <Flex
        align="center"
        justify="between"
        style={{
          padding: "8px 16px",
          borderBottom: "1px solid var(--kui-color-border-subtle, #2a2a2a)",
          flexShrink: 0,
        }}
      >
        <Flex align="center" gap="2">
          <Button kind="tertiary" size="small" onClick={() => navigate(cameFrom || DEFAULT_PATH)}>
            <ArrowLeft size={14} style={{ marginRight: 6 }} />
            Back
          </Button>
          <Text kind="body/bold/sm">Documentation</Text>
          {version ? (
            <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
              {version}
            </Text>
          ) : null}
        </Flex>
        <Button
          kind="tertiary"
          size="small"
          onClick={() => window.open(DOCS_URL, "_blank", "noopener,noreferrer")}
        >
          <ExternalLink size={14} style={{ marginRight: 6 }} />
          Open in a new window
        </Button>
      </Flex>

      {error ? (
        <Box sx={{ p: 2 }}>
          <Banner status="error" kind="inline">
            {error}
          </Banner>
        </Box>
      ) : null}

      <Box sx={{ flex: 1, minHeight: 0 }}>
        {ready ? (
          <iframe
            src={docSrc((location.state as { docPage?: string } | null)?.docPage)}
            title="Greenlight documentation"
            style={{ width: "100%", height: "100%", border: 0, display: "block" }}
          />
        ) : (
          <Box sx={{ p: 3 }}>
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
              {error ? "" : "Loading documentation…"}
            </Text>
          </Box>
        )}
      </Box>
    </Box>
  );
}
