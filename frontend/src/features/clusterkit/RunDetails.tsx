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
import {
  Accordion,
  Banner,
  Button,
  FormField,
  SegmentedControl,
  Tag,
  Text,
  TextInput,
} from "@nvidia/foundations-react-core";
import { Download, RefreshCw } from "lucide-react";
import { dateTime, functionalStatus, skippedStages, summaryIssues } from "./model";
import { failureDiagnosis } from "./failureDiagnosis";
import { FailureDetails } from "./FailureDetails";
import { downloadArtifact, getArtifacts } from "./service";
import type { Artifact, RunStatus } from "./types";

export function RunDetails({
  run,
  activityId,
  error,
  submitting,
  refresh,
}: {
  run: RunStatus | null;
  activityId: string;
  error: string;
  submitting: boolean;
  refresh: () => void;
}) {
  const [tab, setTab] = useState("coverage");
  const nodeDetails = useRef<HTMLDivElement>(null);
  const [filter, setFilter] = useState("");
  const [files, setFiles] = useState<Artifact[]>([]);
  const [fileError, setFileError] = useState("");
  const [downloading, setDownloading] = useState("");
  const [filesLoading, setFilesLoading] = useState(false);
  const [fileTick, setFileTick] = useState(0);
  const hasRun = Boolean(run);
  useEffect(() => {
    if (tab !== "artifacts" || !hasRun) return;
    const abort = new AbortController();
    const start = window.setTimeout(() => {
      setFilesLoading(true);
      setFileError("");
    }, 0);
    void getArtifacts(activityId, abort.signal)
      .then((result) => {
        if (!abort.signal.aborted) setFiles(result);
      })
      .catch((reason) => {
        if (!abort.signal.aborted) setFileError(String(reason));
      })
      .finally(() => {
        window.clearTimeout(start);
        if (!abort.signal.aborted) setFilesLoading(false);
      });
    return () => {
      window.clearTimeout(start);
      abort.abort();
    };
  }, [tab, activityId, hasRun, fileTick]);

  const summary = run?.summary_error ? null : run?.summary;
  const functional = run ? functionalStatus(run) : "PENDING";
  const diagnosis = run ? failureDiagnosis(run) : null;
  const reportingNodes = new Set(diagnosis?.nodes || []);
  const skipped = skippedStages(summary);
  const stages = Object.entries(summary?.stages || {});
  const uniqueMessages = (messages: (string | undefined | null)[]) =>
    [...new Set(messages.filter((message): message is string =>
      typeof message === "string" && Boolean(message.trim())).map((message) => message.trim()))];
  const issues = uniqueMessages([run?.error, run?.summary_error, ...summaryIssues(summary)]);
  const notes = uniqueMessages([error, run?.scheduler_error, ...(summary?.warnings || []),
    ...Object.entries(skipped).map(([name, reason]) => name + ": " + reason)])
    .filter((message) => !issues.includes(message));
  const noteCount = issues.length + notes.length;
  const notesPanel = noteCount ? (
    <Accordion className="ck-run-notes" items={[{
      value: "notes",
      slotTrigger: <Text kind="body/regular/sm">Run notes ({noteCount})</Text>,
      slotContent: (
        <>
          {issues.length ? (
            <>
              <Text kind="label/bold/sm">Issues</Text>
              <ul className="ck-notes-list" aria-label="Run issues">
                {issues.map((message) => <li key={message}>{message}</li>)}
              </ul>
            </>
          ) : null}
          {notes.length ? (
            <ul className="ck-notes-list" aria-label="Run notices">
              {notes.map((message) => <li key={message}>{message}</li>)}
            </ul>
          ) : null}
        </>
      ),
    }]} />
  ) : null;
  async function download(name: string) {
    setDownloading(name);
    setFileError("");
    try {
      await downloadArtifact(activityId, name);
    } catch (reason) {
      setFileError(String(reason));
    } finally {
      setDownloading("");
    }
  }
  return (
    <section className="ck-section" aria-label="Run status and results">
      <div className="ck-bar" style={{ justifyContent: "space-between" }}>
        <h2>Run {activityId.slice(0, 8)}</h2>
        <Button
          kind="secondary"
          color="neutral"
          size="small"
          title="Refresh run"
          aria-label="Refresh run"
          onClick={refresh}
        >
          <RefreshCw size={16} />
        </Button>
      </div>
      <p className="ck-meta ck-mono" style={{ marginTop: 8 }}>
        {activityId}
      </p>
      {submitting ? <p className="ck-run-update ck-meta" role="status">Submission in progress</p> : null}
      {!run ? (
        <>
          <p className="ck-empty">
            {submitting ? "Waiting for submission receipt" : "Status unavailable"}
          </p>
          {notesPanel}
        </>
      ) : (
        <>
          <div className="ck-summary-strip">
            <div>
              <p className="ck-meta">Functional</p>
              <Tag
                readOnly
                color={
                  functional === "PASS"
                    ? "green"
                    : functional === "PENDING"
                      ? "gray"
                      : functional === "WARN"
                        ? "yellow"
                        : "red"
                }
              >
                {functional}
              </Tag>
            </div>
            <div>
              <p className="ck-meta">Performance</p>
              <Tag readOnly color="gray" title="Performance acceptance has not been verified">
                UNVERIFIED
              </Tag>
            </div>
            <div>
              <p className="ck-meta">Scheduler</p>
              <Tag readOnly color="gray">
                {run.scheduler_state}
              </Tag>
              <p className="ck-meta" style={{ marginTop: 8 }}>
                {run.scheduler_job_id || "No Slurm ID confirmed"}
              </p>
            </div>
            <div>
              <p className="ck-meta">Work phase</p>
              <p>{run.work_phase.replaceAll("_", " ")}</p>
            </div>
          </div>
          {diagnosis ? <FailureDetails diagnosis={diagnosis}
            showNodes={() => {
              setTab("nodes");
              window.requestAnimationFrame(() => nodeDetails.current?.focus());
            }} showArtifacts={() => setTab("artifacts")} /> : null}
          {run.work_phase === "submission_unknown" ? (
            <p className="ck-run-update ck-meta" role="status">
              Checking scheduler acceptance. No submission will be retried.
            </p>
          ) : issues.length && !diagnosis ? (
            <p className="ck-run-update ck-meta">This run needs attention. See Run notes for details.</p>
          ) : error || run.scheduler_error ? (
            <p className="ck-run-update ck-meta">Status update unavailable. See Run notes for details.</p>
          ) : null}
          {notesPanel}
          <dl className="ck-facts">
            <div>
              <dt>Started</dt>
              <dd>{dateTime(run.activity.created_at)}</dd>
            </div>
            <div>
              <dt>Finished</dt>
              <dd>{dateTime(run.activity.finished_at)}</dd>
            </div>
            <div>
              <dt>Allocation</dt>
              <dd>
                {run.activity.nodes.length} nodes /{" "}
                {summary?.expected_pair_count ?? "-"} expected pairs
              </dd>
            </div>
            <div>
              <dt>Operator</dt>
              <dd>{run.activity.operator || "-"}</dd>
            </div>
          </dl>
          <details style={{ marginBottom: 20 }}>
            <summary>Exact nodes &amp; attempt directory</summary>
            <pre className="ck-code">
              {run.activity.nodes.join("\n")}
              {"\n\n"}
              {run.activity.log_dir}
            </pre>
          </details>
          <SegmentedControl
            aria-label="Result view"
            value={tab}
            onValueChange={setTab}
            items={[
              { value: "coverage", children: "Pair results" },
              { value: "nodes", children: "Node diagnostics" },
              { value: "artifacts", children: "Artifacts" },
            ]}
          />
          {tab === "coverage" ? (
            <>
              {!stages.length ? (
                <p className="ck-empty">
                  {summary?.preflight?.status === "PASS"
                    ? "Preflight complete. No pairwise measurements in this run."
                    : "No pairwise evidence collected yet"}
                </p>
              ) : (
                <>
                  <div className="ck-fields">
                    <FormField
                      name="ck-pair-filter"
                      slotLabel="Filter node pairs"
                    >
                      <TextInput
                        aria-label="Filter node pairs"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                      />
                    </FormField>
                  </div>
                  {stages.map(([name, stage]) => (
                    <section key={name} className="ck-section">
                      <div className="ck-bar">
                        <h3>
                          {name === "single-path" ? "Single-path" : "Baseline"}
                        </h3>
                        <Tag
                          readOnly
                          color={name in skipped || stage.exit_status === 0 ? "gray" : "red"}
                        >
                          {name in skipped ? "Not run" : `Exit ${stage.exit_status ?? "unknown"}`}
                        </Tag>
                      </div>
                      {name in skipped ? <p className="ck-meta">{skipped[name]}</p> : <>
                      <div className="ck-table-wrap">
                        <table className="ck-table">
                          <thead>
                            <tr>
                              <th>Metric</th>
                              <th>Observed / expected pairs</th>
                              <th>Missing</th>
                              <th>Failed</th>
                              <th>Range</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(stage.metrics || {}).map(
                              ([metric, data]) => {
                                const values = (data.pairs || [])
                                  .map((p) => p.value)
                                  .filter(Number.isFinite);
                                return (
                                  <tr key={metric}>
                                    <td>{metric}</td>
                                    <td>
                                      {data.observed_pair_count ?? 0} /{" "}
                                      {summary?.expected_pair_count}
                                    </td>
                                    <td>{data.missing_pairs?.length ?? "-"}</td>
                                    <td>{data.failed_pairs?.length ?? "-"}</td>
                                    <td>
                                      {values.length
                                        ? `${Math.min(...values).toLocaleString()} - ${Math.max(...values).toLocaleString()} ${data.units}`
                                        : "-"}
                                    </td>
                                  </tr>
                                );
                              },
                            )}
                          </tbody>
                        </table>
                      </div>
                      {Object.entries(stage.metrics || {}).map(
                        ([metric, data]) => (
                          <div className="ck-table-wrap" key={metric}>
                            <table className="ck-table">
                              <caption
                                style={{
                                  textAlign: "left",
                                  padding: "12px 10px",
                                }}
                              >
                                {metric} ({data.units})
                              </caption>
                              <thead>
                                <tr>
                                  <th>Node A</th>
                                  <th>Node B</th>
                                  <th>Measured value</th>
                                </tr>
                              </thead>
                              <tbody>
                                {(data.pairs || [])
                                  .filter((pair) =>
                                    pair.nodes
                                      .join(" ")
                                      .toLowerCase()
                                      .includes(filter.toLowerCase()),
                                  )
                                  .map((pair, i) => (
                                    <tr key={i}>
                                      <td className="ck-mono">
                                        {pair.nodes[0]}
                                      </td>
                                      <td className="ck-mono">
                                        {pair.nodes[1]}
                                      </td>
                                      <td>
                                        {pair.value.toLocaleString()}{" "}
                                        {data.units}
                                      </td>
                                    </tr>
                                  ))}
                              </tbody>
                            </table>
                          </div>
                        ),
                      )}
                      </>}
                    </section>
                  ))}
                </>
              )}
            </>
          ) : null}
          {tab === "nodes" ? (
            <div className="ck-table-wrap" ref={nodeDetails} tabIndex={-1} role="region" aria-label="Node diagnostics">
              <table className="ck-table">
                <thead>
                  <tr>
                    <th>Node / rank</th>
                    <th>Local preflight</th>
                    <th>Run evidence</th>
                    <th>HCA / GID index</th>
                    <th>Link / architecture</th>
                    <th>CPU affinity</th>
                  </tr>
                </thead>
                <tbody>
                  {(summary?.preflight?.nodes || []).map((node) => (
                    <tr key={node.rank} data-run-finding={reportingNodes.has(node.host) || undefined}>
                      <td>
                        <span className="ck-mono">{node.host}</span>
                        <p className="ck-meta">Rank {node.rank}</p>
                      </td>
                      <td>
                        {node.status}
                        {[...new Set([...(node.errors || []), ...(node.error ? [node.error] : [])])].map((e) => (
                          <p key={e}>{e}</p>
                        ))}
                      </td>
                      <td>{reportingNodes.has(node.host)
                        ? <Tag readOnly color="gray" density="compact">{diagnosis?.nodeFinding}</Tag>
                        : "No node-specific finding"}</td>
                      <td>
                        {node.hca || "-"} / {node.gid_index ?? "-"}
                        <p className="ck-meta">{node.global_v2_gid?.gid}</p>
                      </td>
                      <td>
                        {node.link_layer || "-"}
                        <p className="ck-meta">{node.architecture}</p>
                      </td>
                      <td>{node.affinity_after?.join(", ") || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!summary?.preflight?.nodes?.length ? (
                <p className="ck-empty">No normalized node diagnostics yet</p>
              ) : null}
            </div>
          ) : null}
          {tab === "artifacts" ? (
            <>
              <div className="ck-bar" style={{ marginTop: 16 }}>
                <Button
                  kind="secondary"
                  color="neutral"
                  size="small"
                  disabled={filesLoading}
                  onClick={() => setFileTick((tick) => tick + 1)}
                >
                  <RefreshCw size={16} />
                  Refresh artifacts
                </Button>
                <span className="ck-meta">
                  {filesLoading ? "Loading..." : `${files.length} files`}
                </span>
              </div>
              {fileError ? (
                <Banner status="error" kind="inline" style={{ marginTop: 16 }}>
                  {fileError}
                </Banner>
              ) : null}
              <div className="ck-table-wrap">
                <table className="ck-table">
                  <thead>
                    <tr>
                      <th>Artifact</th>
                      <th>Size</th>
                      <th>Download</th>
                    </tr>
                  </thead>
                  <tbody>
                    {files.map((file) => (
                      <tr key={file.name}>
                        <td className="ck-mono">{file.name}</td>
                        <td>
                          {file.size == null
                            ? "-"
                            : `${(file.size / 1024).toFixed(1)} KiB`}
                        </td>
                        <td>
                          <Button
                            kind="tertiary"
                            color="neutral"
                            size="small"
                            aria-label={`Download ${file.name}`}
                            title={`Download ${file.name}`}
                            disabled={Boolean(downloading) || file.downloadable === false}
                            onClick={() => void download(file.name)}
                          >
                            <Download size={16} />
                          </Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {run.command_display ? (
                <details style={{ marginTop: 20 }}>
                  <summary>Command</summary>
                  <pre className="ck-code">{run.command_display}</pre>
                </details>
              ) : null}
              {run.stdout_tail || run.stderr_tail ? (
                <details style={{ marginTop: 16 }}>
                  <summary>Captured output</summary>
                  <pre className="ck-code">
                    {run.stdout_tail}
                    {"\n"}
                    {run.stderr_tail}
                  </pre>
                </details>
              ) : null}
            </>
          ) : null}
        </>
      )}
    </section>
  );
}
