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
// Campaigns: the capstone surface. A campaign names a task (catalog image) × roster × pacing once;
// launching it enqueues a distribute through the operations write-path, so the same definition
// re-runs idempotently and its history is the operations it tagged. Per-cluster, stored by Rack
// Status, drawing its image choices from the catalog and its targets from the rosters.
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Card, Flex, FormField, SegmentedControl, Text, TextInput } from "@nvidia/foundations-react-core";
import { PageHeader } from "../../../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../../../components/layout/contentWidth";
import { useActiveCluster } from "../../../context/ActiveClusterContext";
import { ApiError } from "../lib/apiFetch";
import { OperationLogDialog } from "../components/OperationLogDialog";
import { StageLadderInput } from "../components/StageLadderInput";
import { getCampaigns, getCatalog, getRosters, launchCampaign, saveCampaigns } from "../services/fleetService";
import type { CampaignEdit, RolloutStrategy } from "../services/fleetService";
import type { Operation } from "../types/fleet";

/** A new campaign row: pacing zeroed so every knob inherits the cluster's rollout default. */
const emptyCampaign = (): CampaignEdit => ({
  name: "", description: "", image: "", roster: "",
  wave_size: 0, parallelism: 0, poll_seconds: 0, max_launches: 0, max_unreachable: 0, timeout_seconds: 0,
  fill_wave_size: 0, strategy: "", stages: [],
});

/** The wave plan a campaign can pick; "inherit" stands for the blank override (the cluster decides). */
const STRATEGY_ITEMS = [
  { value: "inherit", children: "Cluster default" },
  { value: "sequential", children: "Sequential" },
  { value: "rack-staged", children: "Rack-staged" },
];

/** The pacing overrides, with the cluster-default each inherits when left at 0, for the inputs. */
const PACING_FIELDS: { key: keyof CampaignEdit; label: string; placeholder: string }[] = [
  { key: "wave_size", label: "Wave size", placeholder: "default" },
  { key: "parallelism", label: "Parallelism", placeholder: "default" },
  { key: "poll_seconds", label: "Poll (s)", placeholder: "default" },
  { key: "max_launches", label: "Max launches", placeholder: "default" },
  { key: "max_unreachable", label: "Max unreachable", placeholder: "default" },
  { key: "timeout_seconds", label: "Timeout (s)", placeholder: "default" },
];

