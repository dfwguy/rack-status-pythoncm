---
description: Frontend React component patterns — KUI-first UI framework with MUI as secondary
globs: frontend/src/**/*.tsx,frontend/src/**/*.ts
alwaysApply: false
---

# Frontend Conventions

## KUI-First Design Philosophy

NVIDIA Kaizen UI (`@nvidia/foundations-react-core`) is the **primary** UI framework. MUI is **secondary** — retained only for complex widgets with no KUI equivalent. When building new features, always reach for KUI first.

Full KUI reference (components, guidelines, CSS variables): https://webassets.nvidia.com/kaizen-ui-foundations/docs/llms.txt
See `kui-components.mdc` for the complete component catalog with KUI-to-MUI migration mappings.

## KUI vs MUI Decision Table

| Need | Use KUI | Do NOT use MUI |
|------|---------|----------------|
| Any text (body, labels, headings, titles) | `<Text kind="body/regular/md">` | `<Typography>` |
| Page titles | `<Text kind="display/sm">` or `<Text kind="title/lg">` | `<Typography variant="h5">` |
| Cards / content containers | `<Card>` | `<Card>` from MUI |
| Status badges / tags | `<Tag color="green" kind="solid" readOnly density="compact">` | `<Chip>` |
| Action buttons | `<Button kind="primary" size="small">` | `<Button>` from MUI |
| Error/info/warning messages | `<Banner status="error">` | `<Alert>` (except inside Snackbar) |
| Skeleton loading states | `<Skeleton>` | `<Skeleton>` from MUI |
| Flex layout wrappers | `<Flex gap="2" align="center" wrap="wrap">` | `<Box display="flex">` |
| Dropdown multi-select filters | `<Combobox items={[...]} multiple>` | Custom toggle buttons |
| Dividers in content areas | `<Divider>` from KUI | `<Divider>` from MUI |

### Button variants — this app is bordered-only

A deliberate divergence from stock KUI guidance, for operators driving a dense cluster console:
**every button carries a border, so nothing that is clickable can read as a caption.**

| Action | Use | Renders |
|---|---|---|
| Primary CTA | `kind="primary" color="brand"` | solid NVIDIA green |
| Everything else | `kind="secondary" color="neutral"` | bordered, theme text |
| Destructive (cancel/scancel, resets, delete) | `kind="primary" color="danger"` | solid red, white text |

- **Do not use `kind="tertiary"` for a standalone button.** It is intentionally borderless in KUI,
  which is the look this app rejects.
- **The one exception is a button nested inside another control** — e.g. the stepper chevrons in
  `TextInput`'s `slotStart`/`slotEnd` (`LaunchOptionsDialog`). The field already draws the box, so a
  bordered button there renders a box inside a box and stops reading as part of the input. Those
  stay `tertiary`; say so in a comment at the call site.
- **Always pass `color` on a destructive action.** `kind="primary"` alone falls back to the neutral
  palette, so a Cancel button ends up visually identical to a harmless one (this is exactly what
  `ActionCard` did until 2026-08-10).
- Never re-create these variants in local CSS — see the header of `src/kaizen-oss.css` and
  `docs/EXECUTION_ARCHITECTURE.md` for why a local override silently beat Kaizen's own rules.

### MUI stays for (no KUI equivalent or renders better in MUI context)

Table, Dialog, Tabs, Sidebar (Drawer + List), AppBar/Toolbar, TextField/Checkbox, Tooltip, IconButton, Menu/MenuItem, Snackbar + Alert (toasts), CircularProgress/LinearProgress, Paper/TableContainer. **Divider inside Drawer/AppBar** — KUI Divider renders differently in MUI shell components.

### MUI Button alias pattern

When a file needs both KUI Button (primary actions) and MUI Button (inside DialogActions):
```tsx
import MuiButton from "@mui/material/Button";
import { Button } from "@nvidia/foundations-react-core";
```

