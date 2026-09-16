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
 * Infrastructure → Ethernet fabric / InfiniBand + NVLink / Storage.
 *
 * Every counter and device table on these pages is now a REAL Grafana panel. The 1:1
 * section→dashboard map comes from the backend (`GET /api/observability/dashboards/infrastructure`);
 * this view resolves the browser-facing URL, applies the app's light/dark theme, and filters the
 * returned sections down to the segment being viewed.
 *
 * Honesty notes: the Ethernet and InfiniBand boards are genuinely DARK right now — the
 * cumulus_* / lldpq_* / ib_* / ufm_* scrapers are stopped for an incident, and UFM REST auth is
 * an open blocker. Storage has NO collector at all on this cluster (no Lustre / GPFS / WEKA /
 * VAST / NFS-server telemetry), so that segment renders an explicit "not wired" state rather
 * than invented mounts and throughput.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Link from "@mui/material/Link";
import { Banner, Button, Card, Flex, Text } from "@nvidia/foundations-react-core";
import { ExternalLink } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { MetricChartGrid } from "../components/observability/MetricChartGrid";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { apiFetch } from "../lib/apiFetch";
import { probeFabric, type FabricProbe } from "../services/fabricService";
import { resolveGrafanaBase } from "../lib/grafanaLinks";

export type InfraSegment = "ethernet" | "ib-nvlink" | "storage";

/* ------------------------------------------------------------------------------------------
 * Backend contract: GET /api/observability/dashboards/<view> and /api/observability/health.
 * NOTE: this block is intentionally duplicated in MetricsView.tsx — the two views are owned by
 * the same track but shared modules are owned elsewhere. Lift it into a shared service
 * (e.g. services/grafanaEmbeds.ts) when the observability tracks merge.
 * ---------------------------------------------------------------------------------------- */

/** One panel of a mapped section. ``url`` is the backend's pre-resolved /d-solo/ embed. */
interface EmbedPanel {
  id: number;
  title?: string;
  url?: string;
}

/** One RECON-B section mapped 1:1 onto a real Grafana dashboard. */
interface EmbedSection {
  section: string;
  uid: string;
  title?: string;
  panel_ids?: number[];
  panels?: EmbedPanel[];
  /** "exact" | "close" | "weak" — how literal the mock→dashboard match is. */
  confidence?: string;
  rationale?: string;
  /** Whole-dashboard embed, when the section maps to a board rather than panels. */
  url?: string;
  /** Segment/view hints so a multi-segment view can filter its own sections. */
  segment?: string;
  view?: string;
  dashboard_open_url?: string;
  dashboard_open_path?: string;
}

/** A RECON-B section with no honest Grafana counterpart — rendered as an explicit placeholder. */
interface UnmappedSection {
  section: string;
  reason?: string;
  rationale?: string;
  segment?: string;
  view?: string;
}

interface ViewEmbeds {
  ok?: boolean;
  view?: string;
  /** Browser-facing Grafana origin (RACK_GRAFANA_PUBLIC_URL). */
  public_url?: string;
  /** Server-side Grafana origin (RACK_GRAFANA_BASE_URL) — fallback only. */
  base_url?: string;
  grafana_reachable?: boolean;
  error?: string;
  sections?: EmbedSection[];
  unmapped?: UnmappedSection[];
  config?: { public_base?: string; public_port?: string };
}

interface GrafanaHealth {
  ok?: boolean;
  reachable?: boolean;
  dashboards?: number;
  error?: string;
  public_url?: string;
  base_url?: string;
}

/** Embed window; matches the ParserGrafanaEmbeds solo-URL shape already used by the tool. */


/** Fetch one view's sections. Never throws; a failure becomes an ``error`` on the result. */
async function fetchViewEmbeds(view: string, query = ""): Promise<ViewEmbeds> {
  const path = `/api/observability/dashboards/${encodeURIComponent(view)}${query}`;
  try {
    const res = await apiFetch(path, { cache: "no-store" });
    if (!res.ok) {
      return { ok: false, error: `Dashboard map unavailable — ${path} returned HTTP ${res.status}.` };
    }
    return (await res.json()) as ViewEmbeds;
  } catch (e) {
    return { ok: false, error: `Dashboard map unavailable — ${e instanceof Error ? e.message : String(e)}` };
  }
}

/**
 * Ask the backend whether it can reach Grafana at all. Never throws.
 *
 * A 404 means the health route itself is not deployed — that is "unknown", NOT "Grafana is
 * down", so ``reachable`` is deliberately left undefined and the panels still render.
 */
async function fetchGrafanaHealth(): Promise<GrafanaHealth> {
  try {
    const res = await apiFetch("/api/observability/health", { cache: "no-store" });
    if (res.status === 404) {
      return { ok: false, error: "/api/observability/health is not deployed — Grafana reachability unknown." };
    }
    if (!res.ok) return { ok: false, reachable: false, error: `HTTP ${res.status}` };
    return (await res.json()) as GrafanaHealth;
  } catch (e) {
    return { ok: false, reachable: false, error: e instanceof Error ? e.message : String(e) };
  }
}

