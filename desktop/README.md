# NVIS Greenlight — desktop shell

An Electron shell around the Greenlight dashboard: a deployment picker, SSH tunnel supervision,
certificate pinning, and signed auto-updates. Windows, macOS and Linux.

## What it is, and what it deliberately is not

**It loads each deployment's own origin. It does not bundle the SPA.** That is the central design
decision and everything else follows from it.

Every API call in the frontend is a root-relative `/api/...` with `credentials: "same-origin"`, the
session cookies are `HttpOnly; SameSite=Lax`, the CSP is `default-src 'self'`, and
`backend/tests/test_security_headers.py::test_no_cors_headers_are_ever_emitted` exists specifically
to stop anyone relaxing that. Serving the bundle from `app://` or a local port would require CORS
with credentials and `SameSite=None`, dismantling the same-origin protection the product currently
gets for free. Pointing a window at the real origin keeps all of it, and means the SPA always
matches the backend version that served it — no client/API version matrix.

So the shell's value is the things a browser tab cannot do:

- brings up the **SSH tunnel** for you, and says something actionable when it fails;
- **pins the self-signed certificate** instead of training operators to click through warnings;
- puts the **customer and cluster in the window title**, so two tunnels that differ only by port
  are distinguishable — which an installed PWA fundamentally cannot manage, since both install as
  identical icons;
- **signed auto-update**.

## Scripts

| Command | Does |
|---|---|
| `npm run build` | compile main + preload + launcher, copy launcher assets |
| `npm start` | build, then launch |
| `npm run smoke` | build, launch, assert 12 checks over the DevTools protocol, shut down |
| `npm run pack` | unpacked `.app`/dir in `release/` — fastest way to test packaging |
| `npm run dist:linux` | **AppImage + deb + rpm, in Docker.** No Linux machine needed |
| `npm run dist:mac` | macOS dmg + zip — unreliable on a Defender-managed Mac, see below |
| `npm run dist:local` | macOS zip only — quickest when you don't need an installer |
| `npm run dist` | every target for the host platform (DMG step fails on this laptop) |
| `npm run dist:all` | mac + Windows + Linux (needs the respective toolchains) |
| `npm run dist:publish` | build and upload to the update feed (CI) |

`npm run smoke` is the one to run after a change. It drives the app over CDP rather than checking
that the process is alive, because "alive" proves nothing — a renderer can throw on its first line
and leave a window that is merely blank, with the error only in DevTools. That is exactly what a
CommonJS `exports` marker in a classic script does, and it is how the test earned its keep.

## What Docker can and cannot build

| Target | In Docker? | |
|---|---|---|
| AppImage / deb / rpm | ✅ | `npm run dist:linux`, verified |
| Windows NSIS | ⚠️ | `npm run dist:win` — works on an x86_64 host, **fails on Apple Silicon** |
| macOS dmg / zip | ❌ | impossible, not merely unsupported |

**macOS is not a missing image, it is outside what containers do.** Containers share the host
kernel and Docker Desktop on macOS *is* a Linux VM, so a Darwin container cannot exist. Checked
rather than assumed: `electronuserland/builder` publishes **100 tags, none** mac/osx/darwin, all
`linux/amd64`. electron-builder's own docs say "Docker images to build Electron app for Linux *or
Windows*". A DMG needs Apple's DiskImages framework (`hdiutil`) and signing needs
`codesign`/`notarytool`; Apple's licence also confines macOS virtualisation to Apple hardware, and
that route is a VM (Tart, Strudel) rather than a container.

**`electron-installer-dmg` does not get around this**, despite reading as pure JavaScript. It
delegates to `appdmg`, which declares `"os": ["darwin"]` and shells out to `hdiutil`:

```
npm error notsup Unsupported platform for appdmg@0.6.6: wanted {"os":"darwin"} (current: {"os":"linux"})
```

The trap is that `appdmg` is an **optional** dependency, and npm silently *skips* optional deps
that fail the platform check — so `npm i electron-installer-dmg` succeeds on Linux and only fails
when you use it. Even a DMG synthesised on Linux (technically possible with `hfsprogs` plus
`libdmg-hfsplus`) would be unsigned and un-notarized, which Gatekeeper refuses outright since
Sequoia. There is no path that removes the Mac.

