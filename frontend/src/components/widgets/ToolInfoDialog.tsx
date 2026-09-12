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
// ToolInfoDialog — everything the catalog knows about one action.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The board's cards are deliberately one line of description each, because a wall of
// variable-height tiles is harder to scan than the fixed list it replaced. That trade only
// works if the detail is still reachable, and this is where it goes: the whole Tool document,
// including the parts an operator needs when a run goes wrong — which script actually executes,
// which endpoint it posts to, which recipe id, what the platform pins.
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import { Banner, Flex, Tag, Text } from "@nvidia/foundations-react-core";
import type { Tool } from "../../types/catalog";
import type { BoardAction } from "./boardActions";
import { planToolLaunch, statusUrlFor } from "../../lib/toolLaunch";

interface ToolInfoDialogProps {
  action: BoardAction | null;
  onClose: () => void;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 12, padding: "6px 0", alignItems: "baseline" }}>
      <Text
        kind="body/regular/sm"
        style={{ color: "var(--text-color-subtle)", minWidth: 132, flexShrink: 0 }}
      >
        {label}
      </Text>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
    </div>
  );
}

function Mono({ children }: { children: React.ReactNode }) {
  return (
    <Text kind="mono/sm" style={{ wordBreak: "break-all" }}>
      {children}
    </Text>
  );
}

/** How this action starts, in the operator's terms rather than the document's. */
function launchSummary(tool: Tool): { label: string; detail: string } {
  const plan = planToolLaunch(tool);
  switch (plan.kind) {
    case "validation-preset":
      return {
        label: "Audited recipe",
        detail: `preset "${plan.presetId}" — prepared and reviewed before launch`,
      };
    case "async-job":
      return {
        label: "Background job",
        detail: `POST ${plan.endpoint}, polled at ${statusUrlFor(plan.endpoint, "<job>")}`,
      };
    case "stream":
      return { label: "Streamed output", detail: `GET ${plan.endpoint}` };
    case "dialog":
      return { label: "Opens a dialog", detail: plan.dialog };
    default:
      return { label: "Not launchable", detail: plan.reason };
  }
}

/**
 * Details for a workflow.
 *
 * Separate from the tool view rather than a pile of conditionals in it: almost none of the
 * tool rows have a workflow equivalent — there is no script, no env override, no platform
 * compatibility — and what a workflow *does* have (steps, version, the designer) has no tool
 * equivalent either. Two small renderings beat one that is mostly `null`.
 */
function WorkflowInfo({ action, onClose }: { action: BoardAction; onClose: () => void }) {
  const wf = action.workflow;
  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600, pb: 1 }}>
        {action.name}
        <Text
          kind="body/regular/sm"
          style={{ display: "block", color: "var(--text-color-subtle)", marginTop: 2 }}
        >
          {action.subtitle}
        </Text>
      </DialogTitle>
      <DialogContent dividers>
        <Flex gap="2" wrap="wrap" style={{ marginBottom: 12 }}>
          <Tag color="teal" kind="outline" readOnly density="compact">
            workflow
          </Tag>
          <Tag color="gray" kind="outline" readOnly density="compact">
            site-authored
          </Tag>
        </Flex>
        {action.description ? (
          <Text kind="body/regular/md" style={{ display: "block", marginBottom: 12 }}>
            {action.description}
          </Text>
        ) : null}
        <Row label="How it runs">
          <Text kind="body/regular/sm">Workflow engine</Text>
          <Mono>POST /api/workflows/{action.id}/run</Mono>
        </Row>
        <Row label="Steps">
          <Text kind="body/regular/sm">{wf ? String(wf.step_count) : "—"}</Text>
        </Row>
        <Row label="Version">
          <Text kind="body/regular/sm">{wf ? `v${wf.version}` : "—"}</Text>
        </Row>
        <Row label="Workflow id">
          <Mono>{action.id}</Mono>
        </Row>
        <Banner status="info" kind="inline">
          Runs persist under the workflow engine and survive a restart. Edit the flow in
          Tools → Workflows; this board launches whatever version is saved there.
        </Banner>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton onClick={onClose} color="inherit">
          Close
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}

