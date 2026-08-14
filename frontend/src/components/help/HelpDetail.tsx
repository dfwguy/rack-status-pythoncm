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
import { Text, Tag, Flex, Anchor, Button } from "@nvidia/foundations-react-core";
import { Sparkles } from "lucide-react";
import { getContent, type HelpContent, type HelpKind } from "../../content";
import { useOptionalAIChat } from "../../context/AIChatContext";

const KIND_COLOR: Record<HelpKind, "blue" | "teal" | "purple" | "gray"> = {
  view: "blue",
  component: "teal",
  dialog: "purple",
  shell: "gray",
};

const KIND_LABEL: Record<HelpKind, string> = {
  view: "Page",
  component: "Component",
  dialog: "Dialog",
  shell: "Shell",
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginTop: 20 }}>
      <Text kind="title/sm" style={{ display: "block", marginBottom: 8 }}>
        {title}
      </Text>
      {children}
    </div>
  );
}

/**
 * Renders one knowledge-registry entry (description, capabilities, how-to,
 * FAQs, tips, related). `onSelectRelated` lets the host navigate between topics
 * without leaving the panel.
 */
export function HelpDetail({
  entry,
  onSelectRelated,
}: {
  entry: HelpContent;
  onSelectRelated?: (id: string) => void;
}) {
  const ai = useOptionalAIChat();

  const askAI = () => {
    if (!ai) return;
    ai.setOpen(true);
    ai.setMinimized(false);
    ai.sendMessage(`How do I use "${entry.title}"? ${entry.summary}`);
  };

  return (
    <div>
      <Flex gap="2" align="center" wrap="wrap" style={{ marginBottom: 8 }}>
        <Text kind="title/lg" style={{ display: "block" }}>
          {entry.title}
        </Text>
        <Tag color={KIND_COLOR[entry.kind]} kind="outline" readOnly density="compact">
          {KIND_LABEL[entry.kind]}
        </Tag>
        {entry.domain && (
          <Tag color="gray" kind="outline" readOnly density="compact">
            {entry.domain}
          </Tag>
        )}
      </Flex>

      <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)", display: "block" }}>
        {entry.summary}
      </Text>

      <Text kind="body/regular/md" style={{ display: "block", marginTop: 12 }}>
        {entry.description}
      </Text>

      {ai && (
        <Flex gap="2" style={{ marginTop: 16 }}>
          <Button kind="secondary" size="small" onClick={askAI}>
            <Sparkles size={16} />
            Ask the AI about this
          </Button>
        </Flex>
      )}

      {entry.features.length > 0 && (
        <Section title="What it does">
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {entry.features.map((f, i) => (
              <li key={i}>
                <Text kind="body/regular/md">{f}</Text>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {entry.howTo.length > 0 && (
        <Section title="How to">
          <Flex direction="col" gap="3">
            {entry.howTo.map((h, i) => (
              <div key={i}>
                <Text kind="body/bold/md" style={{ display: "block", marginBottom: 4 }}>
                  {h.task}
                </Text>
                <ol style={{ margin: 0, paddingLeft: 20 }}>
                  {h.steps.map((s, j) => (
                    <li key={j}>
                      <Text kind="body/regular/md">{s}</Text>
                    </li>
                  ))}
                </ol>
              </div>
            ))}
          </Flex>
        </Section>
      )}

      {entry.faqs && entry.faqs.length > 0 && (
        <Section title="FAQ">
          <Flex direction="col" gap="3">
            {entry.faqs.map((f, i) => (
              <div key={i}>
                <Text kind="body/bold/md" style={{ display: "block" }}>
                  {f.q}
                </Text>
                <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
                  {f.a}
                </Text>
              </div>
            ))}
          </Flex>
        </Section>
      )}

      {entry.tips && entry.tips.length > 0 && (
        <Section title="Tips">
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            {entry.tips.map((t, i) => (
              <li key={i}>
                <Text kind="body/regular/md">{t}</Text>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {entry.related && entry.related.length > 0 && (
        <Section title="Related">
          <Flex gap="2" wrap="wrap">
            {entry.related
              .map((id) => getContent(id))
              .filter((c): c is HelpContent => Boolean(c))
              .map((c) => (
                <Anchor
                  key={c.id}
                  href={`#help/${encodeURIComponent(c.id)}`}
                  onClick={(e) => {
                    if (onSelectRelated) {
                      e.preventDefault();
                      onSelectRelated(c.id);
                    }
                  }}
                >
                  {c.title}
                </Anchor>
              ))}
          </Flex>
        </Section>
      )}
    </div>
  );
}

export default HelpDetail;
