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
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import LinearProgress from "@mui/material/LinearProgress";
import { Text, Button, Banner } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { usePagedRows } from "../hooks/usePagedRows";
import { TablePager } from "../components/TablePager";
import {
  deleteGeneratedReport,
  downloadGeneratedReport,
  fetchGeneratedReports,
} from "../services/generatedReportsService";
import type { GeneratedReport } from "../types/api";

type BugReportItem = { host: string; name: string };

/** Matches server ``_BUG_REPORT_EXCEL_ROOT_HOST`` — ``*.xlsx`` files directly under the reports base dir. */
const BUG_REPORT_EXCEL_ROOT_HOST = "excel-root";

type BugReportInventoryJson = {
  ok?: boolean;
  error?: string;
  base_dir?: string;
  notice?: string;
  items?: unknown[];
};

function parseContentDispositionFilename(disp: string | null): string | null {
  if (!disp) return null;
  const mStar = /filename\*\s*=\s*[^']*''([^;\s]+)/i.exec(disp);
  if (mStar) {
    try {
      return decodeURIComponent(mStar[1].trim());
    } catch {
      return mStar[1].trim();
    }
  }
  const m = /filename\s*=\s*("?)([^";\n]+)\1/i.exec(disp);
  return m ? m[2].trim() : null;
}