export function ToolInfoDialog({ action, onClose }: ToolInfoDialogProps) {
  if (!action) return null;
  // A workflow has no catalog document behind it, so the tool-shaped rows below do not apply.
  if (action.kind === "workflow") return <WorkflowInfo action={action} onClose={onClose} />;
  const tool = action.tool;
  if (!tool) return null;
  const launch = launchSummary(tool);
  const compat = tool.compatible;
  const compatText = compat?.platform_ids?.length
    ? `only: ${compat.platform_ids.join(", ")}`
    : compat?.capabilities?.length
      ? `platforms with: ${compat.capabilities.join(", ")}`
      : compat?.match === "none"
        ? "no platforms"
        : "every platform";
  const bounds =
    tool.launch?.min_nodes || tool.launch?.max_nodes
      ? tool.launch.min_nodes === tool.launch.max_nodes
        ? `exactly ${tool.launch.min_nodes}`
        : `${tool.launch.min_nodes || 1}–${tool.launch.max_nodes || "any"}`
      : "any number";

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600, pb: 1 }}>
        {tool.name}
        {tool.subtitle ? (
          <Text
            kind="body/regular/sm"
            style={{ display: "block", color: "var(--text-color-subtle)", marginTop: 2 }}
          >
            {tool.subtitle}
          </Text>
        ) : null}
      </DialogTitle>
      <DialogContent dividers>
        <Flex gap="2" wrap="wrap" style={{ marginBottom: 12 }}>
          <Tag color="gray" kind="outline" readOnly density="compact">
            {tool.category}
          </Tag>
          {tool.status && tool.status !== "ga" ? (
            <Tag color="yellow" kind="outline" readOnly density="compact">
              {tool.status}
            </Tag>
          ) : null}
          {tool.destructive ? (
            <Tag color="red" kind="outline" readOnly density="compact">
              destructive
            </Tag>
          ) : null}
          {/* Provenance matters when a value looks wrong: a seeded document is ours and will be
              refreshed on boot, an edited one is the site's and will not be. */}
          <Tag color="gray" kind="outline" readOnly density="compact">
            {tool.source === "seed" ? "built-in" : "site-edited"}
          </Tag>
        </Flex>

        {tool.description ? (
          <Text kind="body/regular/md" style={{ display: "block", marginBottom: 12 }}>
            {tool.description}
          </Text>
        ) : null}

        {launch.label === "Not launchable" ? (
          <Banner status="warning" kind="inline">
            {launch.detail}
          </Banner>
        ) : null}

        <Row label="How it runs">
          <Text kind="body/regular/sm">{launch.label}</Text>
          <Mono>{launch.detail}</Mono>
        </Row>
        <Row label="Script">
          <Mono>{tool.script || "—"}</Mono>
        </Row>
        {tool.env_key ? (
          <Row label="Path override">
            <Mono>{tool.env_key}</Mono>
          </Row>
        ) : null}
        <Row label="Scope">
          <Text kind="body/regular/sm">
            {tool.launch?.scopes?.length
              ? tool.launch.scopes.join(", ")
              : tool.execution?.target || "—"}
          </Text>
        </Row>
        <Row label="Nodes">
          <Text kind="body/regular/sm">{bounds}</Text>
        </Row>
        <Row label="Runs on">
          <Text kind="body/regular/sm">{compatText}</Text>
        </Row>
        {tool.params?.length ? (
          <Row label="Parameters">
            <div>
              {tool.params.map((p) => (
                <div key={p.key} style={{ marginBottom: 4 }}>
                  <Mono>
                    {p.key}
                    {p.default ? `=${p.default}` : ""}
                  </Mono>
                  {p.description ? (
                    <Text
                      kind="body/regular/sm"
                      style={{ display: "block", color: "var(--text-color-subtle)" }}
                    >
                      {p.description}
                    </Text>
                  ) : null}
                </div>
              ))}
            </div>
          </Row>
        ) : null}
        {tool.surfaces?.length ? (
          <Row label="Shown on">
            <Mono>{tool.surfaces.join(", ")}</Mono>
          </Row>
        ) : null}
        <Row label="Catalog id">
          <Mono>{tool.id}</Mono>
        </Row>
        {tool.ai_context ? (
          <Row label="Notes">
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {tool.ai_context}
            </Text>
          </Row>
        ) : null}
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <MuiButton onClick={onClose} color="inherit">
          Close
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
