import { useState } from "react";
import Box from "@mui/material/Box";
import { Card, Flex, Tag, Text, StatusMessage } from "@nvidia/foundations-react-core";
import { CalendarX } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { PreviewBanner } from "../components/layout/PreviewBanner";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ScopeExplorer } from "../components/observability/ScopeExplorer";
import { MOCK_EVENTS, EVENT_META, type EventKind } from "../components/observability/mockData";

const KIND_ORDER: EventKind[] = ["job", "alert", "drain", "power", "lock"];

export function EventsView() {
  const [activeKinds, setActiveKinds] = useState<Set<EventKind>>(new Set(KIND_ORDER));

  const toggleKind = (kind: EventKind) =>
    setActiveKinds((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="obsEvents"
        title="Events"
        description="Notable state changes — drains, job lifecycle, power ops, locks, and alerts for the selected scope."
      />
      <PreviewBanner note="Will assemble from the command store, lock context, and Slurm/job lifecycle." />

      <ScopeExplorer statusNote="Newest first (mock)">
        {(selectedHosts) => {
          const hostSet = new Set(selectedHosts);
          const events = MOCK_EVENTS.filter((e) => hostSet.has(e.host)).filter((e) => activeKinds.has(e.kind));
          return (
            <>
              <Flex align="center" gap="2" wrap="wrap" style={{ marginBottom: 16 }}>
                <Text kind="body/bold/sm" style={{ color: "var(--text-color-secondary)" }}>
                  Filter
                </Text>
                {KIND_ORDER.map((kind) => {
                  const meta = EVENT_META[kind];
                  const on = activeKinds.has(kind);
                  return (
                    <button
                      key={kind}
                      type="button"
                      onClick={() => toggleKind(kind)}
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

              <Card style={{ padding: 16 }}>
                <Flex align="center" justify="between" style={{ marginBottom: 12 }}>
                  <Text kind="body/bold/md">Event timeline</Text>
                  <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                    {events.length} events
                  </Text>
                </Flex>
                {events.length === 0 ? (
                  <StatusMessage
                    size="small"
                    slotMedia={<CalendarX />}
                    slotHeading="No events"
                    slotSubheading={
                      selectedHosts.length === 0
                        ? "Select one or more hosts from the scope tree."
                        : "No events for the selected hosts with the current filters."
                    }
                  />
                ) : (
                  <Flex direction="col" gap="0">
                    {events.map((ev, i) => {
                      const meta = EVENT_META[ev.kind];
                      return (
                        <Flex
                          key={i}
                          align="center"
                          gap="3"
                          style={{
                            padding: "10px 0",
                            borderBottom:
                              i < events.length - 1
                                ? "1px solid var(--border-color-base, rgba(128,128,128,0.2))"
                                : "none",
                          }}
                        >
                          <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", minWidth: 64 }}>
                            {ev.ts}
                          </Text>
                          <Tag color={meta.color} kind="solid" readOnly density="compact">
                            {meta.label}
                          </Tag>
                          <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", minWidth: 72 }}>
                            {ev.host}
                          </Text>
                          <Text kind="body/regular/sm">{ev.text}</Text>
                        </Flex>
                      );
                    })}
                  </Flex>
                )}
              </Card>
            </>
          );
        }}
      </ScopeExplorer>
    </Box>
  );
}