function normalizeItems(raw: unknown[] | undefined): BugReportItem[] {
  if (!Array.isArray(raw)) return [];
  const out: BugReportItem[] = [];
  for (const it of raw) {
    if (!it || typeof it !== "object") continue;
    const o = it as Record<string, unknown>;
    const host = typeof o.host === "string" ? o.host.trim() : "";
    const name = typeof o.name === "string" ? o.name.trim() : "";
    if (host && name) out.push({ host, name });
  }
  return out;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(1)} GiB`;
}

function formatCreated(epochSeconds: number): string {
  const date = new Date(epochSeconds * 1000);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function ReportsView() {
  const [generatedLoading, setGeneratedLoading] = useState(false);
  const [generatedError, setGeneratedError] = useState("");
  const [generatedMaxKept, setGeneratedMaxKept] = useState(0);
  const [generatedReports, setGeneratedReports] = useState<GeneratedReport[]>([]);
  const [generatedActionKey, setGeneratedActionKey] = useState<string | null>(null);
  const generatedPage = usePagedRows(generatedReports, 10);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [baseDir, setBaseDir] = useState("");
  const [notice, setNotice] = useState("");
  const [items, setItems] = useState<BugReportItem[]>([]);
  // One row per report file per node; a fleet-wide bug-report sweep runs to thousands.
  const reportsPage = usePagedRows(items);
  const [downloadingKey, setDownloadingKey] = useState<string | null>(null);

  const loadGenerated = useCallback(async () => {
    setGeneratedLoading(true);
    setGeneratedError("");
    try {
      const data = await fetchGeneratedReports();
      setGeneratedReports(data.reports);
      setGeneratedMaxKept(data.max_kept);
    } catch (e) {
      setGeneratedReports([]);
      setGeneratedMaxKept(0);
      setGeneratedError(e instanceof Error ? e.message : String(e));
    } finally {
      setGeneratedLoading(false);
    }
  }, []);

  const loadInventory = useCallback(async () => {
    setLoading(true);
    setError("");
    setNotice("");
    setItems([]);
    setBaseDir("");
    try {
      const res = await fetch("/api/bug-report-inventory", { credentials: "same-origin" });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const raw = await res.text();
      let data: BugReportInventoryJson = {};
      try {
        data = raw ? (JSON.parse(raw) as BugReportInventoryJson) : {};
      } catch {
        throw new Error(`Invalid JSON (HTTP ${res.status}): ${raw.slice(0, 240)}`);
      }
      if (!res.ok || !data.ok) {
        throw new Error(typeof data.error === "string" ? data.error : `HTTP ${res.status}`);
      }
      setBaseDir(data.base_dir || "");
      setNotice(typeof data.notice === "string" ? data.notice.trim() : "");
      setItems(normalizeItems(data.items));
    } catch (e) {
      setItems([]);
      setBaseDir("");
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void loadInventory();
      void loadGenerated();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [loadGenerated, loadInventory]);

  const downloadGenerated = useCallback(async (report: GeneratedReport) => {
    const key = `download:${report.id}`;
    setGeneratedActionKey(key);
    setGeneratedError("");
    try {
      await downloadGeneratedReport(report);
    } catch (e) {
      setGeneratedError(e instanceof Error ? e.message : String(e));
    } finally {
      setGeneratedActionKey(null);
    }
  }, []);

  const removeGenerated = useCallback(async (report: GeneratedReport) => {
    if (!window.confirm(`Delete saved report ${report.filename}? This cannot be undone.`)) return;
    const key = `delete:${report.id}`;
    setGeneratedActionKey(key);
    setGeneratedError("");
    try {
      await deleteGeneratedReport(report.id);
      await loadGenerated();
    } catch (e) {
      setGeneratedError(e instanceof Error ? e.message : String(e));
    } finally {
      setGeneratedActionKey(null);
    }
  }, [loadGenerated]);

  const downloadFile = useCallback(async (host: string, name: string) => {
    const key = `${host}\0${name}`;
    setDownloadingKey(key);
    try {
      const params = new URLSearchParams({ host, name });
      const res = await fetch(`/api/bug-report-file?${params}`, { credentials: "same-origin" });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const ctype = (res.headers.get("Content-Type") || "").toLowerCase();
      if (!res.ok) {
        if (ctype.includes("application/json")) {
          const j = (await res.json().catch(() => ({}))) as { error?: string };
          throw new Error(typeof j.error === "string" ? j.error : `HTTP ${res.status}`);
        }
        const t = await res.text();
        throw new Error(t.slice(0, 400) || `HTTP ${res.status}`);
      }
      const disp = res.headers.get("Content-Disposition");
      const fname = parseContentDispositionFilename(disp) || `${host}_${name || "nvidia-bug-report.log.gz"}`;
      const blob = await res.blob();
      const a = document.createElement("a");
      const href = URL.createObjectURL(blob);
      a.href = href;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 2500);
    } catch (e) {
      window.alert(e instanceof Error ? e.message : "Download failed");
    } finally {
      setDownloadingKey(null);
    }
  }, []);

  const metaLines = [baseDir ? `Directory: ${baseDir}` : "", notice || ""].filter(Boolean);

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="reports"
        title="Reports"
        description="Download durable generated fleet reports, NVIDIA bug report archives, and NVLMapper outputs."
      />

      <Paper variant="outlined" sx={{ p: 3, mb: 3 }}>
        <Box sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 2, mb: 1, flexWrap: "wrap" }}>
          <Box>
            <Text kind="body/bold/md" style={{ display: "block" }}>Generated fleet reports</Text>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Saved automatically when a fleet report completes, so leaving the page or restarting the dashboard does not lose the result.
              {generatedMaxKept > 0 ? ` The newest ${generatedMaxKept} reports are retained.` : ""}
            </Text>
          </Box>
          <Button kind="secondary" onClick={() => void loadGenerated()} disabled={generatedLoading}>
            Refresh generated reports
          </Button>
        </Box>

        {generatedLoading && <LinearProgress sx={{ mb: 2 }} />}
        {generatedError && (
          <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
            {generatedError}
          </Banner>
        )}

        <TableContainer component={Paper} variant="outlined">
          <Table size="small" aria-label="Generated reports">
            <TableHead>
              <TableRow>
                <TableCell sx={{ fontWeight: 700 }}>Created</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>Report</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>Scope</TableCell>
                <TableCell sx={{ fontWeight: 700 }}>Size</TableCell>
                <TableCell sx={{ fontWeight: 700, width: 210 }}>Actions</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {!generatedLoading && generatedReports.length === 0 && !generatedError ? (
                <TableRow>
                  <TableCell colSpan={5}>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      No generated reports have been saved yet. Generate one from Analytics; it will appear here as soon as it completes.
                    </Text>
                  </TableCell>
                </TableRow>
              ) : (
                generatedPage.visible.map((report) => {
                  const downloading = generatedActionKey === `download:${report.id}`;
                  const deleting = generatedActionKey === `delete:${report.id}`;
                  return (
                    <TableRow key={report.id} hover>
                      <TableCell>{formatCreated(report.created)}</TableCell>
                      <TableCell>
                        <Text kind="body/bold/sm" style={{ display: "block" }}>{report.filename}</Text>
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                          {report.kind} · {report.format.toUpperCase()}
                        </Text>
                      </TableCell>
                      <TableCell>
                        {[report.cluster, typeof report.nodes === "number" ? `${report.nodes} nodes` : ""]
                          .filter(Boolean)
                          .join(" · ") || "—"}
                        {typeof report.schema_errors === "number" && report.schema_errors > 0
                          ? ` · ${report.schema_errors} schema errors`
                          : ""}
                      </TableCell>
                      <TableCell>{formatBytes(report.size)}</TableCell>
                      <TableCell>
                        <Box sx={{ display: "flex", gap: 1, flexWrap: "wrap" }}>
                          <Button
                            size="small"
                            kind="secondary"
                            disabled={generatedActionKey !== null || generatedLoading}
                            onClick={() => void downloadGenerated(report)}
                          >
                            {downloading ? "…" : "Download"}
                          </Button>
                          <Button
                            size="small"
                            kind="secondary"
                            disabled={generatedActionKey !== null || generatedLoading}
                            onClick={() => void removeGenerated(report)}
                          >
                            {deleting ? "…" : "Delete"}
                          </Button>
                        </Box>
                      </TableCell>
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </TableContainer>
        <TablePager {...generatedPage} noun="generated reports" />
      </Paper>

      <Paper variant="outlined" sx={{ p: 3, mb: 2 }}>
        <Text kind="body/regular/md" style={{ display: "block", marginBottom: 12, lineHeight: 1.6, color: "var(--text-color-secondary)" }}>
          Reads the reports directory on <strong>this dashboard host</strong> (same base as{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>RACK_BUG_REPORT_REMOTE_DIR</Box>, default{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>/home/cmsupport/reports</Box>
          ): one subdirectory per node or rack id, with{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>nvidia-bug-report*.log.gz</Box>, NVLMapper{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>*.csv</Box> /{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>*.xls*</Box>{" "}
          under each rack subdirectory, optional legacy <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>nvlmapper.*.zip</Box>, and Excel{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>*.xlsx</Box> (e.g. parser exports under{" "}
          <Box component="code" sx={{ fontFamily: "monospace", fontSize: "0.9em" }}>parser-excel/</Box>). No pdsh — sync or copy here if
          needed. Use <strong>Download</strong> on a row to save a file.
        </Text>
        <Text kind="body/regular/md" style={{ display: "block", lineHeight: 1.6, color: "var(--text-color-secondary)" }}>
          To <strong>generate</strong> new reports on compute nodes, use <strong>Generate bug report</strong> from the rack table; when that job
          finishes, use the rack download control for a zip. This page only lists files already present under the server reports directory.
        </Text>
      </Paper>

      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 2, flexWrap: "wrap" }}>
        <Button kind="primary" onClick={() => void loadInventory()} disabled={loading}>
          Refresh list
        </Button>
        {loading && (
          <Box sx={{ flex: 1, minWidth: 160 }}>
            <LinearProgress />
          </Box>
        )}
      </Box>

      {error && (
        <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
          {error}
        </Banner>
      )}

      {metaLines.length > 0 && (
        <Text
          kind="body/regular/md"
          style={{ display: "block", marginBottom: 16, whiteSpace: "pre-wrap", fontFamily: metaLines.some((l) => l.includes("/")) ? "ui-monospace, monospace" : "inherit" }}
        >
          {metaLines.join("\n")}
        </Text>
      )}

      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Reports by node">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 700 }}>Node</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>File</TableCell>
              <TableCell sx={{ fontWeight: 700, width: 140 }}>Download</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {loading && items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3}>
                  <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                    Scanning local reports directory…
                  </Text>
                </TableCell>
              </TableRow>
            ) : !loading && items.length === 0 && !error ? (
              <TableRow>
                <TableCell colSpan={3}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    No nvidia-bug-report*.log.gz, NVLMapper outputs, nvlmapper.*.zip, or *.xlsx files found under the reports directory above.
                  </Text>
                </TableCell>
              </TableRow>
            ) : (
              reportsPage.visible.map((it) => {
                const key = `${it.host}\0${it.name}`;
                const busy = downloadingKey === key;
                const fullPath =
                  baseDir && it.host === BUG_REPORT_EXCEL_ROOT_HOST
                    ? `${baseDir}/${it.name}`
                    : baseDir
                      ? `${baseDir}/${it.host}/${it.name}`
                      : "";
                const nodeLabel =
                  it.host === BUG_REPORT_EXCEL_ROOT_HOST ? "— (reports root)" : it.host;
                return (
                  <TableRow key={key} hover>
                    <TableCell sx={{ fontFamily: "monospace", fontSize: 13 }} title={it.host}>
                      {nodeLabel}
                    </TableCell>
                    <TableCell sx={{ fontFamily: "monospace", fontSize: 13 }} title={fullPath || undefined}>
                      {it.name}
                    </TableCell>
                    <TableCell>
                      <Button size="small" kind="secondary" disabled={busy || loading} onClick={() => void downloadFile(it.host, it.name)}>
                        {busy ? "…" : "Download"}
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </TableContainer>
      <TablePager {...reportsPage} noun="files" />
    </Box>
  );
}
