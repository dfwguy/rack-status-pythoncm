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
  Banner,
  Button,
  Checkbox,
  FormField,
  SegmentedControl,
  Select,
  Tag,
  TextArea,
  TextInput,
} from "@nvidia/foundations-react-core";
import { ClipboardCheck, Play, Server } from "lucide-react";
import { nodeAvailability } from "./model";
import { confirmRun, previewRun } from "./service";
import type { Inventory, Preview, Readiness, RunRequest } from "./types";

interface Props {
  readiness: Readiness;
  inventory: Inventory | null;
  onSubmit: (id: string, submission: () => Promise<unknown>) => void;
}

export function RunComposer({ readiness, inventory, onSubmit }: Props) {
  const [count, setCount] = useState(() =>
    readiness.allowed_node_counts.includes(2)
      ? 2
      : readiness.allowed_node_counts[0] || 1,
  );
  const [source, setSource] = useState("nodes");
  const [group, setGroup] = useState("");
  const [nodes, setNodes] = useState<string[]>([]);
  const [typedNodes, setTypedNodes] = useState("");
  const [search, setSearch] = useState("");
  const [compare, setCompare] = useState(Boolean(readiness.path_comparison));
  const [preview, setPreview] = useState<Preview | null>(null);
  const [expiresAt, setExpiresAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const consumed = useRef(false);
  const counts = [
    1,
    ...readiness.allowed_node_counts.filter((n) => n !== 1),
  ].filter((n) => n <= (readiness.max_nodes || 0));
  const selectedNodes = [
    ...new Set([...nodes, ...typedNodes.split(/[\s,]+/).filter(Boolean)]),
  ];
  const valid =
    source === "group" && count !== 1
      ? Boolean(group)
      : selectedNodes.length === count;
  const configured =
    readiness.configuration_ready && readiness.execution_context_ready;

  const invalidate = () => {
    generation.current += 1;
    controller.current?.abort();
    setBusy(false);
    setPreview(null);
    setError("");
  };
  useEffect(
    () => () => {
      generation.current += 1;
      controller.current?.abort();
    },
    [],
  );
  useEffect(() => {
    if (!preview) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [preview]);

  async function review() {
    if (!configured || !valid || busy) return;
    invalidate();
    const epoch = generation.current;
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    const request: RunRequest = {
      scope:
        count === 1 ? "preflight" : source === "group" ? "group" : "explicit",
      node_count: count,
    };
    if (request.scope === "group") request.group = group;
    else request.nodes = selectedNodes;
    if (count > 1) request.path_comparison = compare;
    try {
      const response = await previewRun(request, abort.signal);
      if (generation.current !== epoch) return;
      consumed.current = false;
      setPreview(response);
      setNow(Date.now());
      setExpiresAt(
        Date.now() + response.confirmation_expires_in_seconds * 1000,
      );
    } catch (reason) {
      if (generation.current === epoch && !abort.signal.aborted)
        setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (generation.current === epoch) setBusy(false);
    }
  }
  function submit() {
    if (
      !preview ||
      consumed.current ||
      Date.now() >= expiresAt ||
      !preview.plan.submission_enabled
    )
      return;
    consumed.current = true;
    // The caller persists the UUID in the URL before waiting for this response.
    const id = preview.activity_id;
    setPreview(null);
    onSubmit(id, () => confirmRun(preview.confirmation_token));
  }

  const visible = (inventory?.nodes || []).filter((node) =>
    node.node.toLowerCase().includes(search.toLowerCase()),
  );
  const remaining = Math.max(0, Math.ceil((expiresAt - now) / 1000));
  return (
    <div className="ck-layout ck-section">
      <section aria-label="Run configuration">
        <div className="ck-bar">
          <Server size={20} />
          <h2>New run</h2>
        </div>
        <div className="ck-fields">
          <FormField slotLabel="Nodes" name="ck-count">
            <SegmentedControl
              aria-label="Node count"
              value={String(count)}
              items={counts.map((n) => ({
                value: String(n),
                children: String(n),
              }))}
              onValueChange={(value) => {
                invalidate();
                setCount(Number(value));
              }}
            />
          </FormField>
          <FormField slotLabel="Selection" name="ck-source">
            <SegmentedControl
              aria-label="Selection"
              value={count === 1 ? "nodes" : source}
              items={[
                { value: "nodes", children: "Exact nodes" },
                {
                  value: "group",
                  children: "Configured group",
                  disabled: count === 1,
                },
              ]}
              onValueChange={(value) => {
                invalidate();
                setSource(value);
              }}
            />
          </FormField>
        </div>
        {count === 1 ? (
          <p className="ck-meta">Preflight</p>
        ) : (
          <Checkbox
            slotLabel="Compare baseline and single-path"
            checked={compare}
            disabled={!readiness.path_comparison}
            onCheckedChange={(checked) => {
              invalidate();
              setCompare(checked === true);
            }}
          />
        )}
        {source === "group" && count > 1 ? (
          <div className="ck-fields">
            <FormField slotLabel="Group" name="ck-group">
              <Select
                aria-label="Group"
                placeholder="Select a group"
                value={group}
                items={(readiness.groups || []).map((g) => ({
                  value: g.name,
                  children: `${g.name} (${g.nodes.length} nodes, ${g.kind})`,
                }))}
                onValueChange={(value: string) => {
                  invalidate();
                  setGroup(value);
                }}
              />
            </FormField>
          </div>
        ) : (
          <>
            <div className="ck-fields">
              <FormField slotLabel="Search inventory" name="ck-search">
                <TextInput
                  aria-label="Search inventory"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </FormField>
              <span className="ck-meta">
                {selectedNodes.length} / {count} selected
              </span>
            </div>
            {inventory?.nodes.length ? (
              <div className="ck-node-table">
                <table className="ck-table">
                  <thead>
                    <tr>
                      <th style={{ width: 48 }}>Select</th>
                      <th>Node</th>
                      <th>Availability</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((node) => {
                      const availability = nodeAvailability(
                        node,
                        readiness.partition,
                      );
                      const checked = nodes.includes(node.node);
                      return (
                        <tr key={node.node}>
                          <td>
                            <Checkbox
                              aria-label={`Select ${node.node}`}
                              checked={checked}
                              disabled={
                                !checked &&
                                (availability === "unavailable" ||
                                  selectedNodes.length >= count)
                              }
                              onCheckedChange={(value) => {
                                invalidate();
                                setNodes((old) =>
                                  value
                                    ? [...old, node.node]
                                    : old.filter((name) => name !== node.node),
                                );
                              }}
                            />
                          </td>
                          <td className="ck-mono">{node.node}</td>
                          <td>
                            <Tag
                              readOnly
                              color={
                                availability === "idle"
                                  ? "green"
                                  : availability === "busy"
                                    ? "yellow"
                                    : "gray"
                              }
                            >
                              {node.states.join(", ")}
                            </Tag>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {!visible.length ? (
                  <p className="ck-empty">No matching nodes</p>
                ) : null}
              </div>
            ) : (
              <p className="ck-empty">No finite node inventory configured</p>
            )}
            {readiness.regex_selection_enabled ? (
              <div style={{ marginTop: 16 }}>
                <FormField
                  slotLabel="Additional exact hostnames"
                  name="ck-hosts"
                >
                  <TextArea
                    aria-label="Additional exact hostnames"
                    value={typedNodes}
                    onChange={(event) => {
                      invalidate();
                      setTypedNodes(event.target.value);
                    }}
                  />
                </FormField>
              </div>
            ) : null}
          </>
        )}
        {error ? (
          <Banner status="error" kind="inline" style={{ marginTop: 16 }}>
            {error}
          </Banner>
        ) : null}
        <div className="ck-bar" style={{ marginTop: 20 }}>
          <Button
            kind="primary"
            disabled={!configured || !valid || busy}
            onClick={() => void review()}
          >
            <ClipboardCheck size={16} />
            {busy ? "Preparing preview..." : "Preview run"}
          </Button>
        </div>
      </section>
      <section className="ck-review" aria-label="Run preview">
        <h2>Review &amp; confirm</h2>
        {!preview ? (
          <p className="ck-empty">No preview</p>
        ) : (
          <>
            <dl className="ck-facts">
              <div>
                <dt>Coverage</dt>
                <dd>
                  {preview.plan.node_count} nodes /{" "}
                  {preview.plan.expected_pair_count} pairs
                </dd>
              </div>
              <div>
                <dt>Partition</dt>
                <dd>{preview.plan.effective_profile.partition}</dd>
              </div>
              <div>
                <dt>Execution</dt>
                <dd>
                  {preview.execution_context.user}@
                  {preview.execution_context.hostname}
                </dd>
              </div>
              <div>
                <dt>HCA</dt>
                <dd>
                  {preview.plan.effective_profile.hca || "Per-node discovery"}
                </dd>
              </div>
              <div>
                <dt>Resources per node</dt>
                <dd>
                  {preview.plan.resources.cpus_per_task} CPU /{" "}
                  {preview.plan.resources.memory_per_node} / no GPUs
                </dd>
              </div>
              <div>
                <dt>Time limit</dt>
                <dd>{preview.plan.resources.wall_seconds} seconds</dd>
              </div>
            </dl>
            <h3>Exact allocation</h3>
            <pre className="ck-code">{preview.plan.nodes.join("\n")}</pre>
            <h3>Attempt directory</h3>
            <p className="ck-meta" style={{ marginTop: 8 }}>
              {preview.plan.results_directory}
            </p>
            <details style={{ marginTop: 16 }}>
              <summary>Submission command</summary>
              <pre className="ck-code">
                {preview.submission_command_display || "Unavailable"}
              </pre>
            </details>
            <div className="ck-errors">
              {preview.plan.execution_blockers.map((message) => (
                <Banner key={message} status="error" kind="inline">
                  {message}
                </Banner>
              ))}
            </div>
            <div className="ck-errors">
              {preview.plan.warnings.map((message) => (
                <p className="ck-meta" key={message}>
                  {message}
                </p>
              ))}
            </div>
            <div className="ck-bar">
              <Button
                kind="primary"
                disabled={!remaining || !preview.plan.submission_enabled}
                onClick={submit}
              >
                <Play size={16} />
                Confirm &amp; run
              </Button>
              <span className="ck-meta" role="timer">
                {remaining ? `Expires in ${remaining}s` : "Preview expired"}
              </span>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
