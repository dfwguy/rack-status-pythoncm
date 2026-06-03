import { useState } from "react";
import Box from "@mui/material/Box";
import { Card, Flex, Text, SegmentedControl, StatusMessage } from "@nvidia/foundations-react-core";
import { FileSearch } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { PreviewBanner } from "../components/layout/PreviewBanner";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ScopeExplorer } from "../components/observability/ScopeExplorer";
import { MOCK_LOGS, type LogLevel } from "../components/observability/mockData";

// Fixed console palette so the log stream stays high-contrast on a dark terminal
// surface in BOTH light and dark app themes (theme tokens are avoided here because
// the console background is always dark). Verified ≥ AA contrast on #0c0c0c.
const CONSOLE_BG = "#0c0c0c";
const CONSOLE_TEXT = "#dcdcdc";
const CONSOLE_MUTED = "#9aa4af";
const CONSOLE_HOST = "#9ccc3b";

const LEVEL_ITEMS = [
  { value: "all", children: "All" },
  { value: "info", children: "Info" },
  { value: "warn", children: "Warn" },
  { value: "error", children: "Error" },
];

function levelColor(level: LogLevel): string {
  return level === "error" ? "#ff8a80" : level === "warn" ? "#ffd54f" : CONSOLE_MUTED;
}

export function LogsView() {
  const [level, setLevel] = useState("all");

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        title="Logs"
        description="Validation, job, and tail output — discrete timestamped records for the selected cluster scope."
      />
      <PreviewBanner note="Will stream parser.sh --json, job logs, and tail output for the selected hosts." />

      <ScopeExplorer statusNote="Tailing · follow (mock)">
        {(selectedHosts) => {
          const hostSet = new Set(selectedHosts);
          const lines = MOCK_LOGS.filter((l) => hostSet.has(l.host)).filter(
            (l) => level === "all" || l.level === level
          );
          return (
            <>
              <Flex align="center" gap="2" style={{ marginBottom: 16 }}>
                <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
                  Level
                </Text>
                <SegmentedControl size="small" items={LEVEL_ITEMS} value={level} onValueChange={setLevel} />
              </Flex>

              <Card style={{ padding: 0, overflow: "hidden" }}>
                <Box
                  sx={{
                    p: 1.5,
                    borderBottom: "1px solid var(--border-color-base, rgba(128,128,128,0.25))",
                    display: "flex",
                    justifyContent: "space-between",
                  }}
                >
                  <Text kind="body/bold/sm">Validation & job logs</Text>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {lines.length} lines
                  </Text>
                </Box>
                {lines.length === 0 ? (
                  <Box sx={{ p: 4 }}>
                    <StatusMessage
                      size="small"
                      slotMedia={<FileSearch />}
                      slotHeading="No matching log lines"
                      slotSubheading={
                        selectedHosts.length === 0
                          ? "Select one or more hosts from the scope tree."
                          : `No ${level !== "all" ? `${level} ` : ""}entries for the selected hosts in this window.`
                      }
                    />
                  </Box>
                ) : (
                  <Box
                    sx={{
                      p: 2,
                      bgcolor: CONSOLE_BG,
                      color: CONSOLE_TEXT,
                      fontFamily: "monospace",
                      fontSize: 13,
                      lineHeight: 1.8,
                    }}
                  >
                    {lines.map((log, i) => (
                      <div key={i} style={{ whiteSpace: "pre-wrap" }}>
                        <span style={{ color: CONSOLE_MUTED }}>{log.ts}</span>{" "}
                        <span style={{ color: levelColor(log.level), fontWeight: 600 }}>
                          {log.level.toUpperCase().padEnd(5)}
                        </span>{" "}
                        <span style={{ color: CONSOLE_HOST }}>{log.host}</span> <span>{log.msg}</span>
                      </div>
                    ))}
                  </Box>
                )}
              </Card>
            </>
          );
        }}
      </ScopeExplorer>
    </Box>
  );
}
