# Proposed ClusterKit Frontend Integration

Status: proposed, awaiting the user's go-ahead. No frontend source has been changed.

## Verified Starting Points

- The working fork is `nvis-ova/rack-status-pythoncm`; its backend branch contains
  the NC-tested modular API, static execution recipes and normalized results.
- Its default branch remains `feature/rack-status-pythoncm` at `8d1896e`.
  Fetch and fast-forward pull confirmed the local backend branch was current.
- The application links to upstream `nvis/nvis-rack-status`. Its fresh `main`,
  fetched for read-only inspection, is `a260a17a23c45420a61e6c2e3d01f56e83ba33fe`.
  This version is branded **NVIS Greenlight** and includes the full frontend.
- Greenlight uses React 19, strict TypeScript, Vite, Kaizen UI, Lucide icons,
  selected MUI components, HashRouter and shared project/cluster contexts.
- The upstream frontend has materially changed. Authentication now uses the
  existing identity/session-renewal flow; backend activity hydration and routes
  have moved into domain modules. Replacing only the frontend would not establish
  compatibility with the older validated backend.
- Upstream already provides rack and rack-to-rack ClusterKit actions, including
  GPU-Direct options. The new page addresses the validated modular node/group
  workflow. Existing actions must continue working.

The NC live handoff supplies successful 1/2/4/8-node API attempts, complete pair
coverage in both modes, a retained initial timeout failure, artifact downloads
and recovery evidence. Performance acceptance remains UNVERIFIED.

## Branch and Backend Alignment

Preserve `feature/clusterkit-backend` as the tested baseline. After approval,
create `feature/clusterkit-ui` from the reviewed Greenlight upstream revision
(or a specifically requested deployment revision). Port the modular feature
through its current extension points; do not replace newer shared files with
older copies or copy a new frontend over an incompatible backend.

Before UI behavior is implemented:

1. Port the modular blueprint, settings resolution and durable lifecycle to
   the current API loader, catalog configuration and activity-hydration modules.
2. Integrate with existing authentication, authorization/action declarations
   and audit hooks. Preserve print-only preview behavior. Do not create a new
   login, role system or configuration store for ClusterKit.
3. Preserve the proven static execution scripts, HCA/GID behavior, resource
   limits, normalized evidence checks and UCX timeout fix. Re-run the HTTP and
   compatibility tests against the current composition root.
4. Retain upstream's existing ClusterKit configuration and launch paths. Extend
   the existing per-cluster configuration model for modular site policy, keeping
   deployed documents backward compatible and mapping the validated NC profile
   without introducing site hostnames or paths into reusable frontend code.

This adaptation is implementation work following approval, not completed work.

## Page and Interaction

Add a lazy route `#/tools/clusterkit` and a Tools navigation entry. Follow
Greenlight's PageHeader, content widths, theme tokens, button styles and table
patterns. Use an operational layout with two tabs: **Run** and **History**.

### Run

- Show the active cluster and configuration/execution readiness. Link to existing
  Settings/Projects flows when configuration is incomplete.
- Offer one-node preflight and profile-enabled 2/4/8-node pairwise sizes.
- Select exact configured nodes from searchable inventory, or a configured group
  and count. Show availability, retain exact hostnames, and explain unavailable
  selections. A selection becoming busy requires a fresh valid preview.
- Derive groups, allowed sizes and limits from the backend. Do not infer physical
  racks from hostname fragments or label an arbitrary group as a whole rack/SU.
- Preview the exact nodes, expected pairs, modes, partition, time/resources and
  warnings. Present the execution command only as expandable technical detail.
- Require confirmation of that preview before submitting its token. Disable
  repeat clicks and invalidate previews after target, cluster, policy or expiry
  changes. The backend remains the final validator.
- Preserve the activity UUID before submission and in the page URL so a refresh
  or lost reply can reopen/reconcile the same attempt. Do not retry a submission
  after a timeout or unknown acceptance.

### Progress and Results

