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
# Allocation-scoped copy of the proven Nscale TX Llama 405B node preflight.
# A one-task Slurm step cannot see GPU device files on this site, so read-only
# GPU queries deliberately run through a short-lived host systemd unit.
set -euo pipefail

node=$(hostname -s)

host_nvidia_smi() {
    sudo systemd-run --wait --pipe --quiet --collect \
        --unit="llama405b-preflight-${SLURM_JOB_ID:-manual}-$$-$RANDOM" \
        /usr/bin/nvidia-smi "$@"
}

compute_process_count() {
    local output
    if ! output=$(host_nvidia_smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null); then
        # DCGM teardown can make nvidia-smi transiently return exit 6. Keep
        # polling and fail closed until the host GPU state settles.
        echo 999999
        return 0
    fi
    sed '/^[[:space:]]*$/d' <<<"$output" | wc -l
}

max_gpu_memory_mib() {
    local output
    if ! output=$(host_nvidia_smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null); then
        echo 999999
        return 0
    fi
    sort -nr <<<"$output" | head -n 1
}

if pgrep -x nvvs >/dev/null 2>&1 \
    || pgrep -x ubergemm >/dev/null 2>&1 \
    || (( $(compute_process_count) > 0 )); then
    echo "PREFLIGHT_CLEAN $node: stopping escaped DCGM diagnostics"
    stop_rc=0
    sudo systemctl stop nvidia-dcgm.service || stop_rc=$?
    if (( stop_rc != 0 )); then
        echo "PREFLIGHT_NOTE $node: DCGM stop returned $stop_rc; final state checks remain authoritative"
    fi
    if pgrep -x nvvs >/dev/null 2>&1 || pgrep -x ubergemm >/dev/null 2>&1; then
        sudo systemctl kill --kill-whom=all --signal=SIGKILL nvidia-dcgm.service || true
    fi
    sudo systemctl start nvidia-dcgm.service
fi

for attempt in $(seq 1 30); do
    process_count=$(compute_process_count)
    max_memory=$(max_gpu_memory_mib)
    max_memory=${max_memory:-999999}
    service_state=$(systemctl is-active nvidia-dcgm.service 2>/dev/null || true)

    if ! pgrep -x nvvs >/dev/null 2>&1 \
        && ! pgrep -x ubergemm >/dev/null 2>&1 \
        && (( process_count == 0 )) \
        && (( max_memory < 1024 )) \
        && [[ $service_state == active ]]; then
        echo "PREFLIGHT_READY $node: processes=$process_count max_mem_mib=$max_memory dcgm=$service_state"
        exit 0
    fi

    sleep 2
done

echo "PREFLIGHT_FAILED $node: processes=$process_count max_mem_mib=$max_memory dcgm=$service_state" >&2
ps -eo user,pid,ppid,etimes,stat,cmd | grep -E 'nvvs|ubergemm' | grep -v grep >&2 || true
host_nvidia_smi >&2 || true
exit 1
