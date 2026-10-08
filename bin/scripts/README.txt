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
  - Presets map to checker.sh flags (see backend/app.py _HW_CHECK_PRESETS). UI runs with RACK_WEB_HW_CHECK=1 on nodes so
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
    file. It is staged into each node's image cache, <NVIS_NODE_CONTAINER_DIR>/<basename> (the
    cluster's connection.node_container_dir; default /tmp/containers, /opt/containers on Nscale NC),
    skipped where the node already holds a same-size copy, and passed to the job as NVIS_NEMO_CONT.
  - HC_CHECKER_SCRIPT (optional): executable checker run on nodes after the job; if unset, skipped.
  - NEMORUN_HOME, network interface names, partition names, and model flags inside the nemo job
    script are site-specific — review the whole file on a new cluster.

parser.sh invokes rack_status.sh from the same directory as parser.sh (both live here).

Tools / container images (Dashboard “Tools”):
  05-download-containers.sh — enroot import for NEMO 25.11.01 .sqsh (and commented stubs for HPC benchmarks).
    Usage: NGC_API_KEY=nvapi-... 05-download-containers.sh <destination_dir> [enroot_arch]
    destination_dir must exist or will be created (mkdir -p). Default arch: aarch64.
    API: POST /api/tools/download-containers JSON { "destination": "/abs/path" } [, "arch": "aarch64" ]
    Stream: POST /api/tools/download-containers-stream (same body) → text/plain merged stdout/stderr until ``--- exit N ---``.
    If nemo-25.11.01.sqsh already exists in the destination and is non-empty, the API returns ok + skipped (no enroot, no NGC key).
    Runs on the dashboard host with Flask’s environment — set NGC_API_KEY before starting the app (not sent from browser).
    Env: RACK_TOOLS_DOWNLOAD_CONTAINERS_DISABLED, RACK_TOOLS_DOWNLOAD_CONTAINERS_TIMEOUT (sec, default 14400),
         RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES=1 to restrict destinations to RACK_STATUS_ALLOWED_PREFIXES (same as log_dir).

  launch-grafana-bright-prometheus.sh — Docker Grafana (--network=host) with provisioned Prometheus datasource
    (Bright metrics) and GPU thermal/power dashboard. See script header for PROMETHEUS_RC / ~/.prometheusrc (USER, PASSWORD).
    API: POST /api/tools/launch-grafana-bright-prometheus JSON {} (no body fields). Uses Docker on the dashboard host.
    Stream: POST /api/tools/launch-grafana-bright-prometheus-stream → text/plain live output.
    When Settings → External connections has both RACK_PROMETHEUS_BASIC_AUTH_USER and RACK_PROMETHEUS_BASIC_AUTH_PASSWORD,
    the server writes a temporary PROMETHEUS_RC for the script; if either is empty, the script resolves default rc paths.
    Env: RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_DISABLED, RACK_TOOLS_LAUNCH_GRAFANA_BRIGHT_PROMETHEUS_TIMEOUT (sec, default 7200).

  workspace-cuda-major-common.sh — Source-only: TBZ/URL/UNPACK (HPC-X) and RUN_FILE/RUN_URL (CUDA .run) for CUDA_MAJ 12|13.
    Included by install-hpcx-workspace.sh and install-cuda-nccl-workspace.sh (same directory).

  install-hpcx-workspace.sh — Mellanox HPC-X only (aarch64, CUDA major 12|13): wget/curl tarball, extract, hpcx_load.
    API: POST /api/tools/install-hpcx-workspace JSON { "cuda_major": "12"|"13", "arch": "aarch64", "workspace": "/abs/path" }
    (workspace defaults to /home/cmsupport/workspace). Same path rules as download-containers when RACK_TOOLS_ENFORCE_ALLOWED_PREFIXES is set.
    Env: RACK_TOOLS_INSTALL_HPCX_WORKSPACE_DISABLED, RACK_TOOLS_INSTALL_HPCX_WORKSPACE_TIMEOUT (sec, default 28800).
    Stream: POST /api/tools/install-hpcx-workspace-stream (same JSON) → text/plain live output.

  01-download-cuda-nccl-workspace.sh — Phase 1 on the **dashboard host**: wget/curl NVIDIA CUDA .run into TOPDIR when
    NVIS_INSTALL_CUDA_TOOLKIT=1. Called by install-cuda-nccl-workspace.sh before pdsh.

  02-install-cuda-nccl-workspace.sh — Phase 2 on a **compute node** (shared TOPDIR): silent .run install + optional NCCL build
    (same logic as before split). Requires pdsh from the dashboard; HPC-X unpack + nvcc for NCCL. ``module load gcc`` before make.

  install-cuda-nccl-workspace.sh — Orchestrator: runs 01 locally, then ``pdsh -w <host> bash -lc '…02…'`` with NVIS_* exports.
    Env: NVIS_INSTALL_CUDA_TOOLKIT, NVIS_BUILD_NCCL, NVIS_CUDA_NCCL_BUILD_HOST (single hostname; set by API from JSON build_host).
    API: POST /api/tools/install-cuda-nccl-workspace JSON {
      "cuda_major": "12"|"13", "arch": "aarch64", "workspace": "/abs/path",
      "install_cuda_toolkit": true|false, "build_nccl": true|false,
      "build_host": "pNN-rMM-ctXX"
    } — at least one of install_cuda_toolkit / build_nccl must be true; build_host required (dashboard needs ``pdsh`` in PATH).
    GET /api/cluster-overview includes ``cuda_nccl_build_host_candidates`` (keys from rack status physical_rf0) for UI suggestions.
    Env: RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_DISABLED, RACK_TOOLS_INSTALL_CUDA_NCCL_WORKSPACE_TIMEOUT (sec, default 28800).
    Stream: POST /api/tools/install-cuda-nccl-workspace-stream (same JSON) → text/plain live output.

  nvloom (NVIDIA MNNVL tools): 00-download-cmake-nvloom-workspace.sh (Kitware CMake v3.28.1 linux-aarch64 tarball → TOPDIR; **dashboard localhost only**), 01-clone-nvloom-workspace.sh (git clone on localhost), 02-build-nvloom-workspace.sh (HPC-X + Boost hints; patches ``Boost_USE_STATIC_LIBS``; removes ``CMakeCache.txt`` / ``CMakeFiles/``; ``cmake -DBoost_NO_BOOST_CMAKE=ON`` + ``Boost_ROOT`` / ``Boost_LIBRARY_DIR``; ``cuda/env.sh``; ``CMAKE_CUDA_ARCHITECTURES=native``; aarch64 build host), install-nvloom-workspace.sh (00+01 localhost, then ``pdsh`` … 02).
    Env: NVIS_NVLOOM_BUILD_HOST (single hostname; API JSON build_host).
    API: POST /api/tools/install-nvloom-workspace JSON { "workspace": "/abs/path", "cuda_major": "12"|"13", "build_host": "host" }.
    Stream: POST /api/tools/install-nvloom-workspace-stream (same JSON).
    Env: RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_DISABLED, RACK_TOOLS_INSTALL_NVLOOM_WORKSPACE_TIMEOUT (sec, default 28800).

    Optional line-buffering for tool streams: RACK_TOOLS_SCRIPT_STREAM_LINEBUFFER (default 1, like other stream scripts).
    GET /api/tools/workspace-toolchain-status?workspace=/abs&cuda_major=12|13 — returns cuda_nvcc, hpcx_init, nccl_all_reduce_perf, all_ready (read-only; not blocked by install-disabled env).

