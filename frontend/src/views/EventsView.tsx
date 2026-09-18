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
 * Observability -> Events.
 *
 * Only sources that genuinely exist are wired. Nothing here is generated.
 *
 * 1. **Slurm node state changes** — ``GET /api/scheduler-node-reasons`` runs ``sinfo -R --long`` on
 *    the head node and returns the reason, the user who set it, the timestamp, the resulting
 *    state and the node list for every drained / down node ("Prolog error", "Not responding",
 *    thermal drains, ...). Parsed with the existing ``parseSinfoRStdout`` and coloured with
 *    ``slurmReasonTone`` so the idiom matches the Scheduler view.
 *
 * 2. **Operator activity** — ``GET /api/rack-locks`` (via ``RackLocksContext``) is the tool's
 *    own record of in-flight operations and explicit rack claims, with operator attribution
 *    and a ``since`` timestamp. No exporter writes it to VictoriaMetrics.
 *
 *    This used to say it was "the only source in the estate that knows *who* did something". That
 *    stopped being true when Phase 1 landed the audit trail, and is now plainly false: see
 *    Observability > Audit Trail, which is the authoritative attribution record. Rack locks show
 *    what is *in flight*; the audit trail shows what *happened*.
 *
 * 3. **Alerts (vmalert ``ALERTS`` on VictoriaMetrics :8429)** — NOT wired. No backend route
 *    serves them to this app, so the section renders an explicit placeholder plus a link to
 *    Grafana's alert list. An empty-but-true list beats a populated-but-fake one.
 *
 * Scope caveat shown to the operator: both wired sources are *current-state* snapshots, not a
 * retained history. Entries leave the list when a node is undrained, an operation finishes, or
 * a claim expires.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import { Banner, Button, Card, Flex, StatusMessage, Tag, Text } from "@nvidia/foundations-react-core";
import { AlertTriangle, CalendarX, ExternalLink, Lock, RefreshCw, Server } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { useRackLocksContext } from "../context/RackLocksContext";
import { fetchSlurmJson } from "../lib/slurmHelpers";
import { parseSinfoRStdout } from "../lib/parseSinfoRStdout";
import { slurmReasonTone } from "../lib/slurmReasonTone";
import { resolveGrafanaBase } from "../lib/grafanaLinks";
import { useGrafanaDeeplinks } from "../lib/useGrafanaDeeplinks";

type TagColor = "green" | "blue" | "teal" | "yellow" | "red" | "gray";

/** The two sources that are actually wired. Alerts are handled separately (not wired). */
type EventSource = "slurm" | "operator";

const SOURCE_META: Record<EventSource, { label: string; color: TagColor }> = {
  slurm: { label: "Slurm state", color: "yellow" },
  operator: { label: "Operator", color: "blue" },
};

const SOURCE_ORDER: EventSource[] = ["slurm", "operator"];

interface EventRow {
  key: string;
  source: EventSource;
  /** Raw timestamp text exactly as the source reported it. */
  tsRaw: string;
  /** Epoch ms when parseable, else null (row sorts last). */
  tsMs: number | null;
  /** Short tag on the row, e.g. the Slurm state or "Lock". */
  label: string;
  color: TagColor;
  /** Node list / rack the event applies to. */
  scope: string;
  text: string;
  actor: string;
}

function headerIndex(headers: string[], name: string): number {
  const want = name.trim().toUpperCase();
  for (let i = 0; i < headers.length; i++) {
    if (String(headers[i] || "").trim().toUpperCase() === want) return i;
  }
  return -1;
}

function toMs(raw: string): number | null {
  const s = String(raw || "").trim();
  if (!s) return null;
  const direct = Date.parse(s);
  if (!Number.isNaN(direct)) return direct;
  // Slurm sometimes emits "YYYY-MM-DD HH:MM:SS" which older parsers reject.
  const spaced = Date.parse(s.replace(" ", "T"));
  return Number.isNaN(spaced) ? null : spaced;
}

function displayTs(row: EventRow): string {
  if (row.tsMs == null) return row.tsRaw || "—";
  return new Date(row.tsMs).toLocaleString();
}

function toneColor(reason: string, state: string): TagColor {
  const tone = slurmReasonTone(`${state} ${reason}`);
  if (tone === "error") return "red";
  if (tone === "warning") return "yellow";
  return "gray";
}

