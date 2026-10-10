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
// Rollouts: pull distribution. One catalog image onto racks through the node agents' desired-state
// documents, rung by rung, with live progress from the agents' metrics. Every rollout is a durable
// run the operator can steer while it runs -- cancel, retry nodes, change the pacing -- with no SSH
// session anywhere in the loop (docs/DESIGN_p2p-pull-agent.md).
import { useCallback, useEffect, useMemo, useState } from "react";
import Box from "@mui/material/Box";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import {
  Banner, Button, Card, Combobox, Flex, FormField, ProgressBar, Select, Tag, Text, TextInput,
} from "@nvidia/foundations-react-core";
import { PageHeader } from "../../../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../../../components/layout/contentWidth";
import { useActiveCluster } from "../../../context/ActiveClusterContext";
import { StageLadderInput } from "../components/StageLadderInput";
import { ApiError } from "../lib/apiFetch";
import { getCatalog } from "../services/fleetService";
import {
  cancelRollout, getRollout, listRollouts, planRollout, retryRollout, setRolloutPacing, startRollout,
} from "../services/rolloutService";
import type { Rollout, RolloutDetail, RolloutPacing, RolloutPlan, RolloutsResponse } from "../types/rollout";
import { TERMINAL_ROLLOUT_STATES } from "../types/rollout";

const POLL_MS = 10_000;

const STATE_COLOR: Record<string, "green" | "red" | "blue" | "yellow" | "gray"> = {
  ready: "green", completed: "green", deferred: "red", failed: "red", cancelled: "yellow",
  downloading: "blue", released: "blue", running: "blue", pending: "gray", waiting: "gray", active: "blue", done: "green",
};

function StateTag({ state }: { state: string }) {
  return (
    <Tag color={STATE_COLOR[state] ?? "gray"} kind="outline" readOnly density="compact">
      {state}
    </Tag>
  );
}

function progress(r: Rollout): number {
  return r.counts.total ? Math.round((100 * r.counts.ready) / r.counts.total) : 0;
}

function PacingEditor({ value, onChange }: { value: RolloutPacing; onChange: (p: RolloutPacing) => void }) {
  return (
    <Flex gap="3" wrap="wrap" align="start">
      <Box sx={{ minWidth: 220 }}>
        <StageLadderInput
          label="Stage ladder"
          value={value.stages}
          onChange={(stages) => onChange({ ...value, stages })}
          help="Cumulative per-rack targets: 1 seeds the rack, then half, then all."
        />
      </Box>
      <FormField name="rollout-budget" slotLabel="Node budget (s)" slotHelp="A node not ready in this long is deferred.">
        <TextInput
          value={String(value.budget_seconds)}
          onValueChange={(v: string) => onChange({ ...value, budget_seconds: Number(v) || 0 })}
        />
      </FormField>
      <FormField name="rollout-racks" slotLabel="Racks in flight" slotHelp="Racks driven at the same time.">
        <TextInput
          value={String(value.max_active_racks)}
          onValueChange={(v: string) => onChange({ ...value, max_active_racks: Number(v) || 0 })}
        />
      </FormField>
    </Flex>
  );
}

