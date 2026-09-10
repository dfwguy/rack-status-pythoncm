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
import { useCallback, useEffect, useMemo, useState } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import Select from "@mui/material/Select";
import MenuItem from "@mui/material/MenuItem";
import FormHelperText from "@mui/material/FormHelperText";
import Alert from "@mui/material/Alert";
import Stack from "@mui/material/Stack";
import { apiFetch } from "../../lib/apiFetch";

export type HwCheckConfirmOpts = { preset?: string; checker_args?: string };

const FALLBACK_PRESET_KEYS = [
  "default",
  "dry_run",
  "no_dmesg",
  "with_imex",
  "with_spx",
  "with_imex_spx",
  "with_process",
  "sys_only",
] as const;

interface HwCheckRunDialogProps {
  open: boolean;
  onClose: () => void;
  /** Fired when the user confirms; close the dialog in the parent if desired. */
  onConfirm: (opts: HwCheckConfirmOpts) => void;
  title?: string;
  /** Shown under the title (e.g. rack + node count). */
  subtitle?: string;
}

export function HwCheckRunDialog({
  open,
  onClose,
  onConfirm,
  title = "Hardware check",
  subtitle,
}: HwCheckRunDialogProps) {
  const [presets, setPresets] = useState<Record<string, string[]> | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [preset, setPreset] = useState("default");
  const [checkerArgs, setCheckerArgs] = useState("");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch("/api/hw-check-presets", { credentials: "same-origin" });
        const data = (await res.json()) as { ok?: boolean; presets?: Record<string, string[]>; error?: string };
        if (!res.ok || !data?.ok || !data.presets) {
          throw new Error(data?.error || res.statusText || "failed to load presets");
        }
        if (!cancelled) {
          setPresets(data.presets);
          setLoadErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setLoadErr(e instanceof Error ? e.message : String(e));
          setPresets(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (open) {
      setCheckerArgs("");
      setPreset("default");
    }
  }, [open]);

  const orderedKeys = useMemo(() => {
    const src = presets ? Object.keys(presets) : [...FALLBACK_PRESET_KEYS];
    const keys = [...src];
    keys.sort((a, b) => {
      if (a === "default") return -1;
      if (b === "default") return 1;
      return a.localeCompare(b);
    });
    return keys;
  }, [presets]);

  const argvPreview = useMemo(() => {
    if (checkerArgs.trim()) return null;
    const map = presets ?? null;
    if (map && map[preset]) {
      const a = map[preset]!;
      return a.length ? a.join(" ") : "(no extra flags)";
    }
    return null;
  }, [checkerArgs, preset, presets]);

  const handleConfirm = useCallback(() => {
    const trimmed = checkerArgs.trim();
    if (trimmed) {
      onConfirm({ checker_args: trimmed });
    } else {
      onConfirm({ preset });
    }
  }, [checkerArgs, onConfirm, preset]);

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
          {loadErr ? (
            <Alert severity="warning">
              Could not load presets from the server ({loadErr}). Using built-in preset names; the server may still
              reject unknown presets.
            </Alert>
          ) : null}
          <FormControl fullWidth size="small">
            <InputLabel id="hw-check-preset-label">Preset</InputLabel>
            <Select
              labelId="hw-check-preset-label"
              label="Preset"
              value={orderedKeys.includes(preset) ? preset : "default"}
              onChange={(e) => setPreset(String(e.target.value))}
              disabled={Boolean(checkerArgs.trim())}
            >
              {orderedKeys.map((k) => (
                <MenuItem key={k} value={k}>
                  {k}
                </MenuItem>
              ))}
            </Select>
            {argvPreview ? (
              <FormHelperText>
                Checker argv for this preset: <code>{argvPreview}</code>
              </FormHelperText>
            ) : null}
          </FormControl>
          <TextField
            label="Custom checker arguments (optional)"
            placeholder='e.g. -d  or  -e 0 -i 1'
            value={checkerArgs}
            onChange={(e) => setCheckerArgs(e.target.value)}
            fullWidth
            size="small"
            multiline
            minRows={2}
            helperText="If set, overrides the preset above. Quoted tokens are supported (POSIX shell rules). See checker.sh --help."
          />
          <Button
            variant="text"
            size="small"
            onClick={() => {
              void (async () => {
                try {
                  const res = await apiFetch("/api/hw-check-help", { credentials: "same-origin" });
                  const data = (await res.json()) as { ok?: boolean; help?: string; error?: string };
                  const text = data.help || data.error || "(empty)";
                  const w = window.open("", "_blank", "noopener,noreferrer");
                  if (w) {
                    const pre = w.document.createElement("pre");
                    pre.style.whiteSpace = "pre-wrap";
                    pre.style.font = "14px monospace";
                    pre.style.padding = "16px";
                    pre.textContent = text;
                    w.document.body.appendChild(pre);
                  }
                } catch {
                  /* ignore */
                }
              })();
            }}
            sx={{ alignSelf: "flex-start", textTransform: "none" }}
          >
            Open checker.sh --help in a new tab
          </Button>
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} color="inherit">
          Cancel
        </Button>
        <Button onClick={handleConfirm} variant="contained" color="primary">
          Run hardware check
        </Button>
      </DialogActions>
    </Dialog>
  );
}
