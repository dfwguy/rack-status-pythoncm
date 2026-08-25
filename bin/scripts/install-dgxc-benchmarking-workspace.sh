#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# install-dgxc-benchmarking-workspace.sh — clone + prepare DGX Cloud Benchmarking
# ("Exemplar" / llmb-run) so the NVIS NCCL/NeMo tools (run_llmb_nccl.sh) can use it.
#
# Mirrors the other Utility installers (NEMO / HPC-X / CUDA-NCCL / NVLoom): a single
# action, invokable from NVIS Rack Status, that downloads and prepares a toolchain on
# the cluster's shared filesystem instead of running the commands by hand on each host.
#
# Two modes (NVIS_EXEMPLAR_MODE):
#   record  (default) — clone/refresh the repo and WRITE a headless install config
#                        (schema: cli/llmb-install/example_config.yaml). Does NOT run
#                        the multi-hour install; prints the config and the exact
#                        `install.sh --play` command to run next. Fast + safe to review.
#   install           — provision prerequisites (git-lfs into ~/.local/bin; uv via
#                        install.sh) + run `./install.sh --play <config>` headless:
#                        sets up the Python 3.12 venv, installs llmb-run / llmb-install,
#                        downloads the NeMo container via SLURM, and installs the
#                        selected workloads to LLMB_INSTALL. Long-running (can be hours).
#
# Reference: docs/VR200_GBx00_FastTrack_Validation_MOP.md §7.4.1;
#            NVIDIA/dgxc-benchmarking cli/llmb-install (--record / --play schema).
#
# Usage: install-dgxc-benchmarking-workspace.sh [TOPDIR]
#   TOPDIR  workspace root on a SHARED filesystem. The repo clones to
#           TOPDIR/dgxc-benchmarking; workloads install to NVIS_EXEMPLAR_INSTALL_PATH
#           (default TOPDIR/llmb), which is the path run_llmb_nccl.sh reads as LLMB_INSTALL.
#
# Environment (all overridable; NVIS_*/LLMB_* are forwarded over SSH by host_exec):
#   NVIS_EXEMPLAR_MODE           record | install              (default record)
#   NVIS_EXEMPLAR_REPO_URL       git URL                        (default NVIDIA/dgxc-benchmarking)
#   NVIS_EXEMPLAR_REPO_BRANCH    git branch/tag                 (default v25.10.01)
#   NVIS_EXEMPLAR_INSTALL_PATH   LLMB_INSTALL target            (default TOPDIR/llmb)
#   NVIS_EXEMPLAR_VENV_TYPE      venv | conda | uv              (default venv)
#   NVIS_EXEMPLAR_SLURM_ACCOUNT  SLURM account                  (default default)
#   NVIS_EXEMPLAR_GPU_PARTITION  SLURM GPU partition            (default defq)
#   NVIS_EXEMPLAR_CPU_PARTITION  SLURM CPU partition            (default defq)
#   NVIS_EXEMPLAR_GPU_GRES       GPUs per node (NVL72 = 4)      (default 4)
#   NVIS_EXEMPLAR_GPU_TYPE       gb200 | gb300 | vr200 | h100   (default gb200). Also drives the
#                                NVL NCCL scale patch: gb*/NVL72 adds 72xN scales, vr*/NVL144
#                                adds 144xN (1..8 racks) to nccl/metadata.yaml so full-rack
#                                microbenchmark_nccl passes. See docs/DGXC_LLMB_NCCL_SCALE_PATCH.md
#   NVIS_EXEMPLAR_ARCH           aarch64 | x86_64               (default aarch64)
#   NVIS_EXEMPLAR_INSTALL_METHOD slurm | local                  (default slurm)
#   NVIS_EXEMPLAR_WORKLOADS      comma list of workloads        (default microbenchmark_nccl,pretrain_nemotron-h)
#   NVIS_EXEMPLAR_IMAGE_FOLDER   shared container image dir (-i) (optional)
#   NVIS_EXEMPLAR_HF_TOKEN       HuggingFace token              (optional; not needed for Nemotron)
#   NVIS_EXEMPLAR_USE_UV         1 (UI default) = bootstrap uv + pre-provision a managed CPython
#                                3.12; uv venv works without the distro python3-venv/ensurepip
#                                package, so it sidesteps nodes whose system python3.12 lacks it.
#                                Needs astral.sh egress. 0 = answer 'n' and use system Python 3.12
#                                (air-gapped / no astral.sh; requires python3-venv installed).
#   NVIS_EXEMPLAR_GIT_LFS_VERSION git-lfs release to bootstrap   (default 3.7.0; install mode only)
#   NVIS_EXEMPLAR_TRUST_GIT      1 (default) = fix git "dubious ownership": chown the workspace
#                                (when root/sudo) + register git safe.directory (incl. the catch-all
#                                '*' so install.sh's nested .cache/git_repos clones are trusted).
#                                0 = only the targeted TOPDIR/REPO_DIR safe.directory entries.
#   NVIS_EXEMPLAR_SLURM_CONF     path to slurm.conf for srun     (auto-detected; install+slurm only)
#   NVIS_EXEMPLAR_CONFIG_PATH    where to write the config      (default REPO_DIR/nvis-llmb-config.yaml)
set -uo pipefail

