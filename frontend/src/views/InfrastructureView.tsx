import { useCallback, useState } from "react";
import Box from "@mui/material/Box";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Paper from "@mui/material/Paper";
import { Banner, Button, Card, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { PreviewBanner } from "../components/layout/PreviewBanner";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { probeFabric, type FabricProbe } from "../services/fabricService";

export type InfraSegment = "ethernet" | "ib-nvlink" | "storage";

type HealthState = "ok" | "warn" | "down";

interface StatCard {
  label: string;
  value: string;
  state?: HealthState;
}

interface DeviceRow {
  name: string;
  type: string;
  detail: string;
  metric: string;
  state: HealthState;
}

interface SegmentConfig {
  title: string;
  description: string;
  stats: StatCard[];
  columns: [string, string, string, string, string];
  rows: DeviceRow[];
}

const STATE_TAG: Record<HealthState, { color: "green" | "yellow" | "red"; label: string }> = {
  ok: { color: "green", label: "Healthy" },
  warn: { color: "yellow", label: "Degraded" },
  down: { color: "red", label: "Down" },
};

const SEGMENTS: Record<InfraSegment, SegmentConfig> = {
  ethernet: {
    title: "Ethernet fabric",
    description: "Management and in-band Ethernet — leaf/spine switches, uplinks, and host ports.",
    stats: [
      { label: "Links up", value: "188 / 192", state: "warn" },
      { label: "Switches", value: "12" },
      { label: "Avg utilization", value: "37%" },
      { label: "Port errors (24h)", value: "4", state: "warn" },
    ],
    columns: ["Device", "Role", "Ports up", "Utilization", "Status"],
    rows: [
      { name: "spine-01", type: "Spine", detail: "32 / 32", metric: "41%", state: "ok" },
      { name: "spine-02", type: "Spine", detail: "32 / 32", metric: "39%", state: "ok" },
      { name: "leaf-07a", type: "Leaf", detail: "46 / 48", metric: "52%", state: "warn" },
      { name: "leaf-07b", type: "Leaf", detail: "48 / 48", metric: "44%", state: "ok" },
      { name: "mgmt-sw-1", type: "Management", detail: "30 / 32", metric: "12%", state: "ok" },
    ],
  },
  "ib-nvlink": {
    title: "InfiniBand / NVLink",
    description: "Compute fabric — InfiniBand links and switches plus intra-rack NVLink domains.",
    stats: [
      { label: "IB links up", value: "286 / 288", state: "warn" },
      { label: "NVLink domains", value: "9 / 9", state: "ok" },
      { label: "Fabric bandwidth", value: "11.2 PB/s" },
      { label: "Symbol errors (24h)", value: "1", state: "warn" },
    ],
    columns: ["Device", "Type", "Links up", "Bandwidth", "Status"],
    rows: [
      { name: "ibsw-spine-1", type: "IB Spine", detail: "64 / 64", metric: "780 Gb/s", state: "ok" },
      { name: "ibsw-leaf-07", type: "IB Leaf", detail: "62 / 64", metric: "742 Gb/s", state: "warn" },
      { name: "nvl-domain-07", type: "NVLink", detail: "18 / 18", metric: "540 GB/s", state: "ok" },
      { name: "nvl-domain-08", type: "NVLink", detail: "18 / 18", metric: "538 GB/s", state: "ok" },
      { name: "ct07-06:mlx5_4", type: "IB HCA", detail: "1 / 1", metric: "392 Gb/s", state: "warn" },
    ],
  },
  storage: {
    title: "Storage",
    description: "Shared filesystems and scratch — mounts, targets, throughput, and capacity.",
    stats: [
      { label: "Mounts healthy", value: "6 / 6", state: "ok" },
      { label: "Read throughput", value: "84 GB/s" },
      { label: "Write throughput", value: "61 GB/s" },
      { label: "Capacity used", value: "68%", state: "warn" },
    ],
    columns: ["Mount", "Type", "Targets", "Throughput", "Status"],
    rows: [
      { name: "/lustre/scratch", type: "Lustre", detail: "24 OSTs", metric: "84 GB/s", state: "ok" },
      { name: "/home", type: "NFS", detail: "1 export", metric: "3.1 GB/s", state: "ok" },
      { name: "/datasets", type: "GPFS", detail: "16 NSDs", metric: "47 GB/s", state: "ok" },
      { name: "/var/cache", type: "NFS", detail: "1 export", metric: "1.2 GB/s", state: "warn" },
      { name: "/checkpoints", type: "Lustre", detail: "12 OSTs", metric: "39 GB/s", state: "ok" },
    ],
  },
};

/**
 * Live fabric probe: classifies a representative compute node's RDMA devices into
 * the compute rails NCCL should use vs the storage/NS plane, and flags any
 * detected-vs-expected discrepancy from the active platform's fabric profile
 * (e.g. an 800G reference architecture that comes up at 400G).
 */
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

const SEGMENT_CONTENT_ID: Record<InfraSegment, string> = {
  ethernet: "infraEthernet",
  "ib-nvlink": "infraIbNvlink",
  storage: "infraStorage",
};

export function InfrastructureView({ segment }: InfrastructureViewProps) {
  const cfg = SEGMENTS[segment];

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader contentId={SEGMENT_CONTENT_ID[segment]} title={cfg.title} description={cfg.description} />
      <PreviewBanner note="Topology, counters, and health will be sourced from the platform fabric probes and Grafana." />

      {segment === "ib-nvlink" && <FabricProbeCard />}

      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
          gap: 2,
          mb: 3,
        }}
      >
        {cfg.stats.map((stat) => (
          <Card key={stat.label} style={{ padding: 16 }}>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
              {stat.label}
            </Text>
            <Flex align="center" gap="2" style={{ marginTop: 6 }}>
              <Text kind="title/lg">{stat.value}</Text>
              {stat.state && stat.state !== "ok" && (
                <Tag color={STATE_TAG[stat.state].color} kind="outline" readOnly density="compact">
                  {STATE_TAG[stat.state].label}
                </Tag>
              )}
            </Flex>
          </Card>
        ))}
      </Box>

      <TableContainer component={Paper} variant="outlined">
        <Table size="small">
          <TableHead>
            <TableRow>
              {cfg.columns.map((col) => (
                <TableCell key={col} sx={{ fontWeight: 700 }}>
                  {col}
                </TableCell>
              ))}
            </TableRow>
          </TableHead>
          <TableBody>
            {cfg.rows.map((row) => (
              <TableRow key={row.name} hover>
                <TableCell sx={{ fontFamily: "monospace" }}>{row.name}</TableCell>
                <TableCell>{row.type}</TableCell>
                <TableCell>{row.detail}</TableCell>
                <TableCell>{row.metric}</TableCell>
                <TableCell>
                  <Tag color={STATE_TAG[row.state].color} kind="solid" readOnly density="compact">
                    {STATE_TAG[row.state].label}
                  </Tag>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}
