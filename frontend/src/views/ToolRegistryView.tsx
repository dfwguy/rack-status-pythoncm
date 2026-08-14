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
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import { Plus, Pencil, Trash2, Wrench } from "lucide-react";
import {
  Banner,
  Button as KuiButton,
  Card,
  Flex,
  FormField,
  Modal,
  ModalCloseButton,
  Select,
  Skeleton,
  StatusMessage,
  Tag,
  Text,
  TextArea,
  TextInput,
} from "@nvidia/foundations-react-core";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { deleteTool, getVocab, listTools, saveTool } from "../services/catalogService";
import type { CatalogVocab, Tool } from "../types/catalog";
import { csvToList, listToCsv } from "../lib/catalogForm";

interface DraftState {
  base: Partial<Tool>;
  surfaces: string;
  match: string;
  capabilities: string;
  platformIds: string;
  mode: string;
  target: string;
}

function toDraft(t?: Tool): DraftState {
  return {
    base: t
      ? { ...t }
      : { name: "", category: "validation", script: "", env_key: "", description: "", ai_context: "", status: "ga" },
    surfaces: listToCsv(t?.surfaces),
    match: t?.compatible?.match || "all",
    capabilities: listToCsv(t?.compatible?.capabilities),
    platformIds: listToCsv(t?.compatible?.platform_ids),
    mode: t?.execution?.mode || "async",
    target: t?.execution?.target || "rack",
  };
}

