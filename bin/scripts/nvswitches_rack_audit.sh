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
# Per-rack NVSwitch audit: BMC Redfish password compliance, NMX-C on first
# switch (-nvsw-01), and UPHY serdes recovery (phy-recovery) state.
#
# Default racks: every nodeset(1) group matching @p<pod>-r<rack> (see nodeset -l).
# Pass rack IDs as arguments (with or without @), or a nodeset that includes
# -nvsw- (rack is inferred from the first host).
#
# Default mode is check-only. Use --fix / --fix-bmc / --fix-nmx / --fix-uphy to
# apply changes (delegates to the existing helper scripts in this directory).
#
# Output: tab-separated columns — rack, bmc, nmx_c, uphy_recovery (green ✔ / red ✘
# by default; use --no-color for plain OK/FAIL).
#
# Usage:
#   nvswitches_rack_audit.sh [--no-header] [--no-color] [--verbose] [--fix | --fix-bmc] [--fix-nmx] [--fix-uphy] [rack ...]

set -uo pipefail

# BCM installs cmsh at an absolute path and does NOT put it on PATH (Nscale TX: BCM 11 keeps
# it at /cm/local/apps/cmd/bin/cmsh). RACK_CMSH is the same knob the backend uses.
CMSH=${RACK_CMSH:-cmsh}
# Never enable errexit in this script: functions must not leave `set -e` on, or a
# single failed check could abort the whole multi-rack loop before all racks run.
set +e

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=bin/scripts/lib/nvswitch_credentials.sh
source "${HERE}/lib/nvswitch_credentials.sh"

ACCOUNT_PATH='/redfish/v1/AccountService/Accounts/root'

PRINT_HEADER=1
USE_COLOR=1
VERBOSE=0
FIX_BMC=0
FIX_NMX=0
FIX_UPHY=0

usage() {
	cat >&2 <<EOF
Usage: ${0##*/} [options] [rack_id ...]

Options:
  --no-header          Do not print the header row
  --no-color           Plain OK/FAIL instead of colored checkmarks
  --verbose            Per-switch BMC errors on stderr as they occur
  --fix                Apply BMC password, NMX-C, and UPHY recovery fixes, then re-check
  --fix-bmc            Run ${HERE}/nvswitches_set_bmc_passwd.sh for each rack's NV switches
  --fix-nmx            Run ${HERE}/nvswitches_configure_nmx_c.sh on *-nvsw-01 (slow; may reboot apps)
  --fix-uphy           Run ${HERE}/nvswitches_enable_uphy_recovery.sh then re-check

With no rack arguments, rack IDs are taken from: nodeset -l
  (only names matching ^@p[0-9]+-r[0-9]+\$ after stripping @).

Environment (optional):
  FORCE_PATCH=1       Passed to nvswitches_set_bmc_passwd.sh when fixing BMC
  NVSWITCHES_UPHY_CHECK_PARALLEL   UPHY check runs 144 \"nv show\" per switch; default 24
                      parallel jobs (GNU xargs -P). Set to 1 for fully sequential (slowest).
EOF
	exit 1
}

while [[ $# -gt 0 ]]; do
	case "$1" in
	-h | --help) usage ;;
	--no-header) PRINT_HEADER=0; shift ;;
	--no-color) USE_COLOR=0; shift ;;
	--verbose) VERBOSE=1; shift ;;
	--fix) FIX_BMC=1 FIX_NMX=1 FIX_UPHY=1; shift ;;
	--fix-bmc) FIX_BMC=1; shift ;;
	--fix-nmx) FIX_NMX=1; shift ;;
	--fix-uphy) FIX_UPHY=1; shift ;;
	--) shift; break ;;
	-*) echo "Unknown option: $1" >&2; usage ;;
	*) break ;;
	esac
done

export VERBOSE

nvis_secret_file NVSWITCH_BMC_TARGET_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-bmc-target.password
NEW_PASSWORD_FILE=$REPLY
nvis_secret_file NVSWITCH_SSH_PASSWORD_FILE /etc/nvis-rack-status/secrets/nvswitch-ssh.password
SSH_PASSWORD_FILE=$REPLY

