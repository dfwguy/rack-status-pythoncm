import Box from "@mui/material/Box";
import { Card, Flex, Text, StatusMessage } from "@nvidia/foundations-react-core";
import { Boxes } from "lucide-react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Filler,
  Tooltip,
  Legend,
} from "chart.js";
import { Line } from "react-chartjs-2";
import { PageHeader } from "../components/layout/PageHeader";
import { PreviewBanner } from "../components/layout/PreviewBanner";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ScopeExplorer } from "../components/observability/ScopeExplorer";
import { METRIC_PANELS, makeSeries, timeLabels } from "../components/observability/mockData";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip, Legend);

const POINTS = 40;
const LABELS = timeLabels(POINTS);

export function MetricsView() {
  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="obsMetrics"
        title="Metrics"
        description="Live numeric time-series — GPU, NVLink, and fabric telemetry for the selected cluster scope."
      />
      <PreviewBanner note="Panels will render Grafana /d-solo embeds once wired; data shown is generated." />

      <ScopeExplorer statusNote="Live · 30s window · auto-refresh (mock)">
        {(selectedHosts) => {
          if (selectedHosts.length === 0) {
            return (
              <StatusMessage
                size="small"
                slotMedia={<Boxes />}
                slotHeading="Nothing selected"
                slotSubheading="Pick a cluster, rack, or node from the scope tree to view its metrics."
              />
            );
          }
          // Phase shifts with selection size so panels visibly re-focus on drill-in.
          const phase = selectedHosts.length * 0.35;
          return (
            <Box
              sx={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
                gap: 2,
              }}
            >
              {METRIC_PANELS.map((panel, idx) => {
                const data = makeSeries(panel.base, panel.amplitude, POINTS, phase + idx * 1.7);
                const latest = data[data.length - 1];
                return (
                  <Card key={panel.id} style={{ padding: 16 }}>
                    <Flex align="center" justify="between" style={{ marginBottom: 8 }}>
                      <Text kind="body/bold/md">{panel.title}</Text>
                      <Text kind="mono/sm" style={{ color: panel.color }}>
                        {latest} {panel.unit}
                      </Text>
                    </Flex>
                    <div style={{ height: 180 }}>
                      <Line
                        data={{
                          labels: LABELS,
                          datasets: [
                            {
                              data,
                              borderColor: panel.color,
                              backgroundColor: `${panel.color}22`,
                              borderWidth: 2,
                              fill: true,
                              pointRadius: 0,
                              tension: 0.35,
                            },
                          ],
                        }}
                        options={{
                          responsive: true,
                          maintainAspectRatio: false,
                          plugins: { legend: { display: false }, tooltip: { intersect: false, mode: "index" } },
                          scales: {
                            x: { grid: { display: false }, ticks: { maxTicksLimit: 6, font: { size: 10 } } },
                            y: { grid: { color: "rgba(128,128,128,0.15)" }, ticks: { font: { size: 10 } } },
                          },
                        }}
                      />
                    </div>
                    <Text
                      kind="body/regular/sm"
                      style={{ color: "var(--text-color-secondary)", marginTop: 8, display: "block" }}
                    >
                      Aggregated over {selectedHosts.length} node{selectedHosts.length === 1 ? "" : "s"} · last 20 min
                    </Text>
                  </Card>
                );
              })}
            </Box>
          );
        }}
      </ScopeExplorer>
    </Box>
  );
}