TOPDIR="${1:-${NVIS_WORKSPACE_DIR:-/home/cmsupport/workspace}}"
TOPDIR="${TOPDIR%/}"

MODE="${NVIS_EXEMPLAR_MODE:-record}"
REPO_URL="${NVIS_EXEMPLAR_REPO_URL:-https://github.com/NVIDIA/dgxc-benchmarking.git}"
REPO_BRANCH="${NVIS_EXEMPLAR_REPO_BRANCH:-v25.10.01}"
REPO_DIR="${TOPDIR}/dgxc-benchmarking"
INSTALL_PATH="${NVIS_EXEMPLAR_INSTALL_PATH:-${TOPDIR}/llmb}"

# uv path (default on): install.sh bootstraps uv and we force uv to use a MANAGED
# standalone CPython 3.12 (ships ensurepip) instead of the node's system python3.12,
# which on many BCM/Ubuntu nodes lacks ensurepip/python3-venv (venv creation fails).
USE_UV=0
case "${NVIS_EXEMPLAR_USE_UV:-0}" in 1|true|yes|on) USE_UV=1 ;; esac
# When using uv, default the venv_type to uv too so llmb-install's per-workload venvs
# also avoid the broken system interpreter (unless the caller pinned NVIS_EXEMPLAR_VENV_TYPE).
if [[ "${USE_UV}" -eq 1 && -z "${NVIS_EXEMPLAR_VENV_TYPE:-}" ]]; then
  VENV_TYPE="uv"
else
  VENV_TYPE="${NVIS_EXEMPLAR_VENV_TYPE:-venv}"
fi
SLURM_ACCOUNT="${NVIS_EXEMPLAR_SLURM_ACCOUNT:-default}"
GPU_PARTITION="${NVIS_EXEMPLAR_GPU_PARTITION:-defq}"
CPU_PARTITION="${NVIS_EXEMPLAR_CPU_PARTITION:-defq}"
GPU_GRES="${NVIS_EXEMPLAR_GPU_GRES:-4}"
GPU_TYPE="${NVIS_EXEMPLAR_GPU_TYPE:-gb200}"
ARCH="${NVIS_EXEMPLAR_ARCH:-aarch64}"
INSTALL_METHOD="${NVIS_EXEMPLAR_INSTALL_METHOD:-slurm}"
WORKLOADS="${NVIS_EXEMPLAR_WORKLOADS:-microbenchmark_nccl,pretrain_nemotron-h}"
IMAGE_FOLDER="${NVIS_EXEMPLAR_IMAGE_FOLDER:-}"
HF_TOKEN="${NVIS_EXEMPLAR_HF_TOKEN:-}"
CONFIG_PATH="${NVIS_EXEMPLAR_CONFIG_PATH:-${REPO_DIR}/nvis-llmb-config.yaml}"
# Trust git repos in the workspace (default on). install.sh clones nested dependency repos
# under <install>/.cache/git_repos/*.git DURING the run; if a prior attempt left them owned
# by another uid, git aborts deep inside with "detected dubious ownership". We can't
# pre-enumerate those paths, so trust registers the git catch-all for THIS user.
TRUST_GIT="${NVIS_EXEMPLAR_TRUST_GIT:-1}"
case "${TRUST_GIT}" in 1|true|yes|on) TRUST_GIT=1 ;; *) TRUST_GIT=0 ;; esac

echo "########## DGXC BENCHMARKING (EXEMPLAR / llmb-run) SETUP ##########"
echo "host=$(hostname 2>/dev/null || echo unknown) mode=${MODE} arch=${ARCH} gpu_type=${GPU_TYPE}"
echo "repo=${REPO_URL}@${REPO_BRANCH} -> ${REPO_DIR}"
echo "install_path (LLMB_INSTALL)=${INSTALL_PATH}  workloads=${WORKLOADS}"

if [[ "${MODE}" != "record" && "${MODE}" != "install" ]]; then
  echo "ERROR: NVIS_EXEMPLAR_MODE must be 'record' or 'install' (got '${MODE}')." >&2
  exit 2
fi
command -v git >/dev/null 2>&1 || { echo "ERROR: git not found on PATH." >&2; exit 1; }
mkdir -p "${TOPDIR}" || { echo "ERROR: cannot create TOPDIR=${TOPDIR}." >&2; exit 1; }

