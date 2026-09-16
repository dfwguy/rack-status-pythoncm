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
       [--storage nfs|local] [--rsync|--no-rsync] [--local-container-dir /opt/containers]
       [--log-dir PATH] [--group-nodelist HOSTLIST ...] [--dry-run]
EOF
}

preset=""; nodelist=""; storage="nfs"; rsync_enabled=0
shared_container_root="${RACK_VALIDATION_CONTAINER_ROOT:-/cluster/OVA/containers/nvftp-8k-known-good/20260824}"
configured_local_container_dir="${RACK_VALIDATION_NODE_CONTAINER_DIR:-/opt/containers}"
default_log_dir="/cluster/OVA/logs/nscaletx-16k-validation"
local_container_dir="$configured_local_container_dir"; log_dir="${RACK_STATUS_LOG_DIR:-$default_log_dir}"; dry_run=0
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
    --no-rsync) rsync_enabled=0; shift ;;
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
[[ "$shared_container_root" == /* && "$shared_container_root" != *".."* ]] || { echo "ERROR: invalid RACK_VALIDATION_CONTAINER_ROOT" >&2; exit 2; }
[[ "$configured_local_container_dir" == /* && "$configured_local_container_dir" != *".."* ]] || { echo "ERROR: invalid RACK_VALIDATION_NODE_CONTAINER_DIR" >&2; exit 2; }
[[ "$local_container_dir" == "$configured_local_container_dir" ]] || { echo "ERROR: local container directory is fixed to $configured_local_container_dir" >&2; exit 2; }
[[ "$storage" == local || "$rsync_enabled" == 0 ]] || { echo "ERROR: --rsync requires --storage local" >&2; exit 2; }

ensure_imex_channel0=0
case "$preset" in
  nccl-2.29.3|nemo-26.04.01|mnubergemm-26.03-v1.17)
    ensure_imex_channel0=1
    ;;
esac

# Log directories are made setgid/group-writable below so Slurm can create
# output as the configured submit identity.  Never apply that policy to a
# login home directory: OpenSSH StrictModes then rejects public-key login.
reject_account_home_log_dir() {
  local requested="$1" resolved="$1" account_home resolved_home
  requested="${requested%/}"
  [[ -n "$requested" ]] || requested=/
  if [[ -d "$requested" ]]; then
    resolved="$(cd -P -- "$requested" 2>/dev/null && pwd -P)" || resolved="$requested"
  else
    resolved="$requested"
  fi
  if [[ "$resolved" == / || "$resolved" == /root || "$resolved" =~ ^/home/[^/]+$ ]]; then
    echo "ERROR: refusing to use an account home directory as validation log directory: $1" >&2
    echo "HINT: omit --log-dir to use $default_log_dir, or choose a dedicated log subdirectory" >&2
    return 2
  fi
  if command -v getent >/dev/null 2>&1; then
    while IFS=: read -r _ _ _ _ _ account_home _; do
      [[ "$account_home" == /* && -d "$account_home" ]] || continue
      resolved_home="$(cd -P -- "$account_home" 2>/dev/null && pwd -P)" || continue
      if [[ "$resolved" == "$resolved_home" ]]; then
        echo "ERROR: refusing to use an account home directory as validation log directory: $1" >&2
        echo "HINT: omit --log-dir to use $default_log_dir, or choose a dedicated log subdirectory" >&2
        return 2
      fi
    done < <(getent passwd 2>/dev/null || true)
  fi
}

reject_account_home_log_dir "$log_dir"

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
  if ! scontrol show node "$nodelist" -o >/dev/null 2>&1; then
    echo "ERROR: one or more requested nodes are not registered in Slurm: $nodelist" >&2
    corrected="${nodelist//ink50-dh01-h/ink50-dh02-h}"
    if [[ "$corrected" != "$nodelist" ]] && scontrol show node "$corrected" -o >/dev/null 2>&1; then
      echo "HINT: h-row compute nodes are under ink50-dh02, not ink50-dh01; use: $corrected" >&2
    fi
    exit 2
  fi
fi

# Rack Status runs as root inside the control-plane container so it can use the
# host Slurm client and shared mounts.  Submitting as root makes BCM's compute
# prolog terminate the allocation before the batch script starts (the job is
# recorded as root/root with exit 0:53).  Use the configured operator identity
# for every preset-owned sbatch, just as the NCCL launcher already does.
submit_uid="${NVIS_SLURM_SUBMIT_UID:-}"
submit_gid="${NVIS_SLURM_SUBMIT_GID:-}"
submit_home="${NVIS_SLURM_SUBMIT_HOME:-}"
slurm_submit_as=()
if [[ -n "$submit_uid" || -n "$submit_gid" ]]; then
  if [[ ! "$submit_uid" =~ ^[0-9]+$ || ! "$submit_gid" =~ ^[0-9]+$ ]]; then
    echo "ERROR: NVIS_SLURM_SUBMIT_UID and NVIS_SLURM_SUBMIT_GID must both be numeric" >&2
    exit 2
  fi
  slurm_submit_as=(--uid="$submit_uid" --gid="$submit_gid")
fi
if [[ -n "$submit_home" ]]; then
  if [[ "$submit_home" != /* || "$submit_home" == *","* || "$submit_home" == *$'\n'* ]]; then
    echo "ERROR: NVIS_SLURM_SUBMIT_HOME must be an absolute path without commas or newlines" >&2
    exit 2
  fi
  export HOME="$submit_home"
fi

# Slurm opens --output as the submitted UID/GID, not as this root-owned web
# launcher. A newly requested canonical directory therefore must not retain the
# root:cmods 2755 mode produced by a plain mkdir: cmods could traverse it but
# could not create stdout, and slurmd terminated the job with WTERMSIG 53.
# Adjust only the exact requested directory (never its parents or contents),
# then verify the ownership/mode contract before any sbatch call.
ensure_submit_log_dir() {
  local path="$1" gid="$2" actual_gid mode mode_bits
  [[ "$path" == /* && "$path" != *$'\n'* ]] || {
    echo "ERROR: log directory must be an absolute path without newlines: $path" >&2
    return 2
  }
  mkdir -p -- "$path" || {
    echo "ERROR: cannot create log directory: $path" >&2
    return 2
  }
  [[ -d "$path" ]] || {
    echo "ERROR: log path is not a directory: $path" >&2
    return 2
  }
  if [[ -n "$gid" ]]; then
    chgrp -- "$gid" "$path" || {
      echo "ERROR: cannot set log directory group to submit gid $gid: $path" >&2
      return 2
    }
  else
    gid="$(id -g)"
  fi
  chmod g+rwx,g+s -- "$path" || {
    echo "ERROR: cannot make log directory setgid and group-writable: $path" >&2
    return 2
  }
  actual_gid="$(stat -c %g -- "$path")" || {
    echo "ERROR: cannot verify log directory group: $path" >&2
    return 2
  }
  mode="$(stat -c %a -- "$path")" || {
    echo "ERROR: cannot verify log directory mode: $path" >&2
    return 2
  }
  mode_bits=$((8#$mode))
  if [[ "$actual_gid" != "$gid" ]] || (( (mode_bits & 02030) != 02030 )); then
    echo "ERROR: log directory permission verification failed: path=$path gid=$actual_gid expected_gid=$gid mode=$mode" >&2
    return 2
  fi
  echo "LOG_DIR_READY path=$path gid=$actual_gid mode=$mode"
}

if (( ! dry_run )); then
  ensure_submit_log_dir "$log_dir" "$submit_gid"
fi

# The dispatcher runs in the Rack Status container, but submitted jobs run on
# compute nodes where /app does not exist.  Runtime helpers therefore live in
# the standardized shared scripts tree, visible to both the OVA and every tray.
node_script_dir="${NVIS_VALIDATION_NODE_SCRIPTS_DIR:-${RACK_VALIDATION_PROVENANCE_ROOT:-/cluster/OVA/scripts/nscaletx-16k-validation}/runtime/bin/scripts}"
if [[ "$node_script_dir" != /* || "$node_script_dir" == *","* || "$node_script_dir" == *$'\n'* ]]; then
  echo "ERROR: NVIS_VALIDATION_NODE_SCRIPTS_DIR must be an absolute path without commas or newlines" >&2
  exit 2
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

if [[ "$preset" == mnubergemm-26.03-v1.17 ]]; then
  if (( ${#group_nodelists[@]} == 0 )); then
    declare -A derived_rack_members=()
    derived_rack_order=()
    for node in "${nodes[@]}"; do
      rack="${node%-*}"
      [[ -n "${derived_rack_members[$rack]:-}" ]] || derived_rack_order+=("$rack")
      derived_rack_members["$rack"]="${derived_rack_members[$rack]:-}${derived_rack_members[$rack]:+,}${node}"
    done
    for rack in "${derived_rack_order[@]}"; do
      group_nodelists+=("${derived_rack_members[$rack]}")
    done
  fi
  if (( ${#group_nodelists[@]} > 1 )); then
    validate_exact_groups
  elif [[ "${NVIS_PRESET_SCOPE_MULTIRACK:-0}" == 1 ]]; then
    echo "ERROR: multirack MNUberGEMM resolved to fewer than two physical rack groups" >&2
    exit 2
  fi
elif (( ${#group_nodelists[@]} > 0 )); then
  echo "ERROR: --group-nodelist is only valid for multirack MNUberGEMM" >&2
  exit 2
fi

print_cmd() { printf 'PLAN:'; printf ' %q' "$@"; printf '\n'; }
run_cmd() {
  print_cmd "$@"
  (( dry_run )) || "$@"
}

require_slurm_job_success() {
  local job_id="$1" label="$2" record state exit_code
  record="$(sacct -X -n -P -j "$job_id" -o JobIDRaw,State,ExitCode | awk -F'|' -v id="$job_id" '$1 == id {print; exit}')"
  IFS='|' read -r _ state exit_code <<<"$record"
  printf 'COMPLETE: %s jobid=%s state=%s exit=%s\n' \
    "$label" "$job_id" "${state:-missing}" "${exit_code:-missing}"
  if [[ "$state" != COMPLETED || "$exit_code" != 0:0 ]]; then
    printf 'ERROR: %s jobid=%s did not complete successfully: state=%s exit=%s\n' \
      "$label" "$job_id" "${state:-missing}" "${exit_code:-missing}" >&2
    return 1
  fi
}

stage_image() {
  local source_image="$1" local_image="$2" expected_size="$3" expected_sha="$4" needs_ib="$5" run_image
  if [[ "$storage" == local ]]; then run_image="$local_image"; else run_image="$source_image"; fi
  STAGE_DEPENDENCY=()
  # A fleet image can be staged and checksum-audited ahead of the launch window.  In that
  # case, do not allocate one giant all-node verification job immediately before a rack-wise
  # MNUberGEMM fanout: a single slurmd/prolog failure would hold the entire campaign.  This is
  # deliberately hash-bound rather than a boolean skip.  The caller must attest the exact
  # allowlisted digest, storage must be local, and rsync must already be disabled.
  if [[ "$storage" == local && "$rsync_enabled" == 0 && -n "${NVIS_LOCAL_STAGE_VERIFIED_SHA256:-}" ]]; then
    [[ "$NVIS_LOCAL_STAGE_VERIFIED_SHA256" == "$expected_sha" ]] || {
      echo "ERROR: pre-staged image attestation SHA256 does not match the preset allowlist" >&2
      exit 2
    }
    if [[ "$ensure_imex_channel0" == 0 ]]; then
      echo "STAGE_SKIPPED_PREVERIFIED path=${run_image} size=${expected_size} sha256=${expected_sha}"
      return
    fi
    echo "STAGE_RECHECK_REQUIRED path=${run_image} reason=imex-channel0-prelaunch"
  fi
  if [[ "$ensure_imex_channel0" == 0 && "$storage" == nfs ]]; then
    print_cmd verify-container --path "$source_image" --size "$expected_size" --sha256 "$expected_sha"
    if (( ! dry_run )); then
      [[ "$(stat -c %s "$source_image" 2>/dev/null || true)" == "$expected_size" ]] || { echo "ERROR: NFS image size gate failed: $source_image" >&2; exit 2; }
      [[ "$(sha256sum "$source_image" | awk '{print $1}')" == "$expected_sha" ]] || { echo "ERROR: NFS image SHA256 gate failed: $source_image" >&2; exit 2; }
    fi
    if [[ "$needs_ib" == 1 ]]; then
      local preflight="${script_dir}/validation_container_preflight.sbatch"
      run_cmd sbatch "${slurm_submit_as[@]}" --wait --no-requeue --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4 \
        --output="${log_dir%/}/validation-container-preflight-%j.out" \
        --export="ALL,NVIS_PREFLIGHT_IMAGE=${source_image}" "$preflight"
    fi
    return
  fi
  local stage_script="${script_dir}/validation_container_stage.sbatch"
  [[ -r "$stage_script" ]] || { echo "ERROR: stage script missing: $stage_script" >&2; exit 2; }
  local export_set="ALL,NVIS_STAGE_SOURCE=${source_image},NVIS_STAGE_DEST=${run_image},NVIS_STAGE_RSYNC=${rsync_enabled},NVIS_STAGE_CONTAINER_PREFLIGHT=${needs_ib},NVIS_STAGE_ENSURE_IMEX_CHANNEL0=${ensure_imex_channel0},NVIS_STAGE_EXPECTED_SIZE=${expected_size},NVIS_STAGE_EXPECTED_SHA256=${expected_sha},NVIS_SCRIPTS_DIR=${script_dir}"
  local output="${log_dir%/}/validation-container-stage-%j.out"
  local cmd=(sbatch "${slurm_submit_as[@]}" --parsable --wait --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=1 --gpus-per-node=4
    --output="$output" --export="$export_set" "$stage_script")
  print_cmd "${cmd[@]}"
  if (( ! dry_run )); then
    local submitted stage_id stage_rc resolved_output
    set +e
    submitted="$("${cmd[@]}" 2>&1)"
    stage_rc=$?
    set -e
    printf '%s\n' "$submitted"
    stage_id="$(printf '%s\n' "$submitted" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | head -1)"
    [[ "$stage_id" =~ ^[0-9]+$ ]] || {
      echo "ERROR: unable to parse stage job id after sbatch rc=${stage_rc}: $submitted" >&2
      exit 2
    }
    resolved_output="${output//%j/$stage_id}"
    echo "STAGE_SUBMITTED jobid=$stage_id output=$resolved_output"
    if (( stage_rc != 0 )); then
      echo "STAGE_FAILED jobid=$stage_id rc=$stage_rc output=$resolved_output" >&2
      exit "$stage_rc"
    fi
    stage_terminal_rc=0
    require_slurm_job_success "$stage_id" container-stage || stage_terminal_rc=$?
    if (( stage_terminal_rc != 0 )); then
      echo "STAGE_FAILED jobid=$stage_id rc=0 output=$resolved_output" >&2
      exit 1
    fi
    echo "STAGE_COMPLETE jobid=$stage_id output=$resolved_output"
  fi
}

llmb_install="${LLMB_INSTALL:-/home/cmods/workspace/llmb}"
llmb_repo="${LLMB_REPO:-${llmb_install}/llmb_repo}"
llmb_run="${LLMB_RUN:-${NVIS_LLMB_RUN:-/home/cmods/workspace/llmb_venv/bin/llmb-run}}"

case "$preset" in
  nccl-2.29.3)
    [[ "$storage" == local && "$rsync_enabled" == 0 ]] || {
      echo "ERROR: nccl-2.29.3 uses the pre-staged /opt/containers image; storage must be local and rsync disabled" >&2; exit 2;
    }
    export NVIS_LOCAL_STAGE_VERIFIED_SHA256=e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca
    export NVIS_VALIDATION_NODE_SCRIPTS_DIR="$node_script_dir"
    export NVIS_NCCL_ITERS=500 NCCL_DEBUG=INFO NCCL_DEBUG_SUBSYS=ALL
    nccl_cmd=(bash "${script_dir}/run-llmb-nccl-container.sh" --nodelist "$nodelist"
      --storage local --no-rsync --local-container-dir "$local_container_dir" --log-dir "$log_dir")
    if (( dry_run )); then
      # The transport contract, submit identity, pinned recipe, and image digest
      # are owned by the nested launcher. Execute its own no-submit dry run so a
      # dispatcher dry run proves those details instead of merely printing the
      # wrapper pathname.
      "${nccl_cmd[@]}" --dry-run
    else
      run_cmd "${nccl_cmd[@]}"
    fi
    ;;

  llama31-405b-fp8)
    (( node_count == 256 )) || { echo "ERROR: Llama 3.1 405B preset requires exactly 256 nodes/1024 GPUs; got ${node_count}" >&2; exit 2; }
    source_image="${shared_container_root}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm-nccl2.30.7.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    node_sha="$(printf '%s\n' "${nodes[@]}" | sort | sha256sum | awk '{print $1}')"
    llama_batch="${script_dir}/llama405b_fp8_1024.sbatch"
    [[ -r "$llama_batch" ]] || { echo "ERROR: 1024-GPU Llama batch missing: $llama_batch" >&2; exit 2; }
    # The production Llama acceptance contract requires NVLink multicast. Keep
    # these values explicit in the Slurm receipt; the batch independently
    # validates them before opening the IMEX channel on every node.
    llama_mnnvl=1
    llama_nvls=1
    llama_ub_skipmc=0
    export_set="ALL,NVIS_LLAMA_SOURCE_IMAGE=${source_image},NVIS_LLAMA_RUN_IMAGE=${image},NVIS_LLAMA_STORAGE=${storage},NVIS_LLAMA_RSYNC=${rsync_enabled},NVIS_LLAMA_EXPECTED_NODE_SHA256=${node_sha},NVIS_LLAMA_SCRIPTS_DIR=${node_script_dir},NVIS_LLAMA_LOG_DIR=${log_dir},NVIS_LLAMA_MNNVL_ENABLE=${llama_mnnvl},NVIS_LLAMA_NVLS_ENABLE=${llama_nvls},NVIS_LLAMA_UB_SKIPMC=${llama_ub_skipmc}"
    run_cmd sbatch "${slurm_submit_as[@]}" --wait --no-requeue --nodes=256 --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 \
      --output="${log_dir%/}/llama405b-fp8-1024-%j.out" --export="$export_set" "$llama_batch"
    ;;

  gpt-oss-cpu)
    (( node_count >= 1 && node_count <= 256 )) || { echo "ERROR: GPT-OSS CPU fan-out requires 1-256 exact nodes; got ${node_count}" >&2; exit 2; }
    source_image="/cluster/llmb/images/tensorrt-llm+release+1.1.0rc5.sqsh"
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
      gpt_cmd=(sbatch "${slurm_submit_as[@]}" --parsable --no-requeue "${STAGE_DEPENDENCY[@]}" --nodelist="$node" --nodes=1 --gpus-per-node=4
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
    source_image="${shared_container_root}/nvidia+nemo+26.04.01-arm.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 42245001216 55130500d8b9cdb0b2be21f859c2ee6a10040d236e5173a11d94e51b3f9892cc 1
    sysinfo_runner="${script_dir}/run-exemplar-system-info.sh"
    run_cmd bash "$sysinfo_runner" --nodelist "$nodelist" --image "$image" --log-dir "$log_dir" \
      "${STAGE_DEPENDENCY[@]}"
    ;;

  nemo-26.04.01)
    source_image="${shared_container_root}/nvidia+nemo+26.04.01.sqsh"
    local_image="${local_container_dir}/nvidia+nemo+26.04.01.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    if [[ "$storage" == local && "$rsync_enabled" == 0 ]]; then
      export NVIS_LOCAL_STAGE_VERIFIED_SHA256=e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca
    fi
    stage_image "$source_image" "$local_image" 42245001216 e9a68d31832f5575eabaae94d9360e45145a0047110740d80412ec491a247aca 1
    # Preserve the full NVLink/NVLS path inside one NVL72 rack. Across physical
    # racks, retain MNNVL for intra-rack traffic and use IB between racks, but
    # disable cross-rack multicast while cliqueId 0x7ffe is repeated. Explicit
    # validated overrides remain available for controlled transport diagnosis.
    case "${NVIS_PRESET_SCOPE_MULTIRACK:-0}" in
      0)
        nemo_default_xrack=0
        nemo_default_nvls=1
        nemo_default_ub_skipmc=0
        ;;
      1)
        nemo_default_xrack=1
        nemo_default_nvls=0
        nemo_default_ub_skipmc=1
        ;;
      *)
        echo "ERROR: NVIS_PRESET_SCOPE_MULTIRACK must be 0 or 1" >&2
        exit 2
        ;;
    esac
    nemo_xrack="${NVIS_NEMO_XRACK:-$nemo_default_xrack}"
    nemo_mnnvl="${NVIS_NCCL_MNNVL:-1}"
    nemo_nvls="${NVIS_NEMO_NVLS_ENABLE:-$nemo_default_nvls}"
    nemo_ub_skipmc="${NVIS_NEMO_UB_SKIPMC:-$nemo_default_ub_skipmc}"
    for transport_value in "$nemo_xrack" "$nemo_mnnvl" "$nemo_nvls" "$nemo_ub_skipmc"; do
      [[ "$transport_value" == 0 || "$transport_value" == 1 ]] || {
        echo "ERROR: NeMo transport controls must be 0 or 1" >&2; exit 2;
      }
    done
    nemo_batch="${node_script_dir}/nemo-nemotron5h_56b-mbridge-fp8.sh"
    [[ -r "$nemo_batch" || "$dry_run" == 1 ]] || { echo "ERROR: compute-visible NeMo script missing: $nemo_batch" >&2; exit 2; }
    if (( ! dry_run )); then
      nemo_batch_sha="$(sha256sum "$nemo_batch" | awk '{print $1}')"
      [[ "$nemo_batch_sha" == a71fd4fc252a98d6b2923612dfed58aaca7b2ab13fc3abbd2804a78a323c739d ]] || {
        echo "ERROR: compute-visible NeMo script is stale: $nemo_batch" >&2; exit 2;
      }
      [[ -r "${node_script_dir}/sysinfo.sh" && -r "${node_script_dir}/lib/fabric.inc.sh" ]] || {
        echo "ERROR: compute-visible NeMo helper tree is incomplete: $node_script_dir" >&2; exit 2;
      }
    fi
    export_set="ALL,NVIS_NEMO_CONT=${image},NVIS_SCRIPTS_DIR=${node_script_dir},NVIS_NEMO_XRACK=${nemo_xrack},NVIS_NCCL_MNNVL=${nemo_mnnvl},NVIS_NEMO_UB_SKIPMC=${nemo_ub_skipmc},NVIS_NEMO_NVLS_ENABLE=${nemo_nvls},NVIS_NEMO_MAX_STEPS=30"
    nemo_output="${log_dir%/}/nemo-26.04.01-%j.out"
    nemo_cmd=(sbatch "${slurm_submit_as[@]}" --parsable --wait --no-requeue "${STAGE_DEPENDENCY[@]}"
      --nodes="$node_count" --nodelist="$nodelist" --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36
      --chdir="$node_script_dir" --output="$nemo_output" --export="$export_set" "$nemo_batch")
    print_cmd "${nemo_cmd[@]}"
    if (( ! dry_run )); then
      set +e
      nemo_submitted="$("${nemo_cmd[@]}" 2>&1)"
      nemo_rc=$?
      set -e
      printf '%s\n' "$nemo_submitted"
      nemo_id="$(printf '%s\n' "$nemo_submitted" | sed -n 's/^\([0-9][0-9]*\)\(;.*\)\{0,1\}$/\1/p' | head -1)"
      [[ "$nemo_id" =~ ^[0-9]+$ ]] || {
        echo "ERROR: unable to parse NeMo job id after sbatch rc=${nemo_rc}: $nemo_submitted" >&2
        exit 2
      }
      nemo_resolved_output="${nemo_output//%j/$nemo_id}"
      echo "SUBMITTED jobid=$nemo_id kind=nemo-26.04.01 output=$nemo_resolved_output"
      (( nemo_rc == 0 )) || {
        echo "ERROR: NeMo jobid=$nemo_id sbatch wait failed rc=$nemo_rc output=$nemo_resolved_output" >&2
        exit "$nemo_rc"
      }
      require_slurm_job_success "$nemo_id" nemo-26.04.01
    fi
    ;;

  mnubergemm-26.03-v1.17)
    # This validation is a bounded qualification, not the separate 24-hour MOP
    # burn-in. Keep both limits in the generated Slurm command so no caller can
    # accidentally inherit an unlimited partition default.
    # Run the requested 1800-second validation burn. The allocation gets a
    # bounded ten-minute envelope for Slurm prolog, container startup, and
    # clean teardown so a healthy 30-minute burn does not end as TIMEOUT.
    mnubergemm_runtime_seconds=1800
    mnubergemm_slurm_time_limit=00:40:00
    source_image="${shared_container_root}/mnubergemm-26.03-v1.17-arm.sqsh"
    local_image="${local_container_dir}/mnubergemm-26.03-v1.17-arm.sqsh"
    if [[ "$storage" == local ]]; then image="$local_image"; else image="$source_image"; fi
    stage_image "$source_image" "$local_image" 13949554688 9832dda6e80c8719e74f81ce617cac3abb77399e48d5385a38e86a040cad93e6 0
    export_set="ALL,NVIS_MNUBERGEMM_CONT=${image},NVIS_BIN_SCRIPTS=${node_script_dir},MNUBERGEMM_TIME_TO_RUN=${mnubergemm_runtime_seconds}"
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
      mnubergemm_cmd=(sbatch "${slurm_submit_as[@]}" --parsable --no-requeue "${STAGE_DEPENDENCY[@]}"
        --time="$mnubergemm_slurm_time_limit" --nodes="$group_count" --nodelist="$group"
        --ntasks-per-node=4 --gpus-per-node=4 --cpus-per-task=36
        --job-name="mnubergemm-${group_label}" --chdir="$node_script_dir"
        --output="${log_dir%/}/mnubergemm-26.03-v1.17-g${group_label}-%j.out"
        --export="$export_set" "${node_script_dir}/mnubergemm.sbatch")
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
      # Consume the complete squeue output before testing it.  With `set -o pipefail`, the
      # former `squeue | grep -q .` stopped grep after the first row; squeue then received
      # SIGPIPE for a large fleet and the loop falsely treated 111 live jobs as terminal.
      while true; do
        if ! mnubergemm_queued="$(squeue -h -j "$mnubergemm_csv" -o '%A|%T' 2>/dev/null)"; then
          echo "ERROR: unable to monitor submitted MNUberGEMM jobs" >&2
          exit 2
        fi
        [[ -n "$mnubergemm_queued" ]] || break
        sleep 5
      done
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
