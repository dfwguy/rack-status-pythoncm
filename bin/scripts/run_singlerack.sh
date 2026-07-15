#!/bin/bash
# Slurm CLI on PATH (non-login SSH shells don't init Modules — see slurm-init.inc.sh).
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/slurm-init.inc.sh"
# Force ssh transport: this cluster's pdsh defaults to rsh (refused on compute nodes).
export PDSH_SSH_ARGS_APPEND="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=quiet -o CheckHostIP=no"
export PDSH_RCMD_TYPE=ssh
#### ROW J
# done:
# p17-r01-ct[01-18] - completed
# p17-r02-ct[01-18] - completed
# p17-r03-ct[01-18] - completed
# p17-r04-ct[01-18] - completed
# p18-r05-ct[01-18] - completed
# p18-r06-ct[01-18] - completed
# p18-r08-ct[01-18] - completed
# p19-r16-ct[01-18] - completed
# p19-r17-ct[01-18] - completed
# p19-r18-ct[01-18] - completed
# p20-r19-ct[01-18] - completed
# p20-r20-ct[01-18] - completed
# p20-r21-ct[01-18] - completed
# p20-r22-ct[01-18] - completed
##
# next:
# p19-r15-ct[01-18] - p19-r15-ct06 - installer stuck - reboot again aux cycle next - ongoing - @walid on it
# p18-r07-ct[01-18] - after the aux cycle the gpu on ct07 is back - dcgmi diag -r 4 PASS - retrying after nvlink cycle - failed after nccl - ct07 reseated - trying again - crashed ask replacement for ct07
                      # i still see problem here on ct08 (previously 07) with link 12 with high ber - after cc replacement we still see this problem
                      # waiting for ct07 raplacement - st07 replaced and ct replaced. Walid started validation

# test on going:
# p19-r15-ct[01-18]


#### ROW K
# done:
# p21-r02-ct[01-18] # maybe low nccl but nemo is ok? - completed
# p21-r03-ct[01-18] - completed
# p22-r08-ct[01-18] - completed
# p23-r15-ct[01-18] - completed
# p23-r16-ct[01-18] - completed
# p23-r18-ct[01-18] # nccl to be checked - completed
# p24-r20-ct[01-18] - completed
# p24-r21-ct[01-18] - completed
##
# next:
# p21-r01-ct[01-18] # nccl got stuck - nvlink cycle - ct01 cable issue?
# p21-r04-ct[01-18] # p21-r04-ct04 possible misscabling - check now @gio - testing
# p22-r05-ct[01-18] # ct10 with ECC errors - need replacement 

# p24-r19-ct[01-18] # nvsw04 is down/not reachable
# p24-r22-ct[01-18] # p24-r22-ct13 one bf3 not connected

# test on going 
# p21-r04-ct[01-18] # p21-r04-ct04 possible misscabling - check now @gio - testing - nccl stack - nvlink cycle
# p23-r17-ct[01-18] # p23-r17-ct15 - need fw upgrade - testing
# p22-r06-ct[01-18] # p22-r06-ct03 - possible misscabling pxe not starting - testing

#### ROW L

# next:
# p25-r01-ct[01-18] - nvsw01 is missing
# p25-r02-ct[01-18] - nvlink cycle



a=(
  p32-r22-ct[01-18]
)

[[ $# -gt 0 ]] && { a=( $1 ); } 

export single_rack=1

# Same directory as parser.sh / rack_status.sh (Flask cwd for validation is log_dir, not here).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

## Per rack test
for r in ${a[*]}
do
 # Rack id for the log basename (…-NEMO-1Rack-<rack>-…): prefer the id the app passes as $2
 # (works for any naming convention, e.g. Boston bos01-b05); fall back to the Bright p<N>-r<N> regex.
 rack="${2:-}"
 [[ -z "$rack" ]] && rack=$(echo "$r" | sed -E 's/(p[0-9]+-r[0-9]+)-ct.*/\1/')
 hosts=($(scontrol show hostname $r))
 : "${NEMO_SQSH_SRC:?set NEMO_SQSH_SRC to the head-node path of the nemo .sqsh image to copy under /raid/data/ on compute nodes}"
 _sqsh_b=$(basename "$NEMO_SQSH_SRC")
 _wl=$(echo "${hosts[*]}" | tr ' ' ',')
 # /raid is root-owned on the compute trays, so the staging runs as root ON EACH COMPUTE NODE.
 # If RACK_SUDO_PASSWORD is set, elevate with sudo -S (cmods password, kept OUT of the repo — set via
 # env or the server secret store). Otherwise use sudo -n (works when passwordless sudo is baked into
 # the compute IMAGE by AIC, and also when the script already runs as root). NOTE: a passwordless
 # visudo edit only on the slogin does NOT help here — this sudo runs on the compute trays.
 if [[ -n "${RACK_SUDO_PASSWORD:-}" ]]; then
   _sudo="printf '%s\n' $(printf %q "$RACK_SUDO_PASSWORD") | sudo -S -p '' "
 else
   _sudo="sudo -n "
 fi
 pdsh -R ssh -w "$_wl" "${_sudo}mkdir -p /raid/data && ${_sudo}rsync -ah $NEMO_SQSH_SRC /raid/data/$_sqsh_b"

 n=$[${#hosts[*]}/2*2]

sbatch \
 -N ${n} \
 -w $(echo "${hosts[*]}"|tr ' ' ',') \
 -t 03:00:00 \
 --job-name=NEMO-1R \
 --output=${USER}-NEMO-1Rack-${rack}-${n}N-%j.txt \
 --export=single_rack,NVIS_NCCL_DEBUG=${NVIS_NCCL_DEBUG:-} \
 "$_SCRIPT_DIR/nemo-nemotron5h_56b-mbridge-fp8.sh"

done
