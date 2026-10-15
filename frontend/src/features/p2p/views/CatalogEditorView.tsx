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
// Author: Rafael M. Koike
//
// Catalog editor: the per-cluster list of distributable images ("tasks"). Rack Status is the source
// of truth; a sync projects it onto the instance's catalog_path. Re-pinning here replaces the manual
// edit of images.json by hand.
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Card, Combobox, Flex, FormField, Text, TextInput } from "@nvidia/foundations-react-core";
import { PageHeader } from "../../../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../../../components/layout/contentWidth";
import { useActiveCluster } from "../../../context/ActiveClusterContext";
import { ApiError } from "../lib/apiFetch";
import { requiredByItems } from "../lib/requiredBy";
import { getCatalog, publishCatalogToScripts, saveCatalog, startOperation } from "../services/fleetService";
import type { CatalogImageEdit, ImageConsumer, ScriptsCatalogPublish } from "../services/fleetService";

/** How the launchers' copy of the catalog fared, as one sentence for the status banner. */
function describeScriptsPublish(published: ScriptsCatalogPublish | undefined): string {
  if (!published) return "";
  return published.ok
    ? ` Launchers now check node images against it (${published.dest}).`
    : ` The launchers' copy was NOT updated (${published.error ?? "unknown error"}); use "Publish to scripts" to retry.`;
}

const BLANK: CatalogImageEdit = { order: 0, name: "", filename: "", bytes: 0, sha256: "", required_by: [] };

export default function CatalogEditorView() {
  const { activeCluster, version } = useActiveCluster();
  const [images, setImages] = useState<CatalogImageEdit[] | null>(null);
  const [authored, setAuthored] = useState(false);
  const [consumers, setConsumers] = useState<ImageConsumer[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const doc = await getCatalog();
      setImages(doc.images.map((i) => ({ ...i, required_by: i.required_by ?? [] })));
      setAuthored(doc.authored);
      setConsumers(doc.consumers ?? []);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setError("P2P is not enabled for this cluster. Enable it in Projects & Clusters first.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load the catalog");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load, version]);

  const update = (idx: number, patch: Partial<CatalogImageEdit>) =>
    setImages((list) => (list ? list.map((img, i) => (i === idx ? { ...img, ...patch } : img)) : list));
  const remove = (idx: number) => setImages((list) => (list ? list.filter((_, i) => i !== idx) : list));
  const add = () =>
    setImages((list) => [...(list ?? []), { ...BLANK, order: (list?.length ?? 0) + 1 }]);

  const onSave = async () => {
    if (!images) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const saved = await saveCatalog(images);
      setImages(saved.images.map((i) => ({ ...i, required_by: i.required_by ?? [] })));
      setAuthored(true);
      setStatus(`Catalog saved. Run a sync to project it onto the fleet.${describeScriptsPublish(saved.scripts_catalog)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to save the catalog");
    } finally {
      setSaving(false);
    }
  };

  const onPublish = async () => {
    setError(null);
    setStatus(null);
    try {
      const published = await publishCatalogToScripts();
      setStatus(`Catalog published for the launchers to ${published.dest} (${published.images ?? 0} image(s)).`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to publish the catalog to the scripts");
    }
  };

  const onSync = async () => {
    setSyncing(true);
    setError(null);
    setStatus(null);
    try {
      const op = await startOperation({ action: "sync", sus: [] });
      setStatus(`Sync started (${op.id}). It writes the manifest and refreshes the nodes; watch its log on Artifact Fleet.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to start the catalog sync");
    } finally {
      setSyncing(false);
    }
  };

  const text = (label: string, value: string, onChange: (v: string) => void, help?: string, placeholder?: string) => (
    <FormField slotLabel={label} slotHelp={help}>
      <TextInput value={value} placeholder={placeholder} onValueChange={onChange} />
    </FormField>
  );

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Catalog — tasks"
        description={`The images this cluster distributes${activeCluster ? ` for ${activeCluster.name}` : ""}. Each is a task; its sha256 is the content pin every node verifies against.`}
        actions={
          <Flex gap="2" align="center">
            <Button kind="secondary" color="neutral" size="small" onClick={() => void load()}>
              Reload
            </Button>
            <Button kind="secondary" color="brand" size="small" disabled={saving || !images} onClick={() => void onSave()}>
              {saving ? "Saving…" : "Save"}
            </Button>
            <Button kind="secondary" color="neutral" size="small" disabled={!authored} onClick={() => void onPublish()}>
              Publish to scripts
            </Button>
            <Button kind="primary" color="brand" size="small" disabled={syncing || !authored} onClick={() => void onSync()}>
              {syncing ? "Syncing…" : "Sync to fleet"}
            </Button>
          </Flex>
        }
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {status ? <Banner status="success" kind="inline" style={{ marginBottom: 16 }}>{status}</Banner> : null}
      {loading ? <Text kind="body/regular/md">Loading…</Text> : null}

      {!loading && images ? (
        <Flex direction="col" gap="3">
          {!authored ? (
            <Banner status="info" kind="inline">
              Showing the cluster&rsquo;s current manifest. Save to make this the authored catalog Rack
              Status owns.
            </Banner>
          ) : null}

          {images.map((img, idx) => (
            <Card key={idx} style={{ padding: 16 }}>
              <Flex justify="between" align="center" style={{ marginBottom: 8 }}>
                <Text kind="body/bold/md">{img.name || img.filename || `Image ${idx + 1}`}</Text>
                <Button kind="secondary" color="danger" size="tiny" onClick={() => remove(idx)}>
                  Remove
                </Button>
              </Flex>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                {text("Name", img.name, (v) => update(idx, { name: v }), "Human-readable, e.g. Stock NeMo ARM")}
                {text("Filename", img.filename, (v) => update(idx, { filename: v }), "Canonical *.sqsh name", "nvidia+nemo+26.04.01-arm.sqsh")}
                {text("sha256", img.sha256, (v) => update(idx, { sha256: v.trim().toLowerCase() }), "64-hex content pin; blank if not yet imported")}
                {text("Bytes", String(img.bytes || ""), (v) => update(idx, { bytes: Number.parseInt(v, 10) || 0 }), "Exact byte count")}
                {text("Order", String(img.order || ""), (v) => update(idx, { order: Number.parseInt(v, 10) || 0 }))}
                <FormField
                  slotLabel="Required by"
                  slotHelp="The launches that run this image. A launch with no image here is refused with this fix."
                >
                  <Combobox
                    aria-label={`Required by for ${img.filename || `image ${idx + 1}`}`}
                    multiple
                    size="medium"
                    items={requiredByItems(consumers, img.required_by ?? [])}
                    value={img.required_by ?? []}
                    onValueChange={(v: string[]) => update(idx, { required_by: v })}
                    placeholder="Select the launches that run this image"
                  />
                </FormField>
              </div>
            </Card>
          ))}

          <Box>
            <Button kind="secondary" color="brand" size="small" onClick={add}>
              Add image
            </Button>
          </Box>
        </Flex>
      ) : null}
    </Box>
  );
}