function StartCard({ info, images, onStarted }: {
  info: RolloutsResponse;
  images: string[];
  onStarted: (runId: string, joined: boolean) => void;
}) {
  const [image, setImage] = useState("");
  const [racks, setRacks] = useState<string[]>([]);
  const [pacing, setPacing] = useState<RolloutPacing>(info.defaults);
  const [plan, setPlan] = useState<RolloutPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  const preview = async () => {
    setBusy(true);
    setError(null);
    try {
      setPlan(await planRollout(image, racks));
    } catch (e) {
      setPlan(null);
      setError(e instanceof Error ? e.message : "Preview failed");
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await startRollout(image, racks, pacing);
      onStarted(res.run_id, Boolean(res.joined));
      setConfirm(false);
      setPlan(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Start failed");
    } finally {
      setBusy(false);
    }
  };

  const blocked = info.blockers.length > 0;
  return (
    <Card style={{ padding: 16 }}>
      <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>New rollout</Text>
      {blocked ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 8 }}>
          Pull rollouts are not configured for this cluster — set in Dragonfly Settings: {info.blockers.join("; ")}.
        </Banner>
      ) : null}
      <Flex direction="col" gap="3">
        <Flex gap="3" wrap="wrap" align="start">
          <FormField name="rollout-image" slotLabel="Image" slotHelp="A pinned image from the catalog.">
            <Select
              aria-label="Image"
              items={images.map((f) => ({ value: f, children: f }))}
              value={image}
              onValueChange={(v: string) => { setImage(v); setPlan(null); }}
              placeholder="Select an image"
            />
          </FormField>
          <FormField name="rollout-rack-select" slotLabel="Racks" slotHelp="Placed by the server's inventory.">
            <Combobox
              aria-label="Racks"
              multiple
              size="medium"
              items={info.racks.map((r) => ({ label: `${r.rack} (${r.nodes})`, value: r.rack }))}
              value={racks}
              onValueChange={(v: string[]) => { setRacks(v); setPlan(null); }}
            />
          </FormField>
        </Flex>
        <PacingEditor value={pacing} onChange={setPacing} />
        {error ? <Banner status="error" kind="inline">{error}</Banner> : null}
        {plan ? (
          <Box>
            {plan.metrics_error ? (
              <Banner status="warning" kind="inline" style={{ marginBottom: 8 }}>
                Agent status unavailable ({plan.metrics_error}); nodes without a live agent will be deferred.
              </Banner>
            ) : null}
            <Table size="small" aria-label="Rollout preview">
              <TableHead>
                <TableRow>
                  <TableCell>Rack</TableCell>
                  <TableCell>Nodes</TableCell>
                  <TableCell>Live agents</TableCell>
                  <TableCell>Already hold it</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {plan.racks.map((r) => (
                  <TableRow key={r.rack}>
                    <TableCell>{r.rack}</TableCell>
                    <TableCell>{r.nodes.length}</TableCell>
                    <TableCell>{r.live_agents}</TableCell>
                    <TableCell>{r.holders}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        ) : null}
        <Flex gap="2" align="center">
          <Button kind="secondary" color="neutral" size="small" disabled={busy || blocked || !image || racks.length === 0}
            onClick={() => void preview()}>
            Preview
          </Button>
          {confirm ? (
            <>
              <Text kind="body/regular/sm">
                Roll {image} onto {racks.length} rack{racks.length === 1 ? "" : "s"}?
              </Text>
              <Button kind="primary" color="brand" size="small" disabled={busy} onClick={() => void start()}>
                {busy ? "Starting…" : "Confirm"}
              </Button>
              <Button kind="tertiary" color="neutral" size="small" onClick={() => setConfirm(false)}>Back</Button>
            </>
          ) : (
            <Button kind="primary" color="brand" size="small" disabled={busy || blocked || !plan}
              onClick={() => setConfirm(true)}>
              Start rollout
            </Button>
          )}
        </Flex>
      </Flex>
    </Card>
  );
}

function DetailCard({ detail, onChanged }: { detail: RolloutDetail; onChanged: () => void }) {
  const live = !TERMINAL_ROLLOUT_STATES.has(detail.state);
  const [pacing, setPacing] = useState<RolloutPacing>(detail.pacing);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card style={{ padding: 16 }} data-testid="rollout-detail">
      <Flex justify="between" align="center" style={{ marginBottom: 8 }}>
        <Flex gap="2" align="center">
          <Text kind="body/bold/md">{detail.image}</Text>
          <StateTag state={detail.state} />
          <Text kind="body/regular/sm">
            {detail.counts.ready}/{detail.counts.total} ready · {detail.counts.deferred} deferred · started by {detail.operator || "—"}
          </Text>
        </Flex>
        {live ? (
          <Flex gap="2" align="center">
            <Button kind="secondary" color="neutral" size="tiny" disabled={busy || detail.counts.deferred === 0}
              onClick={() => void act(() => retryRollout(detail.run_id, []))}>
              Retry deferred
            </Button>
            {confirmCancel ? (
              <>
                <Button kind="primary" color="danger" size="tiny" disabled={busy}
                  onClick={() => void act(() => cancelRollout(detail.run_id)).then(() => setConfirmCancel(false))}>
                  Confirm cancel
                </Button>
                <Button kind="tertiary" color="neutral" size="tiny" onClick={() => setConfirmCancel(false)}>Back</Button>
              </>
            ) : (
              <Button kind="secondary" color="danger" size="tiny" disabled={busy} onClick={() => setConfirmCancel(true)}>
                Cancel
              </Button>
            )}
          </Flex>
        ) : null}
      </Flex>
      {detail.last_error ? (
        <Banner status="warning" kind="inline" style={{ marginBottom: 8 }}>Last tick skipped: {detail.last_error}</Banner>
      ) : null}
      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 8 }}>{error}</Banner> : null}
      {live ? (
        <Box sx={{ mb: 2 }}>
          <PacingEditor value={pacing} onChange={setPacing} />
          <Button kind="secondary" color="brand" size="tiny" disabled={busy}
            onClick={() => void act(() => setRolloutPacing(detail.run_id, pacing))}>
            Apply pacing
          </Button>
        </Box>
      ) : null}
      {detail.rack_detail.map((rack) => (
        <Box key={rack.rack} sx={{ mb: 2 }}>
          <Flex gap="2" align="center" style={{ marginBottom: 4 }}>
            <Text kind="body/bold/sm">{rack.rack}</Text>
            <StateTag state={rack.state} />
            <Text kind="body/regular/sm">rung {rack.rung + 1} of {rack.cuts.length} (targets {rack.cuts.join(", ")})</Text>
          </Flex>
          <Table size="small" aria-label={`Rack ${rack.rack} nodes`}>
            <TableHead>
              <TableRow>
                <TableCell>Node</TableCell>
                <TableCell>State</TableCell>
                <TableCell>Reason</TableCell>
                <TableCell>Attempts</TableCell>
                <TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {rack.nodes.map((n) => (
                <TableRow key={n.node}>
                  <TableCell><Text kind="mono/sm">{n.node}</Text></TableCell>
                  <TableCell><StateTag state={n.state} /></TableCell>
                  <TableCell>{n.reason || ""}</TableCell>
                  <TableCell>{n.attempts}</TableCell>
                  <TableCell>
                    {live && n.state === "deferred" ? (
                      <Button kind="tertiary" color="brand" size="tiny" disabled={busy}
                        onClick={() => void act(() => retryRollout(detail.run_id, [n.node]))}>
                        Retry
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Box>
      ))}
      {detail.log.length ? (
        <Box component="pre" sx={{ fontSize: 12, maxHeight: 220, overflow: "auto", m: 0, p: 1,
          bgcolor: "var(--nv-color-bg-subtle, rgba(0,0,0,0.04))" }}>
          {detail.log.slice(-60).join("\n")}
        </Box>
      ) : null}
    </Card>
  );
}

export default function RolloutsView() {
  const { activeCluster, version } = useActiveCluster();
  const [info, setInfo] = useState<RolloutsResponse | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RolloutDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const [list, cat] = await Promise.all([listRollouts(), getCatalog()]);
      setInfo(list);
      setImages(cat.images.filter((i) => i.sha256).map((i) => i.filename));
      setError(null);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 404) {
        setError("P2P is not enabled for this cluster. Enable it in Projects & Clusters first.");
      } else {
        setError(reason instanceof Error ? reason.message : "Unable to load rollouts");
      }
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (runId: string) => {
    try {
      setDetail(await getRollout(runId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load the rollout");
    }
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(t);
  }, [load, version]);

  useEffect(() => {
    if (!selected) return;
    const t = window.setTimeout(() => void loadDetail(selected), 0);
    return () => window.clearTimeout(t);
  }, [selected, loadDetail]);

  const anyLive = useMemo(() => (info?.rollouts ?? []).some((r) => !TERMINAL_ROLLOUT_STATES.has(r.state)), [info]);
  useEffect(() => {
    if (!anyLive) return;
    const t = window.setInterval(() => {
      void load();
      if (selected) void loadDetail(selected);
    }, POLL_MS);
    return () => window.clearInterval(t);
  }, [anyLive, load, loadDetail, selected]);

  const onStarted = (runId: string, joined: boolean) => {
    setNotice(joined ? "That rollout is already running — showing it." : "Rollout started.");
    setSelected(runId);
    void load();
  };

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="Rollouts"
        description={`Pull distribution${activeCluster ? ` for ${activeCluster.name}` : ""}: an image onto racks through the node agents, rung by rung, steerable while it runs.`}
        actions={
          <Button kind="secondary" color="neutral" size="small" onClick={() => void load()}>
            Reload
          </Button>
        }
      />
      {error ? <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>{error}</Banner> : null}
      {notice ? <Banner status="success" kind="inline" style={{ marginBottom: 16 }}>{notice}</Banner> : null}
      {loading ? <Text kind="body/regular/md">Loading…</Text> : null}
      {!loading && info ? (
        <Flex direction="col" gap="3">
          <StartCard info={info} images={images} onStarted={onStarted} />
          <Card style={{ padding: 16 }}>
            <Text kind="body/bold/md" style={{ display: "block", marginBottom: 8 }}>Recent rollouts</Text>
            {info.rollouts.length === 0 ? (
              <Text kind="body/regular/sm">No rollouts yet.</Text>
            ) : (
              <Table size="small" aria-label="Rollouts">
                <TableHead>
                  <TableRow>
                    <TableCell>Image</TableCell>
                    <TableCell>Racks</TableCell>
                    <TableCell>State</TableCell>
                    <TableCell sx={{ minWidth: 200 }}>Progress</TableCell>
                    <TableCell>Started</TableCell>
                    <TableCell />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {info.rollouts.map((r) => (
                    <TableRow key={r.run_id} selected={r.run_id === selected}>
                      <TableCell>{r.image}</TableCell>
                      <TableCell>{r.racks.join(", ")}</TableCell>
                      <TableCell><StateTag state={r.state} /></TableCell>
                      <TableCell>
                        <ProgressBar value={progress(r)} aria-label={`${r.image}: ${progress(r)}% ready`} />
                        <Text kind="body/regular/sm">
                          {r.counts.ready}/{r.counts.total} ready · {r.counts.downloading + r.counts.released} in flight · {r.counts.deferred} deferred
                        </Text>
                      </TableCell>
                      <TableCell>{r.started_at}</TableCell>
                      <TableCell>
                        <Button kind="tertiary" color="brand" size="tiny" onClick={() => setSelected(r.run_id)}>
                          Open
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Card>
          {detail && detail.run_id === selected ? (
            <DetailCard key={detail.run_id} detail={detail} onChanged={() => { void load(); void loadDetail(detail.run_id); }} />
          ) : null}
        </Flex>
      ) : null}
    </Box>
  );
}
