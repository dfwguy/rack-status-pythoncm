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
import { useEffect, useState } from "react";
import { Banner, Button, Tag } from "@nvidia/foundations-react-core";
import { ArrowLeft, ArrowRight, RefreshCw } from "lucide-react";
import { getHistory } from "./service";
import { dateTime } from "./model";
import type { HistoryPage } from "./types";

export function RunHistory({ onOpen }: { onOpen: (id: string) => void }) {
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [cursors, setCursors] = useState<string[]>([""]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const cursor = cursors.at(-1) || "";
  useEffect(() => {
    const abort = new AbortController();
    void getHistory(cursor, abort.signal)
      .then((result) => {
        if (!abort.signal.aborted) {
          setPage(result);
          setError("");
        }
      })
      .catch((reason) => {
        if (!abort.signal.aborted) setError(String(reason));
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [cursor, tick]);
  return (
    <section className="ck-section" aria-label="Run history">
      <div className="ck-bar" style={{ justifyContent: "space-between" }}>
        <h2>Run history</h2>
        <Button
          kind="secondary"
          color="neutral"
          size="small"
          disabled={loading}
          title="Refresh history"
          aria-label="Refresh history"
          onClick={() => {
            setLoading(true);
            setTick((t) => t + 1);
          }}
        >
          <RefreshCw size={16} />
        </Button>
      </div>
      {error ? (
        <Banner status="error" kind="inline" style={{ marginTop: 16 }}>
          {error}
        </Banner>
      ) : null}
      <div className="ck-table-wrap">
        <table className="ck-table">
          <thead>
            <tr>
              <th>Started</th>
              <th>Run</th>
              <th>Nodes / pairs</th>
              <th>Phase</th>
              <th>Slurm job</th>
              <th>Operator</th>
            </tr>
          </thead>
          <tbody>
            {page?.runs.map((run) => (
              <tr key={run.activity_id}>
                <td>{dateTime(run.created_at)}</td>
                <td>
                  <Button
                    kind="tertiary"
                    size="small"
                    color="neutral"
                    onClick={() => onOpen(run.activity_id)}
                  >
                    {run.activity_id.slice(0, 8)}
                  </Button>
                  <p className="ck-meta">{run.stage}</p>
                </td>
                <td>
                  {run.node_count ?? run.nodes.length} /{" "}
                  {run.expected_pair_count ?? "-"}
                </td>
                <td>
                  <Tag
                    readOnly
                    color={
                      run.phase === "error" || run.phase === "timeout"
                        ? "red"
                        : run.phase === "done"
                          ? "green"
                          : "gray"
                    }
                  >
                    {run.phase}
                  </Tag>
                </td>
                <td>{run.tasks?.[0]?.scheduler_ref || "-"}</td>
                <td>{run.operator || "-"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!page?.runs.length && !loading && !error ? (
        <p className="ck-empty">No attempts in this execution context</p>
      ) : null}
      {loading ? (
        <p className="ck-empty" role="status">
          Loading history...
        </p>
      ) : null}
      <div className="ck-bar" style={{ marginTop: 16 }}>
        <Button
          kind="secondary"
          color="neutral"
          size="small"
          disabled={cursors.length === 1 || loading}
          onClick={() => {
            setLoading(true);
            setCursors((old) => old.slice(0, -1));
          }}
        >
          <ArrowLeft size={16} />
          Previous
        </Button>
        <span className="ck-meta">Page {cursors.length}</span>
        <Button
          kind="secondary"
          color="neutral"
          size="small"
          disabled={!page?.next_cursor || loading}
          onClick={() => {
            if (page?.next_cursor) {
              setLoading(true);
              setCursors((old) => [...old, page.next_cursor!]);
            }
          }}
        >
          Next
          <ArrowRight size={16} />
        </Button>
      </div>
    </section>
  );
}
