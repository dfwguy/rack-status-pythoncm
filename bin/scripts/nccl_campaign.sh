#!/usr/bin/env bash
# nccl_campaign.sh — post-fix NCCL microbenchmark validation campaign (FIRMUS/MEL01).
#
# Runs single (72) -> double (144) -> quad (288) GPU tiers of
# `llmb-run microbenchmark_nccl` across the healthy rack pool, collecting the
# 16 GiB all-reduce bus bandwidth, the data-path transport, and timeout count
# into a results.tsv for a PM-ready summary.
#
# Tiers run sequentially; within a tier all groups are submitted as a wave and
# Slurm schedules them. Only racks with a FULL set of >=18 idle nodes are used
# (so it never grabs nodes someone else is actively running on). Held jobs from
# prolog/cgroup drift are detected and cancelled so a tier can't hang.
#
# Usage:  nohup ./nccl_campaign.sh > /dev/null 2>&1 &
# Output: /home/nvis/nvis_campaign/<ts>/{results.tsv,driver.log}
set -uo pipefail

export LLMB_INSTALL="${LLMB_INSTALL:-/home/nvis/dgxc-benchmarking}"
LLMB_INSTALL="${LLMB_INSTALL%/}"
VENV="${LLMB_VENV:-/home/nvis/llmb_venv}"
RUN="${LLMB_RUN:-${LLMB_INSTALL}/llmb-run}"
D="${LLMB_INSTALL}/workloads/microbenchmark_nccl"
NODES_PER_RACK="${NODES_PER_RACK:-18}"
GPUS_PER_NODE="${GPUS_PER_NODE:-4}"
POLL="${POLL:-30}"
TIER_TIMEOUT="${TIER_TIMEOUT:-$((90 * 60))}"

# Candidate rack pool (PM target list, excl HC07-RK0116 NVLink HW). The live
# FULL-idle filter decides what actually runs each tier.
CANDIDATES=(
  HC07-RK0103 HC07-RK0104 HC07-RK0105 HC07-RK0112 HC07-RK0115
  HC07-RK0208 HC07-RK0213 HC07-RK0215 HC07-RK0216
  HC08-RK0101 HC08-RK0102 HC08-RK0104 HC08-RK0107 HC08-RK0110
  HC08-RK0112 HC08-RK0203 HC08-RK0204 HC08-RK0209 HC08-RK0210
  HC08-RK0212 HC08-RK0213 HC08-RK0214
)

# --- Slurm + venv on PATH (non-login shell) ------------------------------------
if ! command -v sinfo >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  [ -f /etc/profile.d/modules.sh ] && source /etc/profile.d/modules.sh
  module load slurm >/dev/null 2>&1 || true
fi
command -v sinfo >/dev/null 2>&1 || { echo "FATAL: sinfo not on PATH"; exit 1; }
# shellcheck disable=SC1091
[ -f "${VENV}/bin/activate" ] && source "${VENV}/bin/activate"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="/home/nvis/nvis_campaign/${TS}"
mkdir -p "${OUT}"
RES="${OUT}/results.tsv"
LOG="${OUT}/driver.log"
printf 'tier\tscale\tracks\tjobid\tstate\tbusbw16G_GBs\ttransport\ttimeouts\n' > "${RES}"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "${LOG}"; }

# Echo candidate racks that currently have >=NODES_PER_RACK idle nodes.
full_idle_candidates() {
  local live
  live="$(sinfo -N -h -o '%t %N' 2>/dev/null | awk -v N="${NODES_PER_RACK}" '
    $1=="idle" && match($2,/HC[0-9]+-SVR-CN[0-9]{6}$/){
      hc=substr($2,index($2,"HC"),4); rk=substr($2,length($2)-5,4); c[hc"-RK"rk]++
    } END{for(k in c) if(c[k]>=N) print k}')"
  local rk
  for rk in "${CANDIDATES[@]}"; do
    grep -qx "${rk}" <<<"${live}" && echo "${rk}"
  done
}

