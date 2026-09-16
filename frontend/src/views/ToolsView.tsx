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
import { useCallback, useEffect, useState } from "react";
import { useClusterOverview } from "../hooks/useClusterOverview";
import { CheckCircle, Cpu, Gauge, LineChart } from "lucide-react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import MenuItem from "@mui/material/MenuItem";
import Alert from "@mui/material/Alert";
import Paper from "@mui/material/Paper";
import Typography from "@mui/material/Typography";
import CircularProgress from "@mui/material/CircularProgress";
import InputAdornment from "@mui/material/InputAdornment";
import Tooltip from "@mui/material/Tooltip";
import FormControlLabel from "@mui/material/FormControlLabel";
import Checkbox from "@mui/material/Checkbox";
import IconButton from "@mui/material/IconButton";
import { PageHeader } from "../components/layout/PageHeader";
import { CONTENT_MAX_WIDTH } from "../components/layout/contentWidth";
import { ToolsLiveOutputDialog } from "../components/ToolsLiveOutputDialog";
import { CollapsibleLog } from "../components/CollapsibleLog";
import { apiFetch } from "../lib/apiFetch";
type LiveKind = "nemo" | "hpcx" | "cudaNccl" | "nvloom" | "dgxc" | "dcgm" | "grafana";

type ExemplarMode = "record" | "install";
type GpuTypeChoice = "gb200" | "gb300" | "vr200" | "h100" | "b200" | "b300";
const GPU_TYPE_CHOICES: GpuTypeChoice[] = ["gb200", "gb300", "vr200", "h100", "b200", "b300"];

/** Best-effort map of the active platform product line (e.g. "GB300 NVL72") to an llmb gpu_type. */
function deriveGpuType(productLine: string | undefined): GpuTypeChoice {
  const pl = (productLine || "").toLowerCase();
  const hit = GPU_TYPE_CHOICES.find((g) => pl.includes(g));
  return hit ?? "gb200";
}

/** Shown next to Utility card titles when the server reports that artifact is already present. */
function InstalledLabel() {
  return (
    <Typography
      component="span"
      variant="caption"
      sx={{ color: "success.main", fontWeight: 700, letterSpacing: 0.06, whiteSpace: "nowrap" }}
    >
      [INSTALLED]
    </Typography>
  );
}

function parseToolsStreamExit(text: string): number | null {
  const m = text.match(/\r?\n--- exit (-?\d+) ---\s*$/m);
  return m ? parseInt(m[1], 10) : null;
}

/** Read ``text/plain`` response body and push the full string after each chunk. */
async function consumePlainTextStream(res: Response, onFullText: (full: string) => void): Promise<string> {
  const dec = new TextDecoder();
  let acc = "";
  if (!res.body) {
    acc = await res.text();
    onFullText(acc);
    return acc;
  }
  const reader = res.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    acc += dec.decode(value, { stream: true });
    onFullText(acc);
  }
  acc += dec.decode();
  onFullText(acc);
  return acc;
}

/** Max characters kept for the *live* dialog view (full text is still returned for results). */
const LIVE_LOG_DISPLAY_CAP = 100_000;

/**
 * Build a throttled, size-capped sink for live streaming logs.
 *
 * The network reader delivers many chunks per second; pushing the full (ever-growing) string
 * into React state on every chunk re-renders this large view and reflows the entire <pre> each
 * time — O(n) per chunk, O(n^2) overall — which visibly stalls once the log is long. This
 * coalesces updates to at most one per animation frame and only keeps the tail for display.
 */
function createThrottledLogSink(
  apply: (text: string) => void,
): ((full: string) => void) & { flush: () => void } {
  let latest: string | null = null;
  let scheduled = false;
  let handle = 0;
  const raf: (cb: () => void) => number =
    typeof requestAnimationFrame === "function"
      ? (cb) => requestAnimationFrame(cb)
      : (cb) => setTimeout(cb, 16) as unknown as number;
  const cancel: (id: number) => void =
    typeof cancelAnimationFrame === "function" ? (id) => cancelAnimationFrame(id) : (id) => clearTimeout(id);
  const emit = () => {
    scheduled = false;
    handle = 0;
    if (latest == null) return;
    const full = latest;
    latest = null;
    const text =
      full.length > LIVE_LOG_DISPLAY_CAP
        ? `…(${full.length - LIVE_LOG_DISPLAY_CAP} earlier characters hidden — full log saved on completion)…\n` +
          full.slice(full.length - LIVE_LOG_DISPLAY_CAP)
        : full;
    apply(text);
  };
  const sink = (full: string) => {
    latest = full;
    if (!scheduled) {
      scheduled = true;
      handle = raf(emit);
    }
  };
  sink.flush = () => {
    if (handle) cancel(handle);
    scheduled = false;
    emit();
  };
  return sink;
}

const DEST_STORAGE_KEY = "nvis-tools-download-dest";
const HPCX_WORKSPACE_STORAGE_KEY = "nvis-tools-hpcx-workspace";
const CUDA_NCCL_BUILD_HOST_STORAGE_KEY = "nvis-tools-cuda-nccl-build-host";
const NVLOOM_WORKSPACE_STORAGE_KEY = "nvis-tools-nvloom-workspace";
const NVLOOM_BUILD_HOST_STORAGE_KEY = "nvis-tools-nvloom-build-host";
const NVLOOM_CUDA_MAJOR_STORAGE_KEY = "nvis-tools-nvloom-cuda-major";
const DGXC_WORKSPACE_STORAGE_KEY = "nvis-tools-dgxc-workspace";
const DGXC_GPU_TYPE_STORAGE_KEY = "nvis-tools-dgxc-gpu-type";

type ArchChoice = "aarch64" | "x86_64" | "";
type CudaMajorChoice = "12" | "13";

interface ToolsViewProps {
  logDir: string;
}