# --- Phase 0b: repo trust (git "detected dubious ownership") -------------------
# When the workspace was first created by a DIFFERENT uid (a prior root/sudo run, or an
# NFS uid remap), git refuses every op on it ("detected dubious ownership") — breaking our
# fetch/checkout AND dgxc-benchmarking's install.sh git/LFS steps. Prefer chowning the tree
# back to the running user (covers the repo + all nested submodule/LFS checkouts in one
# shot); always also register safe.directory as a no-sudo belt-and-suspenders.
ensure_repo_trust() {
  local me grp owner sudo=""
  me="$(id -un)"; grp="$(id -gn)"
  if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo="sudo -n"
  fi
  owner="$(stat -c '%U' "${REPO_DIR}" 2>/dev/null || echo "")"
  if [[ -d "${REPO_DIR}" && -n "${owner}" && "${owner}" != "${me}" ]]; then
    if [[ "$(id -u)" -eq 0 || -n "${sudo}" ]]; then
      echo "==> Phase 0b: ${REPO_DIR} owned by '${owner}'; chowning to ${me}:${grp} (${sudo:-root})"
      ${sudo} chown -R "${me}:${grp}" "${TOPDIR}" 2>/dev/null \
        || ${sudo} chown -R "${me}" "${TOPDIR}" 2>/dev/null \
        || echo "WARNING: chown failed; relying on git safe.directory"
    else
      echo "==> Phase 0b: ${REPO_DIR} owned by '${owner}' and no root/sudo; using git safe.directory"
    fi
  fi
  # Trust the workspace paths for git even if chown wasn't possible (read-only/remapped NFS).
  local d
  for d in "${TOPDIR}" "${REPO_DIR}"; do
    git config --global --get-all safe.directory 2>/dev/null | grep -qxF "${d}" \
      || git config --global --add safe.directory "${d}"
  done
  # Catch-all so the installer's nested dependency clones (…/.cache/git_repos/*.git),
  # which don't exist yet and may land root-owned, are trusted without hand-patching each.
  if [[ "${TRUST_GIT}" == "1" ]]; then
    git config --global --get-all safe.directory 2>/dev/null | grep -qxF '*' \
      || git config --global --add safe.directory '*'
    echo "==> Phase 0b: git safe.directory='*' registered (trusts installer's nested repo clones)"
  fi
}
ensure_repo_trust

# --- NVL72 / NVL144 NCCL scale allowlist patch ---------------------------------
# Stock microbenchmark_nccl only accepts POWER-OF-TWO scales, so a full NVLink rack
# (GBx00 NVL72 = 72 GPUs, VRx00 NVL144 = 144) and its N-rack multiples are rejected by
# `llmb-run single --scale`. We add the rack multiples (1..8 racks) to the recipe's
# nccl/metadata.yaml for the ACTIVE gpu_type profile, preserving the originals. Idempotent
# and re-applied on every install (metadata.yaml lives in llmb_repo, which llmb-install
# regenerates). See docs/DGXC_LLMB_NCCL_SCALE_PATCH.md.
_rack_gpus_for_gpu_type() {
  case "${1,,}" in
    gb200|gb300|gbx00|gb*) echo 72 ;;
    vr200|vr300|vrx00|vr*) echo 144 ;;
    *) echo 0 ;;
  esac
}
_build_scale_list() {
  # $1 = per-rack GPU count (0 = none). Emit sorted-unique CSV "2, 4, 8, ...".
  local mult="$1" n
  {
    printf '%s\n' 2 4 8 16 32 64 128 256 512 1024 2048
    if [[ "${mult}" -gt 0 ]]; then for n in 1 2 3 4 5 6 7 8; do echo $((mult * n)); done; fi
  } | sort -n -u | paste -sd, - | sed 's/,/, /g'
}
patch_nccl_scales() {
  local mult scales f patched=0 wls
  # Which workloads get the rack-multiple scale list. Upstream ships power-of-two
  # scales only, so a full NVL72 rack (18 trays x 4 = 72 GPUs) is REJECTED by
  # llmb-run's metadata validation -- for nemotron-h exactly as it was for nccl.
  # Deliberately a short list, not every workload: `scales` is a validation
  # whitelist, and widening it for a workload we never run at 72 would let a bad
  # config through instead of failing fast.
  wls="${NVIS_EXEMPLAR_SCALE_PATCH_WORKLOADS:-nccl,nemotron-h}"
  mult="$(_rack_gpus_for_gpu_type "${GPU_TYPE}")"
  if [[ "${mult}" -le 0 ]]; then
    echo "==> scale patch: gpu_type='${GPU_TYPE}' is not an NVL rack profile; leaving power-of-two scales"
    return 0
  fi
  scales="$(_build_scale_list "${mult}")"
  while IFS= read -r f; do
    [[ -f "${f}" ]] || continue
    grep -Eq "^[[:space:]]{4}${GPU_TYPE}:[[:space:]]*$" "${f}" || continue  # only if the active block exists
    [[ -f "${f}.nvis.bak" ]] || cp -a "${f}" "${f}.nvis.bak" 2>/dev/null || true
    awk -v prof="${GPU_TYPE}" -v line="          scales: [${scales}]" '
      $0 ~ ("^    " prof ":[ \t]*$") {inblk = 1; print; next}
      /^    [A-Za-z0-9_]+:[ \t]*$/  {inblk = 0; print; next}
      inblk && /^[[:space:]]*scales:/ {print line; next}
      {print}
    ' "${f}" > "${f}.nvis.new" && mv "${f}.nvis.new" "${f}" && {
      echo "==> scale patch: patched '${GPU_TYPE}' block in ${f}"
      patched=$((patched + 1))
    }
    # IFS is set inside this subshell only, so the comma split cannot leak out.
  done < <(IFS=','; for w in ${wls}; do
             find "${REPO_DIR}" "${INSTALL_PATH}" -type f -path "*/${w}/metadata.yaml" 2>/dev/null
           done | sort -u)
  if [[ "${patched}" -eq 0 ]]; then
    echo "==> scale patch: no {${wls}}/metadata.yaml with a '${GPU_TYPE}:' block yet (retried post-install)"
  else
    echo "==> scale patch: '${GPU_TYPE}' now allows rack multiples of ${mult} (1..8 racks): [${scales}]"
    echo "    workloads considered: ${wls}"
  fi
}

