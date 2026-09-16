# NVSwitch credential-file contract

The NVSwitch audit and remediation scripts never accept credential values on the command line or
in environment variables. They accept only absolute paths to protected, single-line credential
files. Missing, empty, symlinked, incorrectly owned, or broadly readable files stop execution
before a switch or BMC is contacted.

## Required files

The default paths are:

| Purpose | Path-variable override | Default file |
|---|---|---|
| Desired BMC root credential | `NVSWITCH_BMC_TARGET_PASSWORD_FILE` | `/etc/nvis-rack-status/secrets/nvswitch-bmc-target.password` |
| Primary BMC onboarding credential | `NVSWITCH_BMC_PRIMARY_PASSWORD_FILE` | `/etc/nvis-rack-status/secrets/nvswitch-bmc-primary.password` |
| Secondary BMC onboarding credential | `NVSWITCH_BMC_SECONDARY_PASSWORD_FILE` | `/etc/nvis-rack-status/secrets/nvswitch-bmc-secondary.password` |
| NVSwitch `admin` SSH credential | `NVSWITCH_SSH_PASSWORD_FILE` | `/etc/nvis-rack-status/secrets/nvswitch-ssh.password` |

Each override is a file path, never the credential itself. `nvswitches_set_bmc_passwd.sh` requires
the three BMC files. `nvswitches_rack_audit.sh` requires the desired BMC file and SSH file.
`nvswitches_configure_nmx_c.sh` and `nvswitches_enable_uphy_recovery.sh` require the SSH file.

## Provisioning requirements

- Provision credentials outside Git using the approved secret-management workflow.
- Use a regular file, not a symlink, FIFO, directory, or device.
- Set mode `0400` or `0600`.
- Set ownership to either the invoking service identity or root. A root-owned `0400` file is usable
  only when the script itself runs as root; do not add blanket sudo merely to read it.
- Store exactly one non-empty line. A final newline is allowed.
- Rotate every non-factory credential that was present in repository history before distributing
  this release. Sanitizing source does not revoke an exposed credential.

Example installation pattern, deliberately using a placeholder rather than a credential:

```bash
install -d -m 0700 /etc/nvis-rack-status/secrets
install -m 0600 /secure/staging/nvswitch-ssh.password \
  /etc/nvis-rack-status/secrets/nvswitch-ssh.password
```

Do not use `echo`, shell tracing, command-line password flags, or Compose environment values to
provision these files. The release scripts use `sshpass -f` and temporary mode-`0600` Redfish
authentication files so credential values do not appear in process arguments. Temporary files are
removed when each worker function returns.

## Release gate

Run the high-confidence, path-only scanner before packaging:

```bash
bin/check-release-secrets.py
bin/scripts/tests/test_nvswitch_credentials.sh
```

The scanner covers tracked and non-ignored current-tree text. A release bundle must additionally be
made from a new orphan commit, because the predecessor repository contains the retired values in
history. Never create a full-history bundle from the predecessor repository.