## Every call to `/api/*` goes through `apiFetch` — never bare `fetch`

`src/lib/apiFetch.ts`, not `fetch`. This is not a style preference; a bare `fetch` opts the call
site out of session renewal, and since the identity work the session is **two** tokens with very
different lifetimes:

| Token | Lives in | Default TTL |
|---|---|---|
| access | `nvis_access` cookie, `HttpOnly` | 1 hour (7-day ceiling) |
| refresh | `nvis_refresh` cookie, `HttpOnly`, path `/api/auth` | 1 day (30-day ceiling) |

Both are `HttpOnly`, so **JavaScript never holds a token and cannot see when one expires.** The
only signal is a `401`, and `apiFetch` is what acts on it: it POSTs `/api/auth/refresh` once,
replays the original request, and returns the untouched `401` if the *refresh* also failed — which
is the only 401 that means "log in again". It also de-duplicates, because a dashboard firing a
dozen requests at an expired token produces a dozen simultaneous 401s and one shared renewal.

A request that bypasses `apiFetch` still carries the cookie, so it works for an hour and then that
panel fails for good, recovering only if some *other* call happens to renew first. It presents as
one flaky pane, which is why it kept surviving. An earlier codemod moved raw `fetch("/api/...")`
calls with a *literal* URL and missed every call whose URL was built up or passed in — rack
actions, both exports, the log tails and every Slurm panel — so the guard is now a lint rule on the
call itself.

**Every browser request to the backend goes through `lib/apiFetch.ts`**, by what it is:

