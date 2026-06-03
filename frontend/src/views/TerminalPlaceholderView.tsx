import Box from "@mui/material/Box";
import { Card, Flex, Text, Button } from "@nvidia/foundations-react-core";
import { ExternalLink } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { PreviewBanner } from "../components/layout/PreviewBanner";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";

const MOCK_SESSION = [
  "$ ssh cmsupport@head-01",
  "Last login: Wed Jun  3 14:21:07 2026 from 10.32.4.18",
  "[cmsupport@head-01 ~]$ sinfo -p gpu",
  "PARTITION AVAIL  TIMELIMIT  NODES  STATE NODELIST",
  "gpu*         up   infinite      6   idle ct07-[01-06]",
  "[cmsupport@head-01 ~]$ pdsh -w ct07-[01-06] nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader",
  "ct07-01: 63, 64, 62, 65, 61, 63, 64, 62",
  "ct07-05: 78, 77, 76, 79, 75, 77, 78, 76",
  "[cmsupport@head-01 ~]$ _",
];

/**
 * Tools → Terminal preview. The framework places a head-node/compute Terminal in
 * the Tools domain (Section 8.2). A working web terminal already exists at the
 * `/terminal` route; this view previews the embedded-in-shell design and links to it.
 */
export function TerminalPlaceholderView() {
  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Terminal"
        description="Interactive head-node and compute shell, embedded in the tool's chrome."
        actions={
          <Button
            kind="secondary"
            size="small"
            onClick={() => window.open("/terminal", "_blank", "noopener,noreferrer")}
          >
            <Flex align="center" gap="2">
              <ExternalLink size={16} />
              Open web terminal
            </Flex>
          </Button>
        }
      />
      <PreviewBanner note="A live web terminal already runs at /terminal; this previews bringing it inside the app shell." />

      <Card style={{ padding: 0, overflow: "hidden" }}>
        <Flex
          align="center"
          gap="2"
          style={{
            padding: "10px 14px",
            borderBottom: "1px solid var(--border-color-base, rgba(128,128,128,0.25))",
          }}
        >
          <Box sx={{ display: "flex", gap: 0.75 }}>
            <Box sx={{ width: 12, height: 12, borderRadius: "50%", bgcolor: "#ff5f56" }} />
            <Box sx={{ width: 12, height: 12, borderRadius: "50%", bgcolor: "#ffbd2e" }} />
            <Box sx={{ width: 12, height: 12, borderRadius: "50%", bgcolor: "#27c93f" }} />
          </Box>
          <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
            cmsupport@head-01 — ssh
          </Text>
        </Flex>
        <Box
          sx={{
            p: 2,
            bgcolor: "#0c0c0c",
            color: "#d6d6d6",
            fontFamily: "monospace",
            fontSize: 13,
            lineHeight: 1.85,
            minHeight: 280,
          }}
        >
          {MOCK_SESSION.map((line, i) => (
            <div key={i} style={{ whiteSpace: "pre-wrap", color: line.startsWith("$") || line.includes("]$") ? "#76B900" : undefined }}>
              {line}
            </div>
          ))}
        </Box>
      </Card>
    </Box>
  );
}
