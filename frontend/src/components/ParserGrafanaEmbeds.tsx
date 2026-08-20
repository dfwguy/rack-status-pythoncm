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
 * Grafana /d-solo/ embeds for parser results — mirrors legacy appendParserGrafanaEmbeds (index.html).
 */

import Box from "@mui/material/Box";
import Link from "@mui/material/Link";
import { Text, Card, Banner } from "@nvidia/foundations-react-core";
import { ExternalLink } from "lucide-react";
import {
  buildGrafanaDeepLink,
  grafanaEmbedBlockedReason,
  resolveGrafanaBase,
} from "../lib/grafanaLinks";
import { useGrafanaDeeplinks } from "../lib/useGrafanaDeeplinks";

export interface GrafanaEmbedPayload {
  iframe_ready?: boolean;
  disabled?: boolean;
  ok?: boolean;
  note?: string;
  from_ms?: number;
  to_ms?: number;
  from_iso_utc?: string;
  to_iso_utc?: string;
  host_regex?: string;
  /** ``rack_prefix`` (default): short regex from NVIS canonical rack id; ``hosts``: per-node alternates (``RACK_WEB_GRAFANA_HOST_REGEX_PER_HOST=1``); ``open``: ``.*`` */
  host_regex_mode?: string;
  hosts?: string[];
  hosts_source?: string;
  time_source?: string;
  dashboard_uid?: string;
  dash_slug?: string;
  display_timezone?: string;
}

function trimSlash(s: string): string {
  return s.replace(/\/+$/, "");
}

function soloUrl(ge: GrafanaEmbedPayload, base: string, panelId: number): string {
  const uid = ge.dashboard_uid || "bright-gpu-per-host";
  const slug = ge.dash_slug || "bright-gpu-thermal-and-power";
  const regexQ = encodeURIComponent(ge.host_regex || ".*");
  // Match a known-good Grafana solo URL shape (query order matters for some releases):
  // ?orgId=1&panelId=1&var-job_host_regex=...%2A&from=now-6h&to=now&timezone=utc&refresh=10s
  const from = encodeURIComponent("now-6h");
  const to = encodeURIComponent("now");
  const tz = encodeURIComponent("utc");
  const refresh = encodeURIComponent("10s");
  return (
    `${trimSlash(base)}/d-solo/${encodeURIComponent(String(uid))}/${encodeURIComponent(String(slug))}` +
    `?orgId=1&panelId=${encodeURIComponent(String(panelId))}&var-job_host_regex=${regexQ}` +
    `&from=${from}&to=${to}&timezone=${tz}&refresh=${refresh}`
  );
}

// resolveGrafanaBase lives in ../lib/grafanaLinks. This file had a byte-identical private copy;
// two resolvers for one origin is how the embed and the deep-links come to disagree about where
// Grafana is, and the backend derives the same origin again for CSP's frame-src.

export interface ParserGrafanaEmbedsProps {
  grafanaEmbed: GrafanaEmbedPayload | null | undefined;
  grafanaUri?: string;
  grafanaPort?: string | number;
  /** Effective Grafana timezone= (IANA or utc); same as legacy parser_display_timezone resolution. */
  parserDisplayTimezone: string;
  /** Rack id used to scope the "Open in Grafana" deep-links (full BCM/NMC dashboards). */
  rack?: string;
}

/**
 * "Open in Grafana" deep-links to full BCM/NMC dashboards, scoped to this rack (and single
 * node, when the job ran on exactly one host) plus the job's time window. These work even when
 * iframe embedding is disabled, so they render independently of the /d-solo/ embeds below.
 */
function GrafanaJobDeepLinks({ rack, ge }: { rack?: string; ge: GrafanaEmbedPayload | null | undefined }) {
  const cfg = useGrafanaDeeplinks();
  if (!cfg || !cfg.enabled || cfg.dashboards.length === 0) return null;
  const host = ge && ge.hosts && ge.hosts.length === 1 ? ge.hosts[0] : undefined;
  const fromMs = ge && ge.from_ms != null ? ge.from_ms : undefined;
  const toMs = ge && ge.to_ms != null ? ge.to_ms : undefined;
  const windowNote =
    fromMs != null && toMs != null
      ? "job time window"
      : "live window (now-1h)";
  const scopeNote = host ? `node ${host}` : rack ? `rack ${rack}` : "cluster";
  return (
    <Card style={{ padding: 12, marginBottom: 16 }}>
      <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4 }}>
        Open in Grafana (full dashboards)
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 8 }}>
        Scoped to {scopeNote}, {windowNote}. Opens the BCM/NMC dashboards in a new tab (no embedding required).
      </Text>
      <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1.5 }}>
        {cfg.dashboards.map((d) => (
          <Link
            key={d.uid}
            href={buildGrafanaDeepLink(cfg, d, { rack, host, fromMs, toMs })}
            target="_blank"
            rel="noopener noreferrer"
            variant="body2"
            title={d.desc || d.label}
            sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}
          >
            {d.label}
            <ExternalLink size={13} />
          </Link>
        ))}
      </Box>
    </Card>
  );
}

