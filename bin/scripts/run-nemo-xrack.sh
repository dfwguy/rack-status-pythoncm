#!/bin/bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# Multinode NEMO launch. Pass rack targets as one comma-separated spec or multiple arguments, e.g.
#   p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]
# Dashboard and manual launches use the same shared cluster log root.
#
# Slurm stdout basename (example):
#   root-NEMO-p5-r[13-14]-ct[01-18],p5-r[15-16]-ct[01-18]-72N-22403.txt

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
JOB_LOG_DIR="${RACK_JOB_LOG_DIR:-${RACK_STATUS_LOG_DIR:-/cluster/benchmark/harish/rack_status/logs}}"
mkdir -p "${JOB_LOG_DIR}" || { echo "run-nemo-xrack: cannot create log directory: ${JOB_LOG_DIR}" >&2; exit 2; }

# Which NeMo batch script to submit. Default = the Stage-2 soak / Stage-3 LLM engine
# (nemo-nemotron5h_56b-mbridge-fp8.sh, now parametrized by NVIS_NEMO_DTYPE/MAX_STEPS/GBS/MODEL_SIZE).
# Override with NVIS_NEMO_BATCH_SCRIPT to point at a different launcher.
NVIS_NEMO_BATCH_SCRIPT="${NVIS_NEMO_BATCH_SCRIPT:-nemo-nemotron5h_56b-mbridge-fp8.sh}"

# Make the Slurm CLI (scontrol/sbatch) usable in non-login SSH shells: the dashboard
# launches this over `ssh host '<cmd>'` where Environment Modules aren't initialized,
# so scontrol/sbatch would be "command not found". Idempotent; also on the head.
# shellcheck disable=SC1091
[[ -f "${SCRIPT_DIR}/slurm-init.inc.sh" ]] && source "${SCRIPT_DIR}/slurm-init.inc.sh"

# Filename tag: same specs as passed in (comma-separated if multiple argv).
spec_tag=""
for _a in "$@"; do
  [[ -n "$spec_tag" ]] && spec_tag+=","
  spec_tag+="$_a"
done

echo "Hosts: ${spec_tag}"

# Resolve targets to a REAL Slurm nodelist. The dashboard passes CT-validation specs
# like "bos01-b05-ct[01-18],bos01-b06-ct[01-18]"; on GB200 sites those are NOT Slurm
# hostnames (e.g. real = bos01-b05-11-s1-dgx-05-c01), so derive each spec's rack id and
# map it to real nodes via lib/nodes.inc.sh (RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX),
# the same mapping the NCCL launcher uses. Falls back to a direct hostlist expansion
# when the mapping yields nothing (sites where the passed names are already real Slurm
# nodes). Avoids clustershell `nodeset`, which is frequently absent on BCM head nodes.
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/nodes.inc.sh"

# Split the comma-joined specs on commas that are OUTSIDE [...]. A plain IFS=',' split tears a
# bracketed range apart wherever the range itself contains a comma: this site's tray range is
# 11-18,28-37, so "ink50-dh01-h02-[11-18,28-37]" became the two fragments
# "ink50-dh01-h02-[11-18" and "28-37]". Neither is a rack id, so nvis_nodes_resolve found nothing
# and we fell through to the per-spec fallback — which works here only because the CT template
# happens to equal the real hostname, and would produce wrong names on a site where it does not
# Exact Nscale hostnames are inventory-backed. Simple ranges like [11-18] have no inner comma,
# which is why this stayed hidden.
_specs=()
_buf=""
_depth=0
for ((_i = 0; _i < ${#spec_tag}; _i++)); do
  _c="${spec_tag:_i:1}"
  case "$_c" in
    '[') _depth=$((_depth + 1)); _buf+="$_c" ;;
    ']') _depth=$((_depth > 0 ? _depth - 1 : 0)); _buf+="$_c" ;;
    ',')
      if ((_depth > 0)); then _buf+="$_c"; else
        [[ -n "$_buf" ]] && _specs+=("$_buf")
        _buf=""
      fi
      ;;
    *) _buf+="$_c" ;;
  esac
done
[[ -n "$_buf" ]] && _specs+=("$_buf")

