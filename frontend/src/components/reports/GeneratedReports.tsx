// GeneratedReports — reports this deployment produced, kept on disk.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Separate from the bug-report table on the same page because the two are different things: those
// are archives collected from nodes, these are documents the dashboard generated. Grouping them in
// one table would mean a "Node" column that is meaningless for half the rows.
//
// The reason this list exists at all: generating a report is minutes of fleet-wide SSH, and the
// document used to live only in the browser tab that asked for it. Leaving Analytics threw the
// sweep away. Reports are now archived as they finish, so this is where you come back to.
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import IconButton from "@mui/material/IconButton";
import { Banner, Button, Tag, Text } from "@nvidia/foundations-react-core";
import { Download, RefreshCw, Trash2 } from "lucide-react";
import { apiFetch } from "../../lib/apiFetch";

export interface GeneratedReport {
  id: string;
  kind: string;
  format: string;
  filename: string;
  size: number;
  created: number;
  cluster?: string;
  nodes?: number;
  schema_errors?: number;
}

const KIND_LABEL: Record<string, string> = {
  "rack-status": "Rack Status",
  nscale: "Nscale TX schema",
};

const FORMAT_COLOR: Record<string, "blue" | "green" | "purple" | "gray"> = {
  csv: "blue",
  json: "green",
  pdf: "purple",
};

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function humanWhen(epochSeconds: number): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

export function GeneratedReports() {
  const [reports, setReports] = useState<GeneratedReport[] | null>(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [maxKept, setMaxKept] = useState(0);

  const load = useCallback(async () => {
    setError("");
    try {
      const res = await apiFetch("/api/reports/generated");
      const body = (await res.json()) as {
        ok?: boolean;
        error?: string;
        reports?: GeneratedReport[];
        max_kept?: number;
      };
      if (!res.ok || !body.ok) throw new Error(body.error || `HTTP ${res.status}`);
      setReports(body.reports ?? []);
      setMaxKept(body.max_kept ?? 0);
    } catch (e) {
      setReports([]);
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const download = useCallback(async (report: GeneratedReport) => {
    setBusyId(report.id);
    try {
      const res = await apiFetch(`/api/reports/generated/${report.id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Through a blob so the filename from Content-Disposition is honoured and the request
      // carries the session, which a bare window.open to the same URL would also do but without
      // surfacing an error if the report has since been pruned.
      const blob = await res.blob();
      const href = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = href;
      a.download = report.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyId(null);
    }
  }, []);

  const remove = useCallback(
    async (report: GeneratedReport) => {
      setBusyId(report.id);
      try {
        const res = await apiFetch(`/api/reports/generated/${report.id}`, { method: "DELETE" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await load();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setBusyId(null);
      }
    },
    [load],
  );

  const rows = reports ?? [];

  return (
    <Box sx={{ mb: 4 }}>
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, mb: 1 }}>
        <Text kind="title/lg">Generated reports</Text>
        <Button kind="tertiary" size="small" onClick={() => void load()}>
          <RefreshCw size={14} style={{ marginRight: 6 }} />
          Refresh
        </Button>
      </Box>

      <Text
        kind="body/regular/sm"
        style={{ display: "block", marginBottom: 12, color: "var(--text-color-secondary)" }}
      >
        Cluster and customer-schema reports produced from Analytics &rsaquo; Rack Level. Saved when
        they finish, so you can start one and come back — you do not have to wait on the page.
        {maxKept ? ` The ${maxKept} most recent are kept.` : ""}
      </Text>

      {error && (
        <Banner status="error" kind="inline" style={{ marginBottom: 12 }}>
          {error}
        </Banner>
      )}

      <TableContainer component={Paper} variant="outlined">
        <Table size="small" aria-label="Generated reports">
          <TableHead>
            <TableRow>
              <TableCell sx={{ fontWeight: 700 }}>Report</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>Format</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>Generated</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>Nodes</TableCell>
              <TableCell sx={{ fontWeight: 700 }}>Size</TableCell>
              <TableCell sx={{ fontWeight: 700, width: 150 }}>Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {reports === null ? (
              <TableRow>
                <TableCell colSpan={6}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    Loading saved reports…
                  </Text>
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow>
                <TableCell colSpan={6}>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    No reports yet. Generate one from Analytics &rsaquo; Rack Level and it will
                    appear here when it finishes.
                  </Text>
                </TableCell>
              </TableRow>
            ) : (
              rows.map((r) => {
                const busy = busyId === r.id;
                return (
                  <TableRow key={r.id} hover>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ display: "block" }}>
                        {KIND_LABEL[r.kind] ?? r.kind}
                      </Text>
                      <Text
                        kind="mono/sm"
                        style={{ color: "var(--text-color-secondary)" }}
                        title={r.filename}
                      >
                        {r.cluster || r.filename}
                      </Text>
                    </TableCell>
                    <TableCell>
                      <Tag
                        color={FORMAT_COLOR[r.format] ?? "gray"}
                        kind="outline"
                        readOnly
                        density="compact"
                      >
                        {r.format.toUpperCase()}
                      </Tag>
                      {/* A schema failure is a property of the document, so it belongs on the
                          row rather than only in the tab that generated it. */}
                      {!!r.schema_errors && (
                        <Tag color="red" kind="solid" readOnly density="compact">
                          {r.schema_errors} schema {r.schema_errors === 1 ? "error" : "errors"}
                        </Tag>
                      )}
                    </TableCell>
                    <TableCell sx={{ whiteSpace: "nowrap" }}>
                      <Text kind="body/regular/sm">{humanWhen(r.created)}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm">{r.nodes ?? "—"}</Text>
                    </TableCell>
                    <TableCell sx={{ whiteSpace: "nowrap" }}>
                      <Text kind="body/regular/sm">{humanSize(r.size)}</Text>
                    </TableCell>
                    <TableCell>
                      <Box sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
                        <Button
                          size="small"
                          kind="secondary"
                          disabled={busy}
                          onClick={() => void download(r)}
                        >
                          <Download size={14} style={{ marginRight: 4 }} />
                          {busy ? "…" : "Download"}
                        </Button>
                        <Tooltip title="Delete this saved report">
                          <IconButton
                            size="small"
                            disabled={busy}
                            onClick={() => void remove(r)}
                            aria-label={`Delete ${r.filename}`}
                          >
                            <Trash2 size={14} />
                          </IconButton>
                        </Tooltip>
                      </Box>
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
