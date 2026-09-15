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
 * It used to be `<a href="/docs/" target="_blank">`. That is wrong twice over now.
 *
 * **The desktop shell has no browser to open a tab in.** A `target="_blank"` from inside the
 * shell leaves the app entirely and hands the operator to whatever browser the machine happens
 * to have, which is a jarring exit from a tool they are in the middle of using.
 *
 * **And it had stopped working.** `/docs/` is a *server* route behind the authorization gate,
 * and a plain browser navigation does not go through `apiFetch` — so it never benefits from the
 * transparent refresh-on-401 that keeps the rest of the app signed in. The access cookie lives
 * an hour; after that, clicking Documentation showed the login screen. It worked immediately
 * after signing in and failed later, which is the worst shape a bug can have: it looks
 * intermittent and reads as "the app logged me out".
 *
 * So the fix is not only "same tab" — the credential has to be fresh *before* the iframe asks,
 * and stay fresh while somebody reads. `apiFetch` is what knows how to do that, and calling it
 * renews the `nvis_access` cookie as a side effect, which is what the iframe's own requests
 * (and every asset inside it) then carry.
 *
 * An iframe rather than fetching and rendering the HTML: Sphinx pages reference their own CSS,
 * search index and relative links, so the alternative is proxying an entire static site through
 * the SPA. Same-origin, so in-page navigation works exactly as it does standalone.
 */
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Flex, Text } from "@nvidia/foundations-react-core";
import { ExternalLink, RotateCw } from "lucide-react";
import { apiFetch } from "../lib/apiFetch";
import { PageHeader } from "../components/layout/PageHeader";

/** Where the Sphinx build is served from. A server route, not an SPA one. */
const DOCS_URL = "/docs/";

/**
 * How often to renew while the view is open.
 *
 * Well inside the one-hour access lifetime, because the failure it prevents is silent: the
 * cookie expires mid-read and the *next* in-page link click inside the iframe returns the login
 * page, inside the frame, with no way for this component to notice. Ten minutes is cheap — one
 * request — and leaves a wide margin if the deployment shortens the TTL.
 */
const RENEW_INTERVAL_MS = 10 * 60 * 1000;

export function DocumentationView() {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);

  /**
   * Renew the session cookie the iframe will use.
   *
   * Any authenticated endpoint does: `apiFetch` refreshes on a 401 and retries, and the refresh
   * response sets a new `nvis_access` for the whole origin. `/api/auth-check` is the cheapest
   * one and the same call the app already makes on load.
   */
  const renew = useCallback(async (): Promise<boolean> => {
    try {
      const res = await apiFetch("/api/auth-check");
      if (!res.ok) {
        setError(
          "Your session could not be renewed, so the documentation cannot be shown. Sign in again.",
        );
        return false;
      }
      setError("");
      return true;
    } catch (e) {
      setError(
        `Could not reach the server to renew the session (${
          e instanceof Error ? e.message : String(e)
        }).`,
      );
      return false;
    }
  }, []);

  // Before the first render of the iframe, never after: an iframe that loads with an expired
  // cookie shows the login page *inside the frame*, and nothing out here can tell.
  useEffect(() => {
    let live = true;
    void renew().then((ok) => {
      if (live && ok) setReady(true);
    });
    return () => {
      live = false;
    };
  }, [renew]);

  useEffect(() => {
    if (!ready) return;
    const timer = window.setInterval(() => void renew(), RENEW_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [ready, renew]);

  const reload = () => {
    void renew().then((ok) => {
      if (ok) setNonce((n) => n + 1);
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