# --- Phase 1: clone or refresh the repository ----------------------------------
if [[ -d "${REPO_DIR}/.git" ]]; then
  echo "==> Phase 1: repo present; fetching ${REPO_BRANCH}"
  git -C "${REPO_DIR}" fetch --depth 1 origin "${REPO_BRANCH}" 2>&1 || echo "WARNING: git fetch failed; using existing checkout"
  git -C "${REPO_DIR}" checkout -f "${REPO_BRANCH}" 2>&1 || git -C "${REPO_DIR}" checkout -f FETCH_HEAD 2>&1 || true
else
  echo "==> Phase 1: git clone ${REPO_URL} (branch ${REPO_BRANCH})"
  git clone --branch "${REPO_BRANCH}" --depth 1 "${REPO_URL}" "${REPO_DIR}" || { echo "ERROR: git clone failed." >&2; exit 1; }
fi

# Patch the recipe source now so a later manual/headless install inherits the NVL scales
# (also covers `record` mode, which exits before Phase 3). Re-applied post-install below.
patch_nccl_scales

# --- Phase 2: generate the headless install config -----------------------------
# Schema mirrors NVIDIA/dgxc-benchmarking cli/llmb-install/example_config.yaml.
echo "==> Phase 2: writing headless config -> ${CONFIG_PATH}"
# NOTE: the upstream headless schema is INCONSISTENT between its validator and its loader:
#   - load_installation_config() (config/headless.py) REQUIRES top-level `slurm_info` and
#     `env_vars`, and checks slurm_info.slurm.{gpu,cpu}_partition are non-blank.
#   - InstallConfig.from_dict() (config/models.py) instead reads top-level `slurm` and
#     `environment_vars`; if absent, slurm stays None and get_slurm_dict() returns {},
#     which makes the SLURM container download crash with KeyError('slurm').
# So we emit BOTH shapes: slurm_info+env_vars (validator) AND slurm+environment_vars
# (loader). Extra keys are ignored by both paths.
#
# ...BUT v26.06.00+ unified the schema and now REJECTS the legacy keys outright:
#   PlayfileConfig.reject_deprecated_keys() (config/models.py) raises
#   "Playfiles must use top-level slurm and environment_vars; slurm_info/env_vars are
#   no longer supported." if either key is present.
# So the dual shape is fatal on new releases and required on old ones. Detect which
# schema this checkout carries and emit only what it accepts. Detection reads the repo
# (present since Phase 1) because the venv does not exist yet at this point.
LEGACY_SCHEMA=1
if grep -rqs "slurm_info/env_vars are no longer supported" \
     "${REPO_DIR}/cli/llmb-install/src" 2>/dev/null; then
  LEGACY_SCHEMA=0
fi
if [[ "${LEGACY_SCHEMA}" == "1" ]]; then
  echo "==> playfile schema: legacy (emitting slurm_info+env_vars AND slurm+environment_vars)"
else
  echo "==> playfile schema: v26.06+ unified (emitting top-level slurm+environment_vars only)"