- Use reconciled API phase/scheduler state, distinguishing queued, running,
  collecting, unknown acceptance, completed and failed states. Do not invent a
  percentage when the backend provides only phases.
- Render functional result and performance acceptance as separate fields.
  Functional PASS alongside Performance UNVERIFIED must be visually unambiguous.
- Show observed/expected pair coverage separately for bandwidth and latency in
  each available mode. Display per-pair values with vendor units and sortable
  rows; any matrix view must preserve missing values rather than replace them
  with zero or imply agreed acceptance thresholds through colors.
- Include per-node HCA/GID/placement, diagnostic timing and error details, plus
  raw summary, vendor JSON and log downloads through the artifact API.
- Preserve failed attempts as failed, even when a later run succeeds. Initial
  timeouts with no metric stages should show their actual failure evidence.
- Current functionality is CPU/host-memory pairwise testing with no GPU
  allocation. Do not present it as proof of GPU-Direct or full-fabric acceptance.

### History

Use the existing durable activity registry and archive; no browser-only history
or competing job database. Keep both successful and failed attempts and reopen
their normalized result detail by activity UUID.

The current shared helper requires an exact `log_dir`; passing a parent results
root hides child attempts. Add a narrowly scoped read-only list operation, such
as `GET /api/clusterkit/runs`, backed by the existing registry/archive and the
active execution context/site policy. Bound and paginate it, include only
modular ClusterKit attempts belonging to that context, and preserve each exact
attempt directory. Leave existing shared exact-directory filter semantics intact.
Test mixed clusters, unrelated roots, older failures and records beyond a first
page. Final query design should follow the current registry/archive interface.

## Frontend Structure

Follow the current feature-folder pattern:

- `frontend/src/features/clusterkit/types.ts`: API and normalized result types.
- `services/clusterkitService.ts`: readiness, inventory, preview, submission,
  status, history and artifacts through existing `apiFetch`/`apiDownload`.
- `hooks/`: cluster-aware loading, selection and observing a durable activity.
- `components/`: target picker, preview dialog, results and history table.
- `views/ClusterkitView.tsx`: page composition.
- Route/sidebar/help-registry entries and focused colocated tests.

Reuse ActiveClusterContext and shared output/activity infrastructure. The current
rack-command store assumes a physical rack key: extend it to represent a node
or group target where needed, rather than manufacturing a rack ID. Use the
durable activity as the system of record, including after navigation/reload.

The legacy ClusterkitResultsSection fetches a different, rack-specific API and
cannot serve as the new data loader. Reuse compatible rendering helpers where
they fit the normalized schema; do not route this page to legacy launch URLs.

## Verification and AppServer Handoff

On the Mac, run TypeScript, ESLint, Vitest service/component tests, relevant
backend/API integration tests and the normal frontend build. Verify the page
with Playwright in both themes and desktop/mobile viewports using isolated
local backend fixtures drawn from the saved live evidence. Include disabled
configuration, stale previews, auth renewal, lost replies, incomplete coverage,
initial failure, navigation/reload recovery and mixed-cluster history cases.

Package matched source/backend and the complete built SPA (`index.html` and all
hashed assets from one build), with commit/checksums and handoff instructions.
No Linux environment is supplied by copying a Mac virtual environment.

The AppServer agent owns the separate test instance, private port and isolated
settings/history/identity stores, site runtime configuration and real API/Slurm
validation. Sharing the same host does not require sharing production state.
Keep the running deployment untouched. A production change remains a separate
reviewed step after the user sees the isolated frontend working.

## Inputs and Constraints

No extra cluster credentials are needed for local implementation. The useful
remaining input is the running AppServer application's repository and exact
commit or image tag/startup method. That determines the compatible isolated
handoff target and whether it should track latest Greenlight or a pinned version.

Fabric thresholds/counter interpretation do not block the UI; performance stays
UNVERIFIED. Rack/SU-wide acceptance, GPU-Direct and remote SSH execution remain
separate capabilities beyond the currently validated modular recipe.
