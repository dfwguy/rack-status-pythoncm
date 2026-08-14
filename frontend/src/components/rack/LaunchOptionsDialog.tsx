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
import { useEffect, useMemo, useRef, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import Select from "@mui/material/Select";
import MenuItem from "@mui/material/MenuItem";
import FormHelperText from "@mui/material/FormHelperText";
import TextField from "@mui/material/TextField";
import Stack from "@mui/material/Stack";
import {
  Button as KuiButton,
  TextInput,
  Text as KuiText,
  Select as KuiSelect,
} from "@nvidia/foundations-react-core";
import { ChevronDown, ChevronUp } from "lucide-react";
import { parseEnvVars, type LaunchOptions } from "../../lib/rackCommandBodies";

const NCCL_DEBUG_LEVELS = [
  { value: "off", label: "Off (script default)" },
  { value: "WARN", label: "WARN" },
  { value: "INFO", label: "INFO" },
  { value: "TRACE", label: "TRACE" },
] as const;

// NCCL library versions selectable for the direct NCCL microbenchmark. The sbatch loads
// the matching libnccl from the NFS workspace via LD_LIBRARY_PATH (nccl-tests links
// libnccl.so.2, so one binary runs any 2.x lib). Add new vetted builds here as they land.
const NCCL_VERSIONS = [
  { value: "2.30.7", label: "2.30.7 (vetted, default)" },
  { value: "system", label: "System default (2.28.9)" },
] as const;
const DEFAULT_NCCL_VERSION = "2.30.7";

// NEMO training length. 50 is the short perf benchmark (what the LLMB recipe runs by
// default); 2010 is the Stage-2 soak. The value drives NVIS_NEMO_MAX_STEPS *and*
// NEMO_TARGET_STEPS — see LaunchOptions.maxSteps.
const NEMO_STEP_CHOICES = [
  { value: "50", children: "50 — perf benchmark (default)" },
  { value: "2010", children: "2010 — long soak / stress" },
] as const;
const DEFAULT_MAX_STEPS = 50;

const LLAMA_DTYPE_CHOICES = [
  { value: "nvfp4", children: "NVFP4" },
  { value: "fp8", children: "FP8" },
] as const;
const LLAMA_IMAGE_CHOICES = [
  { value: "nccl2.30.7", children: "NeMo 26.04.01 + NCCL 2.30.7 (default)" },
  { value: "stock", children: "NeMo 26.04.01 stock NCCL 2.29 (control)" },
] as const;
const LLAMA_TUNING_CHOICES = [
  { value: "baseline", children: "NCCL A/B baseline" },
  { value: "gb300", children: "GB300 tuned (QPS=4 + cuDNN norms)" },
] as const;

// NCCL sweep rounds: Stage-1 acceptance runs the all_reduce sweep 5× (all must pass).
const ITERS_MIN = 1;
const ITERS_MAX = 50;
const DEFAULT_ITERS = 5;
const ITERS_STEP = 1;

function clampIters(n: number): number {
  return Math.min(ITERS_MAX, Math.max(ITERS_MIN, Math.round(n)));
}

interface LaunchOptionsDialogProps {
  open: boolean;
  onClose: () => void;
  /** Fired when the user confirms; the parent runs the launch and closes the dialog. */
  onConfirm: (opts: LaunchOptions) => void;
  title?: string;
  /** Shown under the title (e.g. which test + rack/node count). */
  subtitle?: string;
  confirmLabel?: string;
  /** Show the NCCL library-version selector (direct NCCL test only; ignored by NEMO/container). */
  showNcclVersion?: boolean;
  /** Show the iterations (sweep-rounds) stepper (direct NCCL test only; ignored by NEMO/container). */
  showIters?: boolean;
  /** Show the NEMO training-steps selector. NEMO launches only. */
  showMaxSteps?: boolean;
  /** Show the Llama 3.1 405B Exemplar precision/image/tuning controls. */
  showLlamaExemplar?: boolean;
}

/**
 * Launch-time parameter modal for NCCL/NEMO jobs. Opens when the user clicks a launch
 * button so they can tune per-run knobs before submitting. Currently exposes the NCCL
 * debug level; add further checkboxes/selects here as new knobs are plumbed through
 * ``LaunchOptions`` + the backend ``_launch_env_from_body``.
 */
export function LaunchOptionsDialog({
  open,
  onClose,
  onConfirm,
  title = "Launch options",
  subtitle,
  confirmLabel = "Launch",
  showNcclVersion = false,
  showIters = false,
  showMaxSteps = false,
  showLlamaExemplar = false,
}: LaunchOptionsDialogProps) {
  const [ncclDebug, setNcclDebug] = useState("off");
  const [ncclVersion, setNcclVersion] = useState(DEFAULT_NCCL_VERSION);
  const [iters, setIters] = useState(DEFAULT_ITERS);
  const [maxSteps, setMaxSteps] = useState(DEFAULT_MAX_STEPS);
  const [llamaDtype, setLlamaDtype] = useState<"fp8" | "nvfp4">("nvfp4");
  const [llamaImageProfile, setLlamaImageProfile] = useState<"nccl2.30.7" | "stock">("nccl2.30.7");
  const [llamaTuning, setLlamaTuning] = useState<"baseline" | "gb300">("baseline");
  const [envText, setEnvText] = useState("");
  const itersInputRef = useRef<HTMLInputElement>(null);

  const bumpIters = (delta: number) => {
    setIters((n) => clampIters(n + delta));
  };

  const stepItersNative = (dir: "up" | "down") => {
    const el = itersInputRef.current;
    if (!el) {
      bumpIters(dir === "up" ? ITERS_STEP : -ITERS_STEP);
      return;
    }
    if (dir === "up") el.stepUp();
    else el.stepDown();
    const parsed = parseInt(el.value, 10);
    if (Number.isFinite(parsed)) setIters(clampIters(parsed));
  };

  useEffect(() => {
    if (open) {
      setNcclDebug("off");
      setNcclVersion(DEFAULT_NCCL_VERSION);
      setIters(DEFAULT_ITERS);
      setMaxSteps(DEFAULT_MAX_STEPS);
      setLlamaDtype("nvfp4");
      setLlamaImageProfile("nccl2.30.7");
      setLlamaTuning("baseline");
      setEnvText("");
    }
  }, [open]);

  // Live preview of the parsed env map (invalid lines are silently dropped).
  const parsedEnv = useMemo(() => parseEnvVars(envText), [envText]);
  const parsedCount = Object.keys(parsedEnv).length;
  const nonBlankLines = envText.split("\n").filter((l) => l.trim() && !l.trim().startsWith("#")).length;
  const droppedLines = Math.max(0, nonBlankLines - parsedCount);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>{title}</DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2}>
          {subtitle ? (
            <FormHelperText component="div" sx={{ m: 0, color: "text.secondary" }}>
              {subtitle}
            </FormHelperText>
          ) : null}
          {showIters ? (
            <div>
              <KuiText kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>
                Iterations (sweep rounds)
              </KuiText>
              <div style={{ width: "25%", minWidth: 120 }}>
                <TextInput
                  ref={itersInputRef}
                  className="px-0"
                  type="number"
                  size="small"
                  min={ITERS_MIN}
                  max={ITERS_MAX}
                  step={ITERS_STEP}
                  value={String(iters)}
                  onValueChange={(v: string) => {
                    if (v.trim() === "") return;
                    const n = parseInt(v, 10);
                    if (Number.isFinite(n)) setIters(clampIters(n));
                  }}
                  aria-label="Number of NCCL sweep rounds"
                  // Borderless on purpose, against the app-wide bordered rule: these live
                  // *inside* the TextInput's shell, so a border draws a box within a box and
                  // the stepper stops reading as part of the field.
                  slotStart={
                    <KuiButton
                      kind="tertiary"
                      size="small"
                      aria-label="Increase iterations"
                      disabled={iters >= ITERS_MAX}
                      tabIndex={-1}
                      onClick={() => stepItersNative("up")}
                    >
                      <ChevronUp size={16} />
                    </KuiButton>
                  }
                  slotEnd={
                    <KuiButton
                      kind="tertiary"
                      size="small"
                      aria-label="Decrease iterations"
                      disabled={iters <= ITERS_MIN}
                      tabIndex={-1}
                      onClick={() => stepItersNative("down")}
                    >
                      <ChevronDown size={16} />
                    </KuiButton>
                  }
                />
              </div>
              <FormHelperText sx={{ mt: 0.5 }}>
                How many times to run the all_reduce sweep ({ITERS_MIN}–{ITERS_MAX}, default {DEFAULT_ITERS}).
                Stage-1 acceptance requires 5 passing rounds.
              </FormHelperText>
            </div>
          ) : null}
          {showNcclVersion ? (
            <FormControl fullWidth size="small">
              <InputLabel id="launch-nccl-version-label">NCCL version</InputLabel>
              <Select
                labelId="launch-nccl-version-label"
                label="NCCL version"
                value={ncclVersion}
                onChange={(e) => setNcclVersion(String(e.target.value))}
              >
                {NCCL_VERSIONS.map((o) => (
                  <MenuItem key={o.value} value={o.value}>
                    {o.label}
                  </MenuItem>
                ))}
              </Select>
              <FormHelperText>
                Which vetted libnccl to load via LD_LIBRARY_PATH from the NFS workspace. 2.30.7 is required for
                cross-rack alltoall; "system" is the host 2.28.9 build.
              </FormHelperText>
            </FormControl>
          ) : null}
          {showMaxSteps ? (
            <div>
              <KuiText kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>
                NEMO training steps
              </KuiText>
              <KuiSelect
                items={[...NEMO_STEP_CHOICES]}
                value={String(maxSteps)}
                onValueChange={(v) => {
                  const n = parseInt(String(v), 10);
                  if (Number.isFinite(n) && n >= 1) setMaxSteps(n);
                }}
                size="small"
                aria-label="NEMO training steps"
              />
              <FormHelperText sx={{ mt: 0.5 }}>
                How many training iterations the run declares. 50 finishes in minutes and is the
                comparable perf number; 2010 is the long soak (hours). The dashboard's "done" threshold
                follows this value.
              </FormHelperText>
            </div>
          ) : null}
          {showLlamaExemplar ? (
            <>
              <div>
                <KuiText kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>
                  Llama precision
                </KuiText>
                <KuiSelect
                  items={[...LLAMA_DTYPE_CHOICES]}
                  value={llamaDtype}
                  onValueChange={(v) => setLlamaDtype(v === "fp8" ? "fp8" : "nvfp4")}
                  size="small"
                  aria-label="Llama Exemplar precision"
                />
              </div>
              <div>
                <KuiText kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>
                  Container NCCL
                </KuiText>
                <KuiSelect
                  items={[...LLAMA_IMAGE_CHOICES]}
                  value={llamaImageProfile}
                  onValueChange={(v) => setLlamaImageProfile(v === "stock" ? "stock" : "nccl2.30.7")}
                  size="small"
                  aria-label="Llama Exemplar NCCL image"
                />
                <FormHelperText sx={{ mt: 0.5 }}>
                  NCCL is baked into the Llama container so PyTorch and the runtime load the same library.
                </FormHelperText>
              </div>
              <div>
                <KuiText kind="body/bold/sm" style={{ display: "block", marginBottom: 6 }}>
                  Tuning profile
                </KuiText>
                <KuiSelect
                  items={[...LLAMA_TUNING_CHOICES]}
                  value={llamaTuning}
                  onValueChange={(v) => setLlamaTuning(v === "gb300" ? "gb300" : "baseline")}
                  size="small"
                  aria-label="Llama Exemplar tuning profile"
                />
                <FormHelperText sx={{ mt: 0.5 }}>
                  Baseline isolates the NCCL image change. GB300 tuned additionally sets four IB QPs per connection
                  and enables cuDNN Transformer Engine norms. NVLS stays disabled in both profiles because rack
                  CliqueIds are not unique.
                </FormHelperText>
              </div>
            </>
          ) : null}
          <FormControl fullWidth size="small">
            <InputLabel id="launch-nccl-debug-label">NCCL debug</InputLabel>
            <Select
              labelId="launch-nccl-debug-label"
              label="NCCL debug"
              value={ncclDebug}
              onChange={(e) => setNcclDebug(String(e.target.value))}
            >
              {NCCL_DEBUG_LEVELS.map((o) => (
                <MenuItem key={o.value} value={o.value}>
                  {o.label}
                </MenuItem>
              ))}
            </Select>
            <FormHelperText>
              INFO/TRACE surfaces NCCL init detail to diagnose a hang. Default (off) keeps the script's own level.
            </FormHelperText>
          </FormControl>

          <TextField
            label="Extra environment variables"
            placeholder={"OMPI_MCA_coll_hcoll_enable=0\nNCCL_ALGO=Ring"}
            value={envText}
            onChange={(e) => setEnvText(e.target.value)}
            multiline
            minRows={3}
            fullWidth
            size="small"
            slotProps={{ input: { style: { fontFamily: "monospace", fontSize: 13 } } }}
            helperText={
              `One KEY=VALUE per line (# comments and blank lines ignored). Forwarded to the job and into the container.` +
              (parsedCount ? ` — ${parsedCount} var${parsedCount === 1 ? "" : "s"} will be applied.` : "") +
              (droppedLines ? ` ${droppedLines} invalid line${droppedLines === 1 ? "" : "s"} ignored.` : "")
            }
          />
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} color="inherit">
          Cancel
        </Button>
        <Button
          onClick={() =>
            onConfirm({
              ncclDebug,
              env: parsedEnv,
              ...(showNcclVersion ? { ncclVersion } : {}),
              ...(showIters ? { iters } : {}),
              ...(showMaxSteps ? { maxSteps } : {}),
              ...(showLlamaExemplar ? { llamaDtype, llamaImageProfile, llamaTuning } : {}),
            })
          }
          variant="contained"
          color="primary"
        >
          {confirmLabel}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
