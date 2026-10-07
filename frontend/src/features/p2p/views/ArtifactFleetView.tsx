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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Artifact Fleet: the operator surface for persisting the validation-image
// catalog into dfstore and materialising it across the compute fleet.
//
// Button policy is this app's bordered-only rule: the primary CTA is solid
// brand, everything routine is bordered neutral, and anything destructive is
// solid danger. Nothing clickable is allowed to read as a caption.
import { useCallback, useMemo, useState } from "react";
import {
  Banner,
  Button,
  Card,
  Combobox,
  Divider,
  Flex,
  ProgressBar,
  Skeleton,
  Tag,
  Text,
} from "@nvidia/foundations-react-core";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Checkbox from "@mui/material/Checkbox";
import {
  CheckCircle2,
  CloudDownload,
  HardDriveDownload,
  Play,
  RefreshCw,
  Rocket,
  Settings as SettingsIcon,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { useNavigate } from "react-router-dom";
import { NodeHeatmap } from "../components/fleet/NodeHeatmap";
import { OperationLogDialog } from "../components/OperationLogDialog";
import { useFleetStatus } from "../hooks/useFleetStatus";
import { startOperation } from "../services/fleetService";
import { provision } from "../services/settingsService";
import { ApiError } from "../lib/apiFetch";
import { integrityExceptions, integrityToneOf } from "../lib/fleetHeatmap";
import type { Operation, OperationAction } from "../types/fleet";

const ALL_SUS = Array.from({ length: 14 }, (_, index) => index + 1);

/** Actions that act on selected images rather than on the whole catalog. */
const IMAGE_SCOPED: ReadonlySet<OperationAction> = new Set(["persist", "distribute", "repair"]);
/** Actions the server rejects unless exactly one image is selected. */
const SINGLE_IMAGE: ReadonlySet<OperationAction> = new Set(["distribute", "repair"]);

function gigabytes(bytes: number): string {
  return `${(bytes / 1e9).toFixed(2)} GB`;
}

export default function ArtifactFleetView() {
  const { status, error, loading, unauthenticated, refresh } = useFleetStatus();
  const [selectedSus, setSelectedSus] = useState<number[]>(ALL_SUS);
  const [selectedImages, setSelectedImages] = useState<string[] | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [provisionBusy, setProvisionBusy] = useState(false);
  const [logOp, setLogOp] = useState<Operation | null>(null);
  const navigate = useNavigate();

  // Enabled-but-bare clusters report `provisioned: false`: the catalog still lists the shipped
  // images, but there is no Dragonfly instance, so operations would 501 and the fleet is empty.
  const notProvisioned = status?.provisioned === false;

  const images = status?.catalog.images ?? [];
  // Default to the whole catalog, but only once the catalog is known.
  const chosenImages = selectedImages ?? images.map((image) => image.filename);

  const running = useMemo(
    () => status?.operations.find((operation) => operation.state === "running") ?? null,
    [status],
  );

  const totals = useMemo(
    () =>
      (status?.fleet ?? []).reduce(
        (sum, item) => ({
          total: sum.total + item.total,
          reachable: sum.reachable + item.reachable,
          eligible: sum.eligible + item.eligible,
          peers: sum.peers + item.peers,
          ready: sum.ready + item.ready,
        }),
        { total: 0, reachable: 0, eligible: 0, peers: 0, ready: 0 },
      ),
    [status],
  );

  const exceptions = useMemo(
    () => integrityExceptions(status?.coverage.nodes ?? []),
    [status],
  );

  const run = useCallback(
    async (action: OperationAction) => {
      setActionError(null);
      setNotice(null);
      if (!selectedSus.length) {
        setActionError("Select at least one SU.");
        return;
      }
      const operationImages = IMAGE_SCOPED.has(action) ? chosenImages : [];
      if (IMAGE_SCOPED.has(action) && !operationImages.length) {
        setActionError("Select at least one image.");
        return;
      }
      if (SINGLE_IMAGE.has(action) && operationImages.length !== 1) {
        setActionError(
          `${action} acts on one image at a time, to keep fleet load bounded. Select exactly one.`,
        );
        return;
      }
      try {
        const operation = await startOperation({ action, sus: selectedSus, images: operationImages });
        setNotice(`Started ${action} on ${selectedSus.length} SU(s). Operation ${operation.id}.`);
        await refresh();
      } catch (reason) {
        // 409 is the server's single-mutation lock, which is a normal answer an
        // operator needs to read -- not a failure to retry.
        if (reason instanceof ApiError && reason.status === 409) {
          setActionError("Another fleet operation is already running. Wait for it to finish.");
          return;
        }
        setActionError(reason instanceof Error ? reason.message : "Operation failed to start");
      }
    },
    [chosenImages, refresh, selectedSus],
  );

  // A manual refresh with visible feedback: `loading` only covers the first fetch, so without this
  // the button fires silently and reads as dead.
  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await refresh();
    } finally {
      setRefreshing(false);
    }
  }, [refresh]);

  // Provisioning is the one fleet action available on a bare cluster: it starts the reconcile
  // workflow that stands up this cluster's Dragonfly instance. A 400 carries `provisioning_blockers`
  // -- the settings are incomplete -- which is a instruction to the operator, not a failure.
  const onProvision = useCallback(async () => {
    setActionError(null);
    setNotice(null);
    setProvisionBusy(true);
    try {
      const res = await provision();
      setNotice(`Provisioning started (run ${res.run_id}). Track it under Tools → Workflows; this page updates when the instance comes up.`);
      await refresh();
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 400) {
        const blockers =
          reason.body && typeof reason.body === "object" && "provisioning_blockers" in reason.body
            ? ((reason.body as { provisioning_blockers?: unknown }).provisioning_blockers ?? [])
            : [];
        const list = Array.isArray(blockers) && blockers.length ? `: ${blockers.join(", ")}` : "";
        setActionError(`P2P settings are incomplete${list}. Finish them in Dragonfly Settings, then provision.`);
        return;
      }
      setActionError(reason instanceof Error ? reason.message : "Failed to start provisioning");
    } finally {
      setProvisionBusy(false);
    }
  }, [refresh]);

  if (loading) {
    return (
      <Flex direction="column" gap="3" style={{ padding: 24 }}>
        <Skeleton style={{ height: 48 }} />
        <Skeleton style={{ height: 160 }} />
        <Skeleton style={{ height: 320 }} />
      </Flex>
    );
  }

  const requiredCount = status?.coverage.required_count ?? 0;

  return (
    <Flex direction="column" gap="3" style={{ padding: 24, maxWidth: 1880, margin: "0 auto" }}>
      <Flex justify="between" align="start" wrap="wrap" gap="2">
        <Flex direction="column" gap="1">
          <Text kind="body/bold/sm" style={{ color: "var(--text-color-brand)" }}>
            NVIS CONTROL PLANE
          </Text>
          <Text kind="display/sm">Artifact Fleet</Text>
          <Text kind="body/regular/md" style={{ color: "var(--text-color-secondary)" }}>
            Persistent Dragonfly storage and verified delivery for the validation-image catalog.
          </Text>
        </Flex>
        <Button
          kind="secondary"
          color="neutral"
          size="small"
          disabled={refreshing}
          onClick={() => void onRefresh()}
        >
          <RefreshCw
            size={16}
            aria-hidden
            style={refreshing ? { animation: "spin 0.8s linear infinite" } : undefined}
          />
          {refreshing ? "Refreshing…" : "Refresh"}
        </Button>
      </Flex>

      {unauthenticated ? (
        <Banner status="error" kind="inline">
          Session expired, or this account does not hold the root role. Sign in again to continue.
        </Banner>
      ) : null}
      {error && !unauthenticated ? (
        <Banner status="error" kind="inline">
          {error}
        </Banner>
      ) : null}
      {actionError ? (
        <Banner status="warning" kind="inline">
          {actionError}
        </Banner>
      ) : null}
      {notice ? (
        <Banner status="success" kind="inline">
          {notice}
        </Banner>
      ) : null}
      {running ? (
        <Banner status="info" kind="inline">
          {`A ${running.action} operation is running since ${running.started_at}. Mutating controls stay locked until it finishes.`}
        </Banner>
      ) : null}

      {notProvisioned ? (
        <Card>
          <Flex direction="column" gap="3">
            <Flex align="center" gap="2">
              <Rocket size={20} aria-hidden style={{ color: "var(--text-color-brand)" }} />
              <Text kind="title/md">This cluster&rsquo;s P2P instance is not provisioned yet</Text>
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              The catalog below lists the shipped validation images, but there is no Dragonfly
              instance to persist or distribute them, so the tiles read greenfield and fleet
              operations are unavailable. Provisioning runs the reconcile workflow that stands up the
              control plane on this cluster&rsquo;s app server; this page fills in once it is up.
            </Text>
            <Flex gap="2" wrap="wrap" align="center">
              <Button
                kind="primary"
                color="brand"
                size="small"
                disabled={provisionBusy}
                onClick={() => void onProvision()}
              >
                <Rocket size={16} aria-hidden />
                {provisionBusy ? "Starting…" : "Provision P2P instance"}
              </Button>
              <Button
                kind="secondary"
                color="neutral"
                size="small"
                onClick={() => navigate("/p2p/settings")}
              >
                <SettingsIcon size={16} aria-hidden />
                Open Dragonfly Settings
              </Button>
            </Flex>
          </Flex>
        </Card>
      ) : null}

      {status?.sync ? (
        <Card>
          <Flex direction="column" gap="2">
            <Flex justify="between" align="center" wrap="wrap" gap="2">
              <Flex direction="column" gap="1">
                <Text kind="title/md">Latest catalog sync wave</Text>
                <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {status.sync.id} · updated {status.sync.updated_at}
                </Text>
              </Flex>
              <Tag
                color={
                  status.sync.state === "running"
                    ? "blue"
                    : status.sync.deferred > 0
                      ? "yellow"
                      : "green"
                }
                kind="solid"
                readOnly
                density="compact"
              >
                {status.sync.state.replaceAll("_", " ")}
              </Tag>
            </Flex>
            <ProgressBar
              value={status.sync.percent}
              aria-label={`Catalog sync wave progress: ${status.sync.percent}% complete`}
            />
            <Text kind="body/regular/sm">
              {`${status.sync.complete.toLocaleString()} complete · ${status.sync.pending.toLocaleString()} pending · ${status.sync.deferred.toLocaleString()} deferred · ${status.sync.percent}%`}
            </Text>
            {status.sync.deferred > 0 ? (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                Deferred nodes carry a terminal reason and are not retried by this wave. Review them
                before calling the rollout complete.
              </Text>
            ) : null}
          </Flex>
        </Card>
      ) : null}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 200px), 1fr))",
          gap: 12,
        }}
      >
        {(
          [
            ["Catalog images", String(requiredCount), requiredCount === images.length],
            [
              "Object store",
              status?.object_store_healthy ? "Healthy" : "Unavailable",
              Boolean(status?.object_store_healthy),
            ],
            [
              "Reachable nodes",
              `${totals.reachable.toLocaleString()} / ${totals.total.toLocaleString()}`,
              totals.reachable === totals.total,
            ],
            ["Eligible nodes", totals.eligible.toLocaleString(), totals.eligible === totals.reachable],
            ["Persistent peers", totals.peers.toLocaleString(), totals.peers === totals.eligible],
            ["Verified complete", totals.ready.toLocaleString(), totals.ready === totals.eligible],
          ] as const
        ).map(([label, value, good]) => (
          <Card key={label}>
            <Flex direction="column" gap="1">
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                {label}
              </Text>
              <Flex align="center" gap="2">
                <Text kind="title/lg">{value}</Text>
                <Tag color={good ? "green" : "yellow"} kind="outline" readOnly density="compact">
                  {good ? "ok" : "check"}
                </Tag>
              </Flex>
            </Flex>
          </Card>
        ))}
      </div>

      <Card>
        <Flex direction="column" gap="3">
          <Text kind="title/md">1. Scope</Text>
          <Combobox
            aria-label="Service units"
            multiple
            size="medium"
            items={ALL_SUS.map((su) => ({ label: `SU${su}`, value: String(su) }))}
            value={selectedSus.map(String)}
            onValueChange={(value: string[]) =>
              setSelectedSus(value.map(Number).sort((a, b) => a - b))
            }
          />
          <Flex gap="2" wrap="wrap">
            <Button
              kind="secondary"
              color="neutral"
              size="tiny"
              onClick={() => setSelectedSus(ALL_SUS)}
            >
              All SUs
            </Button>
            <Button kind="secondary" color="neutral" size="tiny" onClick={() => setSelectedSus([])}>
              None
            </Button>
          </Flex>
        </Flex>
      </Card>

      <Card>
        <Flex direction="column" gap="3">
          <Text kind="title/md">2. Canonical images</Text>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell padding="checkbox" />
                  <TableCell>Image</TableCell>
                  <TableCell>Required by</TableCell>
                  <TableCell>Canonical file</TableCell>
                  <TableCell align="right">Size</TableCell>
                  <TableCell>SHA-256</TableCell>
                  <TableCell>On disk</TableCell>
                  <TableCell>dfstore</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {images.map((image) => (
                  <TableRow key={image.filename} hover>
                    <TableCell padding="checkbox">
                      <Checkbox
                        checked={chosenImages.includes(image.filename)}
                        onChange={() =>
                          setSelectedImages(
                            chosenImages.includes(image.filename)
                              ? chosenImages.filter((name) => name !== image.filename)
                              : [...chosenImages, image.filename],
                          )
                        }
                      />
                    </TableCell>
                    <TableCell>
                      <Text kind="body/bold/sm">
                        {image.order}. {image.name}
                      </Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                        {(image.required_by ?? []).join(" · ")}
                      </Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="mono/sm">{image.filename}</Text>
                    </TableCell>
                    <TableCell align="right">
                      <Text kind="mono/sm">{gigabytes(image.bytes)}</Text>
                    </TableCell>
                    <TableCell>
                      <Text kind="mono/sm">{image.sha256.slice(0, 12)}…</Text>
                    </TableCell>
                    <TableCell>
                      <Tag
                        color={image.canonical_present ? "green" : "red"}
                        kind="outline"
                        readOnly
                        density="compact"
                      >
                        {image.canonical_present ? "present" : "missing"}
                      </Tag>
                    </TableCell>
                    <TableCell>
                      <Tag
                        color={image.persistent_import ? "green" : "gray"}
                        kind="solid"
                        readOnly
                        density="compact"
                      >
                        {image.persistent_import ? "imported" : "not imported"}
                      </Tag>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
            {`Exact content duplicates in the catalog: ${status?.catalog_exact_duplicates.length ?? 0}. Files of equal size with different digests are not duplicates.`}
          </Text>
        </Flex>
      </Card>

      <Card>
        <Flex direction="column" gap="3">
          <Text kind="title/md">3. Workflow</Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            Every action is allow-list driven and runs only against nodes the current preflight
            marked eligible. Sync waits for an explicit success from every available node.
          </Text>
          {notProvisioned ? (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
              Operations are disabled until this cluster&rsquo;s P2P instance is provisioned &mdash;
              use <strong>Provision P2P instance</strong> above.
            </Text>
          ) : null}
          <Flex gap="2" wrap="wrap">
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("preflight")}
            >
              <ShieldCheck size={16} aria-hidden />
              A. Preflight
            </Button>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("deploy")}
            >
              <Play size={16} aria-hidden />
              B. Install peers
            </Button>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("persist")}
            >
              <HardDriveDownload size={16} aria-hidden />
              C. Persist selected
            </Button>
            <Button
              kind="primary"
              color="brand"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("sync")}
            >
              <CloudDownload size={16} aria-hidden />
              D. Sync catalog
            </Button>
            <Button
              kind="secondary"
              color="neutral"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("verify")}
            >
              <CheckCircle2 size={16} aria-hidden />
              E. Verify fleet
            </Button>
            <Divider orientation="vertical" />
            <Button
              kind="primary"
              color="danger"
              size="small"
              disabled={Boolean(running) || notProvisioned}
              onClick={() => void run("repair")}
            >
              <Wrench size={16} aria-hidden />
              Repair one image
            </Button>
          </Flex>
        </Flex>
      </Card>

      {exceptions.length ? (
        <Card>
          <Flex direction="column" gap="2">
            <Flex justify="between" align="center" wrap="wrap" gap="2">
              <Text kind="title/md">Integrity exceptions</Text>
              <Tag color="red" kind="solid" readOnly density="compact">
                {`${exceptions.length.toLocaleString()} node${exceptions.length === 1 ? "" : "s"}`}
              </Tag>
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Nodes holding a wrong-sized file, or content that has never been hashed. These can
              read as complete on count alone, which is why they are listed rather than left for a
              hover.
            </Text>
            <TableContainer style={{ maxHeight: 280 }}>
              <Table size="small" stickyHeader>
                <TableHead>
                  <TableRow>
                    <TableCell>Node</TableCell>
                    <TableCell align="right">SU</TableCell>
                    <TableCell>Finding</TableCell>
                    <TableCell align="right">Verified</TableCell>
                    <TableCell align="right">Wrong size</TableCell>
                    <TableCell align="right">Unhashed</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {exceptions.slice(0, 200).map((node) => (
                    <TableRow key={node.node} hover>
                      <TableCell>
                        <Text kind="mono/sm">{node.node}</Text>
                      </TableCell>
                      <TableCell align="right">{node.su}</TableCell>
                      <TableCell>
                        <Tag
                          color={integrityToneOf(node) === "corrupt" ? "red" : "yellow"}
                          kind="solid"
                          readOnly
                          density="compact"
                        >
                          {integrityToneOf(node) === "corrupt" ? "wrong size" : "unhashed"}
                        </Tag>
                      </TableCell>
                      <TableCell align="right">
                        {node.verified}/{requiredCount}
                      </TableCell>
                      <TableCell align="right">{node.bad_size}</TableCell>
                      <TableCell align="right">{node.unverified}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
            {exceptions.length > 200 ? (
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)" }}>
                {`Showing the first 200 of ${exceptions.length.toLocaleString()}.`}
              </Text>
            ) : null}
          </Flex>
        </Card>
      ) : null}

      <NodeHeatmap
        nodes={status?.coverage.nodes ?? []}
        requiredCount={requiredCount}
        selectedSus={selectedSus}
        generatedAt={status?.coverage.generated_at ?? null}
      />

      <Card>
        <Flex direction="column" gap="2">
          <Text kind="title/md">SU readiness</Text>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {`Preflight snapshot: ${status?.preflight_generated_at ?? "not run"}`}
          </Text>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>SU</TableCell>
                  <TableCell align="right">Reachable</TableCell>
                  <TableCell align="right">Eligible</TableCell>
                  <TableCell align="right">Peers</TableCell>
                  <TableCell align="right">Complete</TableCell>
                  <TableCell align="right">Syncing</TableCell>
                  <TableCell align="right">Incomplete</TableCell>
                  <TableCell align="right">Unavailable</TableCell>
                  <TableCell>Peer coverage</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(status?.fleet ?? []).map((item) => (
                  <TableRow key={item.su} hover>
                    <TableCell>
                      <Text kind="body/bold/sm">SU{item.su}</Text>
                    </TableCell>
                    <TableCell align="right">
                      {item.reachable}/{item.total}
                    </TableCell>
                    <TableCell align="right">{item.eligible}</TableCell>
                    <TableCell align="right">{item.peers}</TableCell>
                    <TableCell align="right">{item.ready}</TableCell>
                    <TableCell align="right">{item.syncing}</TableCell>
                    <TableCell align="right">{item.incomplete}</TableCell>
                    <TableCell align="right">{item.unavailable}</TableCell>
                    <TableCell style={{ minWidth: 160 }}>
                      <ProgressBar
                        value={item.eligible ? (100 * item.peers) / item.eligible : 0}
                        aria-label={`SU${item.su} peer coverage: ${item.peers} of ${item.eligible} eligible nodes`}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Flex>
      </Card>

      <Card>
        <Flex direction="column" gap="2">
          <Text kind="title/md">Recent operations</Text>
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Started</TableCell>
                  <TableCell>Action</TableCell>
                  <TableCell>Scope</TableCell>
                  <TableCell>State</TableCell>
                  <TableCell>Log</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {(status?.operations ?? []).map((operation) => (
                  <TableRow key={operation.id} hover>
                    <TableCell>
                      <Text kind="mono/sm">{operation.started_at}</Text>
                    </TableCell>
                    <TableCell>{operation.action}</TableCell>
                    <TableCell>
                      {`${operation.sus.length} SU / ${operation.images.length} image`}
                    </TableCell>
                    <TableCell>
                      <Tag
                        color={
                          operation.state === "succeeded"
                            ? "green"
                            : operation.state === "failed"
                              ? "red"
                              : "blue"
                        }
                        kind="solid"
                        readOnly
                        density="compact"
                      >
                        {operation.state}
                      </Tag>
                    </TableCell>
                    <TableCell>
                      <Button
                        kind="secondary"
                        color="neutral"
                        size="tiny"
                        onClick={() => setLogOp(operation)}
                      >
                        Open log
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Flex>
      </Card>
      <OperationLogDialog open={logOp !== null} onClose={() => setLogOp(null)} operation={logOp} />
    </Flex>
  );
}
