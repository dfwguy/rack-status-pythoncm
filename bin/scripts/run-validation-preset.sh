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
# Audited dispatcher for Rack Status validation presets.  The browser chooses only
# a preset and exact targets; commands/images/version gates stay code-owned here.
# Author: Rafael M. Koike & Cursor Agent ("Ada")
set -euo pipefail

# Needs bash 4 for `declare -A` (rack grouping) and `mapfile` (hostname expansion). Fine on the
# head node; macOS ships bash 3.2 as /bin/bash, where those fail as `declare: -A: invalid option`
# several hundred lines from the cause. Say it once, here.
if ((BASH_VERSINFO[0] < 4)); then
  printf 'run-validation-preset.sh: needs bash 4+ (found %s at %s).\n' \
    "${BASH_VERSION}" "${BASH}" >&2
  printf '  It uses associative arrays and mapfile. On macOS: brew install bash, then run it\n' >&2
  printf '  with that bash — /bin/bash is 3.2 and cannot run this script.\n' >&2
  exit 2
fi

usage() {
  cat <<'EOF'
Usage: run-validation-preset.sh --preset ID --nodelist HOSTLIST
       [--storage nfs|local] [--rsync] [--local-container-dir NODE_CACHE]
       [--log-dir PATH] [--group-nodelist HOSTLIST ...] [--dry-run]
EOF
}

preset=""; nodelist=""; storage="nfs"; rsync_enabled=0
local_container_dir="${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}"; local_container_dir="${local_container_dir%/}"; log_dir="${RACK_STATUS_LOG_DIR:-$PWD}"; dry_run=0
# Where this site mounts shared container images. A mount point is a site fact, and it was
# hard-coded into five image paths here, so a preset could not run anywhere but Nscale. Only
# the directory is configurable: the basenames below are the reviewed artifacts and the
# size/SHA256 gates in stage_image still have to pass, so a site may point at its own mirror
# and cannot substitute a different image.
container_root="${RACK_VALIDATION_CONTAINER_ROOT:-/cluster/containers}"; container_root="${container_root%/}"
group_nodelists=()
while (($#)); do
  case "$1" in
    --preset) preset="${2:?missing --preset value}"; shift 2 ;;
    --preset=*) preset="${1#*=}"; shift ;;
    --nodelist) nodelist="${2:?missing --nodelist value}"; shift 2 ;;
    --nodelist=*) nodelist="${1#*=}"; shift ;;
    --storage) storage="${2:?missing --storage value}"; shift 2 ;;
    --storage=*) storage="${1#*=}"; shift ;;
    --rsync) rsync_enabled=1; shift ;;
    --local-container-dir) local_container_dir="${2:?missing --local-container-dir value}"; shift 2 ;;
    --local-container-dir=*) local_container_dir="${1#*=}"; shift ;;
    --log-dir) log_dir="${2:?missing --log-dir value}"; shift 2 ;;
    --log-dir=*) log_dir="${1#*=}"; shift ;;
    --group-nodelist) group_nodelists+=("${2:?missing --group-nodelist value}"); shift 2 ;;
    --group-nodelist=*) group_nodelists+=("${1#*=}"); shift ;;
    --dry-run) dry_run=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$preset" ]] || { echo "ERROR: --preset is required" >&2; exit 2; }