export default function CampaignsView() {
  const { activeCluster, version } = useActiveCluster();
  const [campaigns, setCampaigns] = useState<CampaignEdit[] | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [rosterNames, setRosterNames] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [pendingLaunch, setPendingLaunch] = useState<number | null>(null);
  const [launching, setLaunching] = useState(false);
  const [lastOp, setLastOp] = useState<Operation | null>(null);
  const [logOp, setLogOp] = useState<Operation | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const [camps, cat, ros] = await Promise.all([getCampaigns(), getCatalog(), getRosters()]);
      setCampaigns(camps.campaigns.map((c) => ({ ...emptyCampaign(), ...c })));
      setImages(cat.images.map((i) => i.filename).filter(Boolean));
      setRosterNames(ros.rosters.map((r) => r.name).filter(Boolean));
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setError("P2P is not enabled for this cluster. Enable it in Projects & Clusters first.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load campaigns");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load, version]);

  const update = (idx: number, patch: Partial<CampaignEdit>) =>
    setCampaigns((list) => (list ? list.map((c, i) => (i === idx ? { ...c, ...patch } : c)) : list));
  const remove = (idx: number) => setCampaigns((list) => (list ? list.filter((_, i) => i !== idx) : list));
  const add = () => setCampaigns((list) => [...(list ?? []), emptyCampaign()]);

  const onSave = async () => {
    if (!campaigns) return;
    setSaving(true);
    setError(null);
    setStatus(null);
    try {
      const saved = await saveCampaigns(campaigns);
      setCampaigns(saved.campaigns.map((c) => ({ ...emptyCampaign(), ...c })));
      setStatus("Campaigns saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to save campaigns");
    } finally {
      setSaving(false);
    }
  };

  const onLaunch = async (idx: number) => {
    if (!campaigns) return;
    setLaunching(true);
    setError(null);
    setStatus(null);
    try {
      const op = await launchCampaign(campaigns[idx]);
      setStatus(`Launched "${campaigns[idx].name}" — operation ${op.id} is running.`);
      setLastOp(op);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        setError("Another operation is already running on this cluster. Wait for it to finish.");
      } else {
        setError(reason instanceof Error ? reason.message : "Failed to launch campaign");
      }
    } finally {
      setLaunching(false);
      setPendingLaunch(null);
    }
  };

  const canLaunch = useMemo(() => images.length > 0, [images]);

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Campaigns"
        description={`Saved distributions${activeCluster ? ` for ${activeCluster.name}` : ""}: a task × roster × pacing you name once and launch on demand.`}
        actions={
          <Flex gap="2" align="center">
            <Button kind="secondary" color="neutral" size="small" onClick={() => void load()}>
              Reload
            </Button>
            <Button kind="secondary" color="brand" size="small" disabled={saving || !campaigns} onClick={() => void onSave()}>
              {saving ? "Saving…" : "Save"}
            </Button>
          </Flex>
        }
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {status ? (
        <Banner status="success" kind="inline" style={{ marginBottom: 16 }}>
          {status}
          {lastOp ? (
            <Button kind="tertiary" color="brand" size="tiny" style={{ marginLeft: 12 }} onClick={() => setLogOp(lastOp)}>
              View log
            </Button>
          ) : null}
        </Banner>
      ) : null}
      {loading ? <Text kind="body/regular/md">Loading…</Text> : null}

      {!loading && campaigns ? (
        <Flex direction="col" gap="3">
          {campaigns.length === 0 ? (
            <Banner status="info" kind="inline">
              No campaigns yet. Add one to pair a catalog image with a roster and pacing, then launch it.
            </Banner>
          ) : null}
          {!canLaunch ? (
            <Banner status="warning" kind="inline">
              The catalog is empty — add an image under Tasks before a campaign has anything to distribute.
            </Banner>
          ) : null}

          {campaigns.map((c, idx) => (
            <Card key={idx} style={{ padding: 16 }}>
              <Flex justify="between" align="center" style={{ marginBottom: 8 }}>
                <Text kind="body/bold/md">
                  {c.name || `Campaign ${idx + 1}`}
                  {c.image ? ` · ${c.image}` : ""}
                  {c.roster ? ` → ${c.roster}` : " → whole fleet"}
                </Text>
                <Flex gap="2" align="center">
                  <Button
                    kind="secondary"
                    color="brand"
                    size="tiny"
                    disabled={launching || !c.name || !c.image}
                    onClick={() => setPendingLaunch(idx)}
                  >
                    Launch
                  </Button>
                  <Button kind="secondary" color="danger" size="tiny" onClick={() => remove(idx)}>
                    Remove
                  </Button>
                </Flex>
              </Flex>

              {pendingLaunch === idx ? (
                <Banner status="warning" kind="inline" style={{ marginBottom: 12 }}>
                  Launch “{c.name || "this campaign"}” now? It distributes {c.image || "its image"} to{" "}
                  {c.roster || "the whole fleet"}.
                  <Button kind="primary" color="brand" size="tiny" style={{ marginLeft: 12 }} disabled={launching} onClick={() => void onLaunch(idx)}>
                    {launching ? "Launching…" : "Confirm launch"}
                  </Button>
                  <Button kind="tertiary" color="neutral" size="tiny" style={{ marginLeft: 8 }} onClick={() => setPendingLaunch(null)}>
                    Cancel
                  </Button>
                </Banner>
              ) : null}

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
                <FormField slotLabel="Name" slotHelp="Unique name, e.g. nemo-nightly">
                  <TextInput value={c.name} onValueChange={(v) => update(idx, { name: v })} />
                </FormField>
                <FormField slotLabel="Description">
                  <TextInput value={c.description} onValueChange={(v) => update(idx, { description: v })} />
                </FormField>
                <FormField slotLabel="Image (task)" slotHelp="A catalog image to distribute">
                  <Box
                    component="select"
                    aria-label="Image"
                    value={c.image}
                    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => update(idx, { image: e.target.value })}
                    sx={{ width: "100%", p: 1, borderRadius: 1, border: "1px solid var(--color-border-subtle, #ccc)", fontSize: 14 }}
                  >
                    <option value="">— choose an image —</option>
                    {/* The saved image survives even if it later leaves the catalog, so the row still reads true. */}
                    {(images.includes(c.image) || !c.image ? images : [c.image, ...images]).map((f) => (
                      <option key={f} value={f}>{f}</option>
                    ))}
                  </Box>
                </FormField>
                <FormField slotLabel="Roster" slotHelp="A named roster, or the whole fleet">
                  <Box
                    component="select"
                    aria-label="Roster"
                    value={c.roster}
                    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => update(idx, { roster: e.target.value })}
                    sx={{ width: "100%", p: 1, borderRadius: 1, border: "1px solid var(--color-border-subtle, #ccc)", fontSize: 14 }}
                  >
                    <option value="">— whole fleet (default inventory) —</option>
                    {(rosterNames.includes(c.roster) || !c.roster ? rosterNames : [c.roster, ...rosterNames]).map((n) => (
                      <option key={n} value={n}>{n}</option>
                    ))}
                  </Box>
                </FormField>
              </div>

              <Text kind="body/regular/sm" style={{ marginTop: 12, marginBottom: 4, opacity: 0.8 }}>
                Pacing overrides — leave 0 to inherit the cluster&rsquo;s rollout defaults.
              </Text>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 12 }}>
                {PACING_FIELDS.map((f) => (
                  <FormField key={f.key} slotLabel={f.label}>
                    <Box
                      component="input"
                      type="number"
                      min={0}
                      value={String(c[f.key] as number)}
                      placeholder={f.placeholder}
                      onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                        update(idx, { [f.key]: Math.max(0, Number(e.target.value) || 0) } as Partial<CampaignEdit>)
                      }
                      sx={{ width: "100%", p: 1, borderRadius: 1, border: "1px solid var(--color-border-subtle, #ccc)", fontSize: 14 }}
                    />
                  </FormField>
                ))}
              </div>

              <Text kind="body/regular/sm" style={{ marginTop: 12, marginBottom: 4, opacity: 0.8 }}>
                Wave plan — rack-staged gives every rack one copy first, then grows every rack up the
                stage ladder so new nodes pull from rack-mates instead of the seeds.
              </Text>
              <Flex direction="col" gap="2">
                <SegmentedControl
                  size="small"
                  aria-label="Wave plan"
                  items={STRATEGY_ITEMS}
                  value={c.strategy || "inherit"}
                  onValueChange={(v: string) =>
                    update(idx, { strategy: v === "inherit" ? "" : (v as RolloutStrategy) })
                  }
                />
                {c.strategy === "rack-staged" ? (
                  <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: 12 }}>
                    <StageLadderInput
                      label="Stages"
                      value={c.stages}
                      inherits
                      placeholder="cluster default (e.g. 1, 50%, 100%)"
                      help="Per-rack targets, a count or a share, ending at 100%. Blank uses the cluster's ladder."
                      onChange={(stages) => update(idx, { stages })}
                    />
                    <FormField slotLabel="Fill wave size" slotHelp="Nodes per wave after the first stage; 0 inherits.">
                      <Box
                        component="input"
                        type="number"
                        min={0}
                        aria-label="Fill wave size"
                        value={String(c.fill_wave_size)}
                        onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                          update(idx, { fill_wave_size: Math.max(0, Number(e.target.value) || 0) })
                        }
                        sx={{ width: "100%", p: 1, borderRadius: 1, border: "1px solid var(--color-border-subtle, #ccc)", fontSize: 14 }}
                      />
                    </FormField>
                  </div>
                ) : null}
              </Flex>
            </Card>
          ))}

          <Box>
            <Button kind="secondary" color="brand" size="small" onClick={add}>
              Add campaign
            </Button>
          </Box>
        </Flex>
      ) : null}

      <OperationLogDialog open={logOp !== null} operation={logOp} onClose={() => setLogOp(null)} />
    </Box>
  );
}
