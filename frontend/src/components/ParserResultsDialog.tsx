import { useState, useEffect } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Tabs from "@mui/material/Tabs";
import Tab from "@mui/material/Tab";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import { Button, Text, Banner, Card, Flex, Tag } from "@nvidia/foundations-react-core";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
} from "chart.js";
import { Bar, Line } from "react-chartjs-2";

ChartJS.register(
  CategoryScale,
  LinearScale,
  LogarithmicScale,
  BarElement,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  Legend,
  Filler,
);

interface ParserResultsDialogProps {
  open: boolean;
  onClose: () => void;
  rack: string;
  jobId?: string;
  logDir?: string;
}

interface NcclPoint {
  size_bytes: number;
  bus_bw_gbps: number;
}

interface NcclRow {
  job_id: string;
  rack: string;
  peak_busbw_gbps: number;
  all_reduce_busbw_series: NcclPoint[];
}

interface StepPoint {
  iteration: number;
  step_time: number;
}

interface NemotronRow {
  job_id: string;
  rack: string;
  model: string;
  median_step_time: number;
  step_time_series: StepPoint[];
}

interface ParserData {
  nccl_peak_out_of_place_busbw_gbps?: NcclRow[];
  nemotron_5h_56b_train_window?: NemotronRow[];
  parser_flags?: { flag: string; value: string }[];
}

interface ParserResponse {
  ok: boolean;
  rack: string;
  log_file: string;
  data: ParserData;
  error?: string;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(0)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(0)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function NcclChart({ rows }: { rows: NcclRow[] }) {
  if (!rows.length) return <Text kind="body/regular/md">No NCCL data available</Text>;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {rows.map((row, idx) => {
        const series = row.all_reduce_busbw_series || [];
        if (!series.length) return null;

        const chartData = {
          labels: series.map((p) => formatBytes(p.size_bytes)),
          datasets: [
            {
              label: `bus_bw (GB/s) — job ${row.job_id}`,
              data: series.map((p) => p.bus_bw_gbps),
              backgroundColor: "rgba(118, 185, 0, 0.7)",
              borderColor: "#76B900",
              borderWidth: 1,
            },
          ],
        };

        return (
          <Card key={idx} style={{ padding: 16 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 12 }}>
              <Text kind="body/bold/md">NCCL all_reduce — Out-of-place Bus Bandwidth</Text>
              <Tag color="green" kind="solid" readOnly density="compact">
                Peak: {row.peak_busbw_gbps} GB/s
              </Tag>
            </Flex>
            <div style={{ height: 300 }}>
              <Bar
                data={chartData}
                options={{
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: {
                    legend: { display: false },
                    tooltip: {
                      callbacks: {
                        label: (ctx) => `${(ctx.parsed.y ?? 0).toFixed(2)} GB/s`,
                      },
                    },
                  },
                  scales: {
                    x: {
                      title: { display: true, text: "Message Size" },
                      ticks: { maxRotation: 45, font: { size: 10 } },
                    },
                    y: {
                      title: { display: true, text: "Bus Bandwidth (GB/s)" },
                      beginAtZero: true,
                    },
                  },
                }}
              />
            </div>
          </Card>
        );
      })}
    </Box>
  );
}

function StepTimeChart({ rows }: { rows: NemotronRow[] }) {
  if (!rows.length) return <Text kind="body/regular/md">No step time data available</Text>;

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {rows.map((row, idx) => {
        const series = row.step_time_series || [];
        if (!series.length) return null;

        const chartData = {
          labels: series.map((p) => String(p.iteration)),
          datasets: [
            {
              label: `step_time (s) — ${row.model}`,
              data: series.map((p) => p.step_time),
              borderColor: "#58d6ff",
              backgroundColor: "rgba(88, 214, 255, 0.1)",
              borderWidth: 2,
              pointRadius: 1,
              fill: true,
              tension: 0.2,
            },
          ],
        };

        return (
          <Card key={idx} style={{ padding: 16 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 12 }}>
              <Text kind="body/bold/md">{row.model} — Step Time vs Iteration</Text>
              <Tag color="teal" kind="solid" readOnly density="compact">
                Median: {row.median_step_time}s
              </Tag>
            </Flex>
            <div style={{ height: 300 }}>
              <Line
                data={chartData}
                options={{
                  responsive: true,
                  maintainAspectRatio: false,
                  plugins: {
                    legend: { display: false },
                    tooltip: {
                      callbacks: {
                        label: (ctx) => `${(ctx.parsed.y ?? 0).toFixed(3)}s`,
                      },
                    },
                  },
                  scales: {
                    x: {
                      title: { display: true, text: "Iteration" },
                      ticks: {
                        maxTicksLimit: 20,
                        font: { size: 10 },
                      },
                    },
                    y: {
                      title: { display: true, text: "Step Time (s)" },
                    },
                  },
                }}
              />
            </div>
          </Card>
        );
      })}
    </Box>
  );
}

