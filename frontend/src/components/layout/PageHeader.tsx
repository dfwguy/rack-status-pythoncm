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
import type { ReactNode } from "react";
import { Flex, Text, Button } from "@nvidia/foundations-react-core";
import { CircleHelp, Sparkles } from "lucide-react";
import { getContent } from "../../content";
import { useOptionalAIChat } from "../../context/AIChatContext";
import { useOptionalHelpPanel } from "../../context/HelpPanelContext";
import { VisuallyHidden } from "../a11y/VisuallyHidden";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
  /**
   * Knowledge-registry id for this page. When provided, the header exposes a
   * contextual help link and an "Ask AI about this page" action, and adds an
   * accessible description (announced to screen readers) sourced from the
   * registry.
   */
  contentId?: string;
}

export function PageHeader({ title, description, actions, contentId }: PageHeaderProps) {
  const content = contentId ? getContent(contentId) : undefined;
  const ai = useOptionalAIChat();
  const helpPanel = useOptionalHelpPanel();

  const descriptionId = contentId ? `page-desc-${contentId}` : undefined;
  const ariaDescription = content?.aria.description;

  const askAI = () => {
    if (!content || !ai) return;
    ai.setOpen(true);
    ai.setMinimized(false);
    ai.sendMessage(`How do I use the ${content.title} page? ${content.summary}`);
  };

  return (
    <Flex
      gap="3"
      justify="between"
      align="start"
      wrap="wrap"
      style={{ marginBottom: 24 }}
      aria-label={content ? content.aria.label : undefined}
      aria-describedby={ariaDescription ? descriptionId : undefined}
    >
      <div>
        <Text kind="display/sm" style={{ display: "block" }}>{title}</Text>
        {description && (
          <Text kind="body/regular/lg" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 6 }}>
            {description}
          </Text>
        )}
        {ariaDescription && (
          <VisuallyHidden id={descriptionId} live="polite">
            {ariaDescription}
          </VisuallyHidden>
        )}
      </div>
      <Flex gap="2" align="center" style={{ flexShrink: 0 }}>
        {content && (
          <>
            {ai && (
              <Button
                kind="secondary"
                size="small"
                color="neutral"
                onClick={askAI}
                title="Ask the AI about this page"
                aria-label="Ask the AI about this page"
              >
                <Sparkles size={16} />
                Ask AI
              </Button>
            )}
            {helpPanel && (
              <Button
                kind="secondary"
                size="small"
                color="neutral"
                onClick={() => helpPanel.openHelp(content.id)}
                title="Open help for this page"
                aria-label="Open help for this page"
              >
                <CircleHelp size={16} />
                Help
              </Button>
            )}
          </>
        )}
        {actions}
      </Flex>
    </Flex>
  );
}