password_change_required_true() {
	local json_file=$1
	if command -v jq >/dev/null 2>&1; then
		case $(jq -r 'if has("PasswordChangeRequired") then .PasswordChangeRequired|tostring else "absent" end' "$json_file") in
		true) return 0 ;;
		false | absent) return 1 ;;
		*) return 1 ;;
		esac
	fi
	if grep -qE '"PasswordChangeRequired"[[:space:]]*:[[:space:]]*true' "$json_file"; then
		return 0
	fi
	return 1
}

status_ok() {
	if [[ "$USE_COLOR" -eq 1 ]]; then
		printf '\033[32m\u2714\033[0m'
	else
		printf 'OK'
	fi
}

status_fail() {
	if [[ "$USE_COLOR" -eq 1 ]]; then
		printf '\033[31m\u2718\033[0m'
	else
		printf 'FAIL'
	fi
}

# Writes a single line: OK or FAIL to outf; optional BMC_FAIL_LOG gets "host<TAB>detail" on failure.
check_bmc_one() {
	local nvsw=$1
	local outf=$2
	local ip
	local tmp netrc
	local http_code
	local detail

	if ! ip=$("$CMSH" -c "device ; use $nvsw ; interfaces; get rf0 ip" 2>/dev/null) || [[ -z "${ip// }" ]]; then
		echo FAIL >"$outf"
		detail="no_rf0_ip"
		[[ -n "${BMC_FAIL_LOG:-}" ]] && printf '%s\t%s\n' "$nvsw" "$detail" >>"$BMC_FAIL_LOG"
		[[ "${VERBOSE:-0}" -eq 1 ]] && echo "${nvsw}: BMC check: no rf0 IP from cmsh" >&2
		return
	fi
	ip=$(printf '%s' "$ip" | tr -d '\r\n')
	tmp=$(mktemp)
	netrc=$(mktemp)
	chmod 0600 "$tmp" "$netrc"
	trap 'rm -f "$tmp" "$netrc"' RETURN
	nvis_make_netrc "$ip" root "$NEW_PASSWORD_FILE" "$netrc"
	local url="https://${ip}${ACCOUNT_PATH}"
	http_code=$(curl -k -sS -o "$tmp" -w '%{http_code}' \
		--netrc-file "$netrc" \
		--connect-timeout 5 --max-time 10 \
		-H 'Content-Type: application/json' \
		"$url" 2>/dev/null)
	if [[ ! "$http_code" =~ ^2[0-9][0-9]$ ]]; then
		echo FAIL >"$outf"
		detail="GET_HTTP_${http_code:-000}"
		[[ -n "${BMC_FAIL_LOG:-}" ]] && printf '%s\t%s\n' "$nvsw" "$detail" >>"$BMC_FAIL_LOG"
		[[ "${VERBOSE:-0}" -eq 1 ]] && echo "${nvsw}: BMC check: target password GET failed (HTTP ${http_code:-000})" >&2
		return
	fi
	if password_change_required_true "$tmp"; then
		echo FAIL >"$outf"
		detail="PasswordChangeRequired_true"
		[[ -n "${BMC_FAIL_LOG:-}" ]] && printf '%s\t%s\n' "$nvsw" "$detail" >>"$BMC_FAIL_LOG"
		[[ "${VERBOSE:-0}" -eq 1 ]] && echo "${nvsw}: BMC check: PasswordChangeRequired is true" >&2
		return
	fi
	echo OK >"$outf"
}

