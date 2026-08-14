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
// ApiReferenceView — browsable reference for the generated OpenAPI document.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// Reads GET /api/openapi.json, which the backend generates from its own route table, so this
// page cannot describe an endpoint the deployment does not serve.
//
// Written against KUI rather than pulling in Redoc or swagger-ui-react. Those weigh ~7 MB
// unpacked with 21-34 transitive dependencies, and they earn that weight by rendering rich
// request/response schemas — which this spec deliberately does not have (bodies are free-form
// objects, because inventing per-endpoint schemas would be a contract we do not honour). What is
// left to render is a couple of hundred operations with summaries, parameters and status codes: a grouped,
// searchable list. Doing it natively also keeps the page inside the authenticated shell and
// air-gap safe by construction, with no CDN and no vendored blob to keep in lockstep.
//
// Trade-off, stated plainly: there is no "Try it out". If executing requests from the browser
// becomes a requirement, swagger-ui-react can replace the body of this view without changing
// anything else.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Banner, Button, Card, Tag, Text, TextInput } from "@nvidia/foundations-react-core";
import Box from "@mui/material/Box";
import Paper from "@mui/material/Paper";
import { ChevronDown, ChevronRight, Download, RefreshCw } from "lucide-react";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { NVIS_RACK_STATUS_DOC_URL } from "../components/Sidebar";
import { apiFetch } from "../lib/apiFetch";

interface OpenApiParameter {
  name: string;
  in: string;
  required?: boolean;
  schema?: { type?: string; format?: string };
}

interface OpenApiOperation {
  operationId?: string;
  tags?: string[];
  summary?: string;
  description?: string;
  parameters?: OpenApiParameter[];
  requestBody?: { description?: string };
  responses?: Record<string, { description?: string }>;
}

interface OpenApiSpec {
  openapi?: string;
  info?: { title?: string; version?: string; description?: string };
  tags?: { name: string; description?: string }[];
  paths?: Record<string, Record<string, OpenApiOperation>>;
}

/** One flattened operation — the unit this page lists and filters. */
interface Operation {
  key: string;
  method: string;
  path: string;
  tag: string;
  op: OpenApiOperation;
}

/** Verb colours, borrowed from the convention every API browser uses so it reads instantly. */
const METHOD_COLOR: Record<string, "green" | "blue" | "yellow" | "red" | "gray"> = {
  get: "blue",
  post: "green",
  put: "yellow",
  patch: "yellow",
  delete: "red",
};

function flatten(spec: OpenApiSpec | null): Operation[] {
  if (!spec?.paths) return [];
  const out: Operation[] = [];
  for (const [path, methods] of Object.entries(spec.paths)) {
    for (const [method, op] of Object.entries(methods)) {
      out.push({
        key: `${method}:${path}`,
        method,
        path,
        tag: op.tags?.[0] ?? "general",
        op,
      });
    }
  }
  return out.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
}

