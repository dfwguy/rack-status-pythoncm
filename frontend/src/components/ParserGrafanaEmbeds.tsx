/**
 * Grafana /d-solo/ embeds for parser results — mirrors legacy appendParserGrafanaEmbeds (index.html).
 */

import Box from "@mui/material/Box";
import Link from "@mui/material/Link";
import { Text, Card } from "@nvidia/foundations-react-core";

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

function soloUrl(
  ge: GrafanaEmbedPayload,
  base: string,
  panelId: number,
  tzParam: string,
  forNewTab: boolean,
): string {
  const uid = ge.dashboard_uid || "bright-gpu-per-host";
  const slug = ge.dash_slug || "bright-gpu-thermal-and-power";
  const regexQ = encodeURIComponent(ge.host_regex || ".*");
  const wantLiveNewTab = !!forNewTab && String(ge.time_source || "") === "live_last_5m";
  let rangeQs: string;
  if (wantLiveNewTab) {
    rangeQs =
      `from=${encodeURIComponent("now-5m")}&to=${encodeURIComponent("now")}&refresh=${encodeURIComponent("5s")}`;
  } else {
    const useIso = ge.from_iso_utc && ge.to_iso_utc;
    rangeQs = useIso
      ? `from=${encodeURIComponent(String(ge.from_iso_utc))}&to=${encodeURIComponent(String(ge.to_iso_utc))}`
      : `from=${encodeURIComponent(String(ge.from_ms))}&to=${encodeURIComponent(String(ge.to_ms))}`;
  }
  return (
    `${trimSlash(base)}/d-solo/${encodeURIComponent(String(uid))}/${encodeURIComponent(String(slug))}` +
    `?orgId=1&${rangeQs}&timezone=${encodeURIComponent(tzParam)}` +
    `&panelId=${encodeURIComponent(String(panelId))}&var-job_host_regex=${regexQ}&kiosk&theme=dark`
  );
}

function resolveGrafanaBase(grafanaUri: string | undefined, grafanaPort: string | number | undefined): string {
  const portStr =
    grafanaPort != null && String(grafanaPort).trim() ? String(grafanaPort).trim() : "3000";
  let base = (grafanaUri != null && String(grafanaUri).trim()) || "";
  if (!base) {
    if (typeof window !== "undefined") {
      base = `${window.location.protocol}//${window.location.hostname}:${portStr}`;
    } else {
      base = `http://localhost:${portStr}`;
    }
  }
  return trimSlash(base);
}

export interface ParserGrafanaEmbedsProps {
  grafanaEmbed: GrafanaEmbedPayload | null | undefined;
  grafanaUri?: string;
  grafanaPort?: string | number;
  /** Effective Grafana timezone= (IANA or utc); same as legacy parser_display_timezone resolution. */
  parserDisplayTimezone: string;
}

export function ParserGrafanaEmbeds({
  grafanaEmbed: ge,
  grafanaUri,
  grafanaPort,
  parserDisplayTimezone,
}: ParserGrafanaEmbedsProps) {
  if (!ge || ge.disabled) {
    return (
      <Card style={{ padding: 16 }}>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {(ge && ge.note) || "Grafana embed not available."}
        </Text>
      </Card>
    );
  }
  if (!ge.iframe_ready || ge.from_ms == null || ge.to_ms == null) {
    return (
      <Card style={{ padding: 16 }}>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {ge.note || "Grafana context not available (check Slurm host expansion / embed settings)."}
        </Text>
      </Card>
    );
  }

  const base = resolveGrafanaBase(grafanaUri, grafanaPort);
  const tzParam = (parserDisplayTimezone || ge.display_timezone || "utc").trim() || "utc";
  const thSrc = ge.time_source ? String(ge.time_source) : "?";
  const hnSrc = ge.hosts_source ? String(ge.hosts_source) : "?";
  const nhosts = ge.hosts && ge.hosts.length ? String(ge.hosts.length) : "0";

  const meta =
    thSrc === "live_last_5m"
      ? `Rolling window: last 5 minutes (server clock). Grafana timezone: ${tzParam}. Nodes: ${hnSrc}${
          ge.hosts && ge.hosts.length ? ` (${nhosts} hostnames).` : " (no host list — Prometheus regex .*)."
        }`
      : `Time range from NEMO log (${thSrc}). Grafana timezone: ${tzParam}. Nodes: ${hnSrc}${
          ge.hosts && ge.hosts.length ? ` (${nhosts} hostnames).` : " (no host list — Prometheus regex .*)."
        }`;

  const panels: { id: number; label: string }[] = [
    { id: 1, label: "GPU thermal" },
    { id: 2, label: "GPU power usage" },
  ];

  return (
    <Box sx={{ mt: 2 }}>
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>
        Grafana — GPU thermal and power
      </Text>
      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 12, display: "block" }}>
        {meta}
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
          const embedUrl = soloUrl(ge, base, id, tzParam, false);
          const newTabUrl = soloUrl(ge, base, id, tzParam, true);
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
