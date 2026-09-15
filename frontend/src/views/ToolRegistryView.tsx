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
import { deleteTool, getVocab, listPlatforms, listTools, saveTool } from "../services/catalogService";
import { MultiSelectCombobox } from "../components/MultiSelectCombobox";
import type { CatalogVocab, Platform, Tool } from "../types/catalog";
import { csvToList, listToCsv } from "../lib/catalogForm";
import { useActiveCluster } from "../context/ActiveClusterContext";

/**
 * How this tool picks its platforms, as one choice.
 *
 * The wire format is three fields — `match`, `capabilities`, `platform_ids` — resolved in
 * precedence order by `ToolCompatibility.matches`: a non-empty `platform_ids` decides, else a
 * non-empty `capabilities`, else `match`. Three independent inputs for one decision is what
 * made the form misleading: "Match" offered only "all platforms" and "none (disabled)", so it
 * read as the whole answer, while the field that actually selects platforms sat beside it as a
 * CSV box you had to type `gb200-nvl72` into from memory. Filling both left the Match value
 * silently ignored.
 *
 * So the form asks for the mode the backend already implies, and only the input that mode uses
 * is shown. `fromCompatibility` derives it in the same order the engine evaluates.
 */
type CompatMode = "all" | "platforms" | "capabilities" | "none";

const COMPAT_ITEMS: { value: CompatMode; children: string }[] = [
  { value: "all", children: "All platforms" },
  { value: "platforms", children: "Only selected platforms" },
  { value: "capabilities", children: "Platforms with capabilities" },
  { value: "none", children: "None (disabled)" },
];

function fromCompatibility(c?: Tool["compatible"]): CompatMode {
  if (c?.platform_ids?.length) return "platforms";
  if (c?.capabilities?.length) return "capabilities";
  return c?.match === "none" ? "none" : "all";
}

/**
 * Back to the wire shape, clearing the fields the chosen mode does not use.
 *
 * Clearing matters: a tool switched from "only selected platforms" to "all" that kept its
 * `platform_ids` would still be restricted, because a non-empty list wins over `match`. The
 * form would show "All platforms" and the tool would appear on one cluster.
 */
function toCompatibility(mode: CompatMode, platformIds: string[], capabilities: string[]) {
  if (mode === "platforms") return { match: "all", capabilities: [], platform_ids: platformIds };
  if (mode === "capabilities") return { match: "all", capabilities, platform_ids: [] };
  return { match: mode === "none" ? "none" : "all", capabilities: [], platform_ids: [] };
}

interface DraftState {
  base: Partial<Tool>;
  surfaces: string;
  compatMode: CompatMode;
  capabilities: string[];
  platformIds: string[];
  mode: string;
  target: string;
}

function toDraft(t?: Tool): DraftState {
  return {
    base: t
      ? { ...t }
      : { name: "", category: "validation", script: "", env_key: "", description: "", ai_context: "", status: "ga" },
    surfaces: listToCsv(t?.surfaces),
    compatMode: fromCompatibility(t?.compatible),
    capabilities: t?.compatible?.capabilities ?? [],
    platformIds: t?.compatible?.platform_ids ?? [],
    mode: t?.execution?.mode || "async",
    target: t?.execution?.target || "rack",
  };
}

