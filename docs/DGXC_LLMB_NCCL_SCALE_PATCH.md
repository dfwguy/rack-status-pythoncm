# DGXC-Benchmarking (`llmb-run`) — NCCL scale + node-pinning patch (GBx00 / VRx00)

Patch instructions for running the `microbenchmark_nccl` workload from
[`dgxc-benchmarking`](https://github.com/NVIDIA/dgxc-benchmarking) (a.k.a. **Exemplar** /
`llmb-run`) on **full-rack NVLink domains** — GB200/GB300 **NVL72** (GBx00) and VR200
**NVL144** (VRx00). Validated on Boston (GB200 NVL72, DGXC `v25.10.01`, `llmb-run` v25.10.01)
2026-07-03.

**TL;DR** — Out of the box `microbenchmark_nccl` only accepts **power-of-two** GPU scales, so a
full rack (72 GPUs on GBx00, 144 on VRx00) and its N-rack multiples are rejected. You must
(1) add the rack-multiple scales to the workload's `metadata.yaml`, (2) drive the new
`llmb-run single` CLI (the old `submit --nodelist` is gone), pinning nodes via a `sbatch`
`--nodelist` shim (⚠️ **not** `SBATCH_NODELIST` — that env var is ignored by sbatch), and
(3) fix workspace ownership + git trust if the install ran as root.

---

## 0. Symptom

```text
$ llmb-run single -w microbenchmark_nccl -s nccl --dtype fp8 --scale 72 --dryrun
ERROR: Error: Scale 72 not supported for microbenchmark_nccl_nccl.
Supported scales: 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048
```

A full GBx00 rack = **18 nodes × 4 GPUs = 72 GPUs**; a full VRx00 rack = **144 GPUs**. Neither
72 nor 144 (nor their N-rack multiples) is a power of two, so the recipe validator rejects them.

---

## 1. Patch the NCCL scale allowlist (`metadata.yaml`)

`llmb-run` reads the supported scales — and builds both `list` and the "Supported scales"
validation — from the workload recipe's `metadata.yaml`:

```text
<LLMB_INSTALL>/llmb_repo/nccl/metadata.yaml
```

(Boston: `/home/cmods/workspace/llmb/llmb_repo/nccl/metadata.yaml`.) Scales are defined
**per GPU profile** under `run.gpu_configs.<gpu_type>.model_configs[].scales`; the profile is
selected by `launcher.gpu_type` in `<LLMB_INSTALL>/cluster_config.yaml`.

> Stock `v25.10.01` ships only `gb200`, `b200`, `h100` blocks. Confirm which one your cluster
> uses: `grep gpu_type <LLMB_INSTALL>/cluster_config.yaml`. GB200/GB300 NVL72 clusters run the
> **`gb200`** profile. If your cluster reports a `gpu_type` with no matching block, add one
> (copy an existing block and rename) — otherwise `llmb-run` errors before scale validation.

### 1a. GBx00 — NVL72 (72 GPUs/rack)

Add the 72-multiples for 1–8 racks (`72, 144, 216, 288, 360, 432, 504, 576`) while **keeping**
the original power-of-two scales. Edit the `gb200` block's `scales:` line to:

```yaml
          scales: [2, 4, 8, 16, 32, 64, 72, 128, 144, 216, 256, 288, 360, 432, 504, 512, 576, 1024, 2048]
```

| Racks | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|------:|--:|--:|--:|--:|--:|--:|--:|--:|
| GPUs (`--scale`) | 72 | 144 | 216 | 288 | 360 | 432 | 504 | 576 |

### 1b. VRx00 — NVL144 (144 GPUs/rack)

Add the 144-multiples for 1–8 racks (`144, 288, 432, 576, 720, 864, 1008, 1152`), keeping the
originals. Edit the VRx00 profile's `scales:` line (the block named for your `gpu_type`) to:

```yaml
          scales: [2, 4, 8, 16, 32, 64, 128, 144, 256, 288, 432, 512, 576, 720, 864, 1008, 1024, 1152, 2048]
```

| Racks | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|------:|--:|--:|--:|--:|--:|--:|--:|--:|
| GPUs (`--scale`) | 144 | 288 | 432 | 576 | 720 | 864 | 1008 | 1152 |

### 1c. Apply idempotently (target only the right block)