export function ApiReferenceView() {
  const [spec, setSpec] = useState<OpenApiSpec | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [openKeys, setOpenKeys] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const res = await apiFetch("/api/openapi.json");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSpec((await res.json()) as OpenApiSpec);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const operations = useMemo(() => flatten(spec), [spec]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return operations;
    // Match path, verb, tag and summary: an operator searching "reset" or "POST" or "catalog"
    // should all land somewhere sensible.
    return operations.filter(
      (o) =>
        o.path.toLowerCase().includes(q) ||
        o.method.includes(q) ||
        o.tag.toLowerCase().includes(q) ||
        (o.op.summary ?? "").toLowerCase().includes(q),
    );
  }, [operations, query]);

  const grouped = useMemo(() => {
    const map = new Map<string, Operation[]>();
    for (const o of filtered) {
      const list = map.get(o.tag) ?? [];
      list.push(o);
      map.set(o.tag, list);
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  }, [filtered]);

  const tagDescriptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of spec?.tags ?? []) if (t.description) map.set(t.name, t.description);
    return map;
  }, [spec]);

  const toggle = useCallback((key: string) => {
    setOpenKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  return (
    // Same page frame as every other view: capped width, centred, padded. Without it the page ran
    // edge to edge on a wide monitor, which is not just inconsistent — a single line of prose
    // stretched the full viewport is genuinely hard to read.
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        title="API reference"
        description={
          spec
            ? `${operations.length} operations across ${spec.tags?.length ?? 0} groups · OpenAPI ${spec.openapi ?? "?"} · version ${spec.info?.version ?? "?"}`
            : "Loading the OpenAPI document…"
        }
        actions={
          <Box sx={{ display: "flex", gap: 1, alignItems: "center" }}>
            <Button kind="tertiary" size="small" onClick={() => void load()} disabled={loading}>
              <RefreshCw size={14} style={{ marginRight: 6 }} />
              Refresh
            </Button>
            <Button
              kind="secondary"
              size="small"
              onClick={() => window.open("/api/openapi.json", "_blank")}
            >
              <Download size={14} style={{ marginRight: 6 }} />
              openapi.json
            </Button>
          </Box>
        }
      />

      {error && (
        <Banner status="error" kind="inline" style={{ marginTop: 12 }}>
          Could not load /api/openapi.json — {error}
        </Banner>
      )}

      {/* The orientation text and the filter belong together in one panel: the first tells you
          what you are looking at, the second is how you narrow it. */}
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Text
          kind="body/regular/sm"
          style={{
            display: "block",
            // Capped in characters rather than pixels so the measure holds at any panel width.
            maxWidth: "88ch",
            color: "var(--text-color-secondary)",
          }}
        >
          Generated from this deployment&apos;s own route table, so it describes exactly what this
          build serves. Query parameters are the ones each handler actually reads. Request bodies are
          free-form JSON by design — the description carries the contract, not a schema. Authenticate
          with <code>POST /api/login</code> before calling anything else.{" "}
          <a href={NVIS_RACK_STATUS_DOC_URL} target="_blank" rel="noopener noreferrer">
            Source repository
          </a>
          .
        </Text>

        <Box sx={{ mt: 2, display: "flex", gap: 2, alignItems: "center", flexWrap: "wrap" }}>
          <Box sx={{ width: 460, maxWidth: "100%" }}>
            <TextInput
              value={query}
              onChange={(e) => setQuery((e.target as HTMLInputElement).value)}
              placeholder="Filter by path, verb, group or summary…"
              size="small"
              aria-label="Filter API operations"
            />
          </Box>
          {!loading && !error && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {filtered.length === operations.length
                ? `${operations.length} operations`
                : `${filtered.length} of ${operations.length} operations`}
            </Text>
          )}
        </Box>
      </Paper>

      {grouped.map(([tag, ops]) => (
        <Box key={tag} sx={{ mt: 3 }}>
          <Box sx={{ display: "flex", alignItems: "baseline", gap: 1 }}>
            <Text kind="title/lg">{tag}</Text>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {ops.length}
            </Text>
          </Box>
          {tagDescriptions.get(tag) && (
            <Text kind="body/regular/sm" style={{ display: "block", color: "var(--text-color-secondary)" }}>
              {tagDescriptions.get(tag)}
            </Text>
          )}

          {ops.map((o) => {
            const isOpen = openKeys.has(o.key);
            return (
              <Card key={o.key} style={{ marginTop: 8, padding: 0, overflow: "hidden" }}>
                <Box
                  role="button"
                  tabIndex={0}
                  onClick={() => toggle(o.key)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      toggle(o.key);
                    }
                  }}
                  sx={{
                    display: "flex",
                    alignItems: "center",
                    gap: 1.5,
                    p: 1.25,
                    cursor: "pointer",
                    "&:hover": { backgroundColor: "var(--background-color-secondary)" },
                  }}
                >
                  {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  <Tag color={METHOD_COLOR[o.method] ?? "gray"} kind="solid" readOnly density="compact">
                    {o.method.toUpperCase()}
                  </Tag>
                  {/* The path is the identity of the row, so it never truncates. */}
                  <Text kind="mono/sm" style={{ fontWeight: 600, flexShrink: 0 }}>
                    {o.path}
                  </Text>
                  <Text
                    kind="body/regular/sm"
                    style={{
                      color: "var(--text-color-secondary)",
                      // minWidth: 0 is what makes the ellipsis work at all: a flex child will not
                      // shrink below its content width without it, so long summaries were
                      // overflowing the card instead of truncating.
                      flex: 1,
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={o.op.summary}
                  >
                    {o.op.summary}
                  </Text>
                </Box>

                {isOpen && (
                  <Box sx={{ px: 2, pb: 2, pt: 0 }}>
                    {o.op.description && (
                      <Text
                        kind="body/regular/sm"
                        style={{ display: "block", whiteSpace: "pre-wrap", marginBottom: 12 }}
                      >
                        {o.op.description}
                      </Text>
                    )}

                    {(o.op.parameters?.length ?? 0) > 0 && (
                      <Box sx={{ mb: 1.5 }}>
                        <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4 }}>
                          Parameters
                        </Text>
                        {o.op.parameters?.map((p) => (
                          <Box key={`${p.in}:${p.name}`} sx={{ display: "flex", gap: 1, alignItems: "center" }}>
                            <Text kind="mono/sm">{p.name}</Text>
                            <Tag color="gray" kind="outline" readOnly density="compact">
                              {p.in}
                            </Tag>
                            {p.required && (
                              <Tag color="red" kind="outline" readOnly density="compact">
                                required
                              </Tag>
                            )}
                            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                              {p.schema?.type ?? "string"}
                            </Text>
                          </Box>
                        ))}
                      </Box>
                    )}

                    {o.op.requestBody && (
                      <Box sx={{ mb: 1.5 }}>
                        <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4 }}>
                          Request body
                        </Text>
                        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                          {o.op.requestBody.description ?? "application/json"}
                        </Text>
                      </Box>
                    )}

                    <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 4 }}>
                      Responses
                    </Text>
                    <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1 }}>
                      {Object.entries(o.op.responses ?? {}).map(([code, r]) => (
                        <Box key={code} sx={{ display: "flex", gap: 0.5, alignItems: "center" }}>
                          <Tag
                            color={code.startsWith("2") ? "green" : code.startsWith("4") ? "yellow" : "red"}
                            kind="outline"
                            readOnly
                            density="compact"
                          >
                            {code}
                          </Tag>
                          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                            {r.description}
                          </Text>
                        </Box>
                      ))}
                    </Box>

                    {o.op.operationId && (
                      <Text
                        kind="mono/sm"
                        style={{ display: "block", marginTop: 12, color: "var(--text-color-secondary)" }}
                      >
                        operationId: {o.op.operationId}
                      </Text>
                    )}
                  </Box>
                )}
              </Card>
            );
          })}
        </Box>
      ))}

      {!loading && !error && filtered.length === 0 && (
        <Banner status="info" kind="inline" style={{ marginTop: 16 }}>
          No operation matches “{query}”.
        </Banner>
      )}
    </Box>
  );
}
