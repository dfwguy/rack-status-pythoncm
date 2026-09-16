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
#
# For each NV switch in the nodeset, use Redfish to set the BMC root password
# from a protected file when the account reports PasswordChangeRequired (or the
# property is missing on older BMCs).
#
# Authentication order for GET/PATCH:
#   1) credential named by NVSWITCH_BMC_PRIMARY_PASSWORD_FILE
#   2) credential named by NVSWITCH_BMC_SECONDARY_PASSWORD_FILE
# If both fail, the target credential file is tried; success means already compliant.
# succeeds, the BMC is treated as already compliant (password already applied).
#
# Usage: nvswitches_set_bmc_passwd.sh <nodeset>
#
# Optional: FORCE_PATCH=1  -> PATCH after successful default-password auth even
# when PasswordChangeRequired is false (use if your BMC mis-reports the flag).

set -euo pipefail

# BCM installs cmsh at an absolute path and does NOT put it on PATH (Nscale TX: BCM 11 keeps
# it at /cm/local/apps/cmd/bin/cmsh). RACK_CMSH is the same knob the backend uses.
CMSH=${RACK_CMSH:-cmsh}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=bin/scripts/lib/nvswitch_credentials.sh
source "${HERE}/lib/nvswitch_credentials.sh"

ACCOUNT_PATH='/redfish/v1/AccountService/Accounts/root'

usage() {
	echo "Usage: ${0##*/} <nodeset>" >&2
	exit 1
}

password_change_required() {
	local json_file=$1
	if command -v jq >/dev/null 2>&1; then
		case $(jq -r 'if has("PasswordChangeRequired") then .PasswordChangeRequired|tostring else "absent" end' "$json_file") in
		true) return 0 ;;
		false) return 1 ;;
		absent)
			# Older BMCweb: assume change is needed if we are running this tool.
			return 0
			;;
		*) return 0 ;;
		esac
	fi
	if grep -qE '"PasswordChangeRequired"[[:space:]]*:[[:space:]]*true' "$json_file"; then
		return 0
	fi
	if grep -qE '"PasswordChangeRequired"[[:space:]]*:[[:space:]]*false' "$json_file"; then
		return 1
	fi
	return 0
}

set_bmc_password_for_node() {
	local nvsw=$1
	local result_file=$2
	local ip
	local tmp netrc json_payload
	local http_code
	local working_pass_file=""
	local try_pass_file
	local patch_code
	local reachable=0

	record() {
		printf '%s\t%s\t%s\t%s\n' "$nvsw" "$1" "${2:-}" "${3:-}" >"$result_file"
	}

	if ! ip=$("$CMSH" -c "device ; use $nvsw ; interfaces; get rf0 ip" 2>/dev/null) || [[ -z "${ip// }" ]]; then
		echo "${nvsw}: could not resolve rf0 IP" >&2
		record NO_IP "" "cmsh rf0 IP missing"
		return 0
	fi

	ip=$(printf '%s' "$ip" | tr -d '\r\n')

	tmp=$(mktemp)
	netrc=$(mktemp)
	json_payload=$(mktemp)
	chmod 0600 "$tmp" "$netrc" "$json_payload"
	trap 'rm -f "$tmp" "$netrc" "$json_payload"' RETURN

	local url="https://${ip}${ACCOUNT_PATH}"

	# Try the configured onboarding credentials in the documented order.
	for try_pass_file in "$PASS_OPENBMC_FILE" "$PASS_TEMP_FILE"; do
		nvis_make_netrc "$ip" root "$try_pass_file" "$netrc"
		set +e
		http_code=$(curl -k -sS -o "$tmp" -w '%{http_code}' \
			--netrc-file "$netrc" \
			--connect-timeout 5 --max-time 10 \
			-H 'Content-Type: application/json' \
			"$url" 2>/dev/null)
		set -e
		if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
			working_pass_file=$try_pass_file
			break
		fi
		if [[ "$http_code" != "000" && -n "$http_code" ]]; then
			reachable=1
		fi
	done

	if [[ -z "$working_pass_file" ]]; then
		# Candidate credentials rejected: root may already use the target credential.
		nvis_make_netrc "$ip" root "$NEW_PASSWORD_FILE" "$netrc"
		set +e
		http_code=$(curl -k -sS -o "$tmp" -w '%{http_code}' \
			--netrc-file "$netrc" \
			--connect-timeout 5 --max-time 10 \
			-H 'Content-Type: application/json' \
			"$url" 2>/dev/null)
		set -e
		if [[ "$http_code" != "000" && -n "$http_code" ]]; then
			reachable=1
		fi
		if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
			echo "${nvsw} (${ip}): target password already active; compliant (no change)"
			record NO_CHANGE "$ip" "target_password_already_active"
			return 0
		fi
		if [[ "$reachable" -eq 0 ]]; then
			echo "${nvsw} (${ip}): BMC not reachable (HTTPS/Redfish)" >&2
			record NOT_REACHABLE "$ip" "curl: no HTTP response (timeout/refused/SSL?)"
		else
			echo "${nvsw} (${ip}): authentication failed with all configured credential files" >&2
			record AUTH_FAILED "$ip" "GET ${ACCOUNT_PATH} did not return 2xx with factory or target password"
		fi
		return 0
	fi

	if [[ "${FORCE_PATCH:-0}" != "1" ]] && ! password_change_required "$tmp"; then
		echo "${nvsw} (${ip}): PasswordChangeRequired is false; leaving password unchanged (set FORCE_PATCH=1 to override)"
		record NO_CHANGE "$ip" "PasswordChangeRequired=false"
		return 0
	fi

	nvis_make_netrc "$ip" root "$working_pass_file" "$netrc"
	nvis_make_password_json "$NEW_PASSWORD_FILE" "$json_payload"
	patch_code=$(curl -k -sS -o /dev/null -w '%{http_code}' \
		--netrc-file "$netrc" \
		--connect-timeout 5 --max-time 10 \
		-H 'Content-Type: application/json' \
		-X PATCH \
		--data-binary @"$json_payload" \
		"$url" 2>/dev/null || true)

	if [[ "$patch_code" =~ ^2[0-9][0-9]$ ]]; then
		echo "${nvsw} (${ip}): password updated (authenticated with configured credential source)"
		record PASSWORD_UPDATED "$ip" "auth=configured_credential_source"
		return 0
	fi

	if [[ "$patch_code" == "000" ]]; then
		echo "${nvsw} (${ip}): PATCH failed — BMC not reachable" >&2
		record NOT_REACHABLE "$ip" "PATCH had no HTTP response"
		return 0
	fi

	echo "${nvsw} (${ip}): PATCH password failed (HTTP ${patch_code})" >&2
	record PATCH_FAILED "$ip" "HTTP ${patch_code}"
	return 0
}