function SummaryTab({ data, rack, logFile }: { data: ParserData; rack: string; logFile: string }) {
  const ncclRows = data.nccl_peak_out_of_place_busbw_gbps || [];
  const nemoRows = data.nemotron_5h_56b_train_window || [];
  const flags = data.parser_flags || [];

  return (
    <Box sx={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <Card style={{ padding: 16 }}>
        <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Overview</Text>
        <Flex gap="3" wrap="wrap">
          <div>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Rack</Text>
            <Text kind="body/semibold/md">{rack}</Text>
          </div>
          <div>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Log File</Text>
            <Text kind="mono/sm">{logFile.split("/").pop()}</Text>
          </div>
          {ncclRows.length > 0 && (
            <div>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>NCCL Peak bus_bw</Text>
              <Text kind="body/semibold/md">{ncclRows[0].peak_busbw_gbps} GB/s</Text>
            </div>
          )}
          {nemoRows.length > 0 && (
            <div>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>Median Step Time</Text>
              <Text kind="body/semibold/md">{nemoRows[0].median_step_time}s</Text>
            </div>
          )}
        </Flex>
      </Card>

      {flags.length > 0 && (
        <Card style={{ padding: 16 }}>
          <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Parser Flags</Text>
          <Flex gap="2" wrap="wrap">
            {flags.map((f, i) => (
              <Tag key={i} color="gray" kind="outline" readOnly density="compact">
                {f.flag}: {f.value}
              </Tag>
            ))}
          </Flex>
        </Card>
      )}
    </Box>
  );
}

export function ParserResultsDialog({ open, onClose, rack, jobId, logDir }: ParserResultsDialogProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<ParserResponse | null>(null);
  const [tabIndex, setTabIndex] = useState(0);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setError("");
    setResult(null);
    setTabIndex(0);

    const params = new URLSearchParams();
    if (rack) params.set("rack", rack);
    if (jobId && jobId !== "-") params.set("job_id", jobId);
    if (logDir) params.set("log_dir", logDir);

    fetch(`/api/parser-results?${params}`)
      .then((res) => res.json())
      .then((json) => {
        if (json.ok) {
          setResult(json);
        } else {
          setError(json.error || "Parser failed");
        }
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [open, rack, jobId, logDir]);

  const ncclRows = result?.data?.nccl_peak_out_of_place_busbw_gbps || [];
  const nemoRows = result?.data?.nemotron_5h_56b_train_window || [];

  return (
    <Dialog open={open} onClose={onClose} maxWidth="lg" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>
        Parser Results — {rack} {jobId && jobId !== "-" ? `(Job ${jobId})` : ""}
      </DialogTitle>
      <DialogContent dividers>
        {loading && (
          <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
            <CircularProgress />
          </Box>
        )}
        {error && (
          <div style={{ marginBottom: 16 }}>
            <Banner status="error">{error}</Banner>
          </div>
        )}
        {result && !loading && (
          <>
            <Tabs
              value={tabIndex}
              onChange={(_, v) => setTabIndex(v)}
              sx={{ borderBottom: 1, borderColor: "divider", mb: 3 }}
            >
              <Tab label="Summary" />
              <Tab label={`NCCL (${ncclRows.length})`} />
              <Tab label={`NEMO (${nemoRows.length})`} />
            </Tabs>
            {tabIndex === 0 && (
              <SummaryTab data={result.data} rack={result.rack} logFile={result.log_file} />
            )}
            {tabIndex === 1 && <NcclChart rows={ncclRows} />}
            {tabIndex === 2 && <StepTimeChart rows={nemoRows} />}
          </>
        )}
      </DialogContent>
      <DialogActions>
        <Button kind="tertiary" onClick={onClose}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}