check_bmc_rack() {
	local folded=$1
	local rack_label=$2
	local dir f nvsw st
	local bad=0
	local fail_log
	local nhosts
	nhosts=$(nodeset -e "$folded" 2>/dev/null | wc -w)
	if ((nhosts == 0)); then
		status_fail
		echo
		echo "${rack_label}: BMC check: empty nodeset '${folded}'" >&2
		return 1
	fi
	dir=$(mktemp -d)
	fail_log="$dir/bmc_failed.tsv"
	: >"$fail_log"
	export BMC_FAIL_LOG="$fail_log"
	for nvsw in $(nodeset -e "$folded"); do
		f=$(mktemp -p "$dir" "nvsw.XXXXXX")
		check_bmc_one "$nvsw" "$f" &
	done
	wait
	unset BMC_FAIL_LOG
	shopt -s nullglob
	for f in "$dir"/nvsw.*; do
		[[ -f "$f" ]] || continue
		read -r st <"$f" || st=FAIL
		[[ "$st" == OK ]] || bad=$((bad + 1))
	done
	shopt -u nullglob
	if [[ "$bad" -gt 0 && "${VERBOSE:-0}" -ne 1 && -s "$fail_log" ]]; then
		local summary
		summary=$(awk -F'\t' '{printf "%s(%s) ", $1, $2}' "$fail_log" | sed 's/[[:space:]]*$//')
		echo "${rack_label}: BMC failed ${bad} switch(es): ${summary}" >&2
	fi
	rm -rf "$dir"
	if [[ "$bad" -eq 0 ]]; then
		status_ok
		echo
		return 0
	fi
	status_fail
	echo
	return 1
}

check_nmx_c_host() {
	local host=$1
	if ! command -v sshpass >/dev/null 2>&1; then
		status_fail
		echo
		echo "sshpass not in PATH" >&2
		return 1
	fi
	sshpass -f "$SSH_PASSWORD_FILE" ssh -o BatchMode=no -o StrictHostKeyChecking=yes -o ConnectTimeout=15 \
		"admin@${host}" "nv show cluster apps nmx-controller | grep -F 'status            ok'" \
		>/dev/null 2>&1
	local ec=$?
	if [[ "$ec" -eq 0 ]]; then
		status_ok
		echo
		return 0
	fi
	status_fail
	echo
	[[ "${VERBOSE:-0}" -eq 1 ]] && echo "${host}: NMX-C check failed (ssh or nv show / grep)" >&2
	return 1
}