export function ToolRegistryView() {
  const [items, setItems] = useState<Tool[]>([]);
  const [vocab, setVocab] = useState<CatalogVocab | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const [tools, voc] = await Promise.all([listTools(), getVocab()]);
      setItems(tools);
      setVocab(voc);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load tools");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const grouped = useMemo(() => {
    const m = new Map<string, Tool[]>();
    for (const t of items) {
      const list = m.get(t.category) || [];
      list.push(t);
      m.set(t.category, list);
    }
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [items]);

  const setField = (key: keyof Tool, val: string) =>
    setDraft((d) => (d ? { ...d, base: { ...d.base, [key]: val } } : d));
  const setDraftField = (patch: Partial<DraftState>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  const onSave = async () => {
    if (!draft) return;
    try {
      setSaving(true);
      await saveTool({
        ...draft.base,
        surfaces: csvToList(draft.surfaces),
        compatible: {
          match: draft.match,
          capabilities: csvToList(draft.capabilities),
          platform_ids: csvToList(draft.platformIds),
        },
        execution: { mode: draft.mode, target: draft.target },
      });
      setDraft(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save tool");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (t: Tool) => {
    if (!window.confirm(`Delete tool "${t.name}"?`)) return;
    try {
      await deleteTool(t.id);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete tool");
    }
  };

  const compatLabel = (t: Tool): string => {
    if (t.compatible.platform_ids.length) return `platforms: ${t.compatible.platform_ids.join(", ")}`;
    if (t.compatible.capabilities.length) return `needs: ${t.compatible.capabilities.join(", ")}`;
    return t.compatible.match === "none" ? "disabled" : "all platforms";
  };

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Tool Registry"
        description="Each validation/performance/maintenance script as a document: its script, platform compatibility, and where it surfaces in the UI."
        contentId="toolRegistry"
        actions={
          <Button variant="contained" size="small" startIcon={<Plus size={16} />} onClick={() => setDraft(toDraft())}>
            New tool
          </Button>
        }
      />

      {error && (
        <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
          {error}
        </Banner>
      )}

      {loading ? (
        <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 2 }}>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} style={{ height: 168 }} />
          ))}
        </Box>
      ) : items.length === 0 ? (
        <Card style={{ padding: 32 }}>
          <StatusMessage
            size="small"
            slotMedia={<Wrench />}
            slotHeading="No tools registered"
            slotSubheading="Register a script as a tool to make it runnable and platform-aware."
          />
        </Card>
      ) : (
        grouped.map(([category, tools]) => (
          <Box key={category} sx={{ mb: 3 }}>
            <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8, textTransform: "capitalize" }}>
              {category}
            </Text>
            <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 2 }}>
              {tools.map((t) => (
                <Card key={t.id} style={{ padding: 16 }}>
                  <Flex justify="between" align="start" gap="2">
                    <Text kind="body/bold/md">{t.name}</Text>
                    <Tag color="blue" kind="outline" readOnly density="compact">
                      {t.execution.mode || "async"}
                    </Tag>
                  </Flex>
                  <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 2 }}>
                    {t.script}
                  </Text>
                  <Text kind="body/regular/sm" style={{ display: "block", marginTop: 8 }}>
                    {t.description || "—"}
                  </Text>
                  <Flex gap="1" wrap="wrap" style={{ marginTop: 8 }}>
                    <Tag color="teal" kind="outline" readOnly density="compact">{compatLabel(t)}</Tag>
                    {t.surfaces.map((s) => (
                      <Tag key={s} color="purple" kind="outline" readOnly density="compact">{s}</Tag>
                    ))}
                  </Flex>
                  <Flex gap="2" style={{ marginTop: 12 }}>
                    <Button variant="outlined" size="small" startIcon={<Pencil size={14} />} onClick={() => setDraft(toDraft(t))}>
                      Edit
                    </Button>
                    <Button variant="outlined" color="error" size="small" startIcon={<Trash2 size={14} />} onClick={() => void onDelete(t)}>
                      Delete
                    </Button>
                  </Flex>
                </Card>
              ))}
            </Box>
          </Box>
        ))
      )}

      <Modal
        open={draft !== null}
        onOpenChange={(o) => {
          if (!o) setDraft(null);
        }}
        modal
        style={{ width: "760px", maxWidth: "92vw" }}
        slotHeading={draft?.base.id ? "Edit tool" : "New tool"}
        slotFooter={
          <>
            <ModalCloseButton kind="secondary">Cancel</ModalCloseButton>
            <KuiButton kind="primary" color="brand" disabled={saving} onClick={() => void onSave()}>
              {saving ? "Saving…" : "Save"}
            </KuiButton>
          </>
        }
      >
        <Flex direction="col" gap="3">
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
            <FormField name="tool-name" slotLabel="Name">
              <TextInput value={draft?.base.name || ""} onValueChange={(v) => setField("name", v)} placeholder="e.g. Sysinfo" />
            </FormField>
            <FormField name="tool-category" slotLabel="Category">
              <Select items={vocab?.tool_categories || []} value={draft?.base.category || "validation"} onValueChange={(v) => setField("category", v)} placeholder="Select" />
            </FormField>
            <FormField name="tool-script" slotLabel="Script" slotHelp="Basename, e.g. sysinfo.sh">
              <TextInput value={draft?.base.script || ""} onValueChange={(v) => setField("script", v)} placeholder="sysinfo.sh" />
            </FormField>
            <FormField name="tool-envkey" slotLabel="Env key override" slotHelp="Optional, e.g. RACK_SYSINFO_SCRIPT">
              <TextInput value={draft?.base.env_key || ""} onValueChange={(v) => setField("env_key", v)} placeholder="(auto)" />
            </FormField>
            <FormField name="tool-mode" slotLabel="Execution mode">
              <Select items={vocab?.exec_modes || ["async", "sync", "stream"]} value={draft?.mode || "async"} onValueChange={(v) => setDraftField({ mode: v })} placeholder="Select" />
            </FormField>
            <FormField name="tool-target" slotLabel="Execution target">
              <Select items={vocab?.exec_targets || ["rack", "multirack", "cluster", "node"]} value={draft?.target || "rack"} onValueChange={(v) => setDraftField({ target: v })} placeholder="Select" />
            </FormField>
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginTop: 4 }}>
            Platform compatibility
          </Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 2 }}>
            <FormField name="tool-match" slotLabel="Match">
              <Select
                items={[
                  { value: "all", children: "all platforms" },
                  { value: "none", children: "none (disabled)" },
                ]}
                value={draft?.match || "all"}
                onValueChange={(v) => setDraftField({ match: v })}
                placeholder="Select"
              />
            </FormField>
            <FormField name="tool-caps" slotLabel="Required capabilities" slotHelp="CSV">
              <TextInput value={draft?.capabilities || ""} onValueChange={(v) => setDraftField({ capabilities: v })} placeholder="nvswitch, imex" />
            </FormField>
            <FormField name="tool-platforms" slotLabel="Platform ids" slotHelp="CSV, optional">
              <TextInput value={draft?.platformIds || ""} onValueChange={(v) => setDraftField({ platformIds: v })} placeholder="gb200-nvl72" />
            </FormField>
          </Box>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
            Resolution order: platform ids → capabilities → match. Leave all empty for all platforms.
          </Text>

          <FormField name="tool-surfaces" slotLabel="Surfaces" slotHelp="CSV, e.g. rackDetail.hardware, tools">
            <TextInput value={draft?.surfaces || ""} onValueChange={(v) => setDraftField({ surfaces: v })} placeholder="tools, rackDetail.hardware" />
          </FormField>
          <FormField name="tool-description" slotLabel="Description">
            <TextArea value={draft?.base.description || ""} onValueChange={(v) => setField("description", v)} resizeable="auto" placeholder="What this tool does" />
          </FormField>
          <FormField name="tool-aicontext" slotLabel="AI context hint" slotHelp="Extra guidance the assistant should know about this tool">
            <TextArea value={draft?.base.ai_context || ""} onValueChange={(v) => setField("ai_context", v)} resizeable="auto" placeholder="Optional" />
          </FormField>
        </Flex>
      </Modal>
    </Box>
  );
}
