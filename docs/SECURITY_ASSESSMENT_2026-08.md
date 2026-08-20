# Security assessment — nvis-rack-status, August 2026

| | |
|---|---|
| **Repository** | `nvis/nvis-rack-status` |
| **Branch / commit assessed** | `refactor/api-module-migration` @ `6d68fd8` (also `main` @ `fbf612c` for the first pass) |
| **nSpect registration** | NSPECT-S57Q-EI19 |
| **Dates** | 2026-08-17 (first pass), 2026-08-19 (re-baselined after the `api/` refactor) |
| **Requested by** | Rack Status team |
| **Method** | Agentic PLC v1.13.0 local advisory scans, plus manual architecture review |
| **Authority** | **Advisory / early-warning only.** Not release-equivalent; threat model not submitted to nSpect |

## What this is, and what it is not

This is a local, advisory pass. It is **not** a compliance record and does not clear any release
gate. The authoritative gates — nSpect, the Pulse Secret Scanner, SonarQube/Coverity, BlackDuck,
ScanSpect, OSRB and Anchore — are unchanged and unaffected by anything in this document.

Its value is threefold: it found two defects that mattered, it triaged away most of what looks
alarming in raw scanner output, and it leaves a baseline that re-runs in about 90 seconds so a
future change can be shown not to have regressed.

**The most serious finding did not come from a scanner.** It came from reading how the WebSocket
terminal is mounted. That is worth remembering when weighing tool coverage against review time.

## Findings

### CRITICAL — `/ws/terminal` accepts unauthenticated connections — **FIXED 2026-08-19**

> **Status: fixed.** `_WebSocketTerminalMiddleware._refuse_reason()` now runs before the path is
> claimed, and a refused caller gets a plain `403` rather than a WebSocket. The credential check is
> `web_auth.wsgi_environ_authenticated()`, which `app.py` backs with
> `web_auth.set_wsgi_authenticator(_wsgi_environ_authenticated)` — the same hook pattern as
> `config_env.set_materialize_hook`, so `api/terminal.py` never imports `app`. It **fails closed**:
> with no authenticator registered and auth enabled, the answer is deny.
>
> An `Origin` allowlist was added in the same check (`web_auth.wsgi_origin_allowed`,
> `RACK_WEB_ALLOWED_ORIGINS`). It is not redundant with authentication: cookies are not
> port-scoped, so any other service on the operator's `localhost` shares the dashboard's cookie
> jar, and `SameSite=Lax` does not separate them.
>
> Verified by `backend/tests/test_ws_terminal_auth.py` (5 tests) and
> `backend/tests/test_wsgi_auth_hook.py` (38). Both directions are asserted — the authenticated
> upgrade still succeeds, which is what proves the gate is evaluating a credential rather than
> refusing everything.



`backend/api/terminal.py` installs the web terminal as WSGI middleware wrapping `app.wsgi_app`.
When the path matches, `_WebSocketTerminalMiddleware.__call__` handles the request and returns
**without delegating to `self.wsgi_app`**, so Flask never runs — and the dashboard auth gate is
`@app.before_request _require_dashboard_auth`. There is no auth check of any kind inside
`terminal.py`: no cookie, no token, no `Authorization` header.

Verified live, not merely by reading. With `RACK_WEB_AUTH_MODE=token` and a token configured,
`GET /api/status` correctly returned 401 while an anonymous WebSocket upgrade to
`/ws/terminal?target=local` was **accepted**. With demo mode off it proceeded to
`pty.openpty()`, failing only because the test host's sandbox denied a PTY; the next statement is
`subprocess.Popen(["ssh", ...])` using the app's cluster key.

The terminal is **enabled by default** (`RACK_ENABLE_WEB_TERMINAL` defaults to `1`).

Impact is bounded by network reach — these deployments sit behind SSH tunnels on management
networks, not the public internet. But the container runs as root and holds the SSH keys to the
cluster, so anyone who can reach the app port obtains an interactive shell with those credentials.

- **Interim mitigation:** `RACK_ENABLE_WEB_TERMINAL=0` on any deployment not actively using it.
  Config only, no code, no rebuild.
- **Fix:** enforce the check inside `__call__` before it claims the path. Reuse `backend/web_auth.py`
  (for example under `app.request_context(environ)`, since `install(app)` already receives `app`)
  rather than writing a second auth implementation.