racks=()
for _s in "${_specs[@]}"; do
  # Strip the tray part to leave the rack id. The joiner is site-specific (NVIS_CT_ID): Bright
  # is <rack>-ct[…] but Nscale TX is <rack>-[…], so matching a literal "-ct" left the whole
  # spec as the "rack id", nvis_nodes_resolve found nothing, and we silently fell through to the
  # per-spec fallback below — which bypasses this site's rack->node mapping entirely and only
  # happens to work where the CT template already equals the real hostname.
  #
  # Drop a trailing [ranges] group with an optional "ct" joiner, plus the legacy single-tray
  # -ct<N> form. Deliberately NOT a bare trailing -<N>: that would turn a real hostname
  # (ink50-dh01-h02-11) into a truncated prefix instead of leaving it for the fallback.
  racks+=( "$(printf '%s' "${_s}" | sed -E 's/-(ct)?\[[^]]*\]$//; s/-ct[0-9]+$//')" )
done

hosts=()
if nvis_nodes_resolve "${racks[@]}" 2>/dev/null && [[ -n "${NVIS_NODELIST:-}" ]]; then
  mapfile -t hosts < <(scontrol show hostname "${NVIS_NODELIST}" 2>/dev/null)
fi
if ((${#hosts[@]} == 0)); then
  # Fallback: expand the passed spec directly (sites where these ARE real Slurm nodes).
  mapfile -t hosts < <(scontrol show hostname "${spec_tag}" 2>/dev/null)
fi
if ((${#hosts[@]} == 0)); then
  echo "run-nemo-xrack: could not resolve any Slurm nodes for: ${spec_tag}" >&2
  echo "run-nemo-xrack: set RACK_TO_NODE_SED / RACK_NODE_MATCH_SUFFIX for this site, or pass a real nodelist." >&2
  exit 1
fi
echo "run-nemo-xrack: resolved ${#hosts[@]} nodes: ${hosts[0]} ... ${hosts[-1]}"

n=$(( ${#hosts[*]} / 2 * 2 ))

# Force the batch script's multi-rack (IB) path when this allocation spans >1 rack, even if it is
# <=18 nodes. The batch script otherwise infers single-rack from node count and would disable IB +
# force MNNVL (fatal across a rack boundary). Explicit NVIS_NEMO_XRACK from the caller wins.
#
# Group hosts by rack via NVIS_RACK_ID (nvis_nodes_rack_of). This used to hardcode
# a fixed site regex, which matches nothing outside that layout: every hostname
# stayed distinct, so an 18-node SINGLE-rack run counted 18 "racks" and was pushed
# onto the cross-rack IB path. Without the template, fall back to the number of rack
# specs the caller passed (the dashboard always passes one spec per rack).
_nracks=$(for _h in "${hosts[@]}"; do nvis_nodes_rack_of "${_h}"; echo; done 2>/dev/null | sort -u | grep -c .)
if (( _nracks == 0 )); then
  _nracks=$(printf '%s\n' "${racks[@]}" | sort -u | grep -c .)
fi
if [[ -z "${NVIS_NEMO_XRACK:-}" ]]; then
  if (( _nracks > 1 )); then NVIS_NEMO_XRACK=1; else NVIS_NEMO_XRACK=0; fi
fi
export NVIS_NEMO_XRACK
echo "run-nemo-xrack: ${_nracks} distinct rack(s) -> NVIS_NEMO_XRACK=${NVIS_NEMO_XRACK}"

# Prefer the NCCL 2.30.7 image (libnccl + nccl-tests baked at 2.30.7) when present. This overrides
# the caller's NEMO_SQSH_SRC (incl. the app/UI default of the stock image) so every launch uses the
# matched-NCCL image without needing per-launch env. Set NVIS_NEMO_USE_VETTED=0 to force the caller's
# NEMO_SQSH_SRC (e.g. to compare vs stock).
#
# WHY 2.30.7 (not the older 2.28.9 vetted image): cross-rack (multi-domain NVL72) alltoall REQUIRES
# NCCL >= 2.30 — 2.28.9 fails the bootstrap regardless of env vars (validated 2026-07-07: same
# NCCL_NET_PLUGIN=none, 2.30.7 PASSES job 203 vs 2.28.9 FAILS job 210). 2.30.7 then needs
# NCCL_IGNORE_COLLNET_MISMATCH=1 (set below + in the batch script) to tolerate the stale HPC-X SHARP
# plugin's asymmetric CollNet device count while KEEPING SHARP (job 208 all-pass). See
# VALIDATION_TRACKER "Cross-rack alltoall". Override the path with NVIS_NEMO_VETTED_SQSH.
_shared_cache_dir="${NVIS_NEMO_SHARED_CACHE_DIR:-${RACK_VALIDATION_CONTAINER_ROOT:-/cluster/OVA/containers/nvftp-8k-known-good/20260824}}"
_node_cache_dir="${NVIS_NEMO_NODE_CACHE_DIR:-${RACK_VALIDATION_NODE_CONTAINER_DIR:-/opt/containers}}"
_vetted="${NVIS_NEMO_VETTED_SQSH:-${_shared_cache_dir}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh}"
if [[ "${NVIS_NEMO_USE_VETTED:-1}" == "1" && -f "${_vetted}" ]]; then
  echo "run-nemo-xrack: using NCCL 2.30.7 image ${_vetted} (matched NCCL baked in; LD_PRELOAD off; IGNORE_COLLNET_MISMATCH keeps HPC-X/SHARP)"
  NEMO_SQSH_SRC="${_vetted}"
  export NVIS_NCCL_PRELOAD_DISABLE="${NVIS_NCCL_PRELOAD_DISABLE:-1}"
fi
# Cross-rack alltoall fix (keeps SHARP). The batch script defaults this to 1 too, but set it here so
# the launcher is self-documenting and forwards it explicitly. Override with NVIS_NCCL_IGNORE_COLLNET_MISMATCH.
export NVIS_NCCL_IGNORE_COLLNET_MISMATCH="${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1}"
: "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the configured nemo .sqsh image}"
_sqsh_b=$(basename "$NEMO_SQSH_SRC")
_hostcsv="$(echo "${hosts[*]}" | tr ' ' ',')"
_src_size=$(stat -c%s "$NEMO_SQSH_SRC" 2>/dev/null || echo 0)
case "$_sqsh_b" in
  nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh)
    _expected_size=42477232128
    _expected_sha=0829d0c77f38db54848afd095862093a7c62e98778b0b8664b8edff69e273747
    ;;
  nvidia+nemo+26.04.01.sqsh)
    _expected_size=42245001216
    _expected_sha=e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca
    ;;
  nvidia+nemo+26.04.01-arm.sqsh)
    _expected_size=42245001216
    _expected_sha=55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc
    ;;
  *)
    echo "run-nemo-xrack: ERROR: image filename is not in the reviewed validation allowlist: $_sqsh_b" >&2
    exit 1
    ;;
esac
[[ "$_src_size" == "$_expected_size" ]] || {
  echo "run-nemo-xrack: ERROR: image size mismatch for ${NEMO_SQSH_SRC}: got $_src_size, expected $_expected_size" >&2
  exit 1
}
_src_sha=$(sha256sum "$NEMO_SQSH_SRC" 2>/dev/null | awk '{print $1}')
[[ "$_src_sha" == "$_expected_sha" ]] || {
  echo "run-nemo-xrack: ERROR: image SHA256 mismatch for ${NEMO_SQSH_SRC}" >&2
  exit 1
}

# The reviewed File Copy Service keeps every allowlisted image in the shared container directory
# warm under /opt/containers on each compute node. Prefer that copy directly: copying the same
# 40+ GiB image from the head node at launch defeats the cache and creates a fleet-wide NFS
# burst before Slurm even sees the job.
_cache_b="${NVIS_NEMO_CACHE_BASENAME:-}"

# build_containers.sh adds an architecture suffix, while the LLMB installer names the same
# logical image without one. Prefer an exact basename, then the ARM spelling used on this GB300
# fleet. An explicit NVIS_NEMO_CACHE_BASENAME always wins (needed for a separately vetted image).
if [[ -z "${_cache_b}" ]]; then
  _cache_candidates=("${_sqsh_b}")
  if [[ "${_sqsh_b}" == *.sqsh ]]; then
    _cache_candidates+=("${_sqsh_b%.sqsh}-arm.sqsh")
  fi
  for _candidate in "${_cache_candidates[@]}"; do
    _candidate_size=$(stat -c%s "${_shared_cache_dir}/${_candidate}" 2>/dev/null || echo 0)
    if [[ "${_candidate_size}" == "${_src_size}" ]]; then
      _cache_b="${_candidate}"
      break
    fi
  done
fi

_shared_cache_path="${_shared_cache_dir}/${_cache_b}"
_cache_size=$(stat -c%s "${_shared_cache_path}" 2>/dev/null || echo 0)
if [[ -z "${_cache_b}" || "${_cache_size}" != "${_src_size}" ]]; then
  echo "run-nemo-xrack: ERROR: no matching pre-cached image for ${NEMO_SQSH_SRC}." >&2
  echo "run-nemo-xrack: expected ${_src_size} bytes under ${_shared_cache_dir}; refusing the legacy head-node fan-out." >&2
  echo "run-nemo-xrack: stage the image through FileCopyTrigger, or set NVIS_NEMO_CACHE_BASENAME explicitly." >&2
  exit 1
fi
_cache_sha=$(sha256sum "$_shared_cache_path" 2>/dev/null | awk '{print $1}')
[[ "$_cache_sha" == "$_expected_sha" ]] || {
  echo "run-nemo-xrack: ERROR: cached image SHA256 mismatch at $_shared_cache_path" >&2
  exit 1
}

_node_cache_path="${_node_cache_dir}/${_cache_b}"
export NVIS_NEMO_CONT="${_node_cache_path}"
export NVIS_NEMO_EXPECTED_SIZE="${_cache_size}"
export NVIS_NEMO_EXPECTED_SHA256="${_expected_sha}"
echo "run-nemo-xrack: image required at ${NVIS_NEMO_CONT} (${NVIS_NEMO_EXPECTED_SIZE} bytes) on ${#hosts[@]} node(s); verification=allocation"

# Log filename uses just the racks included in the test. The arg may be a full curated nodelist
# (real hostnames) rather than rack-ct specs; using it verbatim would blow past the 255-byte
# filename limit for --output and make sbatch fail before writing any log (job dies ~2s,
# ExitCode 0:53). Derive the distinct rack ids from the resolved hosts instead.
#
# Via nvis_nodes_rack_of (NVIS_RACK_ID), the same way the rack count above does. This was the
# second place hardcoding one site prefix; only the count was converted. On Nscale the
# sed matched nothing, so every hostname stayed distinct and _racktag became all N hostnames
# joined by '+' — roughly 700 characters for 36 nodes, which is exactly the >255-byte --output
# name this comment warns about.
_racktag=$(for _h in "${hosts[@]}"; do nvis_nodes_rack_of "${_h}"; echo; done 2>/dev/null \
  | sort -u | grep -v '^$' | paste -sd+ | tr -d ' ')
# No template match: fall back to the rack specs the caller passed (the dashboard passes one per
# rack), never to the raw hostname list.
if [[ -z "${_racktag}" ]]; then
  _racktag=$(printf '%s\n' "${racks[@]}" | sort -u | paste -sd+ | tr -d ' ')
fi
[[ -n "${_racktag}" ]] || _racktag="${spec_tag}"

# Even a correct rack-only tag exceeds NAME_MAX for a full 16-rack SU (the old tag was 239
# characters before adding root-NEMO-, node count, job id and .txt). Keep the endpoints and rack
# count for humans, plus a digest for uniqueness. Slurm otherwise accepts the job, allocates every
# node, then fails in one second with ExitCode 0:53 because stdout cannot be opened.
if (( ${#_racktag} > 120 )); then
  _racktag_full="${_racktag}"
  _racktag_first="${_racktag_full%%+*}"
  _racktag_last="${_racktag_full##*+}"
  _racktag_hash=$(printf '%s' "${_racktag_full}" | sha256sum | cut -c1-10)
  _racktag="${_racktag_first}+${_racktag_last}+${_nracks}r-${_racktag_hash}"
  echo "run-nemo-xrack: compacted long rack tag to ${_racktag}"
fi
_out_stem="root-NEMO-${_racktag}-${n}N"
_out_path="${JOB_LOG_DIR%/}/${_out_stem}-%j.txt"

# Submit Slurm work as the configured cluster operator. Rack Status itself runs as
# root in a deliberately minimal container; allocation-side checks and Pyxis must
# use the same identity as production validation, especially for IMEX O_RDWR.
submit_uid="${NVIS_SLURM_SUBMIT_UID:-${RACK_SLURM_SUBMIT_UID:-}}"
submit_gid="${NVIS_SLURM_SUBMIT_GID:-${RACK_SLURM_SUBMIT_GID:-}}"
submit_home="${NVIS_SLURM_SUBMIT_HOME:-${RACK_SLURM_SUBMIT_HOME:-}}"
slurm_submit_as=()
if [[ -n "$submit_uid" || -n "$submit_gid" ]]; then
  if [[ ! "$submit_uid" =~ ^[0-9]+$ || ! "$submit_gid" =~ ^[0-9]+$ ]]; then
    echo "run-nemo-xrack: ERROR: submit uid/gid must both be numeric" >&2
    exit 2
  fi
  slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")
fi
if [[ -n "$submit_home" ]]; then
  if [[ "$submit_home" != /* || "$submit_home" == *","* || "$submit_home" == *$'\n'* ]]; then
    echo "run-nemo-xrack: ERROR: submit home must be an absolute path without commas/newlines" >&2
    exit 2
  fi
  export HOME="$submit_home"
fi

_gpu_recovery_check="${NVIS_NEMO_GPU_RECOVERY_CHECK:-1}"
case "$_gpu_recovery_check" in
  0|1) ;;
  *) echo "run-nemo-xrack: ERROR: NVIS_NEMO_GPU_RECOVERY_CHECK must be 0 or 1" >&2; exit 2 ;;
esac
_stage_script="${SCRIPT_DIR}/validation_container_stage.sbatch"
[[ -r "$_stage_script" ]] || {
  echo "run-nemo-xrack: ERROR: allocation preflight script missing: $_stage_script" >&2
  exit 2
}
# Files passed directly to sbatch are spooled from this launcher, but batch
# siblings are opened later on compute nodes. /app is private to the web
# container, so only export the standardized compute-visible runtime tree.
node_script_dir="${NVIS_VALIDATION_NODE_SCRIPTS_DIR:-${RACK_VALIDATION_PROVENANCE_ROOT:-/cluster/OVA/scripts/nscaletx-16k-validation}/runtime/bin/scripts}"
if [[ "$node_script_dir" != /* || "$node_script_dir" == /app || "$node_script_dir" == /app/* || \
      "$node_script_dir" == *","* || "$node_script_dir" == *$'\n'* ]]; then
  echo "run-nemo-xrack: ERROR: NVIS_VALIDATION_NODE_SCRIPTS_DIR must be a compute-visible absolute path outside /app, without commas/newlines" >&2
  exit 2
fi
_stage_out="${JOB_LOG_DIR%/}/nemo-container-preflight-%j.out"
_stage_export="ALL,NVIS_STAGE_SOURCE=${_shared_cache_path},NVIS_STAGE_DEST=${NVIS_NEMO_CONT},NVIS_STAGE_RSYNC=0,NVIS_STAGE_CONTAINER_PREFLIGHT=0,NVIS_STAGE_ENSURE_IMEX_CHANNEL0=1,NVIS_STAGE_GPU_RECOVERY_CHECK=${_gpu_recovery_check},NVIS_STAGE_EXPECTED_GPUS=4,NVIS_STAGE_EXPECTED_SIZE=${NVIS_NEMO_EXPECTED_SIZE},NVIS_STAGE_EXPECTED_SHA256=${NVIS_NEMO_EXPECTED_SHA256},NVIS_SCRIPTS_DIR=${node_script_dir}"
_stage_cmd=(sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue
  --nodes="$n" --nodelist="$_hostcsv" --ntasks-per-node=1 --gpus-per-node=4
  --output="$_stage_out" --export="$_stage_export" "$_stage_script")

# Export the real scripts dir to the job so the batch script can find lib/fabric.inc.sh
# (Slurm spools only the batch script, not lib/). Without it the fabric classifier no-ops
# and NCCL falls back to a naive mlx5_0..3 set that mixes IB + RoCE and fails.
export NVIS_SCRIPTS_DIR="${node_script_dir}"
cd "${JOB_LOG_DIR}" || exit 2
if [[ "${NVIS_NEMO_SUBMIT_DRY_RUN:-0}" == "1" ]]; then
  echo "run-nemo-xrack: submit-dry-run preflight=allocation runtime=${node_script_dir} output=${_stage_out} workload_output=${_out_path} nodes=${n}; no Slurm job submitted"
  exit 0
fi

# Compute-node access is allocation-only on this site. Run the established
# validation-container gate over the exact requested nodelist instead of requiring
# pdsh/SSH credentials in the web container. --wait plus positive per-node marker
# counts keeps missing nodes, corrupt images, and reset-required GPUs fail-closed.
set +e
_stage_reply="$("${_stage_cmd[@]}" 2>&1)"
_stage_rc=$?
set -e
_stage_id="$(printf '%s\n' "$_stage_reply" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | head -1)"
if [[ ! "$_stage_id" =~ ^[0-9]+$ ]]; then
  echo "run-nemo-xrack: ERROR: allocation preflight returned no valid Slurm id (rc=${_stage_rc}): ${_stage_reply}" >&2
  exit 1
fi
_stage_log="${_stage_out//%j/${_stage_id}}"
_image_ok=$(grep -c '^IMAGE_STAGE_READY ' "$_stage_log" 2>/dev/null || true)
_gpu_ok=0
if [[ "$_gpu_recovery_check" == 1 ]]; then
  _gpu_ok=$(grep -c '^GPU_RECOVERY_READY ' "$_stage_log" 2>/dev/null || true)
fi
_complete_ok=$(grep -c "^VALIDATION_CONTAINER_STAGE_COMPLETE nodes=${n} destination=${NVIS_NEMO_CONT}$" "$_stage_log" 2>/dev/null || true)
if (( _stage_rc != 0 || _image_ok != n || _complete_ok != 1 )) || \
   [[ "$_gpu_recovery_check" == 1 && "$_gpu_ok" -ne "$n" ]]; then
  echo "run-nemo-xrack: ERROR: allocation preflight failed: image=${_image_ok}/${n} gpu=${_gpu_ok}/${n} rc=${_stage_rc} log=${_stage_log}" >&2
  tail -40 "$_stage_log" 2>/dev/null | sed 's/^/  /' >&2 || true
  echo "run-nemo-xrack: aborting before NeMo workload submission" >&2
  exit 1
fi
echo "run-nemo-xrack: allocation preflight ready on all ${n} nodes (image=${_image_ok}/${n} gpu=${_gpu_ok}/${n} preflight_id=${_stage_id})"

# Operator/automation preflight: exercise the identical exact-node Slurm gate but
# stop before the NeMo workload allocation. The preflight allocation above is the
# only job submitted in this mode.
if [[ "${NVIS_NEMO_CACHE_CHECK_ONLY:-0}" == "1" ]]; then
  echo "run-nemo-xrack: cache-check-only complete; no NeMo workload submitted"
  exit 0
fi

sbatch "${slurm_submit_as[@]}" -N "${n}" \
  -w "$(echo "${hosts[*]}" | tr ' ' ',')" \
  -t "${NVIS_NEMO_WALLTIME:-03:00:00}" \
  --job-name="NEMO-${n}N" \
  --export="ALL,NVIS_SCRIPTS_DIR=${node_script_dir},NVIS_NEMO_CONT=${NVIS_NEMO_CONT},NVIS_NEMO_EXPECTED_SIZE=${NVIS_NEMO_EXPECTED_SIZE},NVIS_NEMO_EXPECTED_SHA256=${NVIS_NEMO_EXPECTED_SHA256},NVIS_NEMO_XRACK=${NVIS_NEMO_XRACK},NVIS_NCCL_PRELOAD_DISABLE=${NVIS_NCCL_PRELOAD_DISABLE:-0},NVIS_NCCL_IGNORE_COLLNET_MISMATCH=${NVIS_NCCL_IGNORE_COLLNET_MISMATCH:-1},NVIS_NCCL_DEBUG=${NVIS_NCCL_DEBUG:-},NVIS_NEMO_MAX_STEPS=${NVIS_NEMO_MAX_STEPS:-},NVIS_NEMO_DTYPE=${NVIS_NEMO_DTYPE:-},NVIS_NEMO_MODEL_SIZE=${NVIS_NEMO_MODEL_SIZE:-},NVIS_NEMO_GBS=${NVIS_NEMO_GBS:-},NVIS_NEMO_UB_SKIPMC=${NVIS_NEMO_UB_SKIPMC:-},NVIS_NEMO_NVLS_ENABLE=${NVIS_NEMO_NVLS_ENABLE:-},NVIS_NEMO_RECIPE_DEFAULT=${NVIS_NEMO_RECIPE_DEFAULT:-},NVIS_NCCL_PRELOAD_SRC=${NVIS_NCCL_PRELOAD_SRC:-},NVIS_NCCL_NET_PLUGIN=${NVIS_NCCL_NET_PLUGIN:-},NVIS_NEMO_CLI=${NVIS_NEMO_CLI:-},NVIS_NEMO_GPU=${NVIS_NEMO_GPU:-},NVIS_NEMO_FORCE_BF16_ALIAS=${NVIS_NEMO_FORCE_BF16_ALIAS:-},NVIS_NEMO_RECIPE_NAME=${NVIS_NEMO_RECIPE_NAME:-},NVIS_NEMO_HF_HOME=${NVIS_NEMO_HF_HOME:-},NVIS_HF_HOME_CONT=${NVIS_HF_HOME_CONT:-}" \
  --output="${_out_path}" \
  "$SCRIPT_DIR/${NVIS_NEMO_BATCH_SCRIPT}"
