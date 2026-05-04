Bundled operational scripts for NVIS Rack Status (defaults under APP_DIR/bin/scripts).

Python defaults (override with env):
  RACK_STATUS_SCRIPT      rack_status.sh
  RACK_STATUS_LOG_DIR     APP_DIR/var/logs
  RACK_PARSER_SCRIPT      parser.sh
  RACK_VALIDATION_SCRIPT  run_singlerack.sh
  RACK_SYSINFO_SCRIPT     sysinfo.sh
  RACK_GPU_RESET_SCRIPT   gpu_reset.sh
  RACK_HW_CHECK_SCRIPT    hw_check_pdsh.sh (runs bundled checker.sh on nodes via pdcp+pdsh)
  RACK_NVSWITCH_AUDIT_SCRIPT  nvswitches_rack_audit.sh (optional; default here)

NVSwitch bundle (same directory; used by nvswitches_rack_audit.sh --fix paths):
  nvswitches_rack_audit.sh  nvswitches_set_bmc_passwd.sh
  nvswitches_configure_nmx_c.sh  nvswitches_enable_uphy_recovery.sh
  Dashboard: POST /api/run-nvswitch-audit-stream JSON { log_dir, rack, fix?: bool }.
  Env: RACK_NVSWITCH_AUDIT_DISABLED, RACK_NVSWITCH_AUDIT_TIMEOUT (default 7200),
       RACK_NVSWITCH_AUDIT_LINEBUFFER (default 1, like RACK_SYSINFO_LINEBUFFER).
  UPHY speed: nvswitches_rack_audit.sh uses GNU xargs -P on each switch for the 144
       nv show calls; override NVSWITCHES_UPHY_CHECK_PARALLEL (default 24, use 1 for sequential).

Hardware checker (checker.sh + hw_check_pdsh.sh):
  - GET /api/hw-check-help — runs checker.sh --help on the dashboard host (RACK_WEB_PRINT_HELP=1, no root).
    Env: RACK_HW_CHECK_HELP_DISABLED, RACK_HW_CHECK_HELP_TIMEOUT (default 20).
  - Dashboard: POST /api/hw-check-async JSON { "specs": [...], "rack": "pNN-rMM", "preset": "…" } or include
    "checker_args": "-d -e 0" (string, shell-style quoting allowed) or "checker_args": ["-d","-e","0"] (JSON list).
    Non-empty checker_args overrides preset; tokens are validated (no shell metacharacters).
  - Presets map to checker.sh flags (see app.py _HW_CHECK_PRESETS). UI runs with RACK_WEB_HW_CHECK=1 on nodes so
    checker.sh never performs Slurm drain/undrain (even without -d).
  - Requires pdcp (copy checker.sh to targets) and pdsh as for gpu_reset.sh; nodes must run the script as root.
  - checker.sh expects: cpupower, dmidecode, lspci, nvidia-smi, nvidia-imex-ctl, getopt (util-linux), enroot paths,
    optional mst/mlxconfig/mlxlink, lldpcli, df, dmesg, module/slurm only when not using RACK_WEB_HW_CHECK.
  - Env: RACK_HW_CHECK_DISABLED, RACK_HW_CHECK_DRY_RUN, RACK_HW_CHECK_TIMEOUT (default 3600).

External programs the scripts expect:
  - Slurm: module load slurm; squeue, sinfo, scontrol, sbatch, srun (as used in each script)
  - pdsh, pdcp, dshbak (sysinfo.sh, gpu_reset.sh, hw_check_pdsh.sh, run_singlerack.sh, nemo job)
  - ssh (PDSH_SSH_ARGS in nemo script)
  - cmsh (optional paths in rack_status.sh when enabled)
  - python3 (parser.sh --json/--csv embedded snippets)
  - enroot, enroot list/remove (nemo job)
  - nodeset, curl, jq or grep (nvswitches_rack_audit.sh); sshpass, pdsh (NMX-C / UPHY checks)
  - Standard: bash, sed, awk, rsync, timeout, etc.

Cluster-specific before validation:
  - NEMO_SQSH_SRC (required by run_singlerack.sh): path on the head/login node to the nemo .sqsh
    file; rsync copies it to /raid/data/<basename> on nodes. Align basename with CONT and
    nemorun --container_image paths inside nemo-nemotron5h_56b-mbridge-fp8.sh or edit that script.
  - HC_CHECKER_SCRIPT (optional): executable checker run on nodes after the job; if unset, skipped.
  - NEMORUN_HOME, network interface names, partition names, and model flags inside the nemo job
    script are site-specific — review the whole file on a new cluster.

parser.sh invokes rack_status.sh from the same directory as parser.sh (both live here).