/* ---------------------------------------------------------------------------------------- */

interface SegmentMeta {
  title: string;
  description: string;
  contentId: string;
  /** Substrings that identify this segment in a section's segment/view/section fields. */
  match: string[];
  /** True coverage caveat for this segment — no synthetic filler, just why panels may be dark. */
  coverage: string;
}

const SEGMENTS: Record<InfraSegment, SegmentMeta> = {
  ethernet: {
    title: "Ethernet fabric",
    description: "Management and in-band Ethernet — leaf/spine switches, uplinks, and host ports.",
    contentId: "infraEthernet",
    match: ["ethernet"],
    coverage:
      "Live Grafana embeds — no synthetic data. These panels are expected to be EMPTY today: the " +
      "cumulus_* and lldpq_* switch scrapers are stopped for an incident and must not be restarted.",
  },
  "ib-nvlink": {
    title: "InfiniBand / NVLink",
    description: "Compute fabric — InfiniBand links and switches plus intra-rack NVLink domains.",
    contentId: "infraIbNvlink",
    match: ["ib-nvlink", "infiniband", "nvlink", "ib "],
    coverage:
      "Live Grafana embeds — no synthetic data. The NVSwitch tray panels are LIVE (BCM-sourced); the " +
      "InfiniBand panels are expected to be EMPTY because the ib_*/ufm_* scrapers are stopped for an " +
      "incident and UFM REST auth is an open blocker.",
  },
  storage: {
    title: "Storage",
    description: "Shared filesystems and scratch — mounts, targets, throughput, and capacity.",
    contentId: "infraStorage",
    match: ["storage"],
    coverage:
      "No filesystem-service telemetry is collected on this cluster — there is no Lustre, GPFS, WEKA, " +
      "VAST or NFS-server collector. Nothing is shown rather than inventing mounts and throughput.",
  },
};

const EMBEDDING_NOTE =
  "Embedding requires Grafana to run with GF_SECURITY_ALLOW_EMBEDDING=true; if a frame stays blank, use “Open in Grafana”.";

/**
 * Does this section belong to the segment currently being viewed?
 *
 * Prefer the explicit ``segment`` / ``view`` fields when the backend supplies them — they are
 * unambiguous. Only fall back to fuzzy-matching the section label when neither is present,
 * which is where a cross-fabric false positive would otherwise be possible.
 */
function matchesSegment(item: { segment?: string; view?: string; section?: string }, meta: SegmentMeta): boolean {
  const explicit = `${item.segment || ""} ${item.view || ""}`.trim().toLowerCase();
  const hay = explicit || (item.section || "").toLowerCase();
  if (!hay) return false;
  return meta.match.some((needle) => hay.includes(needle));
}
function FabricProbeCard() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [node, setNode] = useState<string | null>(null);
  const [fabric, setFabric] = useState<FabricProbe | null>(null);

  const runProbe = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await probeFabric();
      if (!resp.ok || !resp.fabric) {
        setError(resp.error || "Fabric probe failed");
        setFabric(null);
      } else {
        setFabric(resp.fabric);
        setNode(resp.node || resp.fabric.host || null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setFabric(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const rows: [string, string][] = fabric
    ? [
        ["Scale-out fabric", fabric.link ? fabric.link.toUpperCase() : "—"],
        ["Per-rail rate", fabric.rate_gbps ? `${fabric.rate_gbps} Gb/s` : "—"],
        ["Compute rails", `${fabric.compute_rails}${fabric.gpus ? ` (for ${fabric.gpus} GPUs)` : ""}`],
        ["NCCL_IB_HCA", fabric.nccl_ib_hca || "—"],
        ["NCCL_SOCKET_IFNAME", fabric.ns_ifname || "—"],
        ["UCX_NET_DEVICES", fabric.ucx_net_devices || "—"],
      ]
    : [];

  return (
    <Card style={{ padding: 16, marginBottom: 24 }}>
      <Flex align="center" justify="between" style={{ marginBottom: 8 }}>
        <Text kind="body/bold/md">Live fabric probe</Text>
        <Button kind="secondary" size="small" onClick={runProbe} disabled={loading}>
          {loading ? "Probing…" : node ? "Re-probe" : "Probe fabric"}
        </Button>
      </Flex>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 12 }}>
        Classifies a representative compute node's NICs into compute rails vs the storage/NS plane
        (the map NCCL/UCX use), and compares against the platform's expected fabric profile.
      </Text>

      {error && (
        <Banner status="error" kind="inline">
          {error}
        </Banner>
      )}

      {fabric && (
        <>
          {node && (
            <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 8 }}>
              node: {node}
            </Text>
          )}
          {fabric.discrepancies.length > 0 ? (
            <Banner status="warning" kind="inline">
              {`Fabric discrepancy vs expected profile: ${fabric.discrepancies.join("; ")}`}
            </Banner>
          ) : (
            <Banner status="success" kind="inline">
              Detected fabric matches the platform's expected profile.
            </Banner>
          )}
          <Box sx={{ mt: 1.5 }}>
            {rows.map(([label, value]) => (
              <Flex key={label} align="center" justify="between" style={{ padding: "4px 0" }}>
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {label}
                </Text>
                <Text kind="mono/sm">{value}</Text>
              </Flex>
            ))}
          </Box>
        </>
      )}
    </Card>
  );
}

