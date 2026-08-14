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
import { useMemo, useState, type CSSProperties } from "react";
import { SidePanel, Text, TextInput, Divider, Button, Flex } from "@nvidia/foundations-react-core";
import { ArrowLeft, Search } from "lucide-react";
import { ALL_CONTENT, searchContent, getContent, type HelpContent } from "../../content";
import { useHelpPanel } from "../../context/HelpPanelContext";
import { HelpDetail } from "./HelpDetail";

/** Groups entries by domain, preserving first-seen order. */
function groupByDomain(entries: HelpContent[]): Array<{ domain: string; entries: HelpContent[] }> {
  const order: string[] = [];
  const map = new Map<string, HelpContent[]>();
  for (const entry of entries) {
    const domain = entry.domain ?? "Other";
    if (!map.has(domain)) {
      map.set(domain, []);
      order.push(domain);
    }
    map.get(domain)!.push(entry);
  }
  return order.map((domain) => ({ domain, entries: map.get(domain)! }));
}

export function HelpPanel() {
  const { isOpen, selectedId, closeHelp, selectTopic } = useHelpPanel();
  const [query, setQuery] = useState("");

  const results = useMemo(() => (query.trim() ? searchContent(query) : ALL_CONTENT), [query]);
  const grouped = useMemo(() => groupByDomain(results), [results]);

  const selected = selectedId ? getContent(selectedId) : null;
  const inDetail = Boolean(selected) && !query.trim();

  return (
    <SidePanel
      side="right"
      bordered
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) closeHelp();
      }}
      slotHeading="Help"
      style={{ "--side-panel-width": "540px" } as CSSProperties}
      slotNavigation={
        <Flex direction="col" gap="2" style={{ width: "100%" }}>
          <TextInput
            type="search"
            aria-label="Search help"
            placeholder="Search help…"
            value={query}
            onValueChange={setQuery}
            dismissible
            slotStart={<Search size={16} />}
          />
          {inDetail && (
            <div>
              <Button kind="secondary" size="small" color="neutral" onClick={() => selectTopic(null)}>
                <ArrowLeft size={16} />
                All topics
              </Button>
            </div>
          )}
        </Flex>
      }
    >
      {inDetail && selected ? (
        <HelpDetail entry={selected} onSelectRelated={(id) => selectTopic(id)} />
      ) : (
        <>
          {grouped.length === 0 && (
            <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
              No topics match “{query}”.
            </Text>
          )}
          {grouped.map((group) => (
            <div key={group.domain} style={{ marginBottom: 16 }}>
              <Text
                kind="body/bold/sm"
                style={{ display: "block", color: "var(--text-color-subtle)", marginBottom: 4 }}
              >
                {group.domain}
              </Text>
              {group.entries.map((entry) => (
                <a
                  key={entry.id}
                  href={`#help/${encodeURIComponent(entry.id)}`}
                  onClick={(e) => {
                    e.preventDefault();
                    setQuery("");
                    selectTopic(entry.id);
                  }}
                  style={{
                    display: "block",
                    padding: "8px 10px",
                    borderRadius: 6,
                    textDecoration: "none",
                  }}
                >
                  <Text kind="body/regular/md" style={{ color: "var(--text-color-primary)" }}>
                    {entry.title}
                  </Text>
                  <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
                    {entry.summary}
                  </Text>
                </a>
              ))}
              <Divider style={{ marginTop: 12 }} />
            </div>
          ))}
        </>
      )}
    </SidePanel>
  );
}

export default HelpPanel;
