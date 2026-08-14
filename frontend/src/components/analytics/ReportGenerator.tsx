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
// ReportGenerator — pick a report and a format, generate it, download it.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Lives inside the Rack Level tab rather than the Analytics page header: what it produces is a
// statement about the cluster and its racks, so it belongs with that content rather than floating
// above both tabs as if it applied to the node heatmap too.
//
// The available reports and formats are fetched from GET /api/node-report/kinds instead of being
// hardcoded here. PDF rendering depends on reportlab being present in the image, and a build
// without it must not offer a button that returns 503.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Banner, Button, ProgressBar, Select, Text } from "@nvidia/foundations-react-core";
import Box from "@mui/material/Box";
import { Download } from "lucide-react";
import { apiFetch } from "../../lib/apiFetch";
import { generateNodeReport } from "../../services/bringupService";
import type { NodeReportProgress } from "../../services/bringupService";

interface ReportKind {
  id: string;
  label: string;
  description: string;
  formats: string[];
  default_format: string | null;
}

const FORMAT_LABELS: Record<string, string> = {
  csv: "CSV — one row per node",
  json: "JSON — nested key/value",
  pdf: "PDF — executive view with charts",
};

export function ReportGenerator() {
  const [kinds, setKinds] = useState<ReportKind[]>([]);
  const [kindId, setKindId] = useState("");
  const [format, setFormat] = useState("");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<NodeReportProgress | null>(null);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await apiFetch("/api/node-report/kinds");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const body = (await res.json()) as { kinds?: ReportKind[] };
        if (cancelled) return;
        const list = body.kinds ?? [];
        setKinds(list);
        // Default to our own report: it is the one an operator reads, where the customer-schema
        // one is a deliverable produced on request.
        const preferred = list.find((k) => k.id === "rack-status") ?? list[0];
        if (preferred) {
          setKindId(preferred.id);
          setFormat(preferred.default_format ?? preferred.formats[0] ?? "");
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const kind = useMemo(() => kinds.find((k) => k.id === kindId), [kinds, kindId]);

  const onKindChange = useCallback(
    (next: string) => {
      setKindId(next);
      const k = kinds.find((x) => x.id === next);
      // Formats differ per report — the customer schema is JSON only — so reset rather than
      // carrying a format the new report cannot produce.
      setFormat(k?.default_format ?? k?.formats[0] ?? "");
    },
    [kinds],
  );

  const generate = useCallback(async () => {
    setBusy(true);
    setError("");
    setDone("");
    setProgress(null);
    try {
      const filename = await generateNodeReport(setProgress, 1000, {
        report: kindId,
        format,
      });
      setDone(filename);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }, [kindId, format]);

  const pct = progress?.total
    ? Math.round(((progress.done ?? 0) / progress.total) * 100)
    : 0;

  return (
    <Box
      sx={{
        mt: 2,
        p: 2,
        border: "1px solid var(--border-color-primary, #333)",
        borderRadius: 1,
        display: "flex",
        flexDirection: "column",
        gap: 1.5,
      }}
    >
      <Box>
        <Text kind="body/bold/md" style={{ display: "block" }}>
          Generate a report
        </Text>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          {kind?.description ??
            "Sweeps every node for hardware and firmware identity, then renders the result."}
        </Text>
      </Box>

      <Box sx={{ display: "flex", gap: 2, alignItems: "flex-end", flexWrap: "wrap" }}>
        <Box sx={{ minWidth: 280 }}>
          <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 4 }}>
            Report
          </Text>
          <Select
            value={kindId}
            onValueChange={(v: string) => onKindChange(v)}
            items={kinds.map((k) => ({ value: k.id, children: k.label }))}
            size="small"
            disabled={busy || kinds.length === 0}
            aria-label="Report type"
          />
        </Box>

        <Box sx={{ minWidth: 260 }}>
          <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 4 }}>
            Format
          </Text>
          <Select
            value={format}
            onValueChange={(v: string) => setFormat(v)}
            items={(kind?.formats ?? []).map((f) => ({
              value: f,
              children: FORMAT_LABELS[f] ?? f.toUpperCase(),
            }))}
            size="small"
            disabled={busy || (kind?.formats.length ?? 0) <= 1}
            aria-label="Report format"
          />
        </Box>

        <Button
          kind="secondary"
          size="small"
          onClick={() => void generate()}
          disabled={busy || !kindId || !format}
        >
          <Download size={14} style={{ marginRight: 6 }} />
          {busy ? "Generating…" : "Generate & download"}
        </Button>
      </Box>

      {busy && (
        <Box>
          <Text kind="body/regular/sm" style={{ display: "block", marginBottom: 2 }}>
            {progress?.total
              ? `Swept ${progress.done ?? 0} of ${progress.total} nodes`
              : progress?.phase || "Starting…"}
          </Text>
          <ProgressBar value={pct} aria-label="Report generation progress" />
          {progress?.current && (
            <Text
              kind="mono/sm"
              style={{
                display: "block",
                marginTop: 2,
                color: "var(--text-color-secondary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {progress.current}
            </Text>
          )}
        </Box>
      )}

      {busy && (
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          You can leave this page — the report is saved when it finishes and will be waiting under
          Analytics &rsaquo; Reports.
        </Text>
      )}

      {error && (
        <Banner status="error" kind="inline">
          {error}
        </Banner>
      )}
      {done && !busy && (
        <Banner status="success" kind="inline">
          Downloaded {done} — also saved under Analytics &rsaquo; Reports.
        </Banner>
      )}
    </Box>
  );
}
