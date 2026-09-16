#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
set -euo pipefail

output_dir=/cluster/OVA/logs/nscaletx-16k-validation
output_path="${output_dir}/validated-racks.json"
tmp_path="${output_dir}/.validated-racks.$$.json"
trap 'rm -f -- "$tmp_path"' EXIT

mkdir -p "$output_dir"
# The curator replaces the three Parquet files independently.  During that
# short window the browser can momentarily report that ``jobs`` or ``results``
# does not exist.  Rebuild the whole temporary snapshot after a bounded delay;
# never publish a partial snapshot, and still fail closed after six attempts.
collect_ok=0
for attempt in 1 2 3 4 5 6; do
  rm -f -- "$tmp_path"
  if /usr/bin/python3 /home/hlakshminara/gsheets-16k/collect_16k_data.py --output "$tmp_path"; then
    collect_ok=1
    break
  fi
  if (( attempt < 6 )); then
    echo "WARN: validation evidence collection attempt ${attempt}/6 failed; retrying in 5s" >&2
    sleep 5
  fi
done
if (( collect_ok != 1 )); then
  echo "FATAL: validation evidence collection failed after 6 attempts" >&2
  exit 2
fi
# Run through the service account's Docker-group membership. Never invoke sudo
# from a timer: a missing socket permission must fail closed and be corrected by
# deployment, rather than generating repeated sudo mail to the central root inbox.
/usr/local/bin/docker exec rack-status-rack-status-1 \
  python3 /app/bin/scripts/ingest_16k_validation_evidence.py "$tmp_path"
chmod 0644 "$tmp_path"
mv -f "$tmp_path" "$output_path"
trap - EXIT