The consolation is that it barely matters: `electron-updater` reads the **zip** on macOS, not the
DMG, so `npm run dist:local` covers development and auto-update completely.

## Linux builds run in Docker — `npm run dist:linux`

Produces all three Linux artifacts plus `latest-linux.yml` on a Mac, via
`electronuserland/builder`. Verified 2026-09-08: AppImage 113 MB, deb 87 MB, rpm 77 MB.

**It does not help with macOS.** A DMG needs `hdiutil` and HFS formatting, so mac targets require
a Mac — Docker on macOS is a Linux VM and cannot build them at any price. Signing and notarization
need macOS tooling too.

Two things `scripts/build-linux-docker.sh` handles that are easy to get wrong:

- **`node_modules` is masked with an anonymous volume.** Bind-mounting it would let the container's
  `npm ci` install Linux packages over the host's darwin-arm64 native bindings. That is not
  hypothetical — it happened earlier when a container was pointed at `frontend/`: rolldown's
  `.node` binding was replaced with the Linux build and `npm test` on the host died with
  `MODULE_NOT_FOUND`. `npm ci` on the host was the repair.
- **The image is `linux/amd64` only,** so it runs emulated on Apple Silicon — slow (~2.5 min) but
  correct, because electron-builder *downloads* prebuilt Electron for the target arch rather than
  compiling, and the one production dependency is pure JavaScript.

The `deb` and `rpm` targets also need metadata the AppImage does not: `fpm` refuses without a
`linux.maintainer` and a `homepage`. Getting that wrong fails *halfway* — the AppImage is already
written when the deb step aborts — so it reads as a partial success rather than a config error.

### Windows cannot be built on this laptop — use the `desktop-windows` CI job

`npm run dist:win` gets ~95% of the way: both NSIS payloads are produced (`*-x64.nsis.7z` and
`*-arm64.nsis.7z`, 96 MB each). It then dies on the last step, where NSIS runs the installer stub
under Wine to generate the uninstaller:

