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
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Banner,
  Button,
  Skeleton,
  Tabs,
  Tag,
} from "@nvidia/foundations-react-core";
import { RefreshCw } from "lucide-react";
import { PageHeader } from "../../components/layout/PageHeader";
import { useActiveCluster } from "../../context/ActiveClusterContext";
import { getInventory, getReadiness } from "./service";
import { useRackCommandsStore } from "../../context/RackCommandsStoreContext";
import { RunComposer } from "./RunComposer";
import { RunDetails } from "./RunDetails";
import { RunHistory } from "./RunHistory";
import type { Inventory, Readiness, RunStatus } from "./types";
import "./clusterkit.css";

export default function ClusterKitView() {
  const { version, activeCluster, loading } = useActiveCluster();
  if (loading) return <main className="ck-page"><PageHeader title="ClusterKit" /><Skeleton style={{ height: 240 }} /></main>;
  // A catalog switch invalidates every request, token and cached result in this subtree.
  return (
    <ClusterKitWorkspace
      key={version}
      clusterName={activeCluster?.name || "Active cluster"}
    />
  );
}

export function ClusterKitWorkspace({ clusterName }: { clusterName: string }) {
  const [params, setParams] = useSearchParams();
  const activityId = params.get("run") || "";
  const [tab, setTab] = useState(activityId ? "run" : "new");
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadTick, setLoadTick] = useState(0);
  const [error, setError] = useState("");
  const [runError, setRunError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { getActivity, watchActivity } = useRackCommandsStore();
  const tracked = getActivity(activityId);
  const run = tracked?.status as unknown as RunStatus | null;
  const alive = useRef(true);
  const pendingId = useRef("");
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    const abort = new AbortController();
    async function load() {
      try {
        const state = await getReadiness(abort.signal);
        if (abort.signal.aborted) return;
        setReadiness(state);
        if (state.configuration_ready && state.execution_context_ready) {
          const nodes = await getInventory(abort.signal);
          if (!abort.signal.aborted) setInventory(nodes);
        }
      } catch (reason) {
        if (!abort.signal.aborted)
          setError(reason instanceof Error ? reason.message : String(reason));
      } finally {
        if (!abort.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => abort.abort();
  }, [loadTick]);
  useEffect(() => {
    if (!activityId || submitting) return;
    watchActivity(
      activityId,
      `/api/clusterkit/runs/${encodeURIComponent(activityId)}`,
    );
  }, [activityId, submitting, watchActivity]);
  function openRun(id: string) {
    setRunError("");
    setTab("run");
    setParams({ run: id });
  }
  function submitted(id: string, submit: () => Promise<unknown>) {
    pendingId.current = id;
    setSubmitting(true);
    openRun(id);
    void Promise.resolve().then(submit)
      .catch((reason) => {
        if (alive.current && pendingId.current === id)
          setRunError(
            `Submission response unavailable: ${String(reason)}. Recovering this attempt by ID; no retry.`,
          );
      })
      .finally(() => {
        if (alive.current && pendingId.current === id) setSubmitting(false);
        watchActivity(id, `/api/clusterkit/runs/${encodeURIComponent(id)}`);
      });
  }
  const ready =
    readiness?.configuration_ready && readiness.execution_context_ready;
  const blockers = [
    ...(readiness?.errors || []),
    ...(readiness?.execution_context_errors || []),
    ...(!readiness?.enabled && readiness?.disabled_reason
      ? [readiness.disabled_reason]
      : []),
  ];
  return (
    <main className="ck-page">
      <PageHeader
        title="ClusterKit"
        contentId="clusterkit"
        actions={
          <Button
            kind="secondary"
            size="small"
            color="neutral"
            title="Refresh readiness and inventory"
            aria-label="Refresh readiness and inventory"
            disabled={loading || submitting}
            onClick={() => {
              setLoading(true);
              setError("");
              setReadiness(null);
              setInventory(null);
              setLoadTick((tick) => tick + 1);
            }}
          >
            <RefreshCw size={16} />
          </Button>
        }
      />
      <div className="ck-bar" style={{ marginBottom: 20 }}>
        <Tag readOnly color={ready ? "green" : "gray"}>
          {loading ? "Checking readiness" : ready ? "Configured" : "Not ready"}
        </Tag>
        <span>{clusterName}</span>
        {readiness?.partition ? (
          <span className="ck-meta">Partition {readiness.partition}</span>
        ) : null}
        {readiness?.execution_context ? (
          <span className="ck-meta">
            {readiness.execution_context.user}@
            {readiness.execution_context.hostname}
          </span>
        ) : null}
      </div>
      {error ? (
        <Banner status="error" kind="inline" style={{ marginBottom: 16 }}>
          {error}
        </Banner>
      ) : null}
      {blockers.map((message) => (
        <Banner
          key={message}
          status="warning"
          kind="inline"
          style={{ marginBottom: 12 }}
        >
          {message}
        </Banner>
      ))}
      {!loading && !ready ? (
        <p className="ck-meta" style={{ marginBottom: 20 }}>
          <a className="ck-link" href="#/settings">
            Settings
          </a>{" "}
          /{" "}
          <a className="ck-link" href="#/platform/projects">
            Projects &amp; Clusters
          </a>
        </p>
      ) : null}
      <Tabs
        value={tab}
        onValueChange={setTab}
        items={[
          { value: "new", children: "New run" },
          { value: "run", children: "Status & results", disabled: !activityId },
          { value: "history", children: "History" },
        ]}
      />
      {tab === "new" ? (
        loading ? (
          <Skeleton style={{ height: 240, marginTop: 24 }} />
        ) : readiness && ready ? (
          <RunComposer
            key={loadTick}
            readiness={readiness}
            inventory={inventory}
            onSubmit={submitted}
          />
        ) : null
      ) : null}
      {tab === "run" && activityId ? (
        <RunDetails
          key={activityId}
          run={run}
          activityId={activityId}
          error={tracked?.error || (!run ? runError : "")}
          submitting={submitting}
          refresh={() =>
            watchActivity(
              activityId,
              `/api/clusterkit/runs/${encodeURIComponent(activityId)}`,
              true,
            )
          }
        />
      ) : null}
      {tab === "history" ? <RunHistory onOpen={openRun} /> : null}
    </main>
  );
}