- **Note for the planned JWT/OIDC work:** an OIDC-protected Flask app does *not* close this. The
  gate must run where the middleware lives.
- **Regression test:** `backend/tests/test_ws_terminal_auth.py`. `test_route_parity.py` cannot cover
  this endpoint — it snapshots Flask URL rules and `/ws/terminal` is not one.

### HIGH — `target` is unvalidated and reaches a remote shell command string — **FIXED 2026-08-19**

> **Status: fixed, in three places.** `core.is_valid_node_hostname()` is the shared predicate (RFC
> 1123 labels, 253 chars, no leading hyphen — which also rejects `-oProxyCommand=…`, an ssh
> *option* that carries no shell metacharacter at all and would otherwise have passed a
> metacharacter filter).
>
> 1. `api/terminal.py` checks it at the boundary and again immediately before
>    `ssh_to_node_argv` — the second is deliberate, so the invariant holds for any future caller
>    of that method rather than only for the WSGI path.
> 2. `node_targets.validate_target_for_rack` applied it to the "inventory unavailable, trust the
>    explicit tokens" branch, which was the one path where an unexamined operator string became a
>    node target for `pdsh -w`, `scontrol update nodename=` and script argv.
> 3. `api/fabric_ops.py` NVLink preflight was assigning `body["target"]` straight into the pdsh
>    command, making it **the only pdsh caller in the product with no validation**. It now passes
>    the operator's selection as the *override* argument to `validation_target_for_rack`, which is
>    the inventory intersection every other launch already had.
>
> Verified by `test_wsgi_auth_hook.py` (23 rejection shapes) and two new cases in
> `test_nvlink_preflight_api.py`.



`host_exec.ssh_to_node_argv(node)` appends `node` after `ssh -tt` on the head-node hop. OpenSSH
joins the trailing argv with spaces and the remote shell interprets the result, so shell
metacharacters in the attacker-supplied `target` are command injection **on the head node**.

Confirmed by code reading; deliberately not executed against a live cluster. Validate `target`
against the known node inventory (or at minimum a hostname charset) before building argv. Covered
by the second failing test in `test_ws_terminal_auth.py`. **Separate fix from the auth gate** —
authentication does not address it.

### HIGH — switch/BMC credential committed to git (code fixed; rotation open)

The NVOS `admin` / BMC `root` password was a hardcoded literal in four `bin/scripts` files, present
since the initial commit. Fixed in `d17d9e3`: resolution moved to
`bin/scripts/lib/nvswitch_creds.inc.sh`, reading `RACK_NVSW_ADMIN_PASSWORD` with an optional
`RACK_NVSW_BMC_PASSWORD`, both registered in `config_env.py` so they are settable from Settings and
forwarded over SSH by the existing `RACK_*` rule. Missing values exit 78 rather than attempt an
empty password, which against a BMC would lock out `root`.

**Still open: rotation.** The value remains in git history in every clone and fork. Removing it from
the tree does not undo that; a rotation decision belongs to the cluster owners.

Worth recording how it was found: Semgrep matched only 3 of the 5 assignment lines, because its rule
keys on the `sshpass -p "$var"` call shape. The bare `export SSHPASS=` and two `NEW_PASSWORD=`
assignments were found by `grep`. **SAST is not a secret scanner**, which is why the un-run Pulse
scan below is the most important coverage gap.

### MEDIUM — container runs as root

`Dockerfile` defines no `USER`, so gunicorn runs as root (`missing-user-entrypoint`, Dockerfile:84).
Combined with the terminal finding this is what turns app-port access into cluster access.

### MEDIUM — no authorization model

Authentication is the only boundary. Any authenticated operator can drain nodes, cancel jobs,
rewrite BMC passwords and open a shell. There are no roles and no per-user attribution. This is the
gap the planned JWT/OIDC + authorization work addresses; pairing it with the existing
`validation_activities` registry would also make "who ran what on this rack" answerable.

### LOW — unhandled 500 from splatting query args into `url_for`

`backend/api/exports.py:590` — `redirect(url_for(".api_parser_export_excel", **request.args), code=307)`.
Semgrep flags this as an open redirect; **it is not one** — the host cannot be attacker-controlled
(`?_external=1&_scheme=javascript` still produced `http://localhost/...`). But `?_method=DELETE`
raises `werkzeug.routing.BuildError` and returns 500. Forward an allowlist of expected parameters.