The three profiles carry identical `scales:` lines, so target the block by name with `awk`
(don't blanket-`sed`). Back up first. Example for **GBx00 / `gb200`**:

```bash
F=<LLMB_INSTALL>/llmb_repo/nccl/metadata.yaml
cp -a "$F" "$F.nvis.bak.$(date +%Y%m%d%H%M%S)"
awk '
/^[[:space:]]*gb200:/{inblk=1}
/^[[:space:]]*(b200|h100):/{inblk=0}
inblk && /scales:/{print "          scales: [2, 4, 8, 16, 32, 64, 72, 128, 144, 216, 256, 288, 360, 432, 504, 512, 576, 1024, 2048]"; next}
{print}
' "$F" > "$F.new" && mv "$F.new" "$F"
```

For **VRx00**, swap the block guards to your `gpu_type` (and the reset guard to the following
sibling block) and use the 144-multiple list from §1b.

### 1d. Verify

```bash
LLMB_INSTALL=<LLMB_INSTALL> <LLMB_INSTALL>/llmb-run list
# microbenchmark_nccl → nccl → Scales: … 72 … 144 … 288 … (or 144 … 288 … for VRx00)
```

> **Durability:** `metadata.yaml` lives in `llmb_repo`, which a **reinstall regenerates** — the
> edit is wiped. Re-apply after any `llmb-install`, or bake the patch into your install wrapper
> (NVIS: `bin/scripts/install-dgxc-benchmarking-workspace.sh`, post-clone).

---

## 2. `llmb-run` CLI change: `single`, not `submit` — and no `--nodelist`

`v25.10.01` replaced `llmb-run submit …` with subcommands `{list, bulk, submit-all, single}`
and **removed `--nodelist`**. Node selection is delegated to Slurm.

| Old (≤`release/25.10`) | New (`v25.10.01`) |
|---|---|
| `submit` | `single` |
| — | **`-s/--model_size` required** (NCCL → `-s nccl`) |
| `-d fp8` (dtype) | `--dtype fp8`  ⚠️ `-d` now = **`--dryrun`** |
| `--scale N` | `--scale N` |
| `--nodelist LIST` | **removed** → inject `--nodelist` into `llmb`'s own `sbatch` via a PATH shim (see below) |

Recipe key for NCCL is `microbenchmark_nccl` with `model_size = nccl` (from `llmb-run list`).

### Node pinning: "Slurm provisions, `llmb` consumes"

`single`'s `SbatchLauncher` runs `sbatch -N <count> … launch.sh` **itself**, with no
`--nodelist`, so Slurm free-allocates. ⚠️ **`SBATCH_NODELIST` does NOT work** — it is *not* a
recognized `sbatch` input environment variable (only `--nodelist`/`-w` on the CLI is honored),
so exporting it is silently ignored and the job lands wherever Slurm chooses (validated on
Boston: jobs 139/140 requested rack `b05/b06/b07` but ran on `a05–a08`, `ReqNodeList=(null)`).

The **only** reliable pin is the `--nodelist`/`-w` CLI flag on `llmb`'s own `sbatch` call.
Since we don't control `llmb-run`'s argv, drop a tiny **`sbatch` shim** ahead of the real
binary on `PATH` that **prepends** `--nodelist=<nodes>` when `LLMB_NODELIST` is set, then
execs the real `sbatch`:

- **PREPEND, never append.** `sbatch [OPTIONS] script [script-args]` — any token *after*
  `launch.sh` is passed to the script, not parsed by sbatch. Appending `--nodelist` after
  `launch.sh` is silently ignored (this was the second bug: shim ran, but `--nodelist` landed
  after the script → free allocation again).

The same shim also **rewrites `--output`**: `llmb` hard-codes `sbatch --output=<workload_dir>/
slurm-%j.out`, so the batch stdout escapes the dashboard log dir and the web UI's log-path
guard rejects the tail (*"job stdout path must be under the dashboard log directory"*). The
shim strips `llmb`'s `--output`/`-o` and prepends `--output=<JOB_LOG_DIR>/llmb-nccl_<racks>_%j.out`
(`JOB_LOG_DIR` = the wrapper's launch cwd, which NVIS sets to the dashboard log dir; override
via `RACK_JOB_LOG_DIR`). `scontrol show job <id>` then reports a `StdOut=` under the log dir,
so *Scheduler → tail StdOut* works. Validated on Boston: job 146 →
`StdOut=/home/cmods/nvis-rack-status/var/…/llmb-nccl_bos01-b06_146.out`.

Keep `--scale` consistent with the node count (`scale = nodes × GPUs_per_node`) so `llmb`'s
derived `-N` matches the pinned nodelist. A reservation still maps via `SBATCH_RESERVATION`
(that one *is* a recognized sbatch input var).

```bash
# concept — the shim injects: sbatch --nodelist=<nodes> --parsable … -N 54 … launch.sh
LLMB_NODELIST='bos01-b05-…,…,bos01-b07-37-s1-dgx-07-c18'   # 54 nodes
LLMB_INSTALL=<LLMB_INSTALL> <LLMB_INSTALL>/llmb-run single \
  -w microbenchmark_nccl -s nccl --dtype fp8 --scale 216 -d   # -d = dry-run; drop to submit
```

The NVIS wrapper `bin/scripts/run_llmb_nccl.sh` does all of this — resolves rack ids → Slurm
hostnames, computes `--scale`, installs the `sbatch --nodelist` shim, and calls `single`.
Verify placement after a real submit: `squeue -u <user>` then
`scontrol show job <id> | grep -iE 'ReqNodeList|NodeList'` — `ReqNodeList` must list the
requested racks (validated on Boston: job 144 → `ReqNodeList` = 18×b05 + 18×b06 + 18×b07,
`ExitCode 0:0`, "All tests completed successfully").

---

## 3. Directory layout — `LLMB_INSTALL` vs the `llmb-run` binary

The installer produces **two** dirs under the workspace top; don't conflate them:

| Thing | Path (NVIS installer) |
|---|---|
| **Workspace** (`LLMB_INSTALL`) — `.cache`, `workloads`, `experiments`, `llmb_repo` | `<top>/llmb` |
| Repo shipping the `llmb-run` binary | `<top>/dgxc-benchmarking` (`cli/llmb-run/llmb-run`) |
| Convenience entrypoint (symlink → venv console script) | `<top>/llmb/llmb-run` |

- `LLMB_INSTALL` must point at the **workspace** (`<top>/llmb`), *not* the repo. `llmb-run`
  reads/writes everything relative to it.
- The `<top>/llmb/llmb-run` symlink → `<top>/llmb_venv/bin/llmb-run` (a venv console script) is
  the simplest thing to invoke — its shebang activates the correct venv automatically.
- NVIS `run_llmb_nccl.sh` resolves the binary in order: `$LLMB_RUN` → `$LLMB_REPO/llmb-run` →
  `$LLMB_INSTALL/llmb-run` → `$LLMB_INSTALL/dgxc-benchmarking/llmb-run` →
  `$(dirname $LLMB_INSTALL)/dgxc-benchmarking/llmb-run`.

Set (and persist) `LLMB_INSTALL=<top>/llmb` in the environment that launches jobs. In NVIS
Rack Status this is forwarded over SSH (any `LLMB_*` var is forwarded by `host_exec`).

---

## 4. Ownership + git trust (only if the install ran as root)

If `llmb-install`/`enroot import` ran as **root**, the workspace ends up `root:daemon` and a
later `cmods` submit fails writing `experiments/`, `.cache/`, etc. Take ownership:

```bash
sudo chown -R <user>:<user> <top>/llmb <top>/llmb_venv
```

The installer also clones nested dependency repos under `<top>/llmb/.cache/git_repos/*.git`
(e.g. `Nvidia-Comms-Perf-Suite.git`). If a prior root run left them foreign-owned, git aborts
with `detected dubious ownership`. Trust them for the invoking user:

```bash
git config --global --add safe.directory '*'   # covers all nested clones the installer creates
```

NVIS automates both: `install-dgxc-benchmarking-workspace.sh` chowns the workspace (when
sudo is available) and registers `safe.directory` — controlled by the **"Trust workspace git
repos"** checkbox (`NVIS_EXEMPLAR_TRUST_GIT`, default on) in Tools → Operations & Scripts.

---

## 5. End-to-end checklist

1. `metadata.yaml` scales patched for the platform profile (§1) — `llmb-run list` shows the
   rack multiples.
2. `cluster_config.yaml` `gpu_type` matches a profile that has a `scales` block (§1).
3. `LLMB_INSTALL` = the **workspace** (`<top>/llmb`); binary resolves (§3).
4. Workspace owned by the invoking user; git repos trusted (§4).
5. Drive `llmb-run single` with the `sbatch --nodelist` shim for rack pinning (§2); `--scale`
   = the exact rack-multiple (72×N for GBx00, 144×N for VRx00).
6. Dry-run (`-d`) clean → real submit → confirm `ReqNodeList` (not `(null)`) lists the
   requested racks via `scontrol show job <id>`.

### Validated example (Boston, GBx00 / NVL72)

```bash
# single rack (dry-run)
LLMB_INSTALL=/home/cmods/workspace/llmb GPU_TYPE=gb200 \
RACK_TO_NODE_SED='s/$/-/' RACK_NODE_MATCH_SUFFIX='.*$' \
  /home/cmods/nvis-rack-status/bin/scripts/run_llmb_nccl.sh --dry-run bos01-b06
# → nodes=18 scale=72, nodelist=<18 hostnames> (pinned via sbatch --nodelist shim),
#   llmb-run single -w microbenchmark_nccl -s nccl --dtype fp8 --scale 72 --dryrun

# 3 racks (real submit) — job 144, validated 2026-07-03:
#   nodes=54 scale=216 → ReqNodeList = 18×b05 + 18×b06 + 18×b07, ExitCode 0:0,
#   "All tests completed successfully"
```
