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

export function ReportsView() {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [baseDir, setBaseDir] = useState("");
  const [notice, setNotice] = useState("");
  const [items, setItems] = useState<BugReportItem[]>([]);
  const [downloadingKey, setDownloadingKey] = useState<string | null>(null);

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
    void loadInventory();
  }, [loadInventory]);

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
      let fname = parseContentDispositionFilename(disp) || `${host}_${name || "nvidia-bug-report.log.gz"}`;
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
    <Box sx={{ maxWidth: 1200, mx: "auto", p: 4 }}>
      <PageHeader
        title="Reports"
        description="Browse NVIDIA bug report archives and NVLMapper outputs on this dashboard host (same as legacy Download Reports)."
      />

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
          To <strong>generate</strong> new reports on compute nodes, use <strong>Generate bug report</strong> from the rack table (pdsh); when that job
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
              items.map((it) => {
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
                      <Button size="small" kind="tertiary" disabled={busy || loading} onClick={() => void downloadFile(it.host, it.name)}>
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
    </Box>
  );
}
