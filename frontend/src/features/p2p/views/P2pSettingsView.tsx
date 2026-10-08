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
// Dragonfly Settings: the per-cluster NVIS-P2P instance configuration the reconcile provisions
// against. Everything render-site-config.py needs, editable per cluster, with the missing pieces
// named and provisioning gated until they are filled in.
import { useCallback, useEffect, useState } from "react";
import Box from "@mui/material/Box";
import { Banner, Button, Card, Flex, FormField, SegmentedControl, Text, TextInput } from "@nvidia/foundations-react-core";
import { PageHeader } from "../../../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../../../components/layout/contentWidth";
import { useActiveCluster } from "../../../context/ActiveClusterContext";
import { StageLadderInput } from "../components/StageLadderInput";
import { ApiError } from "../lib/apiFetch";
import { getSettings, provision, saveSettings } from "../services/settingsService";
import type { P2pSettingsDoc, P2pSettingsResponse } from "../types/settings";

type Key = keyof P2pSettingsDoc;

export default function P2pSettingsView() {
  const { activeCluster, version } = useActiveCluster();
  const [doc, setDoc] = useState<P2pSettingsResponse | null>(null);
  const [draft, setDraft] = useState<P2pSettingsDoc | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [provisioning, setProvisioning] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setStatus(null);
    try {
      const s = await getSettings();
      setDoc(s);
      setDraft({ ...s });
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setError("P2P is not enabled for this cluster. Enable it in Projects & Clusters first.");
      } else if (reason instanceof ApiError && reason.isUnauthenticated) {
        setError("Session expired or the P2P role is required.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load P2P settings");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load, version]);

  const set = (patch: Partial<P2pSettingsDoc>) => setDraft((d) => (d ? { ...d, ...patch } : d));

  const onSave = async () => {
    if (!draft) return;
    setSaving(true);
    setStatus(null);
    setError(null);
    try {
      const saved = await saveSettings(draft);
      setDoc(saved);
      setDraft({ ...saved });
      setStatus("Settings saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to save settings");
    } finally {
      setSaving(false);
    }
  };

  const onProvision = async () => {
    setProvisioning(true);
    setStatus(null);
    setError(null);
    setRunId(null);
    try {
      const res = await provision();
      setRunId(res.run_id);
      setStatus(`Reconcile started (run ${res.run_id}). Track it under Tools → Workflows.`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Failed to start provisioning");
    } finally {
      setProvisioning(false);
    }
  };

  const blockers = doc?.provisioning_blockers ?? [];
  const ready = blockers.length === 0;

  const text = (label: string, key: Key, help?: string, placeholder?: string, locked = false) => (
    <FormField name={`p2p-${key}`} slotLabel={label} slotHelp={help}>
      <TextInput
        value={String(draft?.[key] ?? "")}
        placeholder={placeholder}
        disabled={locked}
        onValueChange={(v) => set({ [key]: v } as Partial<P2pSettingsDoc>)}
      />
    </FormField>
  );
  // Where images live is the cluster's to say (its Shared image store / Node image cache); P2P
  // derives these two from it. When the cluster states one, the field here is display-only.
  const fromCluster = doc?.image_dirs_from_cluster;
  const clusterNote = "Set on the cluster (Projects → cluster → Shared image store / Node image cache).";

  const num = (label: string, key: Key, help?: string) => (
    <FormField name={`p2p-${key}`} slotLabel={label} slotHelp={help}>
      <TextInput
        value={String(draft?.[key] ?? "")}
        onValueChange={(v) => set({ [key]: (Number.parseInt(v, 10) || 0) } as Partial<P2pSettingsDoc>)}
      />
    </FormField>
  );

  const section = (title: string, children: React.ReactNode) => (
    <Card style={{ padding: 16 }}>
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>{title}</Text>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>{children}</div>
    </Card>
  );

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Dragonfly Settings"
        description={`Per-cluster NVIS-P2P instance configuration${activeCluster ? ` for ${activeCluster.name}` : ""}. Secrets are generated on the app server and never stored here.`}
        actions={
          <Flex gap="2" align="center">
            <Button kind="secondary" color="neutral" size="small" onClick={() => void load()}>
              Reload
            </Button>
            <Button kind="secondary" color="brand" size="small" disabled={saving || !draft} onClick={() => void onSave()}>
              {saving ? "Saving…" : "Save"}
            </Button>
            <Button
              kind="primary"
              color="brand"
              size="small"
              disabled={provisioning || !ready}
              onClick={() => void onProvision()}
            >
              {provisioning ? "Starting…" : "Provision"}
            </Button>
          </Flex>
        }
      />

      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {status ? <Banner status="success" kind="inline" style={{ marginBottom: 16 }}>{status}</Banner> : null}
      {loading ? <Text kind="body/regular/md">Loading…</Text> : null}

      {!loading && draft ? (
        <Flex direction="col" gap="3">
          {!ready ? (
            <Banner status="warning" kind="inline">
              Not ready to provision — set: {blockers.join("; ")}.
            </Banner>
          ) : (
            <Banner status="info" kind="inline">Settings complete. Provision to stand up (or reconcile) the control plane.</Banner>
          )}

          {section("Addresses & app-server connection", (
            <>
              {text("Manager IP", "manager_ip", "The Dragonfly Manager address on the app server.", "e.g. 10.10.20.22")}
              {text("App server IP", "appserver_ip", "The P2P app server's address (its own host — not the cluster's control node).", "e.g. 10.10.20.22")}
              {text("App server SSH host", "appserver_ssh_host", "How the reconcile reaches the app server over SSH. Blank = the app server IP.")}
              {text("App server SSH user", "appserver_ssh_user", "SSH user the reconcile connects as.")}
              {text("App server SSH key", "appserver_ssh_key", "Path to the private key, readable inside the container. Blank = the cluster's key.")}
              {text("Console bind", "console_bind", "IP:port the console/ops API binds to.")}
            </>
          ))}

          {section("Compute-peer inventory", (
            <>
              {text("Inventory pattern", "inventory_pattern", "Hostname bracket pattern for eligible peers, e.g. sys1-gso1-p-phy-[001-224].")}
              {text("Inventory source", "inventory_source", "Absolute path to the eligible-node file. Blank = a default under the install root the reconcile writes.")}
            </>
          ))}

          {section("Campaign defaults (staged rollout)", (
            <>
              {num("Wave size", "rollout_wave_size", "Nodes per wave; the roster is distributed this many at a time so each wave seeds the next.")}
              {num("Parallelism", "rollout_parallelism", "Concurrent launches within a wave (1–128).")}
              {num("Poll interval (s)", "rollout_poll_seconds", "Seconds between progress checks within a wave.")}
              {num("Max launches", "rollout_max_launches", "Re-launch attempts before a node is deferred.")}
              {num("Max unreachable", "rollout_max_unreachable", "Failed reachability checks before a node is deferred.")}
              {num("Timeout (s)", "rollout_timeout_seconds", "Overall campaign timeout before it gives up.")}
              {num("Download limit (s)", "rollout_download_timeout_seconds", "The longest one node may spend downloading one image. The node kills the transfer itself when it passes, and a launch replaces a download older than this. Default 3600.")}
              {num("Stall (s)", "rollout_stall_seconds", "A download that writes nothing for this long is stopped and relaunched, then deferred after Max launches — instead of holding its wave to the timeout. 0 = off.")}
              <FormField name="p2p-rollout_strategy" slotLabel="Wave plan"
                slotHelp="Sequential slices the roster in order. Rack-staged gives every rack one copy first (the only waves that load the seeds), then grows every rack up the stage ladder.">
                <SegmentedControl
                  size="small"
                  aria-label="Wave plan"
                  items={[
                    { value: "sequential", children: "Sequential" },
                    { value: "rack-staged", children: "Rack-staged" },
                  ]}
                  value={draft?.rollout_strategy ?? "sequential"}
                  onValueChange={(v: string) => set({ rollout_strategy: v === "rack-staged" ? "rack-staged" : "sequential" })}
                />
              </FormField>
              {num("Fill wave size", "rollout_fill_wave_size", "Rack-staged: nodes per wave after the first stage, which pull inside racks and can run wider. 0 = the wave size.")}
              <StageLadderInput
                label="Stages"
                value={draft?.rollout_stages ?? []}
                help="Rack-staged: cumulative per-rack targets — a count or a share — ending at 100%. E.g. 1, 50%, 100% or 1, 2, 4, 8, 100%."
                onChange={(stages) => set({ rollout_stages: stages })}
              />
            </>
          ))}

          {section("Paths & storage", (
            <>
              {text("Install root", "install_root", "Where the P2P instance is installed on the app server.")}
              {text(
                "Container path",
                "container_path",
                fromCluster?.container_path ? `From the cluster's Shared image store. ${clusterNote}` : "The images directory on the app server.",
                undefined,
                Boolean(fromCluster?.container_path),
              )}
              {text("Catalog path", "catalog_path", "Must name a .json file (not the images directory).")}
              {text(
                "Peer root",
                "peer_root",
                fromCluster?.peer_root
                  ? `<Node image cache>/.nvis-p2p, from the cluster. ${clusterNote}`
                  : "Per-node peer working root on compute nodes; images publish to its parent directory.",
                undefined,
                Boolean(fromCluster?.peer_root),
              )}
              {text("Object store data", "object_store_data_path", "dfstore data directory.")}
              {text("Seed cache", "seed_cache_path", "Seed peer cache directory.")}
              {text("systemd dir", "systemd_dir", "Where the peer/service units are installed.")}
            </>
          ))}

          {section("Service account", (
            <>
              {text("Service user", "service_user")}
              {text("Service group", "service_group")}
              {num("Service UID", "service_uid")}
              {num("Service GID", "service_gid")}
            </>
          ))}

          {runId ? (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Reconcile run: {runId}
            </Text>
          ) : null}
        </Flex>
      ) : null}
    </Box>
  );
}