fi
{
  echo "# Generated by NVIS Rack Status (install-dgxc-benchmarking-workspace.sh)"
  echo "venv_type: ${VENV_TYPE}"
  echo "install_path: ${INSTALL_PATH}"
  # --- validator shape (legacy only; REJECTED by v26.06+) ---
  if [[ "${LEGACY_SCHEMA}" == "1" ]]; then
    echo "slurm_info:"
    echo "  slurm:"
    echo "    account: ${SLURM_ACCOUNT}"
    echo "    gpu_partition: ${GPU_PARTITION}"
    echo "    cpu_partition: ${CPU_PARTITION}"
    echo "    gpu_partition_gres: ${GPU_GRES}"
    echo "    cpu_partition_gres: null"
    echo "    node_architecture: ${ARCH}"
  fi
  # --- loader shape (InstallConfig.from_dict) ---
  echo "slurm:"
  echo "  account: ${SLURM_ACCOUNT}"
  echo "  gpu_partition: ${GPU_PARTITION}"
  echo "  cpu_partition: ${CPU_PARTITION}"
  echo "  gpu_partition_gres: ${GPU_GRES}"
  echo "  cpu_partition_gres: null"
  echo "gpu_type: ${GPU_TYPE}"
  echo "node_architecture: ${ARCH}"
  echo "install_method: ${INSTALL_METHOD}"
  echo "selected_workloads:"
  IFS=',' read -ra _wl <<< "${WORKLOADS}"
  for _w in "${_wl[@]}"; do
    _w="${_w#"${_w%%[![:space:]]*}"}"; _w="${_w%"${_w##*[![:space:]]}"}"  # trim
    [[ -n "${_w}" ]] && echo "  - ${_w}"
  done
  # env_vars (legacy validator) + environment_vars (loader). Contents optional (HF_TOKEN).
  if [[ -n "${HF_TOKEN}" ]]; then
    [[ "${LEGACY_SCHEMA}" == "1" ]] && { echo "env_vars:"; echo "  HF_TOKEN: ${HF_TOKEN}"; }
    echo "environment_vars:"
    echo "  HF_TOKEN: ${HF_TOKEN}"
  else
    [[ "${LEGACY_SCHEMA}" == "1" ]] && echo "env_vars: {}"
    echo "environment_vars: {}"
  fi
} > "${CONFIG_PATH}" || { echo "ERROR: cannot write config to ${CONFIG_PATH}." >&2; exit 1; }

echo "----- ${CONFIG_PATH} -----"
sed 's/\(HF_TOKEN:\).*/\1 ***redacted***/' "${CONFIG_PATH}"
echo "--------------------------"

# llmb-install global flags can precede the mode flag; install.sh forwards "$@".
PLAY_CMD=( bash "${REPO_DIR}/install.sh" )
[[ -n "${IMAGE_FOLDER}" ]] && PLAY_CMD+=( -i "${IMAGE_FOLDER}" )
PLAY_CMD+=( --play "${CONFIG_PATH}" )

if [[ "${MODE}" == "record" ]]; then
  echo "==> mode=record: repo prepared and config written; NOT installing."
  echo "    To run the full headless install (NVIS: re-run with mode=install, or by hand):"
  printf '      cd %q && ' "${REPO_DIR}"; printf '%q ' "${PLAY_CMD[@]}"; echo
  echo "    After install, run_llmb_nccl.sh expects: LLMB_INSTALL=${INSTALL_PATH}"
  echo "--- exit 0 ---"
  exit 0
fi

# --- Phase 3 (install mode): headless install ----------------------------------
# install.sh submits srun/sbatch for the container download. Over `ssh host '<cmd>'`
# this is a NON-login shell where `module` is not initialized, so make the SLURM CLI
# usable first (same helper the launchers use).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
[[ -f "${_SCRIPT_DIR}/slurm-init.inc.sh" ]] && source "${_SCRIPT_DIR}/slurm-init.inc.sh"

# --- Phase 2b: existing install -> incremental add, not a failed re-install ----
# llmb-install refuses `--play` against a live install ("Existing LLMB installation
# found ... remove the existing installation") and `express` refuses too ("Express
# mode does not support incremental installation"). The only supported way to add
# workloads is its interactive menu, which needs a tty -- and this script runs over a
# non-interactive SSH exec. llmb_add_workloads.py allocates a pty and answers the two
# prompts, so the GUI action adds the missing workloads instead of dying. Additive and
# idempotent: llmb-install filters out what is already installed.
# Override with NVIS_EXEMPLAR_FORCE_PLAY=1 to take the original --play path anyway.
if [[ -f "${INSTALL_PATH}/cluster_config.yaml" && "${NVIS_EXEMPLAR_FORCE_PLAY:-0}" != "1" ]]; then
  echo "==> existing LLMB install detected at ${INSTALL_PATH}"
  echo "    installed: $(sed -n '/^  installed:/,/^  [a-z]/p' "${INSTALL_PATH}/cluster_config.yaml" 2>/dev/null | grep -oE '^  - .*' | sed 's/^  - //' | tr '\n' ' ')"
  echo "==> switching to incremental add (llmb_add_workloads.py); requested: ${WORKLOADS}"
  ADDER="${_SCRIPT_DIR}/llmb_add_workloads.py"
  if [[ ! -x "${ADDER}" ]]; then
    echo "ERROR: ${ADDER} missing; cannot add workloads to an existing install." >&2
    echo "--- exit 1 ---"; exit 1
  fi
  export PATH="${HOME}/.local/bin:${PATH}"   # git-lfs lives here; llmb-install aborts without it
  # llmb-install downloads HuggingFace config/tokenizer files for EVERY pending workload
  # before installing any of them, and some repos (meta-llama/*) are gated -> 401 aborts
  # the whole resume. The playfile's environment_vars.HF_TOKEN only reaches the `--play`
  # path, never this interactive one, so put the token in the environment the adder (and
  # therefore llmb-install) inherits. Precedence: caller env, then the UI/API field, then
  # the standard huggingface_hub token file.
  if [[ -z "${HF_TOKEN:-}" && -n "${NVIS_EXEMPLAR_HF_TOKEN:-}" ]]; then
    export HF_TOKEN="${NVIS_EXEMPLAR_HF_TOKEN}"
  fi
  if [[ -z "${HF_TOKEN:-}" && -r "${HOME}/.cache/huggingface/token" ]]; then
    HF_TOKEN="$(tr -d '[:space:]' < "${HOME}/.cache/huggingface/token")"
    export HF_TOKEN
  fi
  if [[ -n "${HF_TOKEN:-}" ]]; then
    export HUGGING_FACE_HUB_TOKEN="${HF_TOKEN}"   # older huggingface_hub reads this name
    echo "==> HF token: present (${#HF_TOKEN} chars)"
  else
    echo "==> HF token: absent -- gated HuggingFace repos (meta-llama/*) will 401"
  fi
  # --expect makes the adder exit 4 when a requested workload is still absent from
  # cluster_config.yaml, however cleanly llmb-install exited. Without it a silent
  # no-op install reports success (which is exactly what happened on 2026-07-30/31).
  python3 "${ADDER}" "${INSTALL_PATH}" "${NVIS_EXEMPLAR_ADD_TIMEOUT:-14400}" --expect "${WORKLOADS}"
  _add_rc=$?
  echo "==> incremental add exit=${_add_rc}"
  echo "    workloads now: $(ls -1 "${INSTALL_PATH}/workloads" 2>/dev/null | tr '\n' ' ')"
  echo "--- exit ${_add_rc} ---"
  exit ${_add_rc}
fi

# --- Phase 0: prerequisites --------------------------------------------------
# dgxc-benchmarking's install.sh aborts if its prerequisites are missing (e.g.
# "Git LFS is not installed"). Many BCM/Ubuntu script nodes lack git-lfs and a
# usable python3-venv. Provision the user-space requirements here (no root/apt
# needed) so the installer runs unattended. ~/.local/bin must be on PATH so both
# uv and git-lfs are visible to install.sh (which inherits this environment).
export PATH="${HOME}/.local/bin:${PATH}"

ensure_git_lfs() {
  if git lfs version >/dev/null 2>&1; then
    echo "==> prereq: git-lfs present ($(git lfs version 2>/dev/null))"
    return 0
  fi

  # Resolve an apt privilege escalation: root needs none; a non-root user with
  # PASSWORDLESS sudo on an approved Nscale build host can still
  # install system-wide. A SYSTEM-WIDE git-lfs is preferred over ~/.local/bin
  # because dgxc-benchmarking's install.sh preflight (and its srun/subshell
  # recipes) don't inherit our PATH — a user-space binary is invisible to them.
  local sudo=""
  if [[ "$(id -u)" -eq 0 ]]; then
    sudo=""
  elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    sudo="sudo -n"
  fi
  if { [[ "$(id -u)" -eq 0 ]] || [[ -n "${sudo}" ]]; } && command -v apt-get >/dev/null 2>&1; then
    echo "==> prereq: git-lfs missing; installing system-wide via apt (${sudo:-root})"
    if ${sudo} apt-get update >/dev/null 2>&1 && ${sudo} apt-get install -y git-lfs >/dev/null 2>&1; then
      git lfs install --skip-repo >/dev/null 2>&1 || git lfs install >/dev/null 2>&1 || true
      echo "==> prereq: installed system-wide via apt $(git lfs version 2>/dev/null)"
      return 0
    fi
    echo "==> prereq: apt install failed; falling back to a user-space static binary"
  fi

  # Fallback: user-space static binary (air-gapped / no sudo). Visible to install.sh
  # only because we export ~/.local/bin onto PATH above; system-wide (apt) is more robust.
  local ver arch_tag url tmp bin
  ver="${NVIS_EXEMPLAR_GIT_LFS_VERSION:-3.7.0}"
  case "$(uname -m)" in
    aarch64|arm64) arch_tag="arm64" ;;
    x86_64|amd64)  arch_tag="amd64" ;;
    *)             arch_tag="amd64" ;;
  esac
  url="https://github.com/git-lfs/git-lfs/releases/download/v${ver}/git-lfs-linux-${arch_tag}-v${ver}.tar.gz"
  echo "==> prereq: bootstrapping git-lfs v${ver} (${arch_tag}) to ~/.local/bin (no root/apt)"
  tmp="$(mktemp -d)"
  if curl -fsSL "${url}" -o "${tmp}/glfs.tgz" 2>/dev/null && tar -xzf "${tmp}/glfs.tgz" -C "${tmp}" 2>/dev/null; then
    bin="$(find "${tmp}" -name git-lfs -type f 2>/dev/null | head -n1)"
    if [[ -n "${bin}" ]]; then
      mkdir -p "${HOME}/.local/bin"
      install -m 0755 "${bin}" "${HOME}/.local/bin/git-lfs"
      rm -rf "${tmp}"
      git lfs install --skip-repo >/dev/null 2>&1 || git lfs install >/dev/null 2>&1 || true
      echo "==> prereq: installed $(git lfs version 2>/dev/null) to ~/.local/bin"
      return 0
    fi
  fi
  rm -rf "${tmp}"
  echo "ERROR: could not provision git-lfs (no github egress to git-lfs releases, and no root/passwordless-sudo apt)." >&2
  echo "       Install it manually on $(hostname): sudo apt-get install -y git-lfs && git lfs install --skip-repo  (then re-run)." >&2
  return 1
}

