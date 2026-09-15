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
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import { Plus, Pencil, Trash2, CircuitBoard } from "lucide-react";
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
import {
  deletePlatform,
  getVocab,
  listPlatforms,
  savePlatform,
} from "../services/catalogService";
import { useActiveCluster } from "../context/ActiveClusterContext";
import type { CatalogVocab, Platform } from "../types/catalog";
import { csvToList, intOr, listToCsv, mapToText, textToMap } from "../lib/catalogForm";

const STATUS_COLOR: Record<string, "green" | "blue" | "gray" | "yellow"> = {
  ga: "green",
  preview: "blue",
  future: "yellow",
  deprecated: "gray",
};

interface DraftState {
  base: Partial<Platform>;
  capabilities: string;
  params: string;
}

function emptyDraft(): DraftState {
  return {
    base: {
      name: "",
      generation: "",
      gpu_model: "",
      cpu_vendor: "",
      system_vendor: "",
      form_factor: "",
      status: "ga",
      description: "",
      geometry: {
        gpus_per_node: 4,
        nodes_per_tray: 2,
        trays_per_rack: 18,
        gpus_per_rack: 72,
        nvlink_domain: 72,
      },
    },
    capabilities: "",
    params: "",
  };
}

export function PlatformCatalogView() {
  const { refresh: refreshActive, catalogChanged } = useActiveCluster();
  const [items, setItems] = useState<Platform[]>([]);
  const [vocab, setVocab] = useState<CatalogVocab | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      setLoading(true);
      const [plats, voc] = await Promise.all([listPlatforms(), getVocab()]);
      setItems(plats);
      setVocab(voc);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load platforms");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openNew = () => setDraft(emptyDraft());

  const openEdit = (p: Platform) =>
    setDraft({
      base: { ...p },
      capabilities: listToCsv(p.capabilities),
      params: mapToText(p.tool_param_defaults),
    });

  const onSave = async () => {
    if (!draft) return;
    try {
      setSaving(true);
      await savePlatform({
        ...draft.base,
        capabilities: csvToList(draft.capabilities),
        tool_param_defaults: textToMap(draft.params),
      });
      catalogChanged();
      setDraft(null);
      await load();
      await refreshActive();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save platform");
    } finally {
      setSaving(false);
    }
  };

  const onDelete = async (p: Platform) => {
    if (!window.confirm(`Delete platform "${p.name}"?`)) return;
    try {
      await deletePlatform(p.id);
      catalogChanged();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete platform");
    }
  };

  const setGeo = (key: keyof NonNullable<Platform["geometry"]>, val: string) =>
    setDraft((d) =>
      d
        ? {
            ...d,
            base: {
              ...d.base,
              geometry: {
                ...(d.base.geometry as Platform["geometry"]),
                [key]: intOr(val, 0),
              },
            },
          }
        : d,
    );

  const setField = (key: keyof Platform, val: string) =>
    setDraft((d) => (d ? { ...d, base: { ...d.base, [key]: val } } : d));

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Platform Catalog"
        description="Hardware/system SKUs (GPU, CPU, vendor, topology, geometry) that drive tool compatibility and dashboard geometry."
        contentId="platformCatalog"
        actions={
          <Button variant="contained" size="small" startIcon={<Plus size={16} />} onClick={openNew}>
            New platform
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
            <Skeleton key={i} style={{ height: 184 }} />
          ))}
        </Box>
      ) : items.length === 0 ? (
        <Card style={{ padding: 32 }}>
          <StatusMessage
            size="small"
            slotMedia={<CircuitBoard />}
            slotHeading="No platforms yet"
            slotSubheading="Create a platform SKU to drive tool compatibility and dashboard geometry."
          />
        </Card>
      ) : (
        <Box sx={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 2 }}>
          {items.map((p) => (
            <Card key={p.id} style={{ padding: 16 }}>
              <Flex justify="between" align="start" gap="2">
                <Text kind="body/bold/md">{p.name}</Text>
                <Tag color={STATUS_COLOR[p.status] || "gray"} kind="outline" readOnly density="compact">
                  {p.status}
                </Tag>
              </Flex>
              <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 2 }}>
                {p.id}
              </Text>
              <Text kind="body/regular/sm" style={{ display: "block", marginTop: 8 }}>
                {[p.gpu_model, p.cpu_vendor, p.system_vendor, p.form_factor].filter(Boolean).join(" · ")}
              </Text>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 4 }}>
                {p.geometry.gpus_per_rack} GPUs/rack · {p.geometry.trays_per_rack} trays · NVLink {p.geometry.nvlink_domain}
              </Text>
              <Flex gap="1" wrap="wrap" style={{ marginTop: 8 }}>
                {p.capabilities.map((c) => (
                  <Tag key={c} color="teal" kind="outline" readOnly density="compact">
                    {c}
                  </Tag>
                ))}
              </Flex>
              <Flex gap="2" style={{ marginTop: 12 }}>
                <Button variant="outlined" size="small" startIcon={<Pencil size={14} />} onClick={() => openEdit(p)}>
                  Edit
                </Button>
                <Button variant="outlined" color="error" size="small" startIcon={<Trash2 size={14} />} onClick={() => void onDelete(p)}>
                  Delete
                </Button>
              </Flex>
            </Card>
          ))}
        </Box>
      )}

      <Modal
        open={draft !== null}
        onOpenChange={(o) => {
          if (!o) setDraft(null);
        }}
        modal
        style={{ width: "760px", maxWidth: "92vw" }}
        slotHeading={draft?.base.id ? "Edit platform" : "New platform"}
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
            <FormField name="pf-name" slotLabel="Name">
              <TextInput value={draft?.base.name || ""} onValueChange={(v) => setField("name", v)} placeholder="e.g. GB200 NVL72" />
            </FormField>
            <FormField name="pf-generation" slotLabel="Generation">
              <TextInput value={draft?.base.generation || ""} onValueChange={(v) => setField("generation", v)} placeholder="e.g. Blackwell" />
            </FormField>
            <FormField name="pf-gpu" slotLabel="GPU model">
              <Select items={vocab?.gpu_models || []} value={draft?.base.gpu_model || ""} onValueChange={(v) => setField("gpu_model", v)} placeholder="Select" />
            </FormField>
            <FormField name="pf-cpu" slotLabel="CPU vendor">
              <Select items={vocab?.cpu_vendors || []} value={draft?.base.cpu_vendor || ""} onValueChange={(v) => setField("cpu_vendor", v)} placeholder="Select" />
            </FormField>
            <FormField name="pf-sysvendor" slotLabel="System vendor">
              <Select items={vocab?.system_vendors || []} value={draft?.base.system_vendor || ""} onValueChange={(v) => setField("system_vendor", v)} placeholder="Select" />
            </FormField>
            <FormField name="pf-formfactor" slotLabel="Form factor">
              <Select items={vocab?.form_factors || []} value={draft?.base.form_factor || ""} onValueChange={(v) => setField("form_factor", v)} placeholder="Select" />
            </FormField>
            <FormField name="pf-status" slotLabel="Status">
              <Select
                items={vocab?.statuses || ["ga", "preview", "future", "deprecated"]}
                value={draft?.base.status || "ga"}
                onValueChange={(v) => setField("status", v)}
                placeholder="Select"
              />
            </FormField>
          </Box>

          <Text kind="body/bold/sm" style={{ display: "block", marginTop: 4 }}>
            Geometry (per rack)
          </Text>
          <Box sx={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 1.5 }}>
            {([
              ["gpus_per_node", "GPUs/node"],
              ["nodes_per_tray", "Nodes/tray"],
              ["trays_per_rack", "Trays/rack"],
              ["gpus_per_rack", "GPUs/rack"],
              ["nvlink_domain", "NVLink domain"],
            ] as const).map(([key, label]) => (
              <FormField key={key} name={`pf-geo-${key}`} slotLabel={label}>
                <TextInput
                  inputMode="numeric"
                  value={(draft?.base.geometry?.[key] ?? 0).toString()}
                  onValueChange={(v) => setGeo(key, v)}
                />
              </FormField>
            ))}
          </Box>

          <FormField name="pf-caps" slotLabel="Capabilities" slotHelp="Comma-separated: nvlink, nvswitch, imex, mnnvl, roce">
            <TextInput value={draft?.capabilities || ""} onValueChange={(v) => setDraft((d) => (d ? { ...d, capabilities: v } : d))} placeholder="nvlink, nvswitch, imex" />
          </FormField>
          <FormField name="pf-params" slotLabel="Tool param defaults" slotHelp="One KEY=value per line">
            <TextArea value={draft?.params || ""} onValueChange={(v) => setDraft((d) => (d ? { ...d, params: v } : d))} resizeable="auto" placeholder={"CUDA_MAJ=12\nGPU_MODEL_FLAG=gb200"} />
          </FormField>
          <FormField name="pf-description" slotLabel="Description">
            <TextArea value={draft?.base.description || ""} onValueChange={(v) => setField("description", v)} resizeable="auto" placeholder="Optional notes about this SKU" />
          </FormField>
        </Flex>
      </Modal>
    </Box>
  );
}