/** Turn parsed ``sinfo -R --long`` output into event rows. [] when the table cannot be read. */
function slurmEvents(parsed: ReturnType<typeof parseSinfoRStdout>): EventRow[] {
  if (parsed.useRaw || parsed.headers.length === 0) return [];
  const iReason = headerIndex(parsed.headers, "REASON");
  const iUser = headerIndex(parsed.headers, "USER");
  const iTs = headerIndex(parsed.headers, "TIMESTAMP");
  const iState = headerIndex(parsed.headers, "STATE");
  const iNodes = headerIndex(parsed.headers, "NODELIST");
  if (iReason < 0 || iNodes < 0) return [];

  return parsed.rows.map((cells, i) => {
    const reason = iReason >= 0 ? cells[iReason] || "" : "";
    const user = iUser >= 0 ? cells[iUser] || "" : "";
    const tsRaw = iTs >= 0 ? cells[iTs] || "" : "";
    const state = iState >= 0 ? cells[iState] || "" : "";
    const nodes = iNodes >= 0 ? cells[iNodes] || "" : "";
    return {
      key: `slurm:${i}:${nodes}:${tsRaw}`,
      source: "slurm" as const,
      tsRaw,
      tsMs: toMs(tsRaw),
      label: state || "Slurm",
      color: toneColor(reason, state),
      scope: nodes,
      text: reason || "(no reason recorded)",
      actor: user,
    };
  });
}