echo "==> Phase 0: checking prerequisites (git-lfs, PATH)"
ensure_git_lfs || exit 1

# install_method=slurm: install.sh runs `srun`/`enroot import` to download the container.
# In this non-login SSH shell SLURM_CONF is usually unset (sinfo/srun then fail with
# "DNS SRV lookup failed / Could not establish a configuration source"). Make the SLURM
# CLI usable by locating the shared slurm.conf (override with NVIS_EXEMPLAR_SLURM_CONF).
if [[ "${INSTALL_METHOD}" == "slurm" && -z "${SLURM_CONF:-}" ]]; then
  for _c in "${NVIS_EXEMPLAR_SLURM_CONF:-}" \
            /cm/shared/apps/slurm/etc/slurm/slurm.conf \
            /cm/shared/apps/slurm/var/etc/slurm.conf \
            /etc/slurm/slurm.conf; do
    if [[ -n "${_c}" && -f "${_c}" ]]; then export SLURM_CONF="${_c}"; break; fi
  done
  if [[ -n "${SLURM_CONF:-}" ]]; then
    echo "==> prereq: SLURM_CONF=${SLURM_CONF}"
  else
    echo "WARNING: install_method=slurm but no slurm.conf found; srun may fail. Set NVIS_EXEMPLAR_SLURM_CONF." >&2
  fi