MAC management (macswap/):
  Unified CLI for BCM cluster MAC address operations. Vendored from
  https://gitlab-master.nvidia.com/womari/macswap — re-sync by copying
  macswap, _macswap_common.py, swap_node_macs.py, change_node_macs.py
  from the upstream repo root.
  Modes:
    macswap NODE_A NODE_B [--dry-run]               # swap MACs between two nodes
    macswap NODE [--node-mac MAC] \                 # change one node's MACs;
            [--interface NAME=MAC ...] [--dry-run]  # auto-promotes to swap on
                                                    # single-node conflict.
  Runtime: requires BCM's bundled python3 (has pythoncm). Invoke through a
  login shell with `module load python3` so pythoncm is on sys.path.
  Dashboard: POST /api/mac-change-async JSON
    { "rack": "pNN-rMM", "node": "pNN-rMM-ctXX",
      "node_mac": "aa:bb:..."?, "interfaces": [{"name": "...", "mac": "..."}, ...],
      "dry_run": false }
  At least one of node_mac or a non-empty interfaces[] must be present.
  Reuses the _reset_jobs job table (poll /api/reset-job-status?job_id=...).
  Env: RACK_MACSWAP_SCRIPT (default SCRIPTS_DIR/macswap/macswap),
       RACK_MACSWAP_PYTHON_MODULE (default 'python3' -- the modulefile
       name to `module load`; switch to 'python312' etc. on clusters that
       expose pythoncm under a differently-named module),
       RACK_MAC_CHANGE_DISABLED, RACK_MAC_CHANGE_TIMEOUT (sec, default 600).

BMC recovery (bmc-recovery.sh):
  Cold-reset a node's BMC via ipmitool. Vendored from the operations
  toolbox -- re-sync from
  https://gitlab-master.nvidia.com/womari/scripts/tree/main/tools/bmc-recovery.
  Looks up the BMC MAC + credentials via cmsh, finds the latest DHCPOFFER
  for that MAC in syslog, then issues `ipmitool mc reset cold` against
  the IP.
  Usage: bmc-recovery.sh <node> [interface]   # interface defaults to rf0
  Requires: cmsh (always present on BCM head), ipmitool (must be installed
  separately on the head node: `apt-get install -y ipmitool`), read access
  to /var/log/syslog (override via SYSLOG_PRIMARY / SYSLOG_FALLBACK env).
  Dashboard: POST /api/bmc-recovery-async JSON
    { "rack": "pNN-rMM", "specs": ["pNN-rMM-ct[XX]", ...],
      "iface": "rf0"? }                       # default rf0
  Fans out across the selected node specs serially (one node per
  ipmitool call). Reuses the _reset_jobs job table.
  Env: RACK_BMC_RECOVERY_SCRIPT (default SCRIPTS_DIR/bmc-recovery.sh),
       RACK_BMC_RECOVERY_DISABLED, RACK_BMC_RECOVERY_TIMEOUT (sec, per-node
       default 120).
