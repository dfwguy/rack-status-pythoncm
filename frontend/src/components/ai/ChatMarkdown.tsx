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
/**
 * An assistant reply, rendered as the GitHub-flavoured Markdown the model writes.
 *
 * This replaced ten regexes that knew code, bold, italic and lists and nothing else, so a table
 * arrived as a wall of pipes, `###` and `---` as literal text. It also fed its result to
 * ``dangerouslySetInnerHTML`` unescaped: a reply quoting a node name, a log line or a tool result
 * containing ``<img onerror=...>`` ran it in the page. Here ``marked`` parses (GFM: tables,
 * headings, rules, fenced code) and a private DOMPurify instance sanitizes before anything is
 * inserted -- both in lib/chatMarkdown.
 *
 * Loaded lazily by ChatMessage: the chat widget mounts with the app, and the parser is not needed
 * until the first assistant reply is drawn.
 */
import { useMemo } from "react";
import Box from "@mui/material/Box";
import { renderChatMarkdown } from "../../lib/chatMarkdown";

export default function ChatMarkdown({ text }: { text: string }) {
  const html = useMemo(() => renderChatMarkdown(text), [text]);
  return (
    <Box
      data-testid="chat-markdown"
      dangerouslySetInnerHTML={{ __html: html }}
      sx={{
        fontSize: 14,
        lineHeight: 1.6,
        "& > :first-child": { mt: 0 },
        "& > :last-child": { mb: 0 },
        "& p": { m: 0 },
        "& p + *, & * + p": { mt: 1 },
        "& h1, & h2, & h3, & h4": { fontWeight: 600, lineHeight: 1.3, mt: 1.5, mb: 0.75 },
        "& h1": { fontSize: 18 },
        "& h2": { fontSize: 16 },
        "& h3, & h4": { fontSize: 15 },
        "& hr": { border: 0, borderTop: 1, borderColor: "divider", my: 1.5 },
        "& ul, & ol": { pl: 2.5, my: 0.5 },
        "& li": { mb: 0.25 },
        "& strong": { fontWeight: 600 },
        "& a": { color: "primary.main" },
        "& blockquote": { m: 0, my: 1, pl: 1.5, borderLeft: 3, borderColor: "divider", color: "text.secondary" },
        "& code": { fontFamily: "var(--font-mono)", fontSize: 14 },
        "& :not(pre) > code": { bgcolor: "action.selected", px: 0.5, borderRadius: 0.5 },
        "& pre": {
          bgcolor: "grey.900",
          color: "grey.100",
          borderRadius: 1,
          p: 1,
          my: 1,
          overflow: "auto",
        },
        // A table keeps its columns and scrolls sideways inside the bubble rather than wrapping
        // every cell into a word per line. ``display: block`` is what lets a <table> scroll.
        "& table": {
          display: "block",
          maxWidth: "100%",
          overflowX: "auto",
          borderCollapse: "collapse",
          my: 1,
        },
        "& th, & td": {
          border: 1,
          borderColor: "divider",
          px: 1,
          py: 0.5,
          verticalAlign: "top",
          textAlign: "left",
        },
        "& th": { bgcolor: "action.selected", color: "text.primary", fontWeight: 600, whiteSpace: "nowrap" },
        // GFM column alignment (``|---:|``) arrives as an align attribute.
        "& [align=right]": { textAlign: "right" },
        "& [align=center]": { textAlign: "center" },
      }}
    />
  );
}