[[ -n "$nodelist" ]] || { echo "ERROR: --nodelist is required" >&2; exit 2; }
[[ "$storage" == nfs || "$storage" == local ]] || { echo "ERROR: storage must be nfs or local" >&2; exit 2; }
# The node cache is the cluster's (connection.node_container_dir -> NVIS_NODE_CONTAINER_DIR), not the
# caller's: staging writes into it on every selected node, so --local-container-dir may restate it
# but never move it.
_configured_local_dir="${NVIS_NODE_CONTAINER_DIR:-/tmp/containers}"; _configured_local_dir="${_configured_local_dir%/}"
[[ "${local_container_dir%/}" == "$_configured_local_dir" ]] || { echo "ERROR: local container directory is fixed to this cluster's node cache, ${_configured_local_dir}" >&2; exit 2; }
local_container_dir="$_configured_local_dir"
[[ "$storage" == local || "$rsync_enabled" == 0 ]] || { echo "ERROR: --rsync requires --storage local" >&2; exit 2; }
# On a P2P cluster the node cache is filled only by P2P campaigns; staging here would fan the image
# out from the head and write into a directory P2P manages. Local storage still verifies the image.
[[ "${NVIS_NODE_IMAGES_P2P:-0}" != 1 || "$rsync_enabled" == 0 ]] || { echo "ERROR: --rsync is off on this cluster: NVIS-P2P distributes its images; run a P2P campaign instead" >&2; exit 2; }

# Hand our resolved log directory to every child launcher. The sbatch jobs below take it as an
# explicit --output under "$log_dir", but the chained host launchers (run_nccl_mpi.sh,
# run-exemplar-system-info.sh) read it only from RACK_JOB_LOG_DIR / RACK_STATUS_LOG_DIR. Without
# this export a child fell back to its own baked-in default and tried to mkdir an absolute path
# that does not exist on the execution host -- the whole preset failed before Slurm ever saw it.
export RACK_JOB_LOG_DIR="$log_dir"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if (( dry_run )); then
  node_count="${NVIS_PREPARED_NODE_COUNT:?NVIS_PREPARED_NODE_COUNT is required for offline dry-run}"
  IFS=, read -r -a nodes <<<"$nodelist"
else
# shellcheck disable=SC1091
  [[ -f "${script_dir}/slurm-init.inc.sh" ]] && source "${script_dir}/slurm-init.inc.sh"
# shellcheck disable=SC1091
  source "${script_dir}/lib/nodes.inc.sh"
  command -v scontrol >/dev/null || { echo "ERROR: scontrol not found" >&2; exit 2; }
  command -v sbatch >/dev/null || { echo "ERROR: sbatch not found" >&2; exit 2; }
  nvis_nodes_resolve --nodelist "$nodelist"
  nodelist="$NVIS_NODELIST"
  node_count="$NVIS_NNODES"
  mapfile -t nodes < <(scontrol show hostnames "$nodelist")
  mkdir -p "$log_dir"
fi

