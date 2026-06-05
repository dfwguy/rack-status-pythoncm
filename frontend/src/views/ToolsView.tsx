import { useCallback, useEffect, useState } from "react";
import { useClusterOverview } from "../hooks/useClusterOverview";
import { CheckCircle, Cpu, LineChart } from "lucide-react";
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

type LiveKind = "nemo" | "hpcx" | "cudaNccl" | "nvloom" | "grafana";

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

const DEST_STORAGE_KEY = "nvis-tools-download-dest";
const HPCX_WORKSPACE_STORAGE_KEY = "nvis-tools-hpcx-workspace";
const CUDA_NCCL_BUILD_HOST_STORAGE_KEY = "nvis-tools-cuda-nccl-build-host";
const NVLOOM_WORKSPACE_STORAGE_KEY = "nvis-tools-nvloom-workspace";
const NVLOOM_BUILD_HOST_STORAGE_KEY = "nvis-tools-nvloom-build-host";
const NVLOOM_CUDA_MAJOR_STORAGE_KEY = "nvis-tools-nvloom-cuda-major";

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

  const [hpcxWorkspace, setHpcxWorkspace] = useState("/home/cmsupport/workspace");
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
  const [nvloomWorkspace, setNvloomWorkspace] = useState("/home/cmsupport/workspace");
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
  const [toolchainAllReady, setToolchainAllReady] = useState(false);
  const [toolchainCudaNvcc, setToolchainCudaNvcc] = useState(false);
  const [toolchainHpcxInit, setToolchainHpcxInit] = useState(false);
  const [toolchainNccl, setToolchainNccl] = useState(false);
  const [toolchainTooltip, setToolchainTooltip] = useState("");
  const [nvloomCliPresent, setNvloomCliPresent] = useState(false);
  const [toolchainProbeTick, setToolchainProbeTick] = useState(0);
  const [nemoStatusTick, setNemoStatusTick] = useState(0);

  const [liveDialogOpen, setLiveDialogOpen] = useState(false);
  const [liveDialogKind, setLiveDialogKind] = useState<LiveKind>("nemo");
  const [liveLogs, setLiveLogs] = useState({ nemo: "", hpcx: "", cudaNccl: "", nvloom: "", grafana: "" });
  const [liveRunning, setLiveRunning] = useState({
    nemo: false,
    hpcx: false,
    cudaNccl: false,
    nvloom: false,
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
      if (w) setHpcxWorkspace(w);
    } catch {
      /* ignore */
    }
  }, []);

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
        if (fallback) setNvloomWorkspace(fallback);
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
  }, []);

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
          const res = await fetch(
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
        } catch (e) {
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
          const res = await fetch(`/api/tools/workspace-toolchain-status?${q.toString()}`, {
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
      const res = await fetch("/api/tools/download-containers-stream", {
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
      const text = await consumePlainTextStream(res, (full) => setLiveLogs((p) => ({ ...p, nemo: full })));
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
      const res = await fetch("/api/tools/install-hpcx-workspace-stream", {
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
      const text = await consumePlainTextStream(res, (full) => setLiveLogs((p) => ({ ...p, hpcx: full })));
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
      const res = await fetch("/api/tools/install-cuda-nccl-workspace-stream", {
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
      const text = await consumePlainTextStream(res, (full) => setLiveLogs((p) => ({ ...p, cudaNccl: full })));
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
      const res = await fetch("/api/tools/install-nvloom-workspace-stream", {
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
      const text = await consumePlainTextStream(res, (full) => setLiveLogs((p) => ({ ...p, nvloom: full })));
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

  const runGrafanaLaunch = useCallback(async () => {
    setGrafanaError("");
    setGrafanaResult(null);
    setGrafanaLoading(true);
    setLiveRunning((r) => ({ ...r, grafana: true }));
    setLiveLogs((p) => ({ ...p, grafana: "" }));
    try {
      const res = await fetch("/api/tools/launch-grafana-bright-prometheus-stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({}),
      });
      if (res.status === 401) {
        window.location.href = `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
        return;
      }
      const text = await consumePlainTextStream(res, (full) => setLiveLogs((p) => ({ ...p, grafana: full })));
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
        description="Operational helpers under bin/scripts: NEMO download (NGC_API_KEY), workspace Mellanox HPC-X (install-hpcx-workspace.sh), workspace CUDA toolkit + NCCL (install-cuda-nccl-workspace.sh; aihpc_val 02 + 03), NVIDIA nvloom clone + build (install-nvloom-workspace.sh), Grafana + Docker. Green dot opens live streamed output for that card."
      />

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
            placeholder="/home/cmsupport/workspace"
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
          host (CUDA .run download). Then <Box component="code" sx={{ fontSize: "0.85em" }}>02-install-cuda-nccl-workspace.sh</Box>{" "}
          runs on the chosen compute node via <Box component="code" sx={{ fontSize: "0.85em" }}>pdsh</Box> (same{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>$HOME</Box> path). NCCL needs HPC-X +{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>nvcc</Box> on that node. Timeout:{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT</Box> (default
          28800s).
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={hpcxWorkspace}
            onChange={(e) => persistHpcxWorkspace(e.target.value)}
            placeholder="/home/cmsupport/workspace"
          />
          <TextField
            label="Build host (compute node for pdsh)"
            size="small"
            fullWidth
            value={cudaNcclBuildHost}
            onChange={(e) => persistCudaNcclBuildHost(e.target.value)}
            placeholder="e.g. p22-r05-ct01"
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
          <Box component="code" sx={{ fontSize: "0.85em" }}>01-clone-nvloom-workspace.sh</Box> clones{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>https://github.com/NVIDIA/nvloom</Box> into{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>nvloom/</Box> under the workspace path on this host.{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>00-download-cmake-nvloom-workspace.sh</Box> fetches Kitware CMake 3.28.1 (aarch64 tarball) on this dashboard host only;{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>02-build-nvloom-workspace.sh</Box> runs on the build host (e.g. via{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>pdsh</Box>) and sources{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>cuda/env.sh</Box> (or <Box component="code" sx={{ fontSize: "0.85em" }}>cuda12/</Box> /{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>cuda13/</Box>), then <Box component="code" sx={{ fontSize: "0.85em" }}>cmake .</Box> and{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>make -j</Box> (aarch64 only). Timeout:{" "}
          <Box component="code" sx={{ fontSize: "0.85em" }}>RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT</Box> (default 28800s).
        </Typography>
        <Box sx={{ display: "flex", flexDirection: "column", gap: 2, maxWidth: 640 }}>
          <TextField
            label="Workspace directory (absolute path)"
            size="small"
            fullWidth
            value={nvloomWorkspace}
            onChange={(e) => persistNvloomWorkspace(e.target.value)}
            placeholder="/home/cmsupport/workspace"
          />
          <TextField
            label="Build host (compute node for pdsh)"
            size="small"
            fullWidth
            value={nvloomBuildHost}
            onChange={(e) => persistNvloomBuildHost(e.target.value)}
            placeholder="e.g. p22-r05-ct01"
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
          {result.stdout ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {result.stdout}
            </Typography>
          ) : null}
          {result.stderr ? (
            <Typography
              variant="caption"
              component="pre"
              sx={{ display: "block", color: "error.main", whiteSpace: "pre-wrap" }}
            >
              {result.stderr}
            </Typography>
          ) : null}
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
          {hpcxResult.stdout ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {hpcxResult.stdout}
            </Typography>
          ) : null}
          {hpcxResult.stderr ? (
            <Typography
              variant="caption"
              component="pre"
              sx={{ display: "block", color: "error.main", whiteSpace: "pre-wrap" }}
            >
              {hpcxResult.stderr}
            </Typography>
          ) : null}
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
          {cudaNcclResult.stdout ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {cudaNcclResult.stdout}
            </Typography>
          ) : null}
          {cudaNcclResult.stderr ? (
            <Typography
              variant="caption"
              component="pre"
              sx={{ display: "block", color: "error.main", whiteSpace: "pre-wrap" }}
            >
              {cudaNcclResult.stderr}
            </Typography>
          ) : null}
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
          {nvloomResult.stdout ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {nvloomResult.stdout}
            </Typography>
          ) : null}
          {nvloomResult.stderr ? (
            <Typography
              variant="caption"
              component="pre"
              sx={{ display: "block", color: "error.main", whiteSpace: "pre-wrap" }}
            >
              {nvloomResult.stderr}
            </Typography>
          ) : null}
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
          {grafanaResult.stdout ? (
            <Typography variant="caption" component="pre" sx={{ display: "block", mb: 1, whiteSpace: "pre-wrap" }}>
              {grafanaResult.stdout}
            </Typography>
          ) : null}
          {grafanaResult.stderr ? (
            <Typography
              variant="caption"
              component="pre"
              sx={{ display: "block", color: "error.main", whiteSpace: "pre-wrap" }}
            >
              {grafanaResult.stderr}
            </Typography>
          ) : null}
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
                  : liveRunning.grafana
        }
      />
    </Box>
  );
}