export function ToolsView({ logDir }: ToolsViewProps) {
  const { data: clusterData } = useClusterOverview(logDir);
  const [destination, setDestination] = useState("");
  const [arch, setArch] = useState<ArchChoice>("aarch64");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<{
    ok: boolean;
    skipped?: boolean;
    existing_file?: string;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);
  const [nemoSqshPresent, setNemoSqshPresent] = useState(false);
  const [nemoSqshPath, setNemoSqshPath] = useState<string | null>(null);

  const [grafanaLoading, setGrafanaLoading] = useState(false);
  const [grafanaError, setGrafanaError] = useState("");
  const [grafanaResult, setGrafanaResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);

  const [hpcxWorkspace, setHpcxWorkspace] = useState("");
  const [cudaMajor, setCudaMajor] = useState<CudaMajorChoice>("13");
  const [installCudaToolkit, setInstallCudaToolkit] = useState(true);
  const [buildNccl, setBuildNccl] = useState(true);
  const [hpcxLoading, setHpcxLoading] = useState(false);
  const [hpcxError, setHpcxError] = useState("");
  const [hpcxResult, setHpcxResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);
  const [cudaNcclBuildHost, setCudaNcclBuildHost] = useState("");
  const [cudaNcclLoading, setCudaNcclLoading] = useState(false);
  const [cudaNcclError, setCudaNcclError] = useState("");
  const [cudaNcclResult, setCudaNcclResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);
  const [nvloomWorkspace, setNvloomWorkspace] = useState("");
  const [nvloomBuildHost, setNvloomBuildHost] = useState("");
  const [nvloomCudaMajor, setNvloomCudaMajor] = useState<CudaMajorChoice>("13");
  const [nvloomLoading, setNvloomLoading] = useState(false);
  const [nvloomError, setNvloomError] = useState("");
  const [nvloomResult, setNvloomResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);
  const [dcgmArch, setDcgmArch] = useState<"aarch64" | "x86_64">("aarch64");
  const [dcgmDistro, setDcgmDistro] = useState("dgx");
  const [dcgmCudaMajor, setDcgmCudaMajor] = useState<CudaMajorChoice>("13");
  const [dcgmVersion, setDcgmVersion] = useState("");
  const [dcgmLoading, setDcgmLoading] = useState(false);
  const [dcgmError, setDcgmError] = useState("");
  const [dcgmResult, setDcgmResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);
  const [toolchainAllReady, setToolchainAllReady] = useState(false);
  const [toolchainCudaNvcc, setToolchainCudaNvcc] = useState(false);
  const [toolchainHpcxInit, setToolchainHpcxInit] = useState(false);
  const [toolchainNccl, setToolchainNccl] = useState(false);
  const [toolchainTooltip, setToolchainTooltip] = useState("");
  const [nvloomCliPresent, setNvloomCliPresent] = useState(false);
  const [toolchainProbeTick, setToolchainProbeTick] = useState(0);
  const [nemoStatusTick, setNemoStatusTick] = useState(0);

  const [llmbRacks, setLlmbRacks] = useState("");
  const [llmbDryRun, setLlmbDryRun] = useState(true);
  const [llmbLoading, setLlmbLoading] = useState(false);
  const [llmbError, setLlmbError] = useState("");
  const [llmbPhase, setLlmbPhase] = useState("");
  const [llmbResult, setLlmbResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);

  const [dgxcWorkspace, setDgxcWorkspace] = useState("");
  const [dgxcMode, setDgxcMode] = useState<ExemplarMode>("record");
  const [dgxcGpuType, setDgxcGpuType] = useState<GpuTypeChoice>("gb200");
  const [dgxcUseUv, setDgxcUseUv] = useState(true);
  const [dgxcTrustGit, setDgxcTrustGit] = useState(true);
  const [dgxcImageFolder, setDgxcImageFolder] = useState("");
  const [dgxcLoading, setDgxcLoading] = useState(false);
  const [dgxcError, setDgxcError] = useState("");
  const [dgxcResult, setDgxcResult] = useState<{
    ok: boolean;
    returncode?: number;
    command_display?: string;
    stdout?: string;
    stderr?: string;
  } | null>(null);

  const [liveDialogOpen, setLiveDialogOpen] = useState(false);
  const [liveDialogKind, setLiveDialogKind] = useState<LiveKind>("nemo");
  const [liveLogs, setLiveLogs] = useState({
    nemo: "",
    hpcx: "",
    cudaNccl: "",
    nvloom: "",
    dgxc: "",
    dcgm: "",
    grafana: "",
  });
  const [liveRunning, setLiveRunning] = useState({
    nemo: false,
    hpcx: false,
    cudaNccl: false,
    nvloom: false,
    dgxc: false,
    dcgm: false,
    grafana: false,
  });

  const openLive = useCallback((kind: LiveKind) => {
    setLiveDialogKind(kind);
    setLiveDialogOpen(true);
  }, []);

  useEffect(() => {
    let stored = "";
    try {
      stored = localStorage.getItem(DEST_STORAGE_KEY) || "";
    } catch {
      /* ignore */
    }
    if (stored) {
      setDestination(stored);
      return;
    }
    const def = clusterData?.tools_download_destination_default?.trim();
    if (def) setDestination(def);
  }, [clusterData?.tools_download_destination_default]);

  useEffect(() => {
    try {
      const w = localStorage.getItem(HPCX_WORKSPACE_STORAGE_KEY)?.trim();
      if (w) {
        setHpcxWorkspace(w);
        return;
      }
    } catch {
      /* ignore */
    }
    const def = clusterData?.tools_workspace_default?.trim();
    if (def) setHpcxWorkspace(def);
  }, [clusterData?.tools_workspace_default]);

  useEffect(() => {
    try {
      const h = localStorage.getItem(CUDA_NCCL_BUILD_HOST_STORAGE_KEY)?.trim();
      if (h) setCudaNcclBuildHost(h);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    try {
      const w = localStorage.getItem(NVLOOM_WORKSPACE_STORAGE_KEY)?.trim();
      if (w) {
        setNvloomWorkspace(w);
      } else {
        const fallback = localStorage.getItem(HPCX_WORKSPACE_STORAGE_KEY)?.trim();
        const def = clusterData?.tools_workspace_default?.trim();
        if (fallback) setNvloomWorkspace(fallback);
        else if (def) setNvloomWorkspace(def);
      }
      const bh = localStorage.getItem(NVLOOM_BUILD_HOST_STORAGE_KEY)?.trim();
      if (bh) {
        setNvloomBuildHost(bh);
      } else {
        const fallbackBh = localStorage.getItem(CUDA_NCCL_BUILD_HOST_STORAGE_KEY)?.trim();
        if (fallbackBh) setNvloomBuildHost(fallbackBh);
      }
      const cm = localStorage.getItem(NVLOOM_CUDA_MAJOR_STORAGE_KEY)?.trim();
      if (cm === "12" || cm === "13") setNvloomCudaMajor(cm);
    } catch {
      /* ignore */
    }
  }, [clusterData?.tools_workspace_default]);

  const persistNvloomWorkspace = useCallback((v: string) => {
    setNvloomWorkspace(v);
    try {
      if (v.trim()) localStorage.setItem(NVLOOM_WORKSPACE_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  const persistNvloomBuildHost = useCallback((v: string) => {
    setNvloomBuildHost(v);
    try {
      if (v.trim()) localStorage.setItem(NVLOOM_BUILD_HOST_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  const persistNvloomCudaMajor = useCallback((v: CudaMajorChoice) => {
    setNvloomCudaMajor(v);
    try {
      localStorage.setItem(NVLOOM_CUDA_MAJOR_STORAGE_KEY, v);
    } catch {
      /* ignore */
    }
  }, []);

  const persistCudaNcclBuildHost = useCallback((v: string) => {
    setCudaNcclBuildHost(v);
    try {
      if (v.trim()) localStorage.setItem(CUDA_NCCL_BUILD_HOST_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  // Exemplar (dgxc-benchmarking) workspace + gpu_type: restore from storage, else
  // fall back to the cluster workspace default / active-platform-derived gpu_type.
  useEffect(() => {
    try {
      const w = localStorage.getItem(DGXC_WORKSPACE_STORAGE_KEY)?.trim();
      if (w) {
        setDgxcWorkspace(w);
      } else {
        const fallback = localStorage.getItem(NVLOOM_WORKSPACE_STORAGE_KEY)?.trim();
        const def = clusterData?.tools_workspace_default?.trim();
        if (fallback) setDgxcWorkspace(fallback);
        else if (def) setDgxcWorkspace(def);
      }
      const g = localStorage.getItem(DGXC_GPU_TYPE_STORAGE_KEY)?.trim();
      if (g && (GPU_TYPE_CHOICES as string[]).includes(g)) {
        setDgxcGpuType(g as GpuTypeChoice);
      } else {
        setDgxcGpuType(deriveGpuType(clusterData?.product_line));
      }
    } catch {
      /* ignore */
    }
  }, [clusterData?.tools_workspace_default, clusterData?.product_line]);

  const persistDgxcWorkspace = useCallback((v: string) => {
    setDgxcWorkspace(v);
    try {
      if (v.trim()) localStorage.setItem(DGXC_WORKSPACE_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  const persistDgxcGpuType = useCallback((v: GpuTypeChoice) => {
    setDgxcGpuType(v);
    try {
      localStorage.setItem(DGXC_GPU_TYPE_STORAGE_KEY, v);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    const dest = destination.trim();
    if (!dest) {
      setNemoSqshPresent(false);
      setNemoSqshPath(null);
      return;
    }
    const ac = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        try {
          const res = await apiFetch(
            `/api/tools/download-containers-status?destination=${encodeURIComponent(dest)}`,
            { credentials: "same-origin", signal: ac.signal },
          );
          if (res.status === 401) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
            return;
          }
          const json = (await res.json()) as {
            ok?: boolean;
            exists?: boolean;
            existing_file?: string;
            error?: string;
          };
          if (ac.signal.aborted) return;
          if (!res.ok || !json.ok) {
            setNemoSqshPresent(false);
            setNemoSqshPath(null);
            return;
          }
          setNemoSqshPresent(Boolean(json.exists));
          setNemoSqshPath(json.existing_file?.trim() || null);
        } catch {
          if (ac.signal.aborted) return;
          setNemoSqshPresent(false);
          setNemoSqshPath(null);
        }
      })();
    }, 400);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [destination, nemoStatusTick]);

  useEffect(() => {
    const wsMain = hpcxWorkspace.trim();
    const wsNvloom = nvloomWorkspace.trim();
    if (!wsMain) {
      setToolchainAllReady(false);
      setToolchainCudaNvcc(false);
      setToolchainHpcxInit(false);
      setToolchainNccl(false);
      setToolchainTooltip("");
    }
    if (!wsNvloom) {
      setNvloomCliPresent(false);
    }
    if (!wsMain && !wsNvloom) return;

    const ac = new AbortController();
    const timer = window.setTimeout(() => {
      void (async () => {
        type ToolchainStatusOk = {
          ok: true;
          cuda_nvcc: boolean;
          hpcx_init: boolean;
          nccl: boolean;
          all_ready: boolean;
          nvloom_cli: boolean;
        };
        type ToolchainStatusResult = { ok: false; err: string } | ToolchainStatusOk;

        const fetchStatus = async (workspace: string, cuda_m: CudaMajorChoice): Promise<ToolchainStatusResult | null> => {
          const q = new URLSearchParams({ workspace, cuda_major: cuda_m });
          const res = await apiFetch(`/api/tools/workspace-toolchain-status?${q.toString()}`, {
            credentials: "same-origin",
            signal: ac.signal,
          });
          if (res.status === 401) {
            window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
            return null;
          }
          const json = (await res.json()) as {
            ok?: boolean;
            cuda_nvcc?: boolean;
            hpcx_init?: boolean;
            nccl_all_reduce_perf?: boolean;
            all_ready?: boolean;
            nvloom_cli?: boolean;
            error?: string;
          };
          if (!res.ok || !json.ok) {
            return { ok: false, err: json.error || `HTTP ${res.status}` };
          }
          return {
            ok: true,
            cuda_nvcc: Boolean(json.cuda_nvcc),
            hpcx_init: Boolean(json.hpcx_init),
            nccl: Boolean(json.nccl_all_reduce_perf),
            all_ready: Boolean(json.all_ready),
            nvloom_cli: Boolean(json.nvloom_cli),
          };
        };

        try {
          if (wsMain) {
            const r = await fetchStatus(wsMain, cudaMajor);
            if (ac.signal.aborted) return;
            if (r === null) return;
            if (!r.ok) {
              setToolchainAllReady(false);
              setToolchainCudaNvcc(false);
              setToolchainHpcxInit(false);
              setToolchainNccl(false);
              setToolchainTooltip(r.err);
            } else {
              setToolchainAllReady(r.all_ready);
              setToolchainCudaNvcc(r.cuda_nvcc);
              setToolchainHpcxInit(r.hpcx_init);
              setToolchainNccl(r.nccl);
              const missing: string[] = [];
              if (!r.cuda_nvcc) missing.push("nvcc (CUDA toolkit)");
              if (!r.hpcx_init) missing.push("HPC-X unpack");
              if (!r.nccl) missing.push("nccl/bins/all_reduce_perf");
              setToolchainTooltip(
                r.all_ready
                  ? "CUDA toolkit (nvcc), HPC-X bundle, and NCCL binaries are present for this workspace and CUDA major."
                  : `Not complete — missing: ${missing.join("; ")}.`,
              );
            }
          }

          if (wsNvloom) {
            const r = await fetchStatus(wsNvloom, nvloomCudaMajor);
            if (ac.signal.aborted) return;
            if (r === null) return;
            if (!r.ok) {
              setNvloomCliPresent(false);
            } else {
              setNvloomCliPresent(r.nvloom_cli);
            }
          }
        } catch (e) {
          if (ac.signal.aborted) return;
          if (wsMain) {
            setToolchainAllReady(false);
            setToolchainCudaNvcc(false);
            setToolchainHpcxInit(false);
            setToolchainNccl(false);
            setToolchainTooltip(e instanceof Error ? e.message : String(e));
          }
          if (wsNvloom) setNvloomCliPresent(false);
        }
      })();
    }, 400);
    return () => {
      ac.abort();
      window.clearTimeout(timer);
    };
  }, [hpcxWorkspace, cudaMajor, nvloomWorkspace, nvloomCudaMajor, toolchainProbeTick]);

  const persistDest = useCallback((v: string) => {
    setDestination(v);
    try {
      if (v.trim()) localStorage.setItem(DEST_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  const runDownload = useCallback(async () => {
    setError("");
    setResult(null);
    const dest = destination.trim();
    if (!dest) {
      setError("Enter an absolute destination directory.");
      return;
    }
    setLoading(true);
    setLiveRunning((r) => ({ ...r, nemo: true }));
    setLiveLogs((p) => ({ ...p, nemo: "" }));
    try {
      const res = await apiFetch("/api/tools/download-containers-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          destination: dest,
          ...(arch ? { arch } : {}),
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, nemo: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const skipped = text.toLowerCase().includes("skipping download");
      const skipPath = text.match(/skipping download:\s*(.+?)(\r?\n|$)/i)?.[1]?.trim();
      const lines = text.split("\n");
      const firstLine = lines[0]?.replace(/^#\s*/, "").trim() || "";
      const commandDisplay =
        firstLine && !firstLine.startsWith("ERROR") && !firstLine.startsWith("{") ? firstLine : "05-download-containers.sh";
      setResult({
        ok: Boolean(res.ok && exitOk),
        skipped,
        existing_file: skipped ? skipPath : undefined,
        returncode: rc ?? undefined,
        command_display: commandDisplay,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setError(errMsg);
      } else {
        setError("");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setLiveRunning((r) => ({ ...r, nemo: false }));
      setNemoStatusTick((n) => n + 1);
    }
  }, [destination, arch]);

  const persistHpcxWorkspace = useCallback((v: string) => {
    setHpcxWorkspace(v);
    try {
      if (v.trim()) localStorage.setItem(HPCX_WORKSPACE_STORAGE_KEY, v.trim());
    } catch {
      /* ignore */
    }
  }, []);

  const runHpcxInstall = useCallback(async () => {
    setHpcxError("");
    setHpcxResult(null);
    const ws = hpcxWorkspace.trim();
    if (!ws) {
      setHpcxError("Enter an absolute workspace directory.");
      return;
    }
    setHpcxLoading(true);
    setLiveRunning((r) => ({ ...r, hpcx: true }));
    setLiveLogs((p) => ({ ...p, hpcx: "" }));
    try {
      const res = await apiFetch("/api/tools/install-hpcx-workspace-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          workspace: ws,
          cuda_major: cudaMajor,
          arch: "aarch64",
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, hpcx: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setHpcxResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setHpcxError(errMsg);
      } else {
        setHpcxError("");
      }
    } catch (e) {
      setHpcxError(e instanceof Error ? e.message : String(e));
    } finally {
      setHpcxLoading(false);
      setLiveRunning((r) => ({ ...r, hpcx: false }));
      setToolchainProbeTick((n) => n + 1);
    }
  }, [hpcxWorkspace, cudaMajor]);

  const runCudaNcclInstall = useCallback(async () => {
    setCudaNcclError("");
    setCudaNcclResult(null);
    const ws = hpcxWorkspace.trim();
    if (!ws) {
      setCudaNcclError("Enter an absolute workspace directory.");
      return;
    }
    if (!installCudaToolkit && !buildNccl) {
      setCudaNcclError("Select at least one: install CUDA toolkit and/or build NCCL.");
      return;
    }
    const bh = cudaNcclBuildHost.trim();
    if (!bh) {
      setCudaNcclError("Choose or enter a build host (compute node with GPUs; shared workspace path).");
      return;
    }
    setCudaNcclLoading(true);
    setLiveRunning((r) => ({ ...r, cudaNccl: true }));
    setLiveLogs((p) => ({ ...p, cudaNccl: "" }));
    try {
      const res = await apiFetch("/api/tools/install-cuda-nccl-workspace-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          workspace: ws,
          cuda_major: cudaMajor,
          arch: "aarch64",
          install_cuda_toolkit: installCudaToolkit,
          build_nccl: buildNccl,
          build_host: bh,
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, cudaNccl: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setCudaNcclResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setCudaNcclError(errMsg);
      } else {
        setCudaNcclError("");
      }
    } catch (e) {
      setCudaNcclError(e instanceof Error ? e.message : String(e));
    } finally {
      setCudaNcclLoading(false);
      setLiveRunning((r) => ({ ...r, cudaNccl: false }));
      setToolchainProbeTick((n) => n + 1);
    }
  }, [hpcxWorkspace, cudaMajor, installCudaToolkit, buildNccl, cudaNcclBuildHost]);

  const runDcgmInstall = useCallback(async () => {
    setDcgmError("");
    setDcgmResult(null);
    const ws = hpcxWorkspace.trim();
    if (!ws) {
      setDcgmError("Enter an absolute workspace directory (shared /home path visible to compute nodes).");
      return;
    }
    setDcgmLoading(true);
    setLiveRunning((r) => ({ ...r, dcgm: true }));
    setLiveLogs((p) => ({ ...p, dcgm: "" }));
    try {
      const res = await apiFetch("/api/tools/install-dcgm-workspace-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          workspace: ws,
          cuda_major: dcgmCudaMajor,
          arch: dcgmArch,
          distro: dcgmDistro,
          version: dcgmVersion.trim(),
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, dcgm: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setDcgmResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setDcgmError(errMsg);
      } else {
        setDcgmError("");
      }
    } catch (e) {
      setDcgmError(e instanceof Error ? e.message : String(e));
    } finally {
      setDcgmLoading(false);
      setLiveRunning((r) => ({ ...r, dcgm: false }));
    }
  }, [hpcxWorkspace, dcgmCudaMajor, dcgmArch, dcgmDistro, dcgmVersion]);

  const runLlmbNccl = useCallback(async () => {
    setLlmbError("");
    setLlmbResult(null);
    setLlmbPhase("");
    const racks = llmbRacks
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (racks.length === 0) {
      setLlmbError("Enter one or more Nscale rack ids (e.g. ink50-dh01-h04).");
      return;
    }
    setLlmbLoading(true);
    try {
      const startRes = await apiFetch("/api/run-llmb-nccl-async", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ racks, dry_run: llmbDryRun, log_dir: logDir }),
      });
      if (startRes.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const startJson = (await startRes.json()) as {
        ok?: boolean;
        job_id?: string;
        error?: string;
        client_poll_deadline_ms?: number;
      };
      if (!startRes.ok || !startJson.ok || !startJson.job_id) {
        setLlmbError(startJson.error || `HTTP ${startRes.status}`);
        return;
      }
      const jobId = startJson.job_id;
      setLlmbPhase("starting");
      const pollBudget = Math.min(
        Math.max(Number(startJson.client_poll_deadline_ms) || 2 * 60 * 60 * 1000, 60_000),
        48 * 60 * 60 * 1000,
      );
      const deadline = Date.now() + pollBudget;
      // Poll until terminal, server error, or the server-advertised deadline.
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 4000));
        const sRes = await apiFetch(`/api/run-llmb-nccl-status?job_id=${encodeURIComponent(jobId)}`);
        const statusText = await sRes.text();
        let sJson: {
          ok?: boolean;
          phase?: string;
          returncode?: number;
          command_display?: string;
          stdout?: string;
          stderr?: string;
          error?: string;
        };
        try {
          sJson = JSON.parse(statusText) as typeof sJson;
        } catch {
          throw new Error(`invalid LLMB status response (HTTP ${sRes.status})`);
        }
        if (!sRes.ok || sJson.ok === false) {
          throw new Error(sJson.error || `LLMB status failed (HTTP ${sRes.status})`);
        }
        const phase = sJson.phase || "unknown";
        setLlmbPhase(phase);
        if (phase === "done" || phase === "error" || phase === "timeout") {
          setLlmbResult({
            ok: phase === "done",
            returncode: sJson.returncode,
            command_display: sJson.command_display,
            stdout: sJson.stdout,
            stderr: sJson.stderr || (phase !== "done" ? sJson.error : ""),
          });
          if (phase !== "done") setLlmbError(sJson.error || `Job ${phase}`);
          break;
        }
      }
      if (Date.now() >= deadline) setLlmbError("Timed out waiting for LLMB NCCL status; the server job may still be running.");
    } catch (e) {
      setLlmbError(e instanceof Error ? e.message : String(e));
    } finally {
      setLlmbLoading(false);
    }
  }, [llmbRacks, llmbDryRun, logDir]);

  const runNvloomInstall = useCallback(async () => {
    setNvloomError("");
    setNvloomResult(null);
    const ws = nvloomWorkspace.trim();
    if (!ws) {
      setNvloomError("Enter an absolute workspace directory.");
      return;
    }
    const bh = nvloomBuildHost.trim();
    if (!bh) {
      setNvloomError("Choose or enter a build host (compute node with GPUs; shared workspace path).");
      return;
    }
    setNvloomLoading(true);
    setLiveRunning((r) => ({ ...r, nvloom: true }));
    setLiveLogs((p) => ({ ...p, nvloom: "" }));
    try {
      const res = await apiFetch("/api/tools/install-nvloom-workspace-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          workspace: ws,
          cuda_major: nvloomCudaMajor,
          build_host: bh,
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, nvloom: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setNvloomResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setNvloomError(errMsg);
      } else {
        setNvloomError("");
      }
    } catch (e) {
      setNvloomError(e instanceof Error ? e.message : String(e));
    } finally {
      setNvloomLoading(false);
      setLiveRunning((r) => ({ ...r, nvloom: false }));
      setToolchainProbeTick((n) => n + 1);
    }
  }, [nvloomWorkspace, nvloomCudaMajor, nvloomBuildHost]);

  const runDgxcSetup = useCallback(async () => {
    setDgxcError("");
    setDgxcResult(null);
    const ws = dgxcWorkspace.trim();
    if (!ws) {
      setDgxcError("Enter an absolute workspace directory (shared filesystem).");
      return;
    }
    setDgxcLoading(true);
    setLiveRunning((r) => ({ ...r, dgxc: true }));
    setLiveLogs((p) => ({ ...p, dgxc: "" }));
    if (dgxcMode === "install") openLive("dgxc");
    try {
      const res = await apiFetch("/api/tools/install-dgxc-benchmarking-workspace-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({
          workspace: ws,
          mode: dgxcMode,
          gpu_type: dgxcGpuType,
          use_uv: dgxcUseUv,
          trust_git: dgxcTrustGit,
          image_folder: dgxcImageFolder.trim() || undefined,
        }),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, dgxc: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setDgxcResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setDgxcError(errMsg);
      } else {
        setDgxcError("");
      }
    } catch (e) {
      setDgxcError(e instanceof Error ? e.message : String(e));
    } finally {
      setDgxcLoading(false);
      setLiveRunning((r) => ({ ...r, dgxc: false }));
    }
  }, [dgxcWorkspace, dgxcMode, dgxcGpuType, dgxcUseUv, dgxcTrustGit, dgxcImageFolder, openLive]);

  const runGrafanaLaunch = useCallback(async () => {
    setGrafanaError("");
    setGrafanaResult(null);
    setGrafanaLoading(true);
    setLiveRunning((r) => ({ ...r, grafana: true }));
    setLiveLogs((p) => ({ ...p, grafana: "" }));
    try {
      const res = await apiFetch("/api/tools/launch-grafana-bright-prometheus-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({}),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const sink = createThrottledLogSink((t) => setLiveLogs((p) => ({ ...p, grafana: t })));
      const text = await consumePlainTextStream(res, sink);
      sink.flush();
      const rc = parseToolsStreamExit(text);
      const exitOk = rc === null ? res.ok : rc === 0;
      const firstLine = text.split("\n")[0]?.replace(/^#\s*/, "").trim() || "";
      setGrafanaResult({
        ok: Boolean(res.ok && exitOk),
        returncode: rc ?? undefined,
        command_display: firstLine && !firstLine.startsWith("ERROR") ? firstLine : undefined,
        stdout: text,
        stderr: "",
      });
      if (!res.ok || !exitOk) {
        let errMsg = `HTTP ${res.status}`;
        if (res.headers.get("content-type")?.includes("application/json")) {
          try {
            const j = JSON.parse(text) as { error?: string };
            if (j.error) errMsg = j.error;
          } catch {
            /* ignore */
          }
        } else if (text.trim().startsWith("ERROR:")) {
          errMsg = text.trim().split("\n")[0] ?? errMsg;
        } else if (rc != null && rc !== 0) {
          errMsg = `Script exited with code ${rc}`;
        }
        setGrafanaError(errMsg);
      } else {
        setGrafanaError("");
      }
    } catch (e) {
      setGrafanaError(e instanceof Error ? e.message : String(e));
    } finally {
      setGrafanaLoading(false);
      setLiveRunning((r) => ({ ...r, grafana: false }));
    }
  }, []);

  return (
    <Box sx={{ maxWidth: CONTENT_MAX_WIDTH.comfortable, mx: "auto", p: 4 }}>
      <PageHeader
        contentId="tools"
        title="Utility"
        description="Operational helpers under bin/scripts: NEMO download (NGC_API_KEY), workspace Mellanox HPC-X (install-hpcx-workspace.sh), workspace CUDA toolkit + NCCL (install-cuda-nccl-workspace.sh; aihpc_val 02 + 03), NVIDIA nvloom clone + build (install-nvloom-workspace.sh), Exemplar / dgxc-benchmarking llmb-run setup (install-dgxc-benchmarking-workspace.sh), Grafana + Docker. Green dot opens live streamed output for that card."
      />

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            NCCL Microbenchmark (LLMB)
          </Typography>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Runs <Box component="code" sx={{ fontSize: "0.85em" }}>run_llmb_nccl.sh</Box> (
          <Box component="code" sx={{ fontSize: "0.85em" }}>llmb-run submit -w microbenchmark_nccl -d fp8</Box>, MOP §7.3). Enter
          one or more Nscale rack ids (e.g. <Box component="code" sx={{ fontSize: "0.85em" }}>ink50-dh01-h04</Box>);
          multiple racks run the cross-rack form. The wrapper resolves real PythonCM-managed compute nodes and computes GPU scale (nodes×4; 72 per GB300 NVL72
          rack). Use <strong>Dry run</strong> to print the resolved <Box component="code" sx={{ fontSize: "0.85em" }}>llmb-run</Box>{" "}
          command without submitting.
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Rack id(s) — comma or space separated"
            size="small"
            fullWidth
            value={llmbRacks}
            onChange={(e) => setLlmbRacks(e.target.value)}
            placeholder="ink50-dh01-h04"
          />
          <FormControlLabel
            control={<Checkbox checked={llmbDryRun} onChange={(e) => setLlmbDryRun(e.target.checked)} size="small" />}
            label="Dry run (resolve + print the llmb-run command, do not submit)"
          />
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button
              variant="contained"
              onClick={() => void runLlmbNccl()}
              disabled={llmbLoading || !llmbRacks.trim()}
            >
              {llmbLoading ? "Running…" : llmbDryRun ? "Run LLMB NCCL (dry run)" : "Run LLMB NCCL"}
            </Button>
            {llmbLoading && <CircularProgress size={22} />}
            {llmbLoading && llmbPhase && (
              <Typography variant="body2" color="text.secondary">
                phase: {llmbPhase}
              </Typography>
            )}
          </Box>
          {llmbError ? (
            <Alert severity="error" sx={{ whiteSpace: "pre-wrap" }}>
              {llmbError}
            </Alert>
          ) : null}
          {llmbResult ? (
            <Alert severity={llmbResult.ok ? "success" : "error"} sx={{ whiteSpace: "pre-wrap" }}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>
                LLMB NCCL — {llmbResult.ok ? "success" : "failed"}
                {llmbResult.returncode != null ? ` — exit ${llmbResult.returncode}` : ""}
              </Typography>
              {llmbResult.command_display ? (
                <Box component="pre" sx={{ m: 0, mt: 1, fontSize: "0.8em", whiteSpace: "pre-wrap" }}>
                  {llmbResult.command_display}
                </Box>
              ) : null}
              {llmbResult.stdout ? <CollapsibleLog text={llmbResult.stdout} label="output" /> : null}
              {llmbResult.stderr ? <CollapsibleLog text={llmbResult.stderr} label="stderr" /> : null}
            </Alert>
          ) : null}
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("nemo")}
              aria-label="Open NEMO download live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            Download NEMO container (enroot import)
          </Typography>
          {nemoSqshPresent && <InstalledLabel />}
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Runs <Box component="code" sx={{ fontSize: "0.85em" }}>05-download-containers.sh</Box> from the app’s{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>bin/scripts</Box> directory. The destination folder is
          created if needed. Set <Box component="code" sx={{ fontSize: "0.85em" }}>NGC_API_KEY</Box> on the server before
          starting Flask (not required if <Box component="code" sx={{ fontSize: "0.85em" }}>nemo-25.11.01.sqsh</Box>{" "}
          already exists in the destination). Output is streamed live (green dot); the run continues until the script
          finishes or times out (see{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT</Box>).
        </Typography>

        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Destination directory (absolute path)"
            size="small"
            fullWidth
            value={destination}
            onChange={(e) => persistDest(e.target.value)}
            placeholder={
              clusterData?.tools_download_destination_default?.trim() ||
              "Set NEMO_SQSH_SRC on server for default (parent of .sqsh path)"
            }
            helperText={
              clusterData?.tools_download_destination_default
                ? `From NEMO_SQSH_SRC (directory for .sqsh output): ${clusterData.tools_download_destination_default}`
                : "Set NEMO_SQSH_SRC on the server (e.g. in start.sh) to pre-fill this field."
            }
            slotProps={{
              input: {
                endAdornment: nemoSqshPresent ? (
                  <InputAdornment position="end">
                    <Tooltip title={nemoSqshPath ? `Found: ${nemoSqshPath}` : "NEMO .sqsh already present"}>
                      <Box
                        component="span"
                        sx={{ color: "success.main", display: "inline-flex", alignItems: "center" }}
                        aria-label="Container image file present"
                      >
                        <CheckCircle size={20} aria-hidden />
                      </Box>
                    </Tooltip>
                  </InputAdornment>
                ) : undefined,
              },
            }}
          />
          <TextField
            select
            label="enroot --arch"
            size="small"
            sx={{ maxWidth: 280 }}
            value={arch}
            onChange={(e) => setArch(e.target.value as ArchChoice)}
          >
            <MenuItem value="aarch64">aarch64 (default)</MenuItem>
            <MenuItem value="x86_64">x86_64</MenuItem>
            <MenuItem value="">Use script default only</MenuItem>
          </TextField>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button variant="contained" onClick={() => void runDownload()} disabled={loading}>
              {loading ? "Running…" : "Run download"}
            </Button>
            {loading && <CircularProgress size={22} />}
          </Box>
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("hpcx")}
              aria-label="Open Mellanox HPC-X workspace install live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <Cpu size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            Mellanox HPC-X (workspace, aarch64)
          </Typography>
          {toolchainHpcxInit && <InstalledLabel />}
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Runs <Box component="code" sx={{ fontSize: "0.85em" }}>install-hpcx-workspace.sh</Box> (sourced constants from{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>workspace-cuda-major-common.sh</Box>): download/unpack the HPC-X
          tarball for the chosen CUDA major, then <Box component="code" sx={{ fontSize: "0.85em" }}>hpcx_load</Box>. For
          NVIDIA CUDA toolkit and/or NCCL build, use the card below (<Box component="code" sx={{ fontSize: "0.85em" }}>install-cuda-nccl-workspace.sh</Box>).
          Timeout: <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT</Box> (default
          28800s).
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={hpcxWorkspace}
            onChange={(e) => persistHpcxWorkspace(e.target.value)}
            placeholder={clusterData?.tools_workspace_default || "/home/<user>/workspace"}
            slotProps={{
              input: {
                endAdornment: toolchainAllReady ? (
                  <InputAdornment position="end">
                    <Tooltip title={toolchainTooltip || "CUDA, HPC-X, and NCCL present"}>
                      <Box
                        component="span"
                        sx={{ color: "success.main", display: "inline-flex", alignItems: "center" }}
                        aria-label="Workspace toolchain complete"
                      >
                        <CheckCircle size={20} aria-hidden />
                      </Box>
                    </Tooltip>
                  </InputAdornment>
                ) : undefined,
              },
            }}
          />
          <TextField
            select
            label="CUDA major (HPC-X bundle)"
            size="small"
            sx={{ maxWidth: 280 }}
            value={cudaMajor}
            onChange={(e) => setCudaMajor(e.target.value as CudaMajorChoice)}
          >
            <MenuItem value="12">CUDA 12 (HPC-X v2.22.1, aarch64)</MenuItem>
            <MenuItem value="13">CUDA 13 (HPC-X v2.25.1, aarch64)</MenuItem>
          </TextField>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button variant="contained" onClick={() => void runHpcxInstall()} disabled={hpcxLoading}>
              {hpcxLoading ? "Running…" : "Run HPC-X install"}
            </Button>
            {hpcxLoading && <CircularProgress size={22} />}
          </Box>
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("cudaNccl")}
              aria-label="Open CUDA toolkit and NCCL workspace install live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <Cpu size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            CUDA toolkit &amp; NCCL (workspace, aarch64)
          </Typography>
          {(toolchainCudaNvcc || toolchainNccl) && (
            <Tooltip
              title={[
                ...(toolchainCudaNvcc ? ["CUDA toolkit (nvcc)"] : []),
                ...(toolchainNccl ? ["NCCL all_reduce_perf"] : []),
              ].join(" · ")}
            >
              <Box component="span" sx={{ display: "inline-flex", alignItems: "center" }}>
                <InstalledLabel />
              </Box>
            </Tooltip>
          )}
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          <Box component="code" sx={{ fontSize: "0.85em" }}>01-download-cuda-nccl-workspace.sh</Box> runs on this dashboard
          host. Then <Box component="code" sx={{ fontSize: "0.85em" }}>02-install-cuda-nccl-workspace.sh</Box>{" "}
          runs on the chosen compute node through the documented on-demand fallback. NCCL needs HPC-X and{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>nvcc</Box> on that node. Timeout:{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT</Box>.
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={hpcxWorkspace}
            onChange={(e) => persistHpcxWorkspace(e.target.value)}
            placeholder={clusterData?.tools_workspace_default || "/home/<user>/workspace"}
          />
          <TextField
            label="Build host"
            size="small"
            fullWidth
            value={cudaNcclBuildHost}
            onChange={(e) => persistCudaNcclBuildHost(e.target.value)}
            placeholder="e.g. ink50-dh01-h04-11"
            helperText="Suggestions from rack status (physical_rf0); you may type any reachable hostname."
            slotProps={{ input: { inputProps: { list: "nvis-cuda-nccl-build-hosts" } } }}
          />
          <datalist id="nvis-cuda-nccl-build-hosts">
            {(clusterData?.cuda_nccl_build_host_candidates ?? []).map((h) => (
              <option key={h} value={h} />
            ))}
          </datalist>
          <TextField
            select
            label="CUDA major"
            size="small"
            sx={{ maxWidth: 280 }}
            value={cudaMajor}
            onChange={(e) => setCudaMajor(e.target.value as CudaMajorChoice)}
          >
            <MenuItem value="12">CUDA 12</MenuItem>
            <MenuItem value="13">CUDA 13</MenuItem>
          </TextField>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 0.5 }}>
            <FormControlLabel
              control={
                <Checkbox
                  checked={installCudaToolkit}
                  onChange={(_, c) => setInstallCudaToolkit(c)}
                  size="small"
                />
              }
              label="Install CUDA toolkit (02 — NVIDIA .run silent, cuda12/cuda13 + env.sh)"
            />
            <FormControlLabel
              control={<Checkbox checked={buildNccl} onChange={(_, c) => setBuildNccl(c)} size="small" />}
              label="Build NCCL + nccl-tests (03 — needs HPC-X unpack + nvcc + OMPI_HOME)"
            />
          </Box>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button
              variant="contained"
              onClick={() => void runCudaNcclInstall()}
              disabled={
                cudaNcclLoading || (!installCudaToolkit && !buildNccl) || !cudaNcclBuildHost.trim()
              }
            >
              {cudaNcclLoading ? "Running…" : "Run CUDA / NCCL install"}
            </Button>
            {cudaNcclLoading && <CircularProgress size={22} />}
          </Box>
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("dcgm")}
              aria-label="Open DCGM / DCGMI workspace install live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <Gauge size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            DCGM / DCGMI (workspace)
          </Typography>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Downloads NVIDIA Data Center GPU Manager (DCGM 4.x) and extracts it into the shared workspace on this
          dashboard host — no per-node root install. Because the workspace lives on shared{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>/home</Box>, every compute node sees the same binaries. The{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>mnubergemm</Box>/
          <Box component="code" sx={{ fontSize: "0.85em" }}>ubergemm</Box> stress binary ships in the DCGM cuda-major
          plugins (DCGM 4.3.0+); the installer prints{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>NVIS_MNUBERGEMM_BIN</Box> for{" "}
          MNUberGEMM stress runner. Pick the{" "}
          <strong>target compute-node</strong> architecture (GBx00 = ARM, Bx00 = x86), not this head node&apos;s arch.
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={hpcxWorkspace}
            onChange={(e) => persistHpcxWorkspace(e.target.value)}
            placeholder={clusterData?.tools_workspace_default || "/home/<user>/workspace"}
          />
          <TextField
            select
            label="Target architecture (compute node)"
            size="small"
            sx={{ maxWidth: 360 }}
            value={dcgmArch}
            onChange={(e) => setDcgmArch(e.target.value as "aarch64" | "x86_64")}
          >
            <MenuItem value="aarch64">aarch64 — GBx00 / Grace-Blackwell (ARM)</MenuItem>
            <MenuItem value="x86_64">x86_64 — Bx00 (x86)</MenuItem>
          </TextField>
          <TextField
            select
            label="Linux flavor (package repo)"
            size="small"
            sx={{ maxWidth: 360 }}
            value={dcgmDistro}
            onChange={(e) => setDcgmDistro(e.target.value)}
          >
            <MenuItem value="dgx">NVIDIA DGX OS (Ubuntu 24.04)</MenuItem>
            <MenuItem value="ubuntu2404">Ubuntu 24.04</MenuItem>
            <MenuItem value="ubuntu2204">Ubuntu 22.04</MenuItem>
            <MenuItem value="ubuntu2004">Ubuntu 20.04</MenuItem>
            <MenuItem value="debian12">Debian 12</MenuItem>
            <MenuItem value="rhel9">RHEL 9</MenuItem>
            <MenuItem value="rhel8">RHEL 8</MenuItem>
            <MenuItem value="sles15">SLES 15</MenuItem>
          </TextField>
          <TextField
            select
            label="CUDA major (DCGM plugins)"
            size="small"
            sx={{ maxWidth: 280 }}
            value={dcgmCudaMajor}
            onChange={(e) => setDcgmCudaMajor(e.target.value as CudaMajorChoice)}
          >
            <MenuItem value="12">CUDA 12</MenuItem>
            <MenuItem value="13">CUDA 13</MenuItem>
          </TextField>
          <TextField
            label="Version pin (optional)"
            size="small"
            sx={{ maxWidth: 280 }}
            value={dcgmVersion}
            onChange={(e) => setDcgmVersion(e.target.value)}
            placeholder="e.g. 4.5.3"
            helperText="Leave blank for the newest available in the repo."
          />
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button variant="contained" onClick={() => void runDcgmInstall()} disabled={dcgmLoading}>
              {dcgmLoading ? "Running…" : "Install DCGM to workspace"}
            </Button>
            {dcgmLoading && <CircularProgress size={22} />}
          </Box>
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("nvloom")}
              aria-label="Open nvloom install live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <Cpu size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            NVIDIA nvloom (clone + build)
          </Typography>
          {nvloomCliPresent && (
            <Tooltip title="nvloom_cli present under this workspace (in-tree build).">
              <Box component="span" sx={{ display: "inline-flex", alignItems: "center" }}>
                <InstalledLabel />
              </Box>
            </Tooltip>
          )}
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Clones NVIDIA nvloom and downloads the required CMake toolchain on the dashboard host, then runs the
          existing build recipe on the selected build host through the documented on-demand fallback. Timeout:{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT</Box>.
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={nvloomWorkspace}
            onChange={(e) => persistNvloomWorkspace(e.target.value)}
            placeholder={clusterData?.tools_workspace_default || "/home/<user>/workspace"}
          />
          <TextField
            label="Build host"
            size="small"
            fullWidth
            value={nvloomBuildHost}
            onChange={(e) => persistNvloomBuildHost(e.target.value)}
            placeholder="e.g. ink50-dh01-h04-11"
            helperText="Suggestions from rack status (physical_rf0); you may type any reachable hostname."
            slotProps={{ input: { inputProps: { list: "nvis-nvloom-build-hosts" } } }}
          />
          <datalist id="nvis-nvloom-build-hosts">
            {(clusterData?.cuda_nccl_build_host_candidates ?? []).map((h) => (
              <option key={h} value={h} />
            ))}
          </datalist>
          <TextField
            select
            label="CUDA major (toolkit path cuda12 / cuda13)"
            size="small"
            sx={{ maxWidth: 280 }}
            value={nvloomCudaMajor}
            onChange={(e) => persistNvloomCudaMajor(e.target.value as CudaMajorChoice)}
          >
            <MenuItem value="12">CUDA 12</MenuItem>
            <MenuItem value="13">CUDA 13</MenuItem>
          </TextField>
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button
              variant="contained"
              onClick={() => void runNvloomInstall()}
              disabled={nvloomLoading || !nvloomBuildHost.trim() || !nvloomWorkspace.trim()}
            >
              {nvloomLoading ? "Running…" : "Run install nvloom"}
            </Button>
            {nvloomLoading && <CircularProgress size={22} />}
          </Box>
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("dgxc")}
              aria-label="Open Exemplar (dgxc-benchmarking) setup live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{ width: 14, height: 14, borderRadius: "50%", bgcolor: "success.main", display: "block" }}
              />
            </IconButton>
          </Tooltip>
          <LineChart size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            Download/Setup Exemplar (dgxc-benchmarking)
          </Typography>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Runs <Box component="code" sx={{ fontSize: "0.85em" }}>install-dgxc-benchmarking-workspace.sh</Box> to prepare
          NVIDIA dgxc-benchmarking and llmb-run. Record creates a reviewable headless configuration; Install also runs
          the packaged installer and may use the documented Slurm fallback for workload-manager operations.
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path, shared filesystem)"
            size="small"
            fullWidth
            value={dgxcWorkspace}
            onChange={(e) => persistDgxcWorkspace(e.target.value)}
            placeholder={clusterData?.tools_workspace_default || "/home/<user>/workspace"}
            helperText="Repo clones to <workspace>/dgxc-benchmarking; workloads install to <workspace>/llmb (LLMB_INSTALL)."
          />
          <Box sx={{ display: "flex", gap: 2, flexWrap: "wrap" }}>
            <TextField
              select
              label="Action"
              size="small"
              sx={{ minWidth: 260 }}
              value={dgxcMode}
              onChange={(e) => setDgxcMode(e.target.value as ExemplarMode)}
            >
              <MenuItem value="record">Record (clone + write config, no install)</MenuItem>
              <MenuItem value="install">Install (headless --play; long-running)</MenuItem>
            </TextField>
            <TextField
              select
              label="GPU type (llmb)"
              size="small"
              sx={{ minWidth: 160 }}
              value={dgxcGpuType}
              onChange={(e) => persistDgxcGpuType(e.target.value as GpuTypeChoice)}
              helperText="Defaults from active platform"
            >
              {GPU_TYPE_CHOICES.map((g) => (
                <MenuItem key={g} value={g}>
                  {g}
                </MenuItem>
              ))}
            </TextField>
          </Box>
          <TextField
            label="Shared container image folder (optional, -i)"
            size="small"
            fullWidth
            value={dgxcImageFolder}
            onChange={(e) => setDgxcImageFolder(e.target.value)}
            placeholder="e.g. /shared/llmb-images (recommended for multi-install reuse)"
          />
          <FormControlLabel
            control={<Checkbox checked={dgxcUseUv} onChange={(e) => setDgxcUseUv(e.target.checked)} size="small" />}
            label="Use uv (standalone Python 3.12 — required when the node lacks python3-venv/ensurepip; needs astral.sh)"
          />
          <FormControlLabel
            control={<Checkbox checked={dgxcTrustGit} onChange={(e) => setDgxcTrustGit(e.target.checked)} size="small" />}
            label="Trust workspace git repos (auto-fix 'dubious ownership' — chowns the workspace when sudo is available and registers git safe.directory for the installer's nested dependency clones)"
          />
          {dgxcMode === "install" ? (
            <Alert severity="warning">
              Install mode submits scheduler tasks to download the NeMo container and installs all selected workloads — this can
              run for hours. Use Record first to review the generated config.
            </Alert>
          ) : null}
          <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
            <Button
              variant="contained"
              onClick={() => void runDgxcSetup()}
              disabled={dgxcLoading || !dgxcWorkspace.trim()}
            >
              {dgxcLoading ? "Running…" : dgxcMode === "install" ? "Run Exemplar install" : "Record Exemplar config"}
            </Button>
            {dgxcLoading && <CircularProgress size={22} />}
          </Box>
          {dgxcError ? (
            <Alert severity="error" sx={{ whiteSpace: "pre-wrap" }}>
              {dgxcError}
            </Alert>
          ) : null}
          {dgxcResult ? (
            <Alert severity={dgxcResult.ok ? "success" : "error"} sx={{ whiteSpace: "pre-wrap" }}>
              <Typography variant="body2" sx={{ fontWeight: 700 }}>
                Exemplar {dgxcMode} — {dgxcResult.ok ? "success" : "failed"}
                {dgxcResult.returncode != null ? ` — exit ${dgxcResult.returncode}` : ""}
              </Typography>
              {dgxcResult.stdout ? <CollapsibleLog text={dgxcResult.stdout} label="output" /> : null}
            </Alert>
          ) : null}
        </Box>
      </Paper>

      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, mb: 1 }}>
          <Tooltip title="Open live output (streams while the script runs)">
            <IconButton
              size="small"
              onClick={() => openLive("grafana")}
              aria-label="Open Grafana launch live output"
              sx={{ p: 0.75 }}
            >
              <Box
                component="span"
                sx={{
                  width: 14,
                  height: 14,
                  borderRadius: "50%",
                  bgcolor: "success.main",
                  display: "block",
                }}
              />
            </IconButton>
          </Tooltip>
          <LineChart size={20} aria-hidden />
          <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
            Grafana + Bright Prometheus (Docker)
          </Typography>
        </Box>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Runs <Box component="code" sx={{ fontSize: "0.85em" }}>launch-grafana-bright-prometheus.sh</Box> from{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>bin/scripts</Box> (working directory is the app root). Requires
          Docker on the dashboard host. Prometheus basic auth uses{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_PROMETHEUS_BASIC_AUTH_USER</Box> /{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_PROMETHEUS_BASIC_AUTH_PASSWORD</Box> when both are set in
          Settings; otherwise the script looks for <Box component="code" sx={{ fontSize: "0.85em" }}>.prometheusrc</Box> like
          the CLI. Pulls the Grafana image and may run for several minutes; timeout is{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT</Box> (default
          7200s).
        </Typography>
        <Box sx={{ display: "flex", alignItems: "center", gap: 2 }}>
          <Button variant="contained" onClick={() => void runGrafanaLaunch()} disabled={grafanaLoading}>
            {grafanaLoading ? "Running…" : "Launch script"}
          </Button>
          {grafanaLoading && <CircularProgress size={22} />}
        </Box>
      </Paper>

      {error ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      ) : null}

      {result?.ok && result.skipped ? (
        <Alert severity="info" sx={{ mb: 2 }}>
          Output file already exists (non-empty); download was skipped.
          {result.existing_file ? ` Path: ${result.existing_file}` : ""}
        </Alert>
      ) : null}

      {grafanaError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {grafanaError}
        </Alert>
      ) : null}

      {hpcxError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {hpcxError}
        </Alert>
      ) : null}

      {cudaNcclError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {cudaNcclError}
        </Alert>
      ) : null}

      {nvloomError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {nvloomError}
        </Alert>
      ) : null}

      {dcgmError ? (
        <Alert severity="error" sx={{ mb: 2 }}>
          {dcgmError}
        </Alert>
      ) : null}

      {result ? (
        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Result{" "}
            {result.skipped ? "(skipped — file present)" : result.ok ? "(success)" : "(failed)"}
            {result.returncode != null ? ` — exit ${result.returncode}` : ""}
          </Typography>
          {result.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {result.command_display}
            </Typography>
          ) : null}
          {result.stdout ? <CollapsibleLog text={result.stdout} label="output" /> : null}
          {result.stderr ? <CollapsibleLog text={result.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      {hpcxResult ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Mellanox HPC-X — {hpcxResult.ok ? "success" : "failed"}
            {hpcxResult.returncode != null ? ` — exit ${hpcxResult.returncode}` : ""}
          </Typography>
          {hpcxResult.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {hpcxResult.command_display}
            </Typography>
          ) : null}
          {hpcxResult.stdout ? <CollapsibleLog text={hpcxResult.stdout} label="output" /> : null}
          {hpcxResult.stderr ? <CollapsibleLog text={hpcxResult.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      {cudaNcclResult ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            CUDA toolkit &amp; NCCL — {cudaNcclResult.ok ? "success" : "failed"}
            {cudaNcclResult.returncode != null ? ` — exit ${cudaNcclResult.returncode}` : ""}
          </Typography>
          {cudaNcclResult.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {cudaNcclResult.command_display}
            </Typography>
          ) : null}
          {cudaNcclResult.stdout ? <CollapsibleLog text={cudaNcclResult.stdout} label="output" /> : null}
          {cudaNcclResult.stderr ? <CollapsibleLog text={cudaNcclResult.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      {nvloomResult ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            nvloom — {nvloomResult.ok ? "success" : "failed"}
            {nvloomResult.returncode != null ? ` — exit ${nvloomResult.returncode}` : ""}
          </Typography>
          {nvloomResult.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {nvloomResult.command_display}
            </Typography>
          ) : null}
          {nvloomResult.stdout ? <CollapsibleLog text={nvloomResult.stdout} label="output" /> : null}
          {nvloomResult.stderr ? <CollapsibleLog text={nvloomResult.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      {dcgmResult ? (
        <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            DCGM / DCGMI — {dcgmResult.ok ? "success" : "failed"}
            {dcgmResult.returncode != null ? ` — exit ${dcgmResult.returncode}` : ""}
          </Typography>
          {dcgmResult.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {dcgmResult.command_display}
            </Typography>
          ) : null}
          {dcgmResult.stdout ? <CollapsibleLog text={dcgmResult.stdout} label="output" /> : null}
          {dcgmResult.stderr ? <CollapsibleLog text={dcgmResult.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      {grafanaResult ? (
        <Paper variant="outlined" sx={{ p: 2 }}>
          <Typography variant="subtitle2" sx={{ mb: 1 }}>
            Grafana launch — {grafanaResult.ok ? "success" : "failed"}
            {grafanaResult.returncode != null ? ` — exit ${grafanaResult.returncode}` : ""}
          </Typography>
          {grafanaResult.command_display ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {grafanaResult.command_display}
            </Typography>
          ) : null}
          {grafanaResult.stdout ? <CollapsibleLog text={grafanaResult.stdout} label="output" /> : null}
          {grafanaResult.stderr ? <CollapsibleLog text={grafanaResult.stderr} label="stderr" tone="error" /> : null}
        </Paper>
      ) : null}

      <ToolsLiveOutputDialog
        open={liveDialogOpen}
        onClose={() => setLiveDialogOpen(false)}
        title={
          liveDialogKind === "nemo"
            ? "NEMO container download"
            : liveDialogKind === "hpcx"
              ? "Mellanox HPC-X install"
              : liveDialogKind === "cudaNccl"
                ? "CUDA toolkit & NCCL install"
                : liveDialogKind === "nvloom"
                  ? "nvloom install"
                  : liveDialogKind === "dgxc"
                    ? "Exemplar (dgxc-benchmarking) setup"
                    : liveDialogKind === "dcgm"
                      ? "DCGM / DCGMI workspace install"
                      : "Grafana + Bright Prometheus launch"
        }
        logText={
          liveDialogKind === "nemo"
            ? liveLogs.nemo
            : liveDialogKind === "hpcx"
              ? liveLogs.hpcx
              : liveDialogKind === "cudaNccl"
                ? liveLogs.cudaNccl
                : liveDialogKind === "nvloom"
                  ? liveLogs.nvloom
                  : liveDialogKind === "dgxc"
                    ? liveLogs.dgxc
                    : liveDialogKind === "dcgm"
                      ? liveLogs.dcgm
                      : liveLogs.grafana
        }
        running={
          liveDialogKind === "nemo"
            ? liveRunning.nemo
            : liveDialogKind === "hpcx"
              ? liveRunning.hpcx
              : liveDialogKind === "cudaNccl"
                ? liveRunning.cudaNccl
                : liveDialogKind === "nvloom"
                  ? liveRunning.nvloom
                  : liveDialogKind === "dgxc"
                    ? liveRunning.dgxc
                    : liveDialogKind === "dcgm"
                      ? liveRunning.dcgm
                      : liveRunning.grafana
        }
      />
    </Box>
  );
}