# NMX-C must be checked only on switch index 1 (…-nvsw-01 or …-nvsw-1), never on -02..-09.
# Returns empty if that host is not part of this rack's nodeset.
nmx_controller_switch() {
	local rack=$1 folded=$2
	local h want want_alt idx

	want="${rack}-nvsw-01"
	want_alt="${rack}-nvsw-1"
	for h in $(nodeset -e "$folded"); do
		if [[ "$h" == "$want" || "$h" == "$want_alt" ]]; then
			printf '%s\n' "$h"
			return 0
		fi
	done
	for h in $(nodeset -e "$folded"); do
		[[ "$h" == "${rack}-nvsw-"* ]] || continue
		idx=${h#"${rack}-nvsw-"}
		[[ "$idx" =~ ^[0-9]+$ ]] || continue
		if ((10#$idx == 1)); then
			printf '%s\n' "$h"
			return 0
		fi
	done
	return 1
}

# UPHY: same remote idea as nvswitches_check_uphy_recovery.sh. Fail only when
# serdes-eq-mode is disabled in operational (1st value) or applied (2nd value).
# A third pending column is ignored (two-column nv show is OK). ACP 1..144
# matches nvswitches_enable_uphy_recovery.sh (acp1-144).
check_uphy_rack() {
	local folded=$1
	local out
	local par
	par="${NVSWITCHES_UPHY_CHECK_PARALLEL:-24}"
	[[ "$par" =~ ^[0-9]+$ ]] || par=24
	if ((par < 1)); then par=1; fi
	if ! command -v sshpass >/dev/null 2>&1 || ! command -v pdsh >/dev/null 2>&1; then
		status_fail
		echo
		echo "pdsh and/or sshpass not in PATH" >&2
		return 1
	fi
	# Was: seq|xargs -I{} with default -n1 → 144 sequential nv show per switch (very slow).
	# GNU xargs -P runs many nv show in parallel on each switch (NVOS images ship GNU xargs).
	out=$(sshpass -f "$SSH_PASSWORD_FILE" pdsh -w "$folded" -R ssh \
		"seq 1 144 | xargs -n 1 -P ${par} -I{} nv show fae int acp{} link phy-recovery" 2>&1)
	local ec=$?
	if echo "$out" | grep -qiE ':[[:space:]]*serdes-eq-mode[[:space:]]+disabled\b|:[[:space:]]*serdes-eq-mode[[:space:]]+[^[:space:]]+[[:space:]]+disabled\b'; then
		status_fail
		echo
		[[ "${VERBOSE:-0}" -eq 1 && "$ec" -ne 0 ]] && echo "pdsh exited $ec" >&2
		return 1
	fi
	if [[ "$ec" -ne 0 ]]; then
		status_fail
		echo
		echo "UPHY check: pdsh failed (exit $ec)" >&2
		return 1
	fi
	status_ok
	echo
	return 0
}

normalize_rack_arg() {
	local a=$1
	a=${a#@}
	if [[ "$a" == *"-nvsw"* ]]; then
		a=${a%%-nvsw-*}
	fi
	printf '%s\n' "$a"
}

nvsw_folded_for_rack() {
	local rack=$1
	local n
	n=$(nodeset -e "@${rack}-nvsw" 2>/dev/null | wc -w)
	if ((n > 0)); then
		nodeset -f "@${rack}-nvsw"
		return
	fi
	n=$(nodeset -e "${rack}-nvsw-[01-09]" 2>/dev/null | wc -w)
	if ((n > 0)); then
		nodeset -f "${rack}-nvsw-[01-09]"
		return
	fi
	echo "${rack}-nvsw-[01-09]"
}

default_racks_from_nodeset_l() {
	nodeset -l | sed 's/^@//' | grep -E '^p[0-9]+-r[0-9]+$' | LC_ALL=C sort -V
}

# Expand a spec (rack id or site group like su7) into physical rack rows: rack_id<TAB>folded_nvsw
expand_spec_to_rack_lines() {
	local spec=$1
	local folded hosts h rid
	local -A byrack

	folded=$(nvsw_folded_for_rack "$spec")
	hosts=$(nodeset -e "$folded" 2>/dev/null | tr -s ' ' ' ' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
	local wc=0
	[[ -n "$hosts" ]] && wc=$(echo "$hosts" | wc -w)

	if ((wc == 0)); then
		if hosts=$(nodeset -e "@$spec" 2>/dev/null); then
			hosts=$(echo "$hosts" | tr ' ' '\n' | grep -E -- '-nvsw-[0-9]+$' | tr '\n' ' ')
			[[ -n "$hosts" ]] && wc=$(echo "$hosts" | wc -w)
		fi
	fi

	if ((wc == 0)); then
		echo "No NV switch hosts resolved for spec '${spec}' (tried @${spec}-nvsw / ${spec}-nvsw-[01-09] / @$spec)." >&2
		return
	fi

	for h in $hosts; do
		if [[ "$h" =~ ^(.+)-nvsw-[0-9]+$ ]]; then
			rid="${BASH_REMATCH[1]}"
			# Append to list (was wrongly "${byrack[$rid]:+$h }$h", which dropped prior hosts).
			byrack[$rid]="${byrack[$rid]:+${byrack[$rid]} }$h"
		fi
	done

	if [[ ${#byrack[@]} -eq 0 ]]; then
		folded=$(echo "$hosts" | tr ' ' '\n' | nodeset -f 2>/dev/null || true)
		[[ -z "$folded" ]] && folded=$hosts
		printf '%s\t%s\n' "$spec" "$folded"
		return
	fi

	for rid in $(printf '%s\n' "${!byrack[@]}" | LC_ALL=C sort -V); do
		folded=$(printf '%s\n' ${byrack[$rid]} | nodeset -f 2>/dev/null || echo "${byrack[$rid]}")
		printf '%s\t%s\n' "$rid" "$folded"
	done
}

merge_rack_folded_into() {
	local rack_id=$1
	local folded_new=$2
	if [[ -z "${RACK_TO_FOLDED[$rack_id]:-}" ]]; then
		RACK_TO_FOLDED[$rack_id]=$folded_new
		return
	fi
	RACK_TO_FOLDED[$rack_id]=$(nodeset -f "${RACK_TO_FOLDED[$rack_id]},${folded_new}" 2>/dev/null || echo "${RACK_TO_FOLDED[$rack_id]},${folded_new}")
}

declare -A RACK_TO_FOLDED=()

if [[ $# -gt 0 ]]; then
	for a in "$@"; do
		spec=$(normalize_rack_arg "$a")
		while IFS=$'\t' read -r rid folded; do
			[[ -z "${rid:-}" ]] && continue
			merge_rack_folded_into "$rid" "$folded"
		done < <(expand_spec_to_rack_lines "$spec")
	done
else
	while IFS= read -r line; do
		[[ -z "$line" ]] && continue
		while IFS=$'\t' read -r rid folded; do
			[[ -z "${rid:-}" ]] && continue
			merge_rack_folded_into "$rid" "$folded"
		done < <(expand_spec_to_rack_lines "$line")
	done < <(default_racks_from_nodeset_l)
fi

if [[ ${#RACK_TO_FOLDED[@]} -eq 0 ]]; then
	echo "No racks to process (empty nodeset -l filter, no arguments, or no resolvable NV switches)." >&2
	exit 1
fi

if [[ "$PRINT_HEADER" -eq 1 ]]; then
	printf 'rack\tbmc\tnmx_c\tuphy_recovery\n'
fi

any_fail=0
for rack in $(printf '%s\n' "${!RACK_TO_FOLDED[@]}" | LC_ALL=C sort -V); do
	folded=${RACK_TO_FOLDED[$rack]}
	nhosts=$(nodeset -e "$folded" 2>/dev/null | wc -w)
	if ((nhosts == 0)); then
		echo "${rack}: no NV switch hosts resolved for nodeset '${folded}'" >&2
		printf '%s\t' "$rack"
		status_fail
		printf '\t'
		status_fail
		printf '\t'
		status_fail
		printf '\n'
		any_fail=1
		continue
	fi

	nmx_sw=$(nmx_controller_switch "$rack" "$folded") || true

	if [[ "$FIX_BMC" -eq 1 ]]; then
		( cd "$HERE" && FORCE_PATCH="${FORCE_PATCH:-}" bash ./nvswitches_set_bmc_passwd.sh "$folded" ) >/dev/null || true
	fi
	if [[ "$FIX_NMX" -eq 1 && -n "${nmx_sw:-}" ]]; then
		( cd "$HERE" && bash ./nvswitches_configure_nmx_c.sh "$nmx_sw" ) >/dev/null || true
	fi
	if [[ "$FIX_UPHY" -eq 1 ]]; then
		( cd "$HERE" && bash ./nvswitches_enable_uphy_recovery.sh "$folded" ) >/dev/null || true
	fi

	# Run checks in the current shell (not $(...)) so function exit codes are visible.
	cell_tmp=$(mktemp)
	check_bmc_rack "$folded" "$rack" >"$cell_tmp"
	bmc_rc=$?
	bmc=$(cat "$cell_tmp")
	if [[ -z "${nmx_sw:-}" ]]; then
		echo "${rack}: NMX-C skipped — no ${rack}-nvsw-01 (or ${rack}-nvsw-1) in nodeset '${folded}'" >&2
		{ status_fail; printf '\n'; } >"$cell_tmp"
		nmx_rc=1
		nmx=$(cat "$cell_tmp")
	else
		check_nmx_c_host "$nmx_sw" >"$cell_tmp"
		nmx_rc=$?
		nmx=$(cat "$cell_tmp")
	fi
	check_uphy_rack "$folded" >"$cell_tmp"
	uphy_rc=$?
	uphy=$(cat "$cell_tmp")
	rm -f "$cell_tmp"

	[[ "$bmc_rc" -eq 0 && "$nmx_rc" -eq 0 && "$uphy_rc" -eq 0 ]] || any_fail=1

	printf '%s\t%s\t%s\t%s\n' "$rack" "$bmc" "$nmx" "$uphy"
done

exit "$any_fail"
