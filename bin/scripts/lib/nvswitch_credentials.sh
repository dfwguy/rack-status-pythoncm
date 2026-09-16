#!/usr/bin/env bash
# Shared fail-closed credential handling for NVSwitch maintenance scripts.
# Credential values must never be supplied directly in the environment or argv.

if [[ -n "${_NVIS_NVSWITCH_CREDENTIALS_SH_LOADED:-}" ]]; then
	return 0
fi
readonly _NVIS_NVSWITCH_CREDENTIALS_SH_LOADED=1

nvis_secret_file() {
	local variable_name=$1
	local default_path=$2
	local candidate=${!variable_name:-$default_path}
	local mode owner current_uid first_line

	if [[ -z "$candidate" || "$candidate" != /* ]]; then
		echo "${variable_name} must name an absolute credential-file path" >&2
		return 78
	fi
	if [[ -L "$candidate" || ! -f "$candidate" ]]; then
		echo "${variable_name} must name a regular, non-symlink credential file" >&2
		return 78
	fi
	if [[ ! -r "$candidate" ]]; then
		echo "${variable_name} is not readable by the invoking identity" >&2
		return 78
	fi
	mode=$(stat -Lc '%a' -- "$candidate" 2>/dev/null) || {
		echo "Cannot inspect credential file named by ${variable_name}" >&2
		return 78
	}
	case "$mode" in
	400 | 600) ;;
	*)
		echo "Credential file named by ${variable_name} must have mode 0400 or 0600 (found ${mode})" >&2
		return 78
		;;
	esac
	owner=$(stat -Lc '%u' -- "$candidate" 2>/dev/null) || return 78
	current_uid=$(id -u)
	if [[ "$owner" != "$current_uid" && "$owner" != 0 ]]; then
		echo "Credential file named by ${variable_name} must be owned by the invoking user or root" >&2
		return 78
	fi
	if [[ ! -s "$candidate" ]]; then
		echo "Credential file named by ${variable_name} is empty" >&2
		return 78
	fi
	if ! awk 'NR > 1 { extra=1 } END { exit (NR == 1 && !extra ? 0 : 1) }' "$candidate"; then
		echo "Credential file named by ${variable_name} must contain exactly one line" >&2
		return 78
	fi
	IFS= read -r first_line <"$candidate" || [[ -n "$first_line" ]] || {
		echo "Credential file named by ${variable_name} contains an empty credential" >&2
		return 78
	}
	[[ -n "$first_line" ]] || {
		echo "Credential file named by ${variable_name} contains an empty credential" >&2
		return 78
	}
	unset first_line

	REPLY=$candidate
}

nvis_make_netrc() {
	local host=$1
	local username=$2
	local credential_file=$3
	local destination=$4
	local value

	IFS= read -r value <"$credential_file" || [[ -n "$value" ]] || return 78
	value=${value//\\/\\\\}
	value=${value//\"/\\\"}
	umask 077
	printf 'machine %s login "%s" password "%s"\n' "$host" "$username" "$value" >"$destination"
	chmod 0600 "$destination"
	unset value
}

nvis_make_password_json() {
	local credential_file=$1
	local destination=$2
	local value

	IFS= read -r value <"$credential_file" || [[ -n "$value" ]] || return 78
	value=${value//\\/\\\\}
	value=${value//\"/\\\"}
	value=${value//$'\t'/\\t}
	umask 077
	printf '{"Password":"%s"}\n' "$value" >"$destination"
	chmod 0600 "$destination"
	unset value
}