```
qemu: uncaught target signal 6 (Aborted) - core dumped
wine: dlls/ntdll/unix/virtual.c:267: anon_mmap_fixed:
Assertion `!((UINT_PTR)start & host_page_mask)' failed.
```

**Cause: host page size.** Wine's fixed-address mappings require 4 KB host pages, and Apple
Silicon's 16 KB pages leak through the emulator — hence the assertion on `start & host_page_mask`.
Two earlier readings of this were wrong in ways worth recording, because both cost time:

- *"It is not a page-size mismatch"* — it is. The emulated container **reports** 4096, which is
  what made this look like something else; the assertion is about the **host's** page size, which
  the guest cannot see.
- *"Rosetta does not implement the `MAP_FIXED` semantics Wine needs"* — the binfmt handler is not
  the variable. This failure now reproduces under **QEMU** (note the `qemu:` prefix above, from a
  2026-09-11 run), so switching emulation backends does not help. `wine --version` runs fine either
  way, which is exactly why it keeps looking like a solvable configuration problem.

**The fix is not to emulate.** On a native amd64 runner there is no emulation and no assertion:
the `desktop-windows` CI job builds all three installers in **116 seconds** (x64 90 MB, arm64
84 MB, and a combined 173 MB one that `latest.yml` names as the update payload). Trigger it
manually, passing the release's `BUILD_STAMP`, then download the job artifact into
`desktop/release/` and run `scripts/publish-release.sh`.

## DMG builds on a Defender-managed Mac — use `npm run dist:mac`

Plain `npm run dist` fails in DMG creation on this laptop:

```
dmgbuild.core.DMGError: Unable to detach device cleanly:
hdiutil: couldn't unmount "disk4" - Resource busy
```

**Root cause: Microsoft Defender for Endpoint's real-time scanner.** dmgbuild copies ~180 MB of
app into a freshly mounted HFS volume; `wdavdaemon_unprivileged` (running as `_mdatp`) opens those
files to scan them; `hdiutil` asks to unmount 0.9 s later and gets `EBUSY` (DiskArbitration
`status code 0x00000010`). dmgbuild's default `--detach-retries` is **5**, and the scan can take
longer than that.

`npm run dist:mac` improves the odds by pointing `CUSTOM_DMGBUILD_PATH` at
`scripts/dmgbuild-patient.sh`, which runs the same bundled dmgbuild with `--detach-retries 30`
(its maximum). It changes no security posture — it just waits longer.

**It is a mitigation, not a fix, and do not trust it.** One run with it produced both DMGs, with
the arm64 volume detaching on attempt 4 and the x64 on attempt 2. The very next run wedged on the
x64 DMG with the volume still `EBUSY` **23 minutes** later — far beyond any retry budget. So
Defender can hold the volume effectively indefinitely, and more retries cannot cure that.

If a build hangs, the volume is leaked and needs clearing by hand. `hdiutil` is not required:

```bash
pkill -f dmgbuild; pkill -f electron-builder
diskutil unmount force /Volumes/NVIS\ Greenlight*
```

**Build DMGs in CI, not here.** Release artifacts have to be signed and notarized on a runner
anyway, and a runner has no Defender real-time scanner competing for the volume. Locally, use
`npm run dist:local` — and note the DMG is not needed for auto-update at all, since
`electron-updater` reads the zip on macOS, so this costs nothing day to day.

A Defender exclusion looks like the real fix and mostly is not: exclusions are MDM-managed with
`tamper_protection: "block"`, so they cannot be added locally, and excluding the *output*
directory would not help because the contention is on the mounted volume under `/Volumes/`, not on
the output path. The narrow ask that would work is an exclusion for `/Volumes/NVIS Greenlight*`.
There is precedent for developer exclusions in the current policy (`/Users/*/dev/` is one), but
`/Volumes` is a genuine malware vector, so scope any request tightly.

Three things about this were genuinely misleading, and are why it is written down:

- **`lsof` shows nothing** holding the volume. The holder is a root/`_mdatp` daemon, invisible to
  unprivileged `lsof` — so the obvious diagnostic says "nobody is using it".
- **The kernel cannot tell you either.** diskarbitrationd tries
  `sysctl(vfs.generic.print_busy_vnodes)` and gets `ENOENT`, because that sysctl exists only on
  debug kernels.
- **It presents as consistent, then intermittent.** The scan duration depends on Defender's cache
  state, so a cold cache blows past 5 retries every time while a warm one squeaks under — the same
  build "works sometimes" for no visible reason.

**The trap is what a failure costs you.** A DMG failure aborts the whole build and electron-builder
cleans up, so the *zips are deleted too* — and on macOS the zip is what `electron-updater`
consumes, while the DMG is only the human-facing installer. So a broken DMG step silently destroys
the artifact that actually matters. `npm run dist:local` (zip only) is the quick path when you do
not need an installer.

To re-diagnose from scratch: `scripts/diagnose-dmg.sh` runs a DMG build while sampling `lsof`
against the volume, then
`/usr/bin/log show --last 5m --predicate 'process == "diskarbitrationd"'` shows the unmount
attempts and their status codes. Note `/usr/bin/log` in full — `log` is a zsh builtin that shadows
it and silently produces nothing.

## Deployments

Stored at `~/Library/Application Support/NVIS Greenlight/deployments.json` (equivalents elsewhere),
seeded on first run and editable from the launcher.

Three fields are more load-bearing than they look:

- **`sshAlias`, never a host/user/key triple.** Delegating to `~/.ssh/config` inherits
  `ProxyJump`, `IdentityFile`, Teleport certificates and `ControlMaster` for free. It also matters
  for correctness: `cluster-deployments.mdc` records that on Nscale the IP is *not* cluster
  identity — every kit numbers its hosts out of the same `192.168.10.0/2x` behind a different
  jumphost, so `192.168.10.3` names one cluster via `device2` and a different one via `device100`.
  The alias pins the jumphost.
- **`localPort` is fixed per deployment, never auto-allocated.** The browser origin is
  `https://localhost:<localPort>`, and the origin is the identity for the session cookie and for
  `nvis-rack-client-id` in `localStorage`. A port picked at launch would sign the operator out
  every time and read as "the desktop app keeps logging me out".
- **`scheme` defaults to `https`,** because `startup_gate.py` refuses to serve without TLS and
  generates a self-signed certificate on first boot. A container deployed before that gate landed
  is still on plain HTTP until rebuilt, which is why the field exists.

### A fresh install ships with no deployments, deliberately

The operator adds their own; the launcher's empty state says how. This is not an omission to be
fixed — a build that arrives pre-filled is wrong for two reasons:

- **It discloses one customer's infrastructure to all the others.** One binary goes to Mayo,
  Firmus, Boston and Nscale, so a baked-in jumphost alias, internal port or authentication note is
  handed to every operator at every other site.
- **A pre-filled endpoint is a wrong-cluster hazard.** `cluster-deployments.mdc` opens by warning
  that the wrong host silently hits the wrong cluster, and a row the operator did not type is one
  they have no reason to check. A plausible entry that connects somewhere unintended is worse than
  no entry, because the label is believed.

Connection details belong in `cluster-deployments.mdc` — documentation a person reads and types
from — not compiled into a shipped binary. `scripts/smoke.mjs` asserts the empty first-run state,
so a seed cannot creep back in unnoticed.

**Deploying to a fleet?** Pre-place a `deployments.json` in the app's userData directory
(`~/Library/Application Support/NVIS Greenlight/`) via MDM or a setup script. The loader respects
an existing file and only creates an empty one when none is there. That keeps site configuration
in the site's own provisioning, where it can differ per site, instead of in the source.

As a worked example for typing one in, Nscale TX is: alias `nscale-tx-ova`, remote host
`localhost`, remote port `5056`, local port `5156` (matching `ns-tun.sh`), scheme `http` until
that OVA is rebuilt with the TLS startup gate. Hop 2 is password-only, so run
`ssh nscale-tx-ova 'echo ok'` once in a terminal first and the app reuses the ControlMaster.

### The password hop

Nscale TX authenticates hop 2 with a password, so nothing unattended can open a fresh session. The
shell passes `BatchMode=yes` so that case fails immediately and legibly rather than blocking on a
prompt nobody can see, and then tells you to run `ssh nscale-tx-ova 'echo ok'` once in a terminal;
everything after that rides the `ControlMaster`.

`ConnectTimeout=10` is also load-bearing and was measured: without it an unreachable host took
**75 seconds** to fail, because these names resolve through a search domain and then sit in a TCP
connect. The readiness window is deliberately longer (30 s) so ssh's own diagnosis wins the race
and gets turned into an instruction, rather than this timer reporting the useless truth that the
forward never came up.

## Certificate pinning

Every deployment presents a self-signed certificate, so the options were refuse, blanket-accept, or
pin. Blanket-accept makes a tunnel to the wrong host indistinguishable from the right one, so the
shell shows the fingerprint once with the command to verify it against the host:

```bash
openssl x509 -in var/secrets/tls/server.crt -noout -fingerprint -sha256
```

A *changed* fingerprint later is refused and deliberately **not** re-offered — at that point the
plausible causes are a redeployed certificate and someone intercepting the connection, and no
operator can tell those apart from a dialog. Clearing the pin is an explicit action in the
launcher.

## Security model

Two window kinds, and the difference is who wrote the code inside:

| | Launcher | Cluster window |
|---|---|---|
| Content | our local HTML | the deployment's origin |
| Preload | narrow `contextBridge`, 9 named channels | **none** |
| Sandbox | yes (`app.enableSandbox()`) | yes |

A cluster window renders code served over a tunnel from a customer data centre; handing it a
`contextBridge` API would put "open an SSH tunnel" and "read the deployment list" one XSS away from
a page we do not build. It needs nothing from us, so it gets nothing. Both windows also send
`window.open` and off-origin navigation to the system browser, deny `<webview>` attachment, and
default-deny every permission request.

## Auto-update

`electron-updater` against the GitLab generic Package Registry, with a mandatory signature check
before anything is installed. `autoDownload` is off: an operator mid-validation should not have a
150 MB download start under them.

Two things the release job must get right, both of which have already bitten:

1. **The feed URL must not contain the app's own version.** It is baked into every build at compile
   time, so `${version}` makes the 0.3.0 build ask `…/0.3.0/latest-mac.yml` for news of a newer
   release — forever reporting "you are on the latest version" while 0.4.0 sits one directory away.
   Silent, and invisible in testing, because a single-version registry answers exactly like an
   up-to-date one. Hence the fixed `latest` path segment; GitLab validates `package_version`
   against `\A(\.?[\w\+-]+\.?)+\z`, which `latest` satisfies.
2. **Artifacts upload beside the manifest, and duplicates must be allowed** for that one version.
   `latest-*.yml` names its files relative to the feed URL, and every release after the first
   overwrites them — so the registry's "Reject duplicates" setting needs an exception, or the
   second release is refused.

**Linux updates work only from the AppImage.** `.deb` and `.rpm` are owned by the system package
manager and there is no mechanism for the app to replace itself; `updater.ts` detects that (no
`$APPIMAGE`) and says so rather than spinning.

## Signing and notarizing — `scripts/sign-3s.sh`

One command, and it runs identically on a workstation and on a macOS CI runner:

```bash
npx electron-builder --mac --dir --publish never   # unsigned .app only
scripts/sign-3s.sh                                  # sign → verify → notarize → staple
scripts/sign-3s.sh --no-notarize                    # 3S signing only
```

`--dir` matters: the `.app` must be signed **before** it is packaged. Signing a finished DMG
leaves the `.app` inside it unsigned and invalidates the hashes electron-builder already wrote
into `latest-mac.yml`.

### Four traps the script encodes

Each of these failed silently at least once, which is why they are in code rather than in
somebody's memory:

1. **3S rejects filenames containing spaces** (`HTTP 400: Filename cannot contain spaces`).
   `productName` is "NVIS Greenlight", so `artifactName` in `electron-builder.yml` pins space-free
   names. Contents of the zip may contain spaces; the zip's own name may not.
2. **3S signs `--deep` but skips nested Mach-O under any `Resources/` directory.** Here that is
   exactly one file — `Squirrel.framework/.../Resources/ShipIt`, the binary Squirrel.Mac uses to
   *apply* an update. Left ad-hoc, notarization fails on library validation. The script enumerates
   them rather than hard-coding ShipIt, signs each inside-out, then re-seals the app.
3. **3S strips entitlements unless an `.entitlements` file rides inside the zip**, as a sibling of
   the `.app` and sharing its basename:

   ```
   nvis-greenlight-0.3.0-arm64-ent.zip
   ├── NVIS Greenlight.app
   └── NVIS Greenlight.entitlements
   ```

   This is the expensive one. Without it 3S still applies hardened runtime, so the app signs,
   notarizes, staples and passes `spctl` — **every gate green** — then aborts at
   `v8::Isolate::Initialize`, because V8 cannot map JIT pages without
   `com.apple.security.cs.allow-jit` and `allow-unsigned-executable-memory`. An app that passes
   every check and crashes is worse than one that fails a check. The script verifies the
   entitlements landed on the app *and* on all four Electron helpers — each hosts its own V8 —
   and refuses to notarize if any is missing.
4. **Repackage with `ditto`, never `zip`.** Symlinks and extended attributes in a `.app` do not
   survive `zip`.

The script also rewrites `latest-mac.yml` at the end. electron-builder computed it against the
*unsigned* zip, and signing changes the bytes — publish without rewriting and `electron-updater`
rejects every update on a hash mismatch.

### Credentials

| Purpose | Workstation | CI runner |
|---|---|---|
| 3S | interactive browser login (`nvsec` opens it) | `NVSEC_SSA_CLIENT_ID`, `NVSEC_SSA_CLIENT_SECRET`, `NVSEC_SSA_SCOPE=SIGNING_MACOS` |
| Apple | `NOTARY_PROFILE=<keychain profile>` | `APPLE_API_KEY` (path to `.p8`), `APPLE_API_KEY_ID`, `APPLE_API_ISSUER` |

Workstation setup, once:

```bash
python3 -m pip install --user --break-system-packages nvsec \
  -i "https://gitlab-master.nvidia.com/api/v4/projects/25923/packages/pypi/simple" \
  --extra-index-url "https://urm.nvidia.com/artifactory/api/pypi/sw-cftt-pypi-local/simple"

nvsec 3s user-jobtypes --jobtype_specifier submit    # should list MACOS (id 912)

xcrun notarytool store-credentials rkoike-notarytool \
  --apple-id <you>@nvidia.com --team-id 6KR3T733EC   # prompts for an app-specific password
```

Note `nvsec sign` (the "lighter path" in the runbook) returns `Access Denied` for this job even
with the DL synced — it authenticates from `~/.nvsec.cfg`, while the `3s` subcommands use their
own browser-OAuth token in `~/.3s/`. **Use `nvsec 3s submit`.**

## Releasing — `scripts/publish-release.sh`

Three platforms, three machines, **one version**. macOS must be signed on a Mac; Linux and Windows
are built by CI on amd64 runners. The stamp is what ties them together:

```bash
cd desktop

# 1. macOS, on a Mac, on VPN. This is what decides the release's version.
npx electron-builder --mac --dir --publish never
scripts/sign-3s.sh                       # prints the stamped version, e.g. 0.3.0-260910185147

# 2. Linux and Windows, in CI. Play desktop-linux / desktop-windows with
#    BUILD_STAMP=260910185147, then download each job artifact into release/.
#    Linux can also be built locally:  BUILD_STAMP=… npm run dist:linux

# 3. Publish everything that is present.
GITLAB_TOKEN=<pat-with-api-scope> scripts/publish-release.sh
```

`--dry-run` prints every upload without performing one.

### It publishes every platform present, and refuses a set that disagrees

All three manifests (`latest-mac.yml`, `latest-linux.yml`, `latest.yml`) go to the **same**
`latest/` feed, because that is the one URL every build was compiled to poll. So the script checks
that they name the same version and aborts if not.

That guard is not bureaucracy. Left to read the clock, each platform stamps itself with its own
build time, and a release called `0.3.0-<stamp>` then means a different build on each platform.
Nothing would look wrong: a client reads only its own manifest, so every platform would be
individually satisfied while the release as a whole was incoherent. Pass `BUILD_STAMP` to every
build of one release.

Re-runs are cheap — a file already in the registry at the same size is skipped (one `HEAD` each).
That matters because macOS is signed and published days before the containers build Linux and
Windows, so the second and third runs would otherwise re-send a gigabyte that is already there.

### What each platform ships

| Platform | Artifacts | Signed? |
|---|---|---|
| macOS arm64 | `-arm64-notarized.zip` | Developer ID + notarized + stapled |
| macOS x64 | *not yet built* | would need a second 3S round |
| Linux x64 | `.AppImage` (self-updating), `.deb`, `.rpm` | n/a |
| Windows x64 / arm64 | `-x64.exe`, `-arm64.exe`, combined `.exe` | **no** — see below |

**The Windows installers are not Authenticode-signed**, so Windows shows a "Windows protected your
PC" SmartScreen panel on first run and the user must choose *More info → Run anyway*. 3S does sign
Windows — `nvsec sign --job-name "Windows Authenticode"` — but this account is entitled to one job
type only:

```console
$ nvsec 3s user-jobtypes --jobtype_specifier submit
|   Id | Name  | Display Name      | Submit Group                  |
|  912 | MACOS | MacOS Application | access-cloudsec-3s-sign-macos |
```

Request membership of the Windows signing submit group, then add a signing step to
`desktop-windows` mirroring `scripts/sign-3s.sh`. `publish-release.sh` prints a NOTE for every
unsigned `.exe` rather than refusing, because an unsigned internal build is still installable.

**Artifacts are never committed to git.** `release/` is gitignored deliberately: a 112 MB binary in
history is a permanent tax on every clone, and git has no good story for large binaries. The
generic Package Registry is version-addressed, permanent, and already the pattern this repo uses —
`publish-release.sh` mirrors the `release:publish-bundle` CI job that does exactly this for the
air-gapped Docker image, just with a Personal Access Token instead of `CI_JOB_TOKEN` (which only
exists inside a job).

### Two destinations, both needed

| Path | For |
|---|---|
| `…/nvis-greenlight-desktop/latest/` | the **auto-update feed** |
| `…/nvis-greenlight-desktop/<version>/` | immutable history; what the GitLab Release links to |

The `latest/` path must not contain a version — `electron-builder.yml` bakes that URL into every
build at compile time, so a 0.3.0 build asking a `0.3.0/` directory for news of 0.4.0 would wait
forever while reporting "you are on the latest version". It is overwritten each release, so the
registry's **"Reject duplicates"** setting needs an exception for it or the second release is
refused.

### It refuses to publish a stale pair

The script cross-checks every artifact's SHA-512 against the manifest that names it before
uploading anything, and aborts on a mismatch. This is not a formality — electron-builder writes
the manifest against the *unsigned* artifact, and signing changes the bytes. Publishing the
mismatched pair gives you an auto-update that downloads, fails its integrity check and refuses, for
every user, discovered only in the field. It also warns when a macOS artifact carries no stapled
notarization ticket, because a recipient sees Gatekeeper's refusal as "the download is corrupt".

**It has already earned its keep on a defect nothing else would have caught.** The first Linux
build wrote `NVIS Greenlight-<version>.AppImage` to disk while `latest-linux.yml` recorded the
payload as `Greenlight-<version>.AppImage` — electron-builder does not agree with itself about
what the space in `productName` means. The updater resolves that name against the feed URL, so
every Linux client would have fetched a 404 and reported itself up to date forever, with the real
update one filename away. The build looked completely clean. `linux.artifactName` now pins a
space-free name, as `mac` and `nsis` already did.

## The CI jobs

| Job | Runner | State |
|---|---|---|
| `desktop-linux` | any amd64 Docker | **manual**, ready — verified 190 s |
| `desktop-windows` | any amd64 Docker | **manual**, ready — verified 116 s |
| `macos-sign` | needs a macOS runner | absent unless `NVIS_ENABLE_MACOS_SIGNING=true` |

`desktop-linux` and `desktop-windows` need no credential and no special runner, so they are plain
manual jobs. Both carry `needs: []` — deliberately independent of `lint` and `test`. The desktop
shell does not bundle the frontend (it opens the SPA the deployment serves), so a frontend lint
error says nothing about whether the installer is correct; inheriting the stage gate meant four
`react-refresh` errors in unrelated new components left the Windows installer unbuildable on a
commit that had never touched `desktop/`.

`macos-sign` is defined rather than commented out, so `gitlab-ci lint` parses it on every push and
it cannot drift — but its rules make it *absent from the pipeline* unless
`NVIS_ENABLE_MACOS_SIGNING` is `"true"`, and manual even then.

Three things must exist before enabling it:

1. **A macOS runner**, tagged, with Xcode 13+ and Node 24; set `MACOS_RUNNER_TAG`. There is no
   substitute — `codesign`, `ditto`, `notarytool` and `stapler` are macOS-only.
2. **An SSA service account** with the `SIGNING_MACOS` scope. A separate request from the signer
   DL; see the 3S runbook's NVault/SSA sections.
3. **An App Store Connect API key**, not an app-specific password — a personal Apple ID ties every
   release to one person and cannot be shared with a runner. An App Store Connect Admin issues it.

## Signing prerequisites

Not optional for distribution, and the dominant cost of shipping this:

- **macOS** — Developer ID plus notarization. Since Sequoia the right-click-to-open bypass is gone,
  so an unsigned build is effectively undistributable, and signing without notarizing is a half
  measure. The hardened-runtime entitlements in `resources/entitlements.mac.plist` are required:
  V8 generates code at runtime, and without the JIT allowances a *notarized* build crashes on
  launch while the unsigned local build works perfectly — the most misleading failure in the whole
  packaging path.
- **Windows** — Authenticode (EV or cloud HSM), or SmartScreen warns every downloader. Sign
  *before* the updater signature is generated, or updates break.
- **Linux** — no signing required for AppImage.

This also means CI needs macOS and Windows runners, which today is Kaniko-only.