# Fold hostnames with cluster nodeset(1) for compact summary lines.
print_folded_nodelist() {
	local indent=$1
	shift
	(($# == 0)) && return 0
	local folded
	if command -v nodeset >/dev/null 2>&1; then
		folded=$(printf '%s\n' "$@" | nodeset -f 2>/dev/null) || folded=''
		if [[ -n "$folded" ]]; then
			# nodeset -f may print one range per line; join for a single summary line.
			folded=$(printf '%s\n' "$folded" | paste -sd' ' -)
			printf '%s%s\n' "$indent" "$folded"
			return 0
		fi
	fi
	printf '%s%s\n' "$indent" "$*"
}

print_summary() {
	local resdir=$1
	# Empty () so arrays exist under "set -u" before any += (avoids "unbound variable").
	local -a updated=() no_change=() unreachable=() failed=()

	local f node st ip det
	shopt -s nullglob
	for f in "$resdir"/*; do
		[[ -f "$f" ]] || continue
		IFS=$'\t' read -r node st ip det <"$f" || continue
		case "$st" in
		PASSWORD_UPDATED) updated+=("$node") ;;
		NO_CHANGE) no_change+=("$node") ;;
		NOT_REACHABLE) unreachable+=("$node") ;;
		*) failed+=("$node|$st|${ip:-}|${det:-}") ;;
		esac
	done
	shopt -u nullglob

	local n ok_total
	ok_total=$((${#updated[@]} + ${#no_change[@]}))
	local total
	total=$((${#updated[@]} + ${#no_change[@]} + ${#unreachable[@]} + ${#failed[@]}))

	echo ""
	echo "========== Summary =========="
	n=${#updated[@]}
	echo "Success — password updated: ${n}"
	if [[ "$n" -gt 0 ]]; then
		print_folded_nodelist '  ' "${updated[@]}"
	fi
	n=${#no_change[@]}
	echo "Success — no change required (already compliant): ${n}"
	if [[ "$n" -gt 0 ]]; then
		print_folded_nodelist '  ' "${no_change[@]}"
	fi
	echo "Success total: ${ok_total} / ${total}"

	n=${#unreachable[@]}
	echo "Not reachable: ${n}"
	if [[ "$n" -gt 0 ]]; then
		print_folded_nodelist '  ' "${unreachable[@]}"
	fi

	n=${#failed[@]}
	echo "Failed (not success): ${n}"
	if [[ "$n" -gt 0 ]]; then
		local entry
		for entry in "${failed[@]}"; do
			echo "  ${entry//|/ — }"
		done
	fi
	echo "=============================="
}

[[ $# -ge 1 ]] || usage

nvis_secret_file NVSWITCH_BMC_TARGET_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-bmc-target.password
NEW_PASSWORD_FILE=$REPLY
nvis_secret_file NVSWITCH_BMC_PRIMARY_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-bmc-primary.password
PASS_OPENBMC_FILE=$REPLY
nvis_secret_file NVSWITCH_BMC_SECONDARY_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-bmc-secondary.password
PASS_TEMP_FILE=$REPLY

resdir=$(mktemp -d)
trap 'rm -rf "$resdir"' EXIT

for nvsw in $(nodeset -e "$1"); do
	outfile=$(mktemp -p "$resdir" "nvsw.XXXXXX")
	set_bmc_password_for_node "$nvsw" "$outfile" &
done

wait
print_summary "$resdir"