# Echo a bracketed Slurm nodelist of the first NODES_PER_RACK idle nodes in a rack.
rack_nodelist() {
  local rk="$1" hc num pref
  hc="${rk%%-RK*}"; num="${rk#*-RK}"
  pref="MEL01-${hc}-SVR-CN${num}"
  local nodes csv
  mapfile -t nodes < <(sinfo -N -h -o '%t %N' 2>/dev/null \
    | awk -v P="${pref}" '$1=="idle" && $2 ~ ("^"P"[0-9][0-9]$"){print $2}' \
    | sort -u | head -"${NODES_PER_RACK}")
  [ "${#nodes[@]}" -ge "${NODES_PER_RACK}" ] || return 1
  csv="$(IFS=,; echo "${nodes[*]}")"
  scontrol show hostlist "${csv}" 2>/dev/null || echo "${csv}"
}

# Extract "busbw transport timeouts" for a finished jobid.
extract() {
  local j="$1" ed s bw tr to
  ed="$(ls -dt "${D}"/experiments/*_"${j}" 2>/dev/null | head -1)"
  if [ -z "${ed}" ]; then echo "- nodir 0"; return; fi
  s="$(ls -t "${ed}"/LOG_*/1_1_LOG_all_reduce*/job_step_stdout.txt 2>/dev/null | head -1)"
  bw="$(grep -hE '^[[:space:]]*17179869184' "${s}" 2>/dev/null | tail -1 | awk '{print $8}')"
  [ -z "${bw}" ] && bw="-"
  tr="$(grep -rhoE 'via NET/(SPCX|Socket|IB)' "${ed}"/LOG_*/*iter1/rank*/nccl_debug_file.*.txt 2>/dev/null \
    | sort -u | paste -sd, -)"
  [ -z "${tr}" ] && tr="NVLink/intra"
  to="$(grep -rhcE 'ibv_modify_qp failed|Connection timed out' "${ed}"/LOG_*/*iter1/rank*/nccl_debug_file.*.txt 2>/dev/null \
    | awk '{s+=$1} END{print s+0}')"
  echo "${bw} ${tr} ${to}"
}

