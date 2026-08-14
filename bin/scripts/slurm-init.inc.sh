# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: LicenseRef-NvidiaProprietary
#
# NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
# property and proprietary rights in and to this material, related
# documentation and any modifications thereto. Any use, reproduction,
# disclosure or distribution of this material and related documentation
# without an express license agreement from NVIDIA CORPORATION or
# its affiliates is strictly prohibited.
# shellcheck shell=bash
# Make the Slurm CLI (sbatch/scontrol/sinfo/squeue) usable in NON-LOGIN shells.
#
# The dashboard runs these launchers over `ssh host '<command>'`, which is a
# non-interactive, non-login shell: the Environment Modules system is NOT
# initialized, so `module` is undefined and a bare `module load slurm` fails with
# "module: command not found" (then scontrol/sbatch are "command not found", exit 127).
#
# This include is idempotent and safe to source from any launcher. It:
#   1. Initializes Modules (lmod or tcl) if `module` isn't already a function.
#   2. Adds the BCM modulefile dirs to MODULEPATH (a bare init does not include them).
#   3. `module load slurm` (best effort).
#   4. Falls back to the BCM node-local slurm bin dir on PATH if sbatch is still missing.
#      Override the bin dir with RACK_SLURM_BIN.
#
# Robust under `set -u`: callers (e.g. run_llmb_nccl.sh) may run with nounset, but
# the BCM/Lmod module init files reference unbound vars (e.g. ENABLE_LMOD in
# /etc/sysconfig/modules/init.sh), which would abort the caller. Save the caller's
# nounset state, relax it for the duration of this include, and restore it at the end.
_rk_had_nounset=0
case "$-" in *u*) _rk_had_nounset=1 ;; esac
set +u

if ! command -v module >/dev/null 2>&1; then
  for _i in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
            /usr/share/Modules/init/bash \
            /cm/local/apps/environment-modules/current/init/bash; do
    # shellcheck disable=SC1090
    [ -r "$_i" ] && { . "$_i"; break; }
  done
fi

for _mp in /cm/local/modulefiles /cm/shared/modulefiles; do
  [ -d "$_mp" ] || continue
  case ":${MODULEPATH:-}:" in
    *":$_mp:"*) ;;
    *) export MODULEPATH="${MODULEPATH:+$MODULEPATH:}$_mp" ;;
  esac
done

command -v module >/dev/null 2>&1 && module load slurm 2>/dev/null || true

# An explicit site pin wins even when an older Slurm happens to be on PATH. This
# keeps launcher scripts on the same client version as the Python platform layer.
if [ -n "${RACK_SLURM_BIN:-}" ] && [ -x "${RACK_SLURM_BIN}/sbatch" ]; then
  export PATH="${RACK_SLURM_BIN}:$PATH"
elif ! command -v sbatch >/dev/null 2>&1; then
  for _d in "${RACK_SLURM_BIN:-}" /cm/local/apps/slurm/current/bin /cm/local/apps/slurm/*/bin; do
    [ -n "$_d" ] && [ -x "$_d/sbatch" ] && { export PATH="$_d:$PATH"; break; }
  done
fi

# Restore the caller's nounset setting (see top of file).
[ "${_rk_had_nounset:-0}" = 1 ] && set -u
unset _rk_had_nounset

# Slurm needs a config source. `module load slurm` sets SLURM_CONF on the head node; on
# hosts without the slurm modulefile (e.g. the slogin/management node we run privileged
# tools from) it stays unset and sbatch/sinfo die with "Could not establish a
# configuration source". Fall back to the shared cluster slurm.conf. Override with
# RACK_SLURM_CONF.
if [ -z "${SLURM_CONF:-}" ]; then
  for _c in "${RACK_SLURM_CONF:-}" /etc/slurm/slurm.conf \
            /cm/shared/apps/slurm/etc/slurm/slurm.conf \
            /cm/shared/apps/slurm/var/etc/slurm.conf \
            /cm/shared/apps/slurm/var/etc/*/slurm.conf; do
    [ -n "$_c" ] && [ -r "$_c" ] && { export SLURM_CONF="$_c"; break; }
  done
fi

unset _i _mp _d _c 2>/dev/null || true