validate_exact_groups() {
  (( ${#group_nodelists[@]} >= 2 )) || {
    echo "ERROR: multirack MNUberGEMM requires at least two exact --group-nodelist values" >&2
    exit 2
  }
  local node group member
  declare -A selected=() grouped=()
  for node in "${nodes[@]}"; do
    [[ -n "$node" ]] && selected["$node"]=1
  done
  for group in "${group_nodelists[@]}"; do
    [[ -n "$group" ]] || { echo "ERROR: empty MNUberGEMM node group" >&2; exit 2; }
    IFS=, read -r -a _group_members <<<"$group"
    (( ${#_group_members[@]} > 0 )) || { echo "ERROR: empty MNUberGEMM node group" >&2; exit 2; }
    for member in "${_group_members[@]}"; do
      [[ "$member" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
        echo "ERROR: MNUberGEMM group contains a non-explicit node name: $member" >&2; exit 2;
      }
      [[ -n "${selected[$member]:-}" ]] || {
        echo "ERROR: MNUberGEMM group node is outside the reviewed target: $member" >&2; exit 2;
      }
      [[ -z "${grouped[$member]:-}" ]] || {
        echo "ERROR: MNUberGEMM node appears in more than one rack group: $member" >&2; exit 2;
      }
      grouped["$member"]=1
    done
  done
  (( ${#grouped[@]} == node_count )) || {
    echo "ERROR: MNUberGEMM rack groups cover ${#grouped[@]} nodes, reviewed target has ${node_count}" >&2
    exit 2
  }
}

if [[ "$preset" == mnubergemm-26.03-v1.17 && "${NVIS_PRESET_SCOPE_MULTIRACK:-0}" == 1 ]]; then
  validate_exact_groups
elif (( ${#group_nodelists[@]} > 0 )); then
  echo "ERROR: --group-nodelist is only valid for multirack MNUberGEMM" >&2
  exit 2
fi

print_cmd() { printf 'PLAN:'; printf ' %q' "$@"; printf '\n'; }
run_cmd() {
  print_cmd "$@"
  (( dry_run )) || "$@"
}

stage_image() {
  local source_image="$1" local_image="$2" expected_size="$3" expected_sha="$4" needs_ib="$5" run_image
  if [[ "$storage" == local ]]; then run_image="$local_image"; else run_image="$source_image"; fi
  STAGE_DEPENDENCY=()
  if [[ "$storage" == nfs ]]; then
    print_cmd verify-container --path "$source_image" --size "$expected_size" --sha256 "$expected_sha"
    if (( ! dry_run )); then
      [[ "$(stat -c %s "$source_image" 2>/dev/null || true)" == "$expected_size" ]] || { echo "ERROR: NFS image size gate failed: $source_image" >&2; exit 2; }
      [[ "$(sha256sum "$source_image" | awk '{print $1}')" == "$expected_sha" ]] || { echo "ERROR: NFS image SHA256 gate failed: $source_image" >&2; exit 2; }
    fi
    if [[ "$needs_ib" == 1 ]]; then
      local preflight="${script_dir}/validation_container_preflight.sbatch"
      run_cmd sbatch --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4 \
        --output="${log_dir%/}/validation-container-preflight-%j.out" \
        --export="ALL,NVIS_PREFLIGHT_IMAGE=${source_image}" "$preflight"
    fi
    return
  fi
  local stage_script="${script_dir}/validation_container_stage.sbatch"
  [[ -r "$stage_script" ]] || { echo "ERROR: stage script missing: $stage_script" >&2; exit 2; }
  local export_set="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=${needs_ib},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha},NVIS_SCRIPTS_DIR=${script_dir},NVIS_VAR_DIR=${NVIS_VAR_DIR:-}"
  local output="${log_dir%/}/validation-container-stage-%j.out"
  local cmd=(sbatch --parsable --wait --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4
    --output="$output" --export="$export_set" "$stage_script")
  print_cmd "${cmd[@]}"
  if (( ! dry_run )); then
    local submitted stage_id
    submitted="$("${cmd[@]}")"; stage_id="${submitted%%;*}"
    [[ "$stage_id" =~ ^[0-9]+$ ]] || { echo "ERROR: unable to parse stage job id: $submitted" >&2; exit 2; }
    echo "STAGE_SUBMITTED jobid=$stage_id output=$output"
    echo "STAGE_COMPLETE jobid=$stage_id output=$output"
  fi
}

# LLMB's own image store, separate from the container mount above and parameterised for the
# same reason: its install paths already were, and leaving only the image absolute meant the
# one hard-coded path left in this preset.
llmb_image_root="${LLMB_IMAGE_ROOT:-/cluster/llmb/images}"; llmb_image_root="${llmb_image_root%/}"
llmb_install="${LLMB_INSTALL:-/home/cmods/workspace/llmb}"
llmb_repo="${LLMB_REPO:-${llmb_install}/llmb_repo}"
llmb_run="${LLMB_RUN:-${NVIS_LLMB_RUN:-/home/cmods/workspace/llmb_venv/bin/llmb-run}}"

case "$preset" in
  nccl-2.31.2)
    [[ "$storage" == nfs && "$rsync_enabled" == 0 ]] || {
      echo "ERROR: nccl-2.31.2 is a direct host runtime and has no container to stage" >&2; exit 2;
    }
    export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
    export NVIS_NCCL_RUNTIME_PREFLIGHT=1 NVIS_NCCL_MIN_CUDA_VERSION=13.0 NVIS_NCCL_SBATCH_WAIT=1
    run_cmd bash "${script_dir}/run_nccl_mpi.sh" --nodelist "$nodelist" \
      --nccl-version 2.30.7 --rounds 5 --size-max 16G
    ;;

  llama31-405b-fp8)
    (( node_count == 256 )) || { echo "ERROR: Llama 3.1 405B preset requires exactly 256 nodes/1024 GPUs; got ${node_count}" >&2; exit 2; }
    source_image="${container_root}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    node_sha="$(printf '%s\n' "${nodes[@]}" | sort | sha256sum | awk '{print $1}')"
    llama_batch="${script_dir}/llama405b_fp8_1024.sbatch"
    [[ -r "$llama_batch" ]] || { echo "ERROR: 1024-GPU Llama batch missing: $llama_batch" >&2; exit 2; }
    export_set="ALL,NVIS_LLAMA_SOURCE_IMAGE=${source_image},NVIS_LLAMA_RUN_IMAGE=${image},NVIS_LLAMA_STORAGE=${storage},NVIS_LLAMA_RSYNC=${rsync_enabled},NVIS_LLAMA_EXPECTED_NODE_SHA256=${node_sha},NVIS_LLAMA_SCRIPTS_DIR=${script_dir},NVIS_LLAMA_LOG_DIR=${log_dir}"
    run_cmd sbatch --wait --no-requeue --nodes=256 --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 \
      --output="${log_dir%/}/llama405b-fp8-1024-%j.out" --export="$export_set" "$llama_batch"
    ;;

  gpt-oss-cpu)
    (( node_count >= 1 && node_count <= 256 )) || { echo "ERROR: GPT-OSS CPU fan-out requires 1-256 exact nodes; got ${node_count}" >&2; exit 2; }
    source_image="${llmb_image_root}/tensorrt-llm+release+1.1.0rc5.sqsh"
    local_image="${local_container_dir}/tensorrt-llm+release+1.1.0rc5.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 30284881920 80de1820829418a3789a4ff5ce0c661f669ffb07c013552ac648822b9c1f9e72 0
    cpu_launch="${llmb_repo}/microbenchmarks/cpu_overhead/launch.sh"
    [[ -r "$cpu_launch" || "$dry_run" == 1 ]] || { echo "ERROR: GPT-OSS CPU launch script missing: $cpu_launch" >&2; exit 2; }
    model="${llmb_install}/workloads/microbenchmark_cpu_overhead/gpt-oss-120b/metal/model.bin"
    [[ -r "$model" || "$dry_run" == 1 ]] || { echo "ERROR: GPT-OSS model is missing/unreadable: $model" >&2; exit 2; }
    gpt_batch="${script_dir}/gpt-oss-cpu-strict.sbatch"
    export_set="ALL,LLMB_INSTALL=${llmb_install},LLMB_REPO=${llmb_repo},RUN_CONF_IMAGE=${image},GPU_TYPE=gb300,NUM_PROMPTS=100000,LLMB_EXPERIMENTS_ROOT=${log_dir}"
    # Keep the validated recipe single-node, but submit one independently
    # attributable job for every exact selected node. Submit the full fan-out
    # before waiting so a rack does not serialize eighteen CPU microbenchmarks.
    gpt_job_ids=()
    for node in "${nodes[@]}"; do
      gpt_cmd=(sbatch --parsable --no-requeue "${STAGE_DEPENDENCY[@]}" --nodelist="$node" --nodes=1 --gpus-per-node=4
        --job-name="gpt-oss-${node}" --output="${log_dir%/}/gpt-oss-cpu-${node}-%j.out"
        --export="$export_set" "$gpt_batch")
      print_cmd "${gpt_cmd[@]}"
      if (( ! dry_run )); then
        submitted="$("${gpt_cmd[@]}")"
        gpt_id="${submitted%%;*}"
        [[ "$gpt_id" =~ ^[0-9]+$ ]] || { echo "ERROR: unable to parse GPT-OSS job id: $submitted" >&2; exit 2; }
        gpt_job_ids+=("$gpt_id")
        echo "SUBMITTED: gpt-oss node=${node} jobid=${gpt_id}"
      fi
    done
    if (( ! dry_run )); then
      gpt_csv=$(IFS=,; printf '%s' "${gpt_job_ids[*]}")
      while squeue -h -j "$gpt_csv" 2>/dev/null | grep -q .; do sleep 5; done
      gpt_failed=0
      gpt_seen=0
      while IFS='|' read -r gpt_id gpt_state gpt_exit _rest; do
        [[ "$gpt_id" =~ ^[0-9]+$ ]] || continue
        gpt_seen=$((gpt_seen + 1))
        printf 'COMPLETE: gpt-oss jobid=%s state=%s exit=%s\n' "$gpt_id" "$gpt_state" "$gpt_exit"
        [[ "$gpt_state" == COMPLETED && "$gpt_exit" == 0:0 ]] || gpt_failed=1
      done < <(sacct -X -n -P -j "$gpt_csv" -o JobIDRaw,State,ExitCode)
      (( gpt_seen == ${#gpt_job_ids[@]} )) || gpt_failed=1
      (( gpt_failed == 0 )) || { echo "ERROR: one or more GPT-OSS node jobs failed" >&2; exit 2; }
    fi
    ;;

  exemplar-system-info)
    [[ -d "${llmb_repo}/microbenchmarks/system_info" || "$dry_run" == 1 ]] || {
      echo "ERROR: v26.06.01 system_info recipe missing under LLMB_REPO=${llmb_repo}" >&2; exit 2;
    }
    (( dry_run )) || git -C "$llmb_repo" rev-parse -q --verify 'v26.06.01^{commit}' >/dev/null || {
      echo "ERROR: LLMB_REPO has no v26.06.01 tag" >&2; exit 2;
    }
    (( dry_run )) || git -C "$llmb_repo" diff --quiet v26.06.01 -- microbenchmarks/system_info || {
      echo "ERROR: installed system_info recipe differs from NVIDIA v26.06.01" >&2; exit 2;
    }
    source_image="${container_root}/nvidia+nemo+26.04.01-arm.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 42245001216 55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc 1
    sysinfo_runner="${script_dir}/run-exemplar-system-info.sh"
    run_cmd bash "$sysinfo_runner" --nodelist "$nodelist" --image "$image" --log-dir "$log_dir" \
      "${STAGE_DEPENDENCY[@]}"
    ;;

  nemo-26.04.01)
    source_image="${container_root}/nvidia+nemo+26.04.01-arm.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 42245001216 55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc 1
    nemo_xrack="${NVIS_PRESET_SCOPE_MULTIRACK:-0}"
    export_set="ALL,NVIS_NEMO_CONT=${image},NVIS_SCRIPTS_DIR=${script_dir},NVIS_VAR_DIR=${NVIS_VAR_DIR:-},NVIS_NEMO_XRACK=${nemo_xrack},NVIS_NEMO_MAX_STEPS=50"
    run_cmd sbatch --wait --no-requeue "${STAGE_DEPENDENCY[@]}" --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36 \
      --chdir="$script_dir" --output="${log_dir%/}/nemo-26.04.01-%j.out" --export="$export_set" "${script_dir}/nemo-nemotron5h_56b-mbridge-fp8.sh"
    ;;

  mnubergemm-26.03-v1.17)
    # This validation is a bounded qualification, not the separate 24-hour MOP
    # burn-in. Keep both limits in the generated Slurm command so no caller can
    # accidentally inherit an unlimited partition default.
    # Leave five minutes inside the one-hour allocation for Slurm prolog,
    # container startup, and clean teardown; equal payload/wall clocks would
    # deterministically turn an otherwise healthy run into TIMEOUT.
    mnubergemm_runtime_seconds=3300
    mnubergemm_slurm_time_limit=01:00:00
    source_image="${container_root}/mnubergemm-26.03-v1.17-arm.sqsh"
    local_image="${local_container_dir}/mnubergemm-26.03-v1.17-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 13949554688 9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6 0
    export_set="ALL,NVIS_MNUBERGEMM_CONT=${image},NVIS_BIN_SCRIPTS=${script_dir},MNUBERGEMM_TIME_TO_RUN=${mnubergemm_runtime_seconds}"
    if (( ${#group_nodelists[@]} == 0 )); then
      group_nodelists=("$nodelist")
    fi
    mnubergemm_job_ids=()
    group_index=0
    for group in "${group_nodelists[@]}"; do
      group_index=$((group_index + 1))
      IFS=, read -r -a _group_members <<<"$group"
      group_count="${#_group_members[@]}"
      group_label="$(printf '%03d' "$group_index")"
      mnubergemm_cmd=(sbatch --parsable --no-requeue "${STAGE_DEPENDENCY[@]}"
        --time="$mnubergemm_slurm_time_limit" --nodes="$group_count" --nodelist="$group"
        --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36
        --job-name="mnubergemm-${group_label}" --chdir="$script_dir"
        --output="${log_dir%/}/mnubergemm-26.03-v1.17-g${group_label}-%j.out"
        --export="$export_set" "${script_dir}/mnubergemm.sbatch")
      print_cmd "${mnubergemm_cmd[@]}"
      if (( ! dry_run )); then
        submitted="$("${mnubergemm_cmd[@]}")"
        mnubergemm_id="${submitted%%;*}"
        [[ "$mnubergemm_id" =~ ^[0-9]+$ ]] || {
          echo "ERROR: unable to parse MNUberGEMM job id: $submitted" >&2; exit 2;
        }
        mnubergemm_job_ids+=("$mnubergemm_id")
        echo "SUBMITTED: mnubergemm group=${group_label} nodes=${group_count} jobid=${mnubergemm_id}"
      fi
    done
    if (( ! dry_run )); then
      mnubergemm_csv="$(IFS=,; printf '%s' "${mnubergemm_job_ids[*]}")"
      while squeue -h -j "$mnubergemm_csv" 2>/dev/null | grep -q .; do sleep 5; done
      mnubergemm_failed=0
      mnubergemm_seen=0
      while IFS='|' read -r mnubergemm_id mnubergemm_state mnubergemm_exit _rest; do
        [[ "$mnubergemm_id" =~ ^[0-9]+$ ]] || continue
        mnubergemm_seen=$((mnubergemm_seen + 1))
        printf 'COMPLETE: mnubergemm jobid=%s state=%s exit=%s\n' \
          "$mnubergemm_id" "$mnubergemm_state" "$mnubergemm_exit"
        [[ "$mnubergemm_state" == COMPLETED && "$mnubergemm_exit" == 0:0 ]] || mnubergemm_failed=1
      done < <(sacct -X -n -P -j "$mnubergemm_csv" -o JobIDRaw,State,ExitCode)
      (( mnubergemm_seen == ${#mnubergemm_job_ids[@]} )) || mnubergemm_failed=1
      (( mnubergemm_failed == 0 )) || {
        echo "ERROR: one or more MNUberGEMM rack jobs failed" >&2; exit 2;
      }
    fi
    ;;

  *) echo "ERROR: unsupported preset: $preset" >&2; exit 2 ;;
esac

if (( dry_run )); then
  echo "VALIDATION_PRESET_DRY_RUN preset=${preset} nodes=${node_count} storage=${storage} rsync=${rsync_enabled}"
else
  echo "VALIDATION_PRESET_LAUNCHED preset=${preset} nodes=${node_count} storage=${storage} rsync=${rsync_enabled}"
fi