run_tier() {
  local tier="$1" rpg="$2" scale="$3"
  log "================ TIER ${tier}  (racks/group=${rpg} scale=${scale}) ================"
  local racks=()
  mapfile -t racks < <(full_idle_candidates)
  log "FULL-idle candidates (${#racks[@]}): ${racks[*]:-none}"
  if [ "${#racks[@]}" -lt "${rpg}" ]; then
    log "not enough idle racks for tier ${tier}; skipping"
    return
  fi

  local -a JOBIDS=() LABELS=()
  local i=0
  while [ "${i}" -lt "${#racks[@]}" ]; do
    local grp=("${racks[@]:${i}:${rpg}}")
    i=$((i + rpg))
    [ "${#grp[@]}" -eq "${rpg}" ] || { log "leftover group (${grp[*]}) < ${rpg}; stop"; break; }

    local combined=() ok=1 nl
    for rk in "${grp[@]}"; do
      nl="$(rack_nodelist "${rk}")" || { ok=0; break; }
      combined+=("${nl}")
    done
    if [ "${ok}" -ne 1 ]; then log "group ${grp[*]} no longer full; skip"; continue; fi

    local csv full_nl label before after out jid
    csv="$(IFS=,; echo "${combined[*]}")"
    full_nl="$(scontrol show hostlist "${csv}" 2>/dev/null || echo "${csv}")"
    label="$(IFS=+; echo "${grp[*]}")"

    before="$(squeue -h -u "$(whoami)" -o '%i' 2>/dev/null | sort)"
    log "submit ${tier} ${label} scale=${scale} nodelist=${full_nl}"
    cd "${LLMB_INSTALL}" || { log "cannot cd ${LLMB_INSTALL}"; return; }
    out="$("${RUN}" submit -w microbenchmark_nccl -d fp8 --scale "${scale}" --nodelist "${full_nl}" 2>&1)"
    echo "${out}" >> "${LOG}"
    jid="$(grep -oE 'Submitted batch job [0-9]+' <<<"${out}" | grep -oE '[0-9]+' | tail -1)"
    if [ -z "${jid}" ]; then
      sleep 5
      after="$(squeue -h -u "$(whoami)" -o '%i' 2>/dev/null | sort)"
      jid="$(comm -13 <(echo "${before}") <(echo "${after}") | head -1)"
    fi
    if [ -z "${jid}" ]; then
      log "  NO JOBID for ${label} (submit failed)"
      printf '%s\t%s\t%s\t-\tSUBMIT_FAIL\t-\t-\t-\n' "${tier}" "${scale}" "${label}" >> "${RES}"
      continue
    fi
    log "  -> job ${jid}"
    JOBIDS+=("${jid}"); LABELS+=("${label}")
  done

  [ "${#JOBIDS[@]}" -gt 0 ] || { log "tier ${tier}: nothing submitted"; return; }

  # --- wait for the wave ---
  local -A DONE=()
  local deadline=$(( $(date +%s) + TIER_TIMEOUT ))
  while :; do
    local active=0 idx j st reason
    for idx in "${!JOBIDS[@]}"; do
      j="${JOBIDS[$idx]}"
      [ -n "${DONE[$j]:-}" ] && continue
      st="$(squeue -h -j "${j}" -o '%t' 2>/dev/null)"
      if [ -z "${st}" ]; then DONE[$j]="COMPLETED"; continue; fi
      reason="$(squeue -h -j "${j}" -o '%r' 2>/dev/null)"
      if grep -qiE 'launch_failed_requeued_held|JobHeldUser|JobHeldAdmin|BadConstraints' <<<"${reason}"; then
        log "  job ${j} held (${reason}) -> scancel"
        scancel "${j}" 2>/dev/null
        DONE[$j]="HELD_CANCELLED"
        continue
      fi
      active=$((active + 1))
    done
    [ "${active}" -eq 0 ] && break
    if [ "$(date +%s)" -ge "${deadline}" ]; then
      log "tier ${tier} TIMEOUT; cancelling stragglers"
      for idx in "${!JOBIDS[@]}"; do
        j="${JOBIDS[$idx]}"
        [ -z "${DONE[$j]:-}" ] && { scancel "${j}" 2>/dev/null; DONE[$j]="TIMEOUT"; }
      done
      break
    fi
    sleep "${POLL}"
  done

  # --- collect ---
  local idx j label state rest
  for idx in "${!JOBIDS[@]}"; do
    j="${JOBIDS[$idx]}"; label="${LABELS[$idx]}"; state="${DONE[$j]:-UNKNOWN}"
    rest="$(extract "${j}")"
    local bw tr to
    bw="$(awk '{print $1}' <<<"${rest}")"
    tr="$(awk '{print $2}' <<<"${rest}")"
    to="$(awk '{print $3}' <<<"${rest}")"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${tier}" "${scale}" "${label}" "${j}" "${state}" "${bw}" "${tr}" "${to}" >> "${RES}"
    log "RESULT ${tier} ${label} job=${j} state=${state} busbw=${bw} transport=${tr} timeouts=${to}"
  done
}

if [ "${1:-}" = "--check" ]; then
  echo "LLMB_INSTALL=${LLMB_INSTALL}  RUN=${RUN}"
  [ -x "${RUN}" ] || [ -f "${RUN}" ] && echo "llmb-run: present" || echo "llmb-run: MISSING"
  echo "FULL-idle candidates:"; full_idle_candidates | sed 's/^/  /'
  first="$(full_idle_candidates | head -1)"
  if [ -n "${first}" ]; then
    echo "sample nodelist for ${first}:"; rack_nodelist "${first}" | sed 's/^/  /'
  fi
  exit 0
fi

log "campaign start: LLMB_INSTALL=${LLMB_INSTALL} out=${OUT}"
run_tier single 1 72
run_tier double 2 144
run_tier quad   4 288
log "campaign done. results: ${RES}"
column -t -s $'\t' "${RES}" | tee -a "${LOG}"