### LOW — TLS verification disabled where BMC credentials are sent

`bin/scripts/nscale_ps_sweep.py:48` uses `ssl._create_unverified_context()` and then sends BMC
basic-auth credentials over that channel. Defensible for self-signed BMC certificates on a
management network, but it should be a **documented deviation** rather than an implicit one.

### LOW — `shell=True` with an interpolated path

`bin/lib/prod_drift_compare.py:227`. A developer tool, not reachable from HTTP; still trivially
avoidable by passing an argv list.

### Dependencies — 26 advisories

| Ecosystem | Package | Detail |
|---|---|---|
| Python | `cryptography 48.0.1` | PYSEC-2026-3552 / -3553 / -3554; fixed in 50.0.0 |
| npm | `undici 7.28.0` | 5 advisories (HIGH/MODERATE) |
| npm | `react-router 7.16.0` | 5 advisories (HIGH/MODERATE) |
| npm | `nanoid 3.3.15` | 2 advisories (HIGH) |
| npm | `postcss 8.5.16` | 2 advisories (HIGH/MODERATE) |
| npm | `brace-expansion 5.0.7` | 2 advisories (HIGH) |
| npm | `mermaid 11.15.0` | 5 advisories (LOW/MODERATE) |
| npm | `dompurify 3.4.11` | 2 advisories (LOW/MODERATE) |

All are reported `unknown-baseline` because this is the first recorded run; a second run after
upgrading will classify new versus pre-existing.

### Secret scanning — 0 verified secrets, 5 unverified, all false positives

Pulse Secret Scanner 1.70 over all 832 tracked files (831 copied, 1 symlink skipped; 9.4 MB,
1,266 chunks). Result: **0 verified secrets, 5 unverified.** All five triaged against source and
dismissed:

| # | Location | Detector preview | Verdict |
|---|---|---|---|
| 1–2 | `bin/scripts/nvswitches_configure_nmx_c.sh:20` | `ssh…WORD` (`sshpass`) | `sshpass -p "$NVSW_ADMIN_PASSWORD"` — env-var reference after `d17d9e3`, not a literal |
| 3 | `bin/scripts/nvswitches_rack_audit.sh:224` | `ssh…PASS` (`sshpass`) | `sshpass -p "$SSH_PASS"` — same |
| 4 | `docs/NSCALE_TX_DEPLOYMENT.md:302` | `"pa…EN>"` | `'{"password":"<RACK_WEB_AUTH_TOKEN>"}'` — documentation placeholder |
| 5 | `frontend/src/views/SettingsView.tsx:276` | `"pa…ext"` | `type={passwordField ? "password" : "text"}` — a React input prop |

Two things this does and does not prove. It **confirms the `d17d9e3` fix**: the scanner now sees
only environment-variable references where the literal used to be. It does **not** clear the
historical exposure — Pulse scans working-tree state only and never looks at git history, so the
committed credential remains present in every clone and is invisible to this check. Rotation is
still the open item.

Also worth noting the detector keys on the `sshpass -p` *call shape* rather than on literal values,
which is why it still fires on the fixed lines. Two of the five (findings 1 and 2) are duplicate hits
on the same line, and finding 4's reported line number was 102 against an actual 302 — spot-check a
line before acting on it.

### Threat model — 43 threats, 2 Critical, both the same defect

SecNemo Agent TAVA, run 2026-08-19 (task `cc9af9d2-a36d-4705-ab5a-1c4c0fd3b0aa`, ~13 minutes).
Reviewable copy: `.plc/security/threat-model-NVIS-RACK-STATUS.md` (tracked). Raw engine output:
`threat-model-NVIS-RACK-STATUS-tava-raw.json` (3.1 MB, gitignored). **Generated, not submitted to
nSpect.**

43 threat rows across 4 trust-boundary flows and 11 assets: 2 Critical, 17 High, 14 Medium, 10 Low.
Ten rows carry High residual risk. 42 of 43 rows are `NOT MITIGATED`, and 133 candidate security
requirements were derived (39 mandatory, 57 optional, 21 low, 16 needing review).

**The notable result: both Critical rows are the `/ws/terminal` defect, reached independently from
two different flows** — Flask app → compute trays, and Flask app → cluster head node. The engine
composed the missing authentication with the unvalidated `target` parameter and arrived at
unauthenticated remote command execution on cluster infrastructure, which is the same conclusion
manual review reached. Independent corroboration of the top finding.