| Request | Use | Why |
|---|---|---|
| JSON, or a streamed body (`res.body.getReader()`) | `apiFetch` | Renews on 401 and replays; the retry happens before the body is read, so streams work |
| File download | `apiDownload(url, fallbackName)` | `apiFetch` → blob → named download; throws with the server's `error` text |
| New tab to `/api/...` or `/terminal` | `openApiUrl(url)` | Opens the tab synchronously (keeps the click's user gesture), navigates it only once `ensureFreshSession()` succeeds |
| WebSocket (`/ws/terminal`) | `await ensureFreshSession()` first | The handshake sends the cookie but cannot renew it |

```tsx
import { apiFetch } from "../lib/apiFetch";        // depth varies: ./lib, ../lib, ../../lib

const res = await apiFetch(`/api/status?${params}`, { cache: "no-store" });
```

`credentials` and the `X-Rack-Operator` / `X-Rack-Client-Id` headers are set for you, so a call
site does not need to restate them — and must not bypass them: rack-lock tracking attributes an
action to the operator from those headers.

**A 401 needs no handling at the call site.** When the renewal fails, `apiFetch` calls the
`onSessionLost` handler and `AuthProvider` shows sign-in in place, keeping the hash route. Never
redirect to `/login` by hand: that loses the route (this is a `HashRouter`) and skips the handler.
Just stop — return, or show "session expired" in a streaming pane.

**Enforced by ESLint** (`frontend/eslint.config.js`, a hard CI gate): `fetch` and `EventSource` as
globals, `window.fetch`/`globalThis.fetch`, `new WebSocket`, `window.open` of an `/api` or
`/terminal` URL, and assigning a `/login` URL to `href` are all errors. **Two deliberate
exceptions, and only these two:** `apiFetch.ts` itself, whose own `fetch("/api/auth/refresh")` must
stay raw or renewal recurses; and `srpClient.ts`, which *obtains* the session and so has nothing to
renew. Tests are exempt so they can stub `fetch`. The one `new WebSocket` (`hooks/useTerminal.ts`)
carries an inline disable naming the `ensureFreshSession()` call before it.

`/api/auth/refresh`, `/api/auth/srp/` and `/api/auth/logout` are on `NO_REFRESH` inside the
wrapper — **`/api/auth-check` is deliberately not**, and putting it back reintroduces a defect
where a page reload signed the operator out of a live session (see that constant's comment).
`ensureFreshSession()` depends on that: it is a renewing call to `/api/auth-check`.

## Code splitting: a lazy route is not enough, and a lazy dialog is not enough either

Every route in `App.tsx` is `lazy()`, with `ClusterDashboard`, `SingleRack` and `RackDetail` eager
for first paint. That was written down and then quietly broken, because the rule was about
**routes** and the leak came through **dialogs**.

**A conditionally rendered component is still statically imported.** The bundler does not care
that React will never mount it. The three eager views each opened a heavy dialog — parser charts
(`chart.js`), the web terminal (`xterm` + WebGL addon), the workflow run panel (`react-flow` +
`dagre`) — so ~1.2 MB of source sat in the initial chunk, downloaded before the dashboard painted
by every operator on an air-gapped cluster over an SSH tunnel. Fixing it took the initial chunk
from **2,199 kB to 1,212 kB** (gzip 658 → 370).

So the rule is weight and optionality, not routes:

> A component that pulls a heavy dependency and is not needed for first paint must be `lazy()`,
> **and** must not be rendered until it is open.

```tsx
// ✅ both halves
const ParserResultsDialog = lazy(() =>
  import("./ParserResultsDialog").then((m) => ({ default: m.ParserResultsDialog })),
);
{parserOpen ? <Suspense fallback={null}><ParserResultsDialog open … /></Suspense> : null}

// ❌ lazy, and still downloaded at first paint: `lazy()` fetches on first RENDER, and this
//    renders immediately — it just renders as nothing.
<ParserResultsDialog open={parserOpen} … />
```

Gating also changes one thing worth knowing: the dialog unmounts on close, so MUI's exit
transition is skipped and its internal state resets. For these dialogs that is an improvement.

**`src/initialBundle.test.ts` enforces it** by walking the static import graph from `main.tsx` and
`App.tsx` and failing with the exact import chain. It is a static check, not a size threshold, so
it needs no build, runs in the existing `frontend-test` job, and points at a cause rather than a
symptom. Add a package to its `HEAVY` map when you introduce one.

**Do not read Vite's "chunks are larger than 500 kB" warning as the signal.** It fires per chunk
with no idea which chunks are in the initial graph, so its loudest complaints are Monaco (2.7 MB)
and its JSON mode (1.2 MB) — both already on demand and correctly split. The limit is raised to
2800 in `vite.config.ts` for exactly that reason; the test above is the real gate.

## TypeScript & Lint

- Build: `tsc -b && vite build`. All code must pass strict TypeScript.
- **`npm run lint` (eslint) is part of the gate, not optional.** CI's job is *called*
  `typescript-check` and runs `tsc -b --noEmit` **and** `npm run lint`. A green typecheck has
  shipped eslint errors to CI. Watch `react-refresh/only-export-components` in particular: a file
  exporting a component may not also export an object, function or hook (a `const` holding a
  literal is fine), because Vite cannot hot-replace such a module.
- MUI 9: Move `fontWeight`/`fontFamily`/`fontSize` into `sx`. Use `slotProps` not `primaryTypographyProps`.
- `noUnusedLocals` enabled. Remove all unused imports.
- Python lint: `ruff check`. Remove unused imports, no extraneous f-string prefixes.

## Dependencies: `npm audit` audits the install tree, not the bundle

The distinction decides whether a "production" advisory is actually exposed, and it is easy to
get wrong in both directions.

`npm audit --omit=dev` walks `package.json`, so it reports packages that never reach a browser.
`undici` is the standing example: it arrives as
`@nvidia/foundations-react-core → isomorphic-dompurify → jsdom → undici`, is reported **high**,
and appears **zero times** in `static/spa/assets/` after a build. It is a Node-side path. Check
before treating a number as shipped risk:

```bash
npm run build && grep -rl '<package>' ../static/spa/assets/ | wc -l
```

`dompurify` is the opposite case — three bundled chunks contain it, so an advisory there is real.

**Two `overrides` in `package.json`, and why they exist** (a JSON file cannot hold the comment):

| Override | Reason |
|---|---|
| `dompurify: ^3.4.16` | ≤3.4.12 is vulnerable and it **ships**. Reached via KUI, mermaid and monaco, which pinned 3.4.8–3.4.11. A patch-level lift inside 3.4.x. Also a **direct** dependency (with `marked`) since the AI chat renders Markdown through `lib/chatMarkdown.ts`: npm refuses (`EOVERRIDE`) a direct range that differs from its override, so bump the two together. |
| `undici: ^7.29.0` | 7.0.0–7.28.0 is vulnerable. Does not ship, but jsdom is what **vitest** runs in, so it is worth being current where the tests live. |

Re-check both when KUI is bumped: if the upstream pins move past these, the override is dead
weight that reads like protection. **Never take `npm audit fix --force` blindly** — for
`monaco-editor` it proposes *downgrading* 0.56.0 to 0.53.0, and that entry is only flagged `via
dompurify` in the first place, so fixing dompurify clears it.

## Styling Rules

- **KUI components**: `style` prop. Example: `style={{ color: "var(--text-color-secondary)" }}`
- **MUI components**: `sx` prop. Example: `sx={{ display: "flex", gap: 2 }}`
- **Colors**: NVIDIA design tokens: `--text-color-primary`, `--text-color-secondary`, `--text-color-subtle`, `--background-color-surface-base`. No hardcoded hex — **including the fallback**: `var(--token, #hex)` still ships a hex, and that hex is what renders when the token name is wrong, which is how a surface ends up light in a dark theme. Inside MUI `sx`, prefer theme tokens that track light/dark (`color: "text.primary"`, `bgcolor: "action.hover"`) over raw CSS vars. **Never set a background without a paired foreground** — a surface with inherited text is the white-on-white trap that sank the P2P operation-log viewer.
- **Font sizes**: Minimum 14px. Never `fontSize: 12` or `variant="caption"`.
- **Icons**: `lucide-react` exclusively.

## Frontend Architecture

- **Views**: SingleRackView, ClusterDashboardView, MultirackView, SlurmView, ReportsView, AnalyticsView, ToolsView, SettingsView
- **Command tracking**: `RackCommandsStoreContext` manages ephemeral async job state (launch, poll, results) across all views. `RackCommandContext` per-rack.
- **Validation activities**: `ValidationActivitiesTable` (in `ValidationTab` + `MultirackView`) polls durable `/api/validation-activities` — not the same as ephemeral `RackCommandsStoreContext` UUIDs. Service: `validationActivitiesService.ts`; types: `ValidationActivity` / `ActivityTask`.
- **Hooks**: `useRackStatus` (polling /api/status), `useClusterOverview` (cluster-wide data)
- **Lib**: `rackCommandTypes.ts`, `rackLog.ts`, `slurmHelpers.ts`, `parseSqueueStdout.ts`, `parseSinfoRStdout.ts`, `nvloomHeatmapParse.ts`
- **Tests**: Vitest + Testing Library. Co-located `*.test.tsx` files. Run: `npm test`

## Import Patterns

```tsx
// KUI — single import for multiple components
import { Text, Card, Tag, Button, Banner, Flex, Combobox } from "@nvidia/foundations-react-core";

// MUI — deep imports (one per line), aliased when needed alongside KUI
import Dialog from "@mui/material/Dialog";
import MuiButton from "@mui/material/Button";
import Divider from "@mui/material/Divider";  // only inside Sidebar/Toolbar

// Icons — named imports from lucide
import { CircleCheck, Ellipsis, ArrowDownToLine } from "lucide-react";

// Charts
import { Bar, Line } from "react-chartjs-2";
```

Full KUI docs: https://webassets.nvidia.com/kaizen-ui-foundations/docs/llms.txt
Per-component docs: `https://webassets.nvidia.com/kaizen-ui-foundations/docs/components/<Component>.txt`