export function ToolRegistryView() {
  // Announced on every write: the boards fetch their action list on mount, so without
  // this a tool registered here does not appear in "Add action" until a page reload.
  const { catalogChanged } = useActiveCluster();
  const [items, setItems] = useState<Tool[]>([]);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [vocab, setVocab] = useState<CatalogVocab | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      // Platforms come along because the compatibility editor offers them by name — the whole
      // point of this screen's fix. Loaded together so the modal never opens onto an empty list.
      const [tools, voc, plats] = await Promise.all([listTools(), getVocab(), listPlatforms()]);
      setItems(tools);
      setVocab(voc);
      setPlatforms(plats);
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

  /** Every platform in the catalog, labelled the way the Platform catalog lists it. */
  const platformItems = useMemo(
    () =>
      [...platforms]
        .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id))
        .map((p) => ({ value: p.id, children: p.name ? `${p.name} (${p.id})` : p.id })),
    [platforms],
  );

  /**
   * Capabilities, taken from the platforms that declare them rather than from a hard-coded
   * list. `/api/catalog/vocab` has no capability vocabulary, and inventing one here would put a
   * second answer beside the catalog's — a capability an operator adds to a platform would then
   * be unofferable to a tool.
   */
  const capabilityItems = useMemo(() => {
    const seen = new Set<string>();
    for (const p of platforms) for (const c of p.capabilities ?? []) if (c) seen.add(c);
    // A capability already on this tool stays offerable even if no platform declares it now,
    // or editing an untouched tool would silently drop it on save.
    for (const c of draft?.capabilities ?? []) if (c) seen.add(c);
    return [...seen].sort().map((c) => ({ value: c, children: c }));
  }, [platforms, draft?.capabilities]);

  /** The chosen names, for the tags under the selector. */
  const compatSummary = useMemo(() => {
    if (!draft) return null;
    if (draft.compatMode === "platforms" && draft.platformIds.length) {
      return draft.platformIds.map(
        (id) => platforms.find((p) => p.id === id)?.name || id,
      );
    }
    if (draft.compatMode === "capabilities" && draft.capabilities.length) return draft.capabilities;
    return null;
  }, [draft, platforms]);

  /**
   * "Only selected platforms" with nothing selected is the one combination that fails quietly:
   * an empty `platform_ids` falls through to `match`, which `toCompatibility` writes as "all",
   * so the tool would be offered *everywhere* — the exact opposite of what was asked for.
   * Refusing to save is the honest answer.
   */
  const compatProblem = useMemo(() => {
    if (!draft) return "";
    if (draft.compatMode === "platforms" && draft.platformIds.length === 0) {
      return "Select at least one platform, or choose “All platforms”.";
    }
    if (draft.compatMode === "capabilities" && draft.capabilities.length === 0) {
      return "Select at least one capability, or choose “All platforms”.";
    }
    return "";
  }, [draft]);

  const setField = (key: keyof Tool, val: string) =>
    setDraft((d) => (d ? { ...d, base: { ...d.base, [key]: val } } : d));
  const setDraftField = (patch: Partial<DraftState>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  const onSave = async () => {
    if (!draft || compatProblem) return;
    try {
      setSaving(true);
      await saveTool({
        ...draft.base,
        surfaces: csvToList(draft.surfaces),
        compatible: toCompatibility(draft.compatMode, draft.platformIds, draft.capabilities),
        execution: { mode: draft.mode, target: draft.target },
      });
      setDraft(null);
      await load();
      catalogChanged();
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
      catalogChanged();
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
            <KuiButton
              kind="primary"
              color="brand"
              disabled={saving || compatProblem !== ""}
              onClick={() => void onSave()}
            >
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
          <Box sx={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 2 }}>
            <FormField name="tool-match" slotLabel="Match">
              <Select
                items={COMPAT_ITEMS}
                value={draft?.compatMode || "all"}
                onValueChange={(v) => setDraftField({ compatMode: v as CompatMode })}
                placeholder="Select"
              />
            </FormField>

            {/* Only the input the chosen mode actually uses, because the others are ignored by
                the resolver and showing them invites filling in a field that does nothing. */}
            {draft?.compatMode === "platforms" ? (
              <FormField
                name="tool-platforms"
                slotLabel="Platforms"
                slotHelp={
                  platformItems.length
                    ? "From the Platform catalog"
                    : "No platforms in the catalog yet — add one in Platform catalog"
                }
              >
                <MultiSelectCombobox
                  items={platformItems}
                  value={draft.platformIds}
                  onValueChange={(v) => setDraftField({ platformIds: v })}
                  disabled={platformItems.length === 0}
                  placeholder="Select platforms"
                  formatSummaryLabel={(n) => (n === 1 ? "1 platform" : `${n} platforms`)}
                />
              </FormField>
            ) : null}

            {draft?.compatMode === "capabilities" ? (
              <FormField
                name="tool-caps"
                slotLabel="Required capabilities"
                slotHelp="Every selected capability must be present"
              >
                <MultiSelectCombobox
                  items={capabilityItems}
                  value={draft.capabilities}
                  onValueChange={(v) => setDraftField({ capabilities: v })}
                  disabled={capabilityItems.length === 0}
                  placeholder="Select capabilities"
                />
              </FormField>
            ) : null}
          </Box>

          {/* The names, spelled out. The trigger summarises as "2 platforms" so a dozen
              selections cannot push the layout around, which leaves the form unable to answer
              "which two?" without opening the list again. */}
          {compatSummary ? (
            <Flex gap="1" wrap="wrap">
              {compatSummary.map((label) => (
                <Tag key={label} color="blue" kind="outline" readOnly density="compact">
                  {label}
                </Tag>
              ))}
            </Flex>
          ) : null}

          {compatProblem ? (
            <Banner status="warning" kind="inline">
              {compatProblem}
            </Banner>
          ) : (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block" }}>
              {draft?.compatMode === "none"
                ? "The tool stays in the catalog but is offered on no cluster."
                : "Which clusters offer this tool. A platform the tool does not match simply never shows it."}
            </Text>
          )}

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