fi

echo "==> Phase 3: headless install (can take hours; downloads the NeMo container via SLURM)"
printf '==> exec:'; printf ' %q' "${PLAY_CMD[@]}"; echo
cd "${REPO_DIR}" || { echo "ERROR: cannot cd ${REPO_DIR}." >&2; exit 1; }

if [[ "${USE_UV}" -eq 1 ]]; then
  # install.sh installs uv to ~/.local/bin; make sure it (and an already-present uv) is on PATH.
  export PATH="${HOME}/.local/bin:${PATH}"
  # uv's cache (~/.cache/uv, usually local disk) and the per-workload venvs (often on NFS
  # /home) live on DIFFERENT filesystems; force copy so uv doesn't warn/fail on the
  # hardlink fallback.
  export UV_LINK_MODE="${UV_LINK_MODE:-copy}"
  # IMPORTANT: do NOT set UV_PYTHON_PREFERENCE. llmb-install creates each per-workload venv
  # with `uv venv --managed-python`, and uv ERRORS if `--managed-python` is combined with
  # `--python-preference` (which the UV_PYTHON_PREFERENCE env var maps to):
  #   "error: the argument `--managed-python` cannot be used with `--python-preference`" (exit 2).
  # uv venv does not need ensurepip (unlike `python -m venv`), so it works with the node's
  # system Python too; --managed-python just pins a standalone CPython. Pre-provision that
  # interpreter (if uv is already present) so no venv has to download it mid-install.
  if command -v uv >/dev/null 2>&1; then
    echo "==> uv mode: pre-installing managed CPython 3.12 (UV_LINK_MODE=${UV_LINK_MODE})"
    uv python install 3.12 >/dev/null 2>&1 || echo "WARNING: 'uv python install 3.12' failed; install.sh will provision on demand"
  else
    echo "==> uv mode: UV_LINK_MODE=${UV_LINK_MODE} (install.sh will bootstrap uv)"
  fi