Other High-residual themes worth attention, several of which no other check surfaced:

- **Secrets readable from `/proc/<pid>/environ` on the head node.** Forwarded secrets (sudo, switch
  and BMC passwords, tokens) are visible to any local user or compromised service on the head node
  via the environment table of the app's SSH sessions. Suggested control is mounting `/proc` with
  `hidepid=2`. This is a direct consequence of the `RACK_*` env-forwarding design.
- **Script-execution API as an injection path — reviewed against the source tree 2026-08-19 and
  the mechanism is misidentified.** The threat as written ("shell metacharacters in script *name*
  or arguments") describes an API this product does not have: no route under `backend/api/` accepts
  a script name, script path or command string from the request. Every launcher resolves its script
  through `paths.py` (19 `_<tool>_script_path` functions) and passes argv, with
  `platforms/base.wrap_script_cmd` quoting both the path and each argument. So the recommended
  control — an allowlist of authorized `bin/scripts/` entries — has nothing to allowlist.

  The product's real arbitrary-shell surface is the **workflow engine**: a `host:` step is a raw
  shell string with `${param}` substitution from `POST /api/workflows/<id>/run`
  (`workflow_engine.py:1462-1499`), as are `run:` inline scripts and `scheduler.command`. That is
  the designed capability of the workflow designer, not an input-validation defect — the correct
  control is authorization (who may author and run a workflow), which is the RBAC work, not a
  sanitizer. Reframed that way the threat is true and remains open; as written it is a false
  positive, and it is a good illustration of the caveat below about the engine's input being a
  written description rather than the code.

  One genuine inconsistency *was* found while checking: NVLink preflight's unvalidated pdsh
  target, now fixed — see the HIGH finding above.
- **Unvalidated responses from external services.** Grafana, Prometheus/DCGM, the NVIDIA Inference
  API, Google Sheets and nvFTP responses are consumed without schema validation; the AI agent loop
  makes poisoned completions a path to command execution.
- **Firmware pushed on checksum alone**, without signature verification against a root of trust.
- **Root container + SSH keys** recurs as an amplifier across several rows, reinforcing the
  Dockerfile finding above.
- **No dual control** on destructive operations such as switch firmware rewrites and BMC password
  rotation.

Two caveats that matter for reading this table. `NOT MITIGATED` here mostly means *the engine could
not verify a control from the description it was given* — it is not a confirmed vulnerability, with
the exception of the two Critical rows and the `target` injection. And the engine's input was a
**written architecture description, not the source tree**, so anything absent from that description
is unmodelled; the frontend in particular is modelled only as the operator-browser boundary.

### Licensing — 1 of 68 packages needs review

`paramiko 5.0.0` is LGPL-2.1 and classified `license_requires_osrb_review`. It is load-bearing for
the SSH transport, so this needs an OSRB conversation rather than a substitution.

## Triaged as not defects

Raw output was 59 ERROR-severity SAST findings. Roughly 45 are noise, and recording why saves the
next reader from re-deriving it:

| Finding | Count | Verdict |
|---|---|---|
| `sqlalchemy-execute-raw-query` | 7 | **False positive.** `results_history._filter_sql` binds every value with `?`; the only interpolation is `{direction}`, constrained by a ternary to the literals `ASC`/`DESC`, plus a PRAGMA taking an internal int |
| `dangerous-subprocess-use-audit` | 20 | **By design.** `host_exec` / `transport` exist to run commands; audit severity |
| `arbitrary-sleep` | 11 | Noise — polling loops |
| `hooks-dns-exfiltration-generic` | 4 | **False positive** — AI-supply-chain rules misfiring on ordinary shell |
| `mcp-tool-poisoning-generic` | 1 | **False positive** — fires on SSH code |
| `identical-is-comparison` | 3 | **False positive** — the `if fcntl is None` optional-import guard |
| `detected-ssh-password` | 3 | **Now false positives** — all three read from environment variables after `d17d9e3` |
| `open-never-closed` | 3 | `app.py`-side is a deliberate lease (`noqa: SIM115`); the other two are worth a glance |
| `ifs-tampering` | 5 | **False positive** — idiomatic `"$(IFS=,; echo "${arr[*]}")"` array join, scoped to a subshell |

## Coverage — including what was not covered

**Scanned:** 598 files (Python, TS/TSX, JS, shell, sbatch, Dockerfile), excluding the vendored
`frontend/src/styles/kaizen/`. Scope is recorded verbatim in `.plc/security/scope-files.txt`.

**Closed on 2026-08-19:** the first pass left **41 files unparsed** by Semgrep (34 `.sh`, 2
`.sbatch`, 4 `.tsx`, 1 `.ts`) — a real hole, since that is where the privileged cluster tooling
lives. All 41 have now been scanned by other means, and the honest result is that **no new security
defect was found**:

- All 36 shell/sbatch files are valid bash (`bash -n` clean). The failures are Semgrep's bash parser
  rejecting ordinary constructs such as the `case` pattern `lo|docker*|veth*)`.
- ShellCheck 0.10.0: 130 warning-or-above findings, of which 4 are error-level (`SC2068`, unquoted
  array expansion, in the two NeMo launchers). The bulk is `SC2046`/`SC2086` unquoted expansion —
  robustness, not security, in a trusted-input context.
- Semgrep at **all** severities over the same 36 files: 180 findings, 4 ERROR (all already in the
  baseline), 26 WARNING, 150 INFO.
- `eval` sites reviewed individually (`checker.sh:676`, `lib/fabric.inc.sh:146,233`,
  `lib/rack_fname.inc.sh:54,57`): all evaluate output of local trusted commands or enroot config;
  exploiting them requires root on the node already.
- `rm -rf ${NEMORUN_HOME}` (`nemo-llma3_8b.sh:147`) is unquoted, but the variable is hardcoded to
  `/mnt/nemo` on the preceding line and is neither SSH-forwarded nor backend-controlled. Style issue;
  add `"${VAR:?}"`.
- The 5 TypeScript files yielded 29 findings, **all** i18n/style (`jsx-not-internationalized`,
  `react-props-spreading`), none security. Their parse errors are Semgrep rejecting valid TypeScript
  (generic call args `>()`, a regex literal, an `& versions` intersection type); parsing was partial,
  not absent.

**Still not covered:**

- **Secret scanning covers the working tree only, never git history.** Pulse ran clean on
  2026-08-19 (see above), but a committed-then-removed credential is invisible to it by design. No
  history-scanning tool was run.
- **The threat model's input was a description, not the code.** SecNemo received the architecture
  write-up, not the source tree, so its coverage is bounded by what that description said.
- **ERROR severity only** for the main baseline. The reported "0 medium, 0 low" means *not
  evaluated*, not clean — the runner passes `--severity ERROR`.
- **No DAST, no fuzzing, no container image scan.** The Dockerfile finding is static; nothing
  inspected the built image layers.
- **No authorization testing** — there is nothing to test yet. (The workflow-engine surface described
  under the reframed script-execution row is the clearest reason this matters: it is a real
  arbitrary-shell capability whose only control today is "is this caller logged in".)
- Frontend: 259 files scanned with zero ERROR findings, a real result — but TS rule coverage is
  thinner than Python (204 vs 345 rule files) and the ERROR filter removes most React/XSS rules.

## Effect of the `api/` refactor

Between the two passes, `backend/app.py` went from 18,479 lines and 136 routes to 2,015 lines and 5
routes across 38 modules. Two consequences worth recording:

- **The refactor was security-neutral.** Every per-rule finding total is identical before and after
  (59 → 59); only file attribution moved. That is useful evidence for a change of this size.
- **It fixed the tooling.** The first pass *errored out* on `app.py` and needed the per-rule timeout
  raised to 180s. The scan now completes in 89 seconds at Semgrep's default 5s. Anyone wiring this
  into CI no longer needs the workaround.

## Recommended order of work

1. ~~Close the `/ws/terminal` auth gap~~ — **done 2026-08-19.** `RACK_ENABLE_WEB_TERMINAL=0` is no
   longer needed as a mitigation.
2. ~~Validate `target` against the node inventory~~ — **done 2026-08-19**, in three places.
3. **Bind the published container port to loopback.** New finding, see below — cheaper and more
   decisive than anything else on this list.
4. Decide on credential rotation with the cluster owners.
5. Add a non-root `USER` to the Dockerfile.
6. Upgrade `cryptography` to 50.0.0 and the five npm HIGH packages; re-run to get a real baseline.
7. Open the OSRB conversation for `paramiko`.
8. Consider a full-severity SAST pass to see what the ERROR filter hides.
9. Work the threat model's High-residual rows as a requirements backlog — starting with `hidepid=2`
   on head nodes. Note that the script-execution allowlist row is not actionable as written; see the
   reframing above.

### Correction to the impact analysis above: the app port is published on `0.0.0.0`

The CRITICAL finding says impact is "bounded by network reach — these deployments sit behind SSH
tunnels on management networks". **That understates it.** `docker-compose.yml:33` publishes
`"${RACK_WEB_PORT:-5000}:5000"` with no bind address, so Docker binds `0.0.0.0`. On the Nscale TX
OVA that makes `192.168.10.3:5056` answer plaintext HTTP to anything routable on that segment, and
`deploy/nscale-tx.override.consolidated.yml` documents `http://<host>:5056/docs/…` as a normal
access path — so the non-tunnel path is used, not hypothetical. For the window in which the terminal
was unauthenticated, the reachable population was everything on that segment rather than everyone
holding SSH access.

The tunnel is a convention, not an enforcement. Two independent controls follow, and they are worth
having together: `"127.0.0.1:${RACK_WEB_PORT:-5000}:5000"` removes the surface, and TLS protects
whatever remains. Tracked as Phase 0.5 of the authentication program.

## Reproducing this

Agentic PLC installs per developer; the skills are not committed. Scanner state lives under
`.plc/tools/` and all raw output under `.plc/security/`, both gitignored.

```bash
# one-time, from the repo root
curl -fsSL https://gitlab-master.nvidia.com/api/v4/projects/310906/packages/generic/agentic-plc/bootstrap/install.sh | bash -s -- --mode repo

# scanner state must stay repo-local (and out of $HOME)
export UV_CACHE_DIR="$PWD/.plc/tools/uv-cache" UV_TOOL_DIR="$PWD/.plc/tools/uv-tools"
export XDG_CONFIG_HOME="$PWD/.plc/tools/xdg" XDG_CACHE_HOME="$PWD/.plc/tools/cache/xdg-cache"

# full-repo SAST baseline (the runner defaults to changed files only)
args=(); while IFS= read -r f; do args+=(--path "$f"); done \
  < <(git ls-files | rg '\.(py|ts|tsx|js|jsx|mjs|sh|sbatch)$|Dockerfile' | rg -v '^frontend/src/styles/kaizen/')
python3 .agents/skills/plc-security-scan/scripts/plc_security_scan.py run-sast "${args[@]}"

python3 .agents/skills/plc-security-scan/scripts/plc_security_scan.py run-dependency-vuln \
  --path backend/requirements.txt --path frontend/package-lock.json

# shell coverage Semgrep cannot parse
uvx --from 'shellcheck-py==0.10.0.1' shellcheck --severity=warning --shell=bash bin/scripts/*.sh

# render the evidence table for an MR description
python3 .agents/skills/plc-security-scan/scripts/plc_security_scan.py summary
```

## Evidence

Tool versions: Agentic PLC v1.13.0 · Semgrep 1.157.0 · pip-audit 2.9.0 · osv-scanner 2.3.5 ·
pip-licenses 5.5.5 · ShellCheck 0.10.0 · Pulse Secret Scanner 1.70 · SecNemo Agent TAVA.

Artifacts (gitignored, developer-local): `.plc/security/semgrep.json`,
`pulse-secret-scan.json`,
`dependency-backend-requirements-requirements-txt.json`,
`dependency-frontend-npm-package-lock-json.json`, `pip-licenses.json`, `scope-files.txt`.
Append-only history of every run: `plc-evidence.jsonl` (11 rows). Committed:
`.plc/profile.json` and `.plc/security/threat-model-NVIS-RACK-STATUS.md` — the raw 3.1 MB engine
JSON stays gitignored and local.

```text
| Check           | Status | Tool                  | Details                                              |
|-----------------|--------|-----------------------|------------------------------------------------------|
| sast            | WARN   | semgrep               | 59 high (severity filter: ERROR)                     |
| dependency-vuln | WARN   | pip-audit/osv-scanner | 26 unknown-baseline finding(s); 2 contexts scanned   |
| license         | WARN   | pip-licenses          | 1 of 68 packages requires OSRB review                |
| secret-scan     | PASS   | Pulse 1.70            | 0 verified; 5 unverified, all triaged false positive |
| tava-report     | GENERATED | SecNemo Agent TAVA | 43 threats: 2 Critical, 17 High; not submitted to nSpect |
```
