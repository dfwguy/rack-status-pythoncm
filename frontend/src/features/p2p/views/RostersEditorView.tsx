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
// Rosters editor: named node lists an operation can target by name instead of the single inventory.
// Per-cluster, stored by Rack Status.
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Card, Flex, FormField, Text, TextInput } from "@nvidia/foundations-react-core";
import { PageHeader } from "../../../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../../../components/layout/contentWidth";
import { useActiveCluster } from "../../../context/ActiveClusterContext";
import { ApiError } from "../lib/apiFetch";
import { getRosters, saveRosters } from "../services/fleetService";
import type { RosterEdit } from "../services/fleetService";

export default function RostersEditorView() {
  const { activeCluster, version } = useActiveCluster();
  const [rosters, setRosters] = useState<RosterEdit[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const doc = await getRosters();
      setRosters(doc.rosters.map((r) => ({ ...r, nodes: r.nodes ?? [] })));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setError("P2P is not enabled for this cluster. Enable it in Projects & Clusters first.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load rosters");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load, version]);

  const update = (idx: number, patch: Partial<RosterEdit>) =>
    setRosters((list) => (list ? list.map((r, i) => (i === idx ? { ...r, ...patch } : r)) : list));
  const remove = (idx: number) => setRosters((list) => (list ? list.filter((_, i) => i !== idx) : list));
  const add = () => setRosters((list) => [...(list ?? []), { name: "", description: "", nodes: [] }]);

  const onSave = async () => {
    if (!rosters) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const saved = await saveRosters(rosters);
      setRosters(saved.rosters.map((r) => ({ ...r, nodes: r.nodes ?? [] })));
      setStatus("Rosters saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to save rosters");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Rosters"
        description={`Named node lists${activeCluster ? ` for ${activeCluster.name}` : ""} an operation can target by name instead of the whole inventory.`}
        actions={
          <Flex gap="2" align="center">
            <Button kind="secondary" color="neutral" size="small" onClick={() => void load()}>
              Reload
            </Button>
            <Button kind="secondary" color="brand" size="small" disabled={saving || !rosters} onClick={() => void onSave()}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </Flex>
        }
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {status ? <Banner status="success" kind="inline" style={{ marginBottom: 16 }}>{status}</Banner> : null}
      {loading ? <Text kind="body/regular/md">Loading…</Text> : null}

      {!loading && rosters ? (
        <Flex direction="col" gap="3">
          {rosters.length === 0 ? (
            <Banner status="info" kind="inline">No rosters yet. Add one to target a subset of the fleet.</Banner>
          ) : null}

          {rosters.map((roster, idx) => (
            <Card key={idx} style={{ padding: 16 }}>
              <Flex justify="between" align="center" style={{ marginBottom: 8 }}>
                <Text kind="body/bold/md">{roster.name || `Roster ${idx + 1}`} · {roster.nodes.length} node(s)</Text>
                <Button kind="secondary" color="danger" size="tiny" onClick={() => remove(idx)}>
                  Remove
                </Button>
              </Flex>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <FormField slotLabel="Name" slotHelp="Unique name, e.g. SU19 or verified">
                  <TextInput value={roster.name} onValueChange={(v) => update(idx, { name: v })} />
                </FormField>
                <FormField slotLabel="Description">
                  <TextInput value={roster.description} onValueChange={(v) => update(idx, { description: v })} />
                </FormField>
              </div>
              <FormField slotLabel="Nodes" slotHelp="One hostname per line">
                <Box
                  component="textarea"
                  value={roster.nodes.join("\n")}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                    update(idx, { nodes: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })
                  }
                  sx={{
                    width: "100%", minHeight: 120, mt: 0.5, p: 1, fontFamily: "ui-monospace, monospace",
                    fontSize: 13, borderRadius: 1, border: "1px solid var(--color-border-subtle, #ccc)",
                    resize: "vertical",
                  }}
                />
              </FormField>
            </Card>
          ))}

          <Box>
            <Button kind="secondary" color="brand" size="small" onClick={add}>
              Add roster
            </Button>
          </Box>
        </Flex>
      ) : null}
    </Box>
  );
}