fi

# Self-heal known-bad upstream uv pins. Some workload deps pin a git ref whose
# pyproject.toml has an invalid `[tool.uv] conflicts` entry: it uses `name = "..."`
# where uv only accepts `package`/`extra`/`group` (uv's deny_unknown_fields rejects
# it, exit 2). Concretely NVIDIA-NeMo/Run @04f900a (pulled by pretrain_nemotron-h):
#   { group = "docs", name = "colorama" }   ->   { group = "docs", package = "colorama" }
# uv clones each git dep into ~/.cache/uv/git-v0/checkouts/<digest>/<rev>/ BEFORE it
# parses the pyproject, so after a failed run the bad file is present on disk. We
# rewrite `name =` -> `package =` only on inline-table lines that also carry an
# `extra`/`group` field (so `[project] name = "..."` is never touched) and retry once.
patch_uv_conflicts() {
  local cache root f n total=0
  cache="${UV_CACHE_DIR:-${HOME}/.cache/uv}"
  root="${cache}/git-v0/checkouts"
  [[ -d "${root}" ]] || return 1
  while IFS= read -r f; do
    grep -Eq '(\{[^}]*(extra|group)[^}]*name[[:space:]]*=)|(\{[^}]*name[[:space:]]*=[^}]*(extra|group))' "${f}" 2>/dev/null || continue
    sed -i -E '/(extra|group)[[:space:]]*=.*name[[:space:]]*=|name[[:space:]]*=.*(extra|group)[[:space:]]*=/ s/\bname[[:space:]]*=/package =/g' "${f}"
    n=$(grep -c 'package = ' "${f}" 2>/dev/null || echo 0)
    echo "    patched uv conflicts schema in ${f} (package fields now: ${n})"
    total=$((total + 1))
  done < <(find "${root}" -name pyproject.toml 2>/dev/null)
  [[ "${total}" -gt 0 ]]
}

# install.sh runs under `set -e` and its environment-bootstrap phase is INTERACTIVE
# (read -p "Install 'uv'? [Y/n]", then a "Press Enter to launch" read) — only the
# llmb-install phase is headless via --play. Feeding /dev/null makes those reads hit
# EOF and abort (exit 1). Instead feed an endless answer stream so no read ever blocks
# or EOFs. USE_UV=1 -> 'y' (bootstrap uv); USE_UV=0 -> 'n' (use system Python 3.12).
_ANS="n"
[[ "${USE_UV}" -eq 1 ]] && _ANS="y"
echo "==> driving install.sh prompts non-interactively (answer='${_ANS}'; NVIS_EXEMPLAR_USE_UV to toggle)"
# Pre-emptively patch any bad checkouts left by an earlier attempt.
[[ "${USE_UV}" -eq 1 ]] && { echo "==> pre-flight: scanning uv git cache for invalid conflicts schema"; patch_uv_conflicts || true; }
"${PLAY_CMD[@]}" < <(yes "${_ANS}")
rc=$?
echo "==> install.sh --play exit ${rc}"
if [[ "${rc}" -ne 0 && "${USE_UV}" -eq 1 ]]; then
  echo "==> install failed (exit ${rc}); checking uv git cache for the invalid 'name' conflicts schema"
  if patch_uv_conflicts; then
    echo "==> patched a known-bad upstream pin; retrying install.sh once"
    "${PLAY_CMD[@]}" < <(yes "${_ANS}")
    rc=$?
    echo "==> install.sh --play retry exit ${rc}"
  else
    echo "==> no patchable conflicts schema found; not retrying"
  fi
fi
if [[ "${rc}" -eq 0 ]]; then
  # llmb-install (re)generated <INSTALL_PATH>/llmb_repo/nccl/metadata.yaml from the recipe;
  # re-apply the NVL rack-multiple scales to the runtime copy llmb-run actually reads.
  patch_nccl_scales
  echo "OK: LLMB ready. run_llmb_nccl.sh uses LLMB_INSTALL=${INSTALL_PATH}"
fi
echo "--- exit ${rc} ---"
exit "${rc}"