export function ParserGrafanaEmbeds({
  grafanaEmbed: ge,
  grafanaUri,
  grafanaPort,
  parserDisplayTimezone,
  rack,
}: ParserGrafanaEmbedsProps) {
  if (!ge || ge.disabled) {
    return (
      <Box sx={{ mt: 2 }}>
        <GrafanaJobDeepLinks rack={rack} ge={ge} />
        <Card style={{ padding: 16 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {(ge && ge.note) || "Grafana embed not available."}
          </Text>
        </Card>
      </Box>
    );
  }
  if (!ge.iframe_ready || ge.from_ms == null || ge.to_ms == null) {
    return (
      <Box sx={{ mt: 2 }}>
        <GrafanaJobDeepLinks rack={rack} ge={ge} />
        <Card style={{ padding: 16 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {ge.note || "Grafana context not available (check Slurm host expansion / embed settings)."}
          </Text>
        </Card>
      </Box>
    );
  }

  const base = resolveGrafanaBase(grafanaUri, grafanaPort);
  const blockedReason = grafanaEmbedBlockedReason(base);
  if (blockedReason) {
    // Say why rather than render an empty rectangle. The deep-links open in a new tab and are
    // unaffected by mixed content, so they stay.
    return (
      <Box sx={{ mt: 2 }}>
        <GrafanaJobDeepLinks rack={rack} ge={ge} />
        <Banner status="warning">{blockedReason}</Banner>
      </Box>
    );
  }
  const tzParam = (parserDisplayTimezone || ge.display_timezone || "utc").trim() || "utc";
  const thSrc = ge.time_source ? String(ge.time_source) : "?";
  const hnSrc = ge.hosts_source ? String(ge.hosts_source) : "?";
  const nhosts = ge.hosts && ge.hosts.length ? String(ge.hosts.length) : "0";

  const filterNote =
    ge.host_regex_mode === "rack_prefix"
      ? " Prometheus filter: canonical rack id + .* (all trays on this rack)."
      : ge.host_regex_mode === "hosts"
        ? " Prometheus filter: per-node hostname alternates."
        : "";

  const meta =
    (thSrc === "live_last_5m"
      ? `Rolling window: last 5 minutes (server clock). Grafana timezone: ${tzParam}. Nodes: ${hnSrc}${
          ge.hosts && ge.hosts.length ? ` (${nhosts} hostnames).` : " (no host list — Prometheus regex .*)."
        }`
      : `Time range from NEMO log (${thSrc}). Grafana timezone: ${tzParam}. Nodes: ${hnSrc}${
          ge.hosts && ge.hosts.length ? ` (${nhosts} hostnames).` : " (no host list — Prometheus regex .*)."
        }`) + filterNote;

  const panels: { id: number; label: string }[] = [
    { id: 1, label: "GPU thermal" },
    { id: 2, label: "GPU power usage" },
  ];

  return (
    <Box sx={{ mt: 2 }}>
      <GrafanaJobDeepLinks rack={rack} ge={ge} />
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
        Grafana — GPU thermal and power
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
        {meta} Embedded panels use the Grafana solo URL shape{" "}
        <code>from=now-6h&amp;to=now&amp;timezone=utc&amp;refresh=10s</code> (log-derived times above are for parser
        charts only).
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 16, display: "block" }}>
        Requires Grafana with GF_SECURITY_ALLOW_EMBEDDING=true. If panels stay blank, check mixed content (HTTPS vs
        HTTP Grafana): set RACK_WEB_GRAFANA_URI to a browser-reachable base URL.
      </Text>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
          gap: 2,
        }}
      >
        {panels.map(({ id, label }) => {
          const embedUrl = soloUrl(ge, base, id);
          const newTabUrl = soloUrl(ge, base, id);
          return (
            <Card key={id} style={{ padding: 12, overflow: "hidden" }}>
              <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 1, flexWrap: "wrap", mb: 1 }}>
                <Text kind="body/bold/sm">{label}</Text>
                <Link href={newTabUrl} target="_blank" rel="noopener noreferrer" variant="body2">
                  Open in new tab
                </Link>
              </Box>
              <Box sx={{ position: "relative", width: "100%", pt: "56%", bgcolor: "action.hover", borderRadius: 1 }}>
                <iframe
                  title={label}
                  src={embedUrl}
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  style={{
                    position: "absolute",
                    inset: 0,
                    width: "100%",
                    height: "100%",
                    border: 0,
                    borderRadius: 4,
                  }}
                />
              </Box>
            </Card>
          );
        })}
      </Box>
    </Box>
  );
}