interface InfrastructureViewProps {
  segment: InfraSegment;
}

export function InfrastructureView({ segment }: InfrastructureViewProps) {
  const meta = SEGMENTS[segment];
  const [map, setMap] = useState<ViewEmbeds | null>(null);
  const [health, setHealth] = useState<GrafanaHealth | null>(null);

  const load = useCallback(async () => {
    // ``segment`` is a hint: the backend may filter server-side, otherwise we filter below.
    const [embeds, hp] = await Promise.all([
      fetchViewEmbeds("infrastructure", `?segment=${encodeURIComponent(segment)}`),
      fetchGrafanaHealth(),
    ]);
    setMap(embeds);
    setHealth(hp);
  }, [segment]);

  useEffect(() => {
    void load();
  }, [load]);

  const allSections = useMemo<EmbedSection[]>(() => (map && Array.isArray(map.sections) ? map.sections : []), [map]);
  // If the backend already filtered by segment, every section matches and this is a no-op.
  const sections = useMemo(() => allSections.filter((s) => matchesSegment(s, meta)), [allSections, meta]);
  const grafanaBase = resolveGrafanaBase(map?.config?.public_base || "", map?.config?.public_port || "3001");
  const grafanaUrl = (value: string | undefined) => {
    const raw = String(value || "").trim();
    return raw.startsWith("/") ? `${grafanaBase}${raw}` : raw;
  };

  const mapError = map && map.ok === false ? map.error || "Dashboard map unavailable." : null;
  const grafanaDown = health != null && health.reachable === false;

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader contentId={meta.contentId} title={meta.title} description={meta.description} />

      {grafanaDown && (
        <Box sx={{ mb: 2 }}>
          <Banner status="error" kind="inline">
            {`Grafana is not reachable from the Rack Status backend${
              health?.error ? ` (${health.error})` : ""
            }. No panel below can load — this is a broken data path, not an empty cluster.`}
          </Banner>
        </Box>
      )}

      {mapError && (
        <Box sx={{ mb: 2 }}>
          <Banner status="error" kind="inline">
            {mapError}
          </Banner>
        </Box>
      )}

      <Box sx={{ mb: 3 }}>
        <Banner status="warning" kind="inline">
          {sections.length > 0 ? `${meta.coverage} ${EMBEDDING_NOTE}` : meta.coverage}
        </Banner>
      </Box>

      {segment === "ib-nvlink" && <FabricProbeCard />}

      {/* Native Chart.js from our own /api/metrics (VictoriaMetrics). Replaces the previous
          Grafana <iframe> embeds: no second tunnel, no second login, no allow_embedding. */}
      <MetricChartGrid
        group="infra"
        showRackFilter={false}
        note="Platform inventory and scheduler state. Fabric charts stay empty while the switch scrapers are stopped for the incident."
      />

      {sections.map((section) => (
        <Card key={`${section.uid}:${section.section}`} style={{ padding: 16, marginTop: 24 }}>
          <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 12 }}>
            <div>
              <Text kind="body/bold/md" style={{ display: "block" }}>{section.title || section.section}</Text>
              {section.rationale ? (
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{section.rationale}</Text>
              ) : null}
            </div>
            {grafanaUrl(section.dashboard_open_url || section.dashboard_open_path) ? (
              <Link
                href={grafanaUrl(section.dashboard_open_url || section.dashboard_open_path)}
                target="_blank"
                rel="noopener noreferrer"
                sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, fontSize: 14 }}
              >
                Open in Grafana <ExternalLink size={14} />
              </Link>
            ) : null}
          </Flex>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 2 }}>
            {(section.panels || []).map((panel) => (
              <Box key={`${section.uid}:${panel.id}`}>
                <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>{panel.title || `Panel ${panel.id}`}</Text>
                <iframe
                  src={grafanaUrl(panel.url)}
                  title={`${section.title || section.section} — ${panel.title || `Panel ${panel.id}`}`}
                  loading="lazy"
                  style={{ width: "100%", height: 300, border: 0, borderRadius: 4 }}
                />
              </Box>
            ))}
          </Box>
        </Card>
      ))}

    </Box>
  );
}