export function EventsView() {
  const [activeSources, setActiveSources] = useState<Set<EventSource>>(() => new Set(SOURCE_ORDER));

  const [sinfoStdout, setSinfoStdout] = useState("");
  const [sinfoCmd, setSinfoCmd] = useState("");
  const [sinfoError, setSinfoError] = useState("");
  const [sinfoLoading, setSinfoLoading] = useState(false);
  const [sinfoAt, setSinfoAt] = useState("");

  const locksCtx = useRackLocksContext();
  const grafana = useGrafanaDeeplinks();

  const loadSinfo = useCallback(async () => {
    setSinfoLoading(true);
    setSinfoError("");
    try {
      const { res, data } = await fetchSlurmJson("/api/scheduler-node-reasons");
      setSinfoCmd(String(data.command_display || ""));
      if (!res.ok || data.ok === false) {
        setSinfoStdout("");
        setSinfoError(data.error || `sinfo -R --long failed (HTTP ${res.status})`);
        return;
      }
      setSinfoStdout(String(data.stdout || ""));
      setSinfoAt(new Date().toLocaleTimeString());
    } catch (e) {
      setSinfoStdout("");
      setSinfoError(e instanceof Error ? e.message : String(e));
    } finally {
      setSinfoLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSinfo();
  }, [loadSinfo]);

  const toggleSource = (source: EventSource) =>
    setActiveSources((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      return next;
    });

  const operatorEvents = useMemo((): EventRow[] => {
    return Object.entries(locksCtx.locks).map(([rack, entry]) => {
      const isClaim = entry.kind === "claim";
      const bits: string[] = [];
      bits.push(isClaim ? "Rack claimed" : "Operation in flight");
      if (entry.detail) bits.push(entry.detail);
      if (entry.job_id) bits.push(`job ${entry.job_id}`);
      if (entry.stolen_from) bits.push(`taken over from ${entry.stolen_from}`);
      if (isClaim && entry.expires_at) bits.push(`expires ${entry.expires_at}`);
      return {
        key: `lock:${rack}`,
        source: "operator" as const,
        tsRaw: entry.since || "",
        tsMs: toMs(entry.since || ""),
        label: isClaim ? "Lock" : "Command",
        color: isClaim ? ("blue" as TagColor) : ("yellow" as TagColor),
        scope: rack,
        text: bits.join(" · "),
        actor: entry.operator || "",
      };
    });
  }, [locksCtx.locks]);

  const sinfoParsed = useMemo(() => parseSinfoRStdout(sinfoStdout), [sinfoStdout]);
  const slurmRows = useMemo(() => slurmEvents(sinfoParsed), [sinfoParsed]);
  /** ``sinfo`` produced output the fixed-width parser could not turn into a table. */
  const sinfoUnparsed = sinfoParsed.useRaw && sinfoStdout.trim().length > 0;

  const rows = useMemo((): EventRow[] => {
    const all = [...slurmRows, ...operatorEvents].filter((r) => activeSources.has(r.source));
    all.sort((a, b) => {
      if (a.tsMs == null && b.tsMs == null) return 0;
      if (a.tsMs == null) return 1;
      if (b.tsMs == null) return -1;
      return b.tsMs - a.tsMs;
    });
    return all;
  }, [slurmRows, operatorEvents, activeSources]);

  const grafanaBase = grafana?.enabled ? resolveGrafanaBase(grafana.base, grafana.port) : "";

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="obsEvents"
        title="Events"
        description="Slurm node state changes and this tool's own operator activity — current state, newest first."
        actions={
          <Button kind="secondary" size="small" onClick={() => void loadSinfo()} disabled={sinfoLoading}>
            <RefreshCw size={16} />
            Refresh
          </Button>
        }
      />

      <Box sx={{ mb: 3 }}>
        <Banner status="info" kind="inline">
          Both wired sources are current-state snapshots, not a retained history: rows leave the
          list when a node is undrained, an operation finishes, or a rack claim expires. Nothing
          on this page is generated.
        </Banner>
      </Box>

      {sinfoError && (
        <Box sx={{ mb: 3 }}>
          <Banner status="error" kind="inline">
            {sinfoError}
            {sinfoCmd ? ` (${sinfoCmd})` : ""}
          </Banner>
        </Box>
      )}

      {locksCtx.error && (
        <Box sx={{ mb: 3 }}>
          <Banner status="warning" kind="inline">
            Operator activity unavailable: {locksCtx.error}
          </Banner>
        </Box>
      )}

      {sinfoUnparsed && (
        <Box sx={{ mb: 3 }}>
          <Banner status="warning" kind="inline">
            sinfo returned output that could not be read as a table, so no Slurm rows are listed.
            The raw text is available on the Scheduler page.
          </Banner>
        </Box>
      )}

      <Flex align="center" gap="2" wrap="wrap" style={{ marginBottom: 16 }}>
        <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
          Source
        </Text>
        {SOURCE_ORDER.map((source) => {
          const meta = SOURCE_META[source];
          const on = activeSources.has(source);
          return (
            <button
              key={source}
              type="button"
              onClick={() => toggleSource(source)}
              style={{ background: "none", border: "none", padding: 0, cursor: "pointer", opacity: on ? 1 : 0.4 }}
              aria-pressed={on}
            >
              <Tag color={meta.color} kind={on ? "solid" : "outline"} readOnly density="compact">
                {meta.label}
              </Tag>
            </button>
          );
        })}
      </Flex>

      <Card style={{ padding: 16, marginBottom: 24 }}>
        <Flex align="center" justify="between" wrap="wrap" gap="2" style={{ marginBottom: 12 }}>
          <Flex align="center" gap="2">
            <Server size={18} />
            <Text kind="body/bold/md">Event timeline</Text>
          </Flex>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {rows.length} event{rows.length === 1 ? "" : "s"}
            {sinfoAt ? ` · sinfo ${sinfoAt}` : ""}
          </Text>
        </Flex>

        {(sinfoLoading || locksCtx.loading) && <LinearProgress sx={{ mb: 2 }} />}

        {rows.length === 0 ? (
          <StatusMessage
            size="small"
            slotMedia={<CalendarX />}
            slotHeading="No events"
            slotSubheading={
              activeSources.size === 0
                ? "Every source is filtered out — re-enable one above."
                : "No node is drained or down with a recorded reason, and no rack operation or claim is active right now."
            }
          />
        ) : (
          <Flex direction="col" gap="0">
            {rows.map((row, i) => (
              <Flex
                key={row.key}
                align="start"
                gap="3"
                wrap="wrap"
                style={{
                  padding: "10px 0",
                  borderBottom:
                    i < rows.length - 1 ? "1px solid var(--border-color-base, rgba(128,128,128,0.2))" : "none",
                }}
              >
                <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", minWidth: 160 }}>
                  {displayTs(row)}
                </Text>
                <Tag color={row.color} kind="solid" readOnly density="compact">
                  {row.label}
                </Tag>
                <Tag color={SOURCE_META[row.source].color} kind="outline" readOnly density="compact">
                  {SOURCE_META[row.source].label}
                </Tag>
                <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", minWidth: 160 }}>
                  {row.scope || "—"}
                </Text>
                <Text kind="body/regular/sm">{row.text}</Text>
                {row.actor && (
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    by {row.actor}
                  </Text>
                )}
              </Flex>
            ))}
          </Flex>
        )}
      </Card>

      <Card style={{ padding: 16 }}>
        <Flex align="center" gap="2" style={{ marginBottom: 12 }}>
          <AlertTriangle size={18} />
          <Text kind="body/bold/md">Alerts</Text>
        </Flex>
        <StatusMessage
          size="small"
          slotMedia={<AlertTriangle />}
          slotHeading="Alerts not wired yet"
          slotSubheading="vmalert ALERTS on VictoriaMetrics (:8429) are not exposed to this app — no backend route serves them, so no alert rows are shown rather than synthetic ones."
        />
        {grafanaBase && (
          <Box sx={{ mt: 1.5 }}>
            <Link
              href={`${grafanaBase}/alerting/list`}
              target="_blank"
              rel="noopener noreferrer"
              sx={{ display: "inline-flex", alignItems: "center", gap: 0.5, fontSize: 13 }}
            >
              Open Grafana alert list
              <ExternalLink size={13} />
            </Link>
          </Box>
        )}
      </Card>

      <Box sx={{ mt: 3 }}>
        <Flex align="center" gap="2">
          <Lock size={14} />
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Operator attribution comes from this tool's rack-lock store (GET /api/rack-locks); no
            Grafana dashboard carries an actor label, so it cannot be sourced from telemetry.
          </Text>
        </Flex>
      </Box>
    </Box>
  );
}
