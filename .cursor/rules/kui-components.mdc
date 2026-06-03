---
description: Kaizen UI (KUI) component reference — available components, API guidelines, and KUI-first policy
globs: frontend/src/**/*.tsx,frontend/src/**/*.ts
alwaysApply: false
---

# Kaizen UI Foundations — Component Reference

Source: https://webassets.nvidia.com/kaizen-ui-foundations/docs/llms.txt
Package: `@nvidia/foundations-react-core` (aka `@kui/foundations-react`)

## KUI-First Policy

**Always prefer KUI components over MUI, HTML divs, or custom elements.** Only use MUI when KUI has no equivalent (e.g., MUI Table for sortable/paginated data, MUI Dialog, MUI Drawer for sidebar). When unsure whether KUI covers a use case, check the component list below or fetch the detailed docs.

## General Guidelines

- Use KUI components over divs/custom elements
- Components have default padding — don't add extra (e.g., Card, avoid p-6)
- Stack/Flex: use `gap` prop with number values (gap="1,2,3,4" multiplies by 4 for spacing). Require multiple children, not single child
- Text component: use `kind` prop ("display/xl", "title/lg", "body/regular/sm"), not className for typography
- Use `style` prop for inline styles on KUI components, not `sx`
- Use KUI semantic CSS variables for colors — automatic light/dark theme support
- When using AppBar, do not add a logo to slotLeft — it is included by default

## Available Components

Each component has detailed docs at: `https://webassets.nvidia.com/kaizen-ui-foundations/docs/components/<Name>.txt`

| Component | Purpose |
|-----------|---------|
| **Accordion** | Vertically stacked expandable/collapsible sections |
| **Anchor** | Styled link element (also known as Link) |
| **AnimatedChevron** | Chevron icon that animates to show open/closed state |
| **AppBar** | Top-level masthead — branding, nav, global actions. One per app |
| **Avatar** | User/entity identifier — image, initials, or icon fallback |
| **Badge** | Non-interactive color-coded label for status/category/metadata |
| **Banner** | Important messages/alerts. Props: `status` (error/warning/success/info), `kind` (inline/header/global) |
| **Block** | General-purpose block-level container primitive |
| **Breadcrumbs** | Navigation trail showing hierarchy location |
| **Button** | Action trigger. Props: `kind` (primary/secondary/tertiary), `size` (small/tiny/medium/large), `color` (brand/neutral/danger) |
| **ButtonGroup** | Row of related buttons as a single visual set (up to 4) |
| **Card** | Content container — groups content, actions, optional media |
| **Checkbox** | Toggle on/off form control with mixed state support |
| **CodeSnippet** | Source code display with syntax highlighting and copy button |
| **Collapsible** | Disclosure region — toggles content expanded/collapsed |
| **Combobox** | Text input + filterable listbox. Props: `items`, `multiple`, `value`, `onValueChange`, `size` |
| **DatePicker** | Date picker with input fields and calendar popup. Single or range mode |
| **Divider** | Horizontal/vertical separator line between content sections |
| **Dropdown** | Select one/multiple options, navigate links, or take actions |
| **Flex** | Primitive flex layout container |
| **FormField** | Form field wrapper — label, description, helper text, validation |
| **Grid** | Primitive grid layout container |
| **Group** | Joins related items into a single visual cluster |
| **Hero** | Large page banner with messaging, CTAs, and imagery |
| **HorizontalNav** | Horizontal navigation bar for section links |
| **Inline** | Primitive span container |
| **InputShell** | Internal wrapper to style elements as inputs |
| **Label** | Form label (prefer FormField for full form fields) |
| **List** | Flexible list — unordered, ordered, or icon lists |
| **Menu** | Static menu displaying a list of items (for dropdown behavior, use Dropdown) |
| **Modal** | Dialog using native `<dialog>` element — backdrop, focus management |
| **Notification** | Messages/alerts with icon, heading, subheading, close, footer |
| **PageHeader** | Page title with breadcrumbs and page-level actions |
| **Pagination** | Navigation controls for paginated content |
| **Panel** | Simple card-like container with background, border, padding |
| **Popover** | Rich content portal triggered by button click |
| **ProgressBar** | Visual progress for loading/downloading/uploading |
| **RadioGroup** | Mutually exclusive radio button set |
| **RangeSlider** | Two-thumb range selector (min/max selection) |
| **SegmentedControl** | Mutually exclusive option selector (radio group style) |
| **Select** | Single-value dropdown selection |
| **SidePanel** | Side dialog (left/right) for supplementary content |
| **Skeleton** | Loading placeholder mimicking content layout |
| **Slider** | Single-value range selector |
| **Spinner** | Indeterminate loading indicator (for 1-5 second waits) |
| **Stack** | Vertical/horizontal layout container (Flex wrapper) |
| **StatusIndicator** | Simple circle for signaling status/changes |
| **StatusMessage** | Empty state / no data message with next steps |
| **Stepper** | Progress tracker through discrete steps |
| **Switch** | Binary on/off toggle (also known as Toggle) |
| **Table** | Data table organizing sets into columns and rows |
| **Tabs** | Tabbed interface for switching content sections |
| **Tag** | Categorize/label items. Props: `color` (green/red/yellow/blue/teal/purple/gray), `kind` (solid/outline), `readOnly`, `density` |
| **Text** | Typography primitive. Props: `kind` ("body/regular/sm", "title/lg", "display/sm", "mono/md", etc.) |
| **TextArea** | Multi-line text input with icons and status |
| **TextInput** | Single-line text input (InputShell + input composition) |
| **ThemeProvider** | Manages density and light/dark themes for all children |
| **Toast** | Temporary overlay message (non-critical feedback) |
| **Tooltip** | Additional info on hover/focus. Use TooltipProvider at app root |
| **TreeNav** | Tree navigation with native `<details>/<summary>` expand/collapse |
| **Upload** | File upload with previews and basic file management |
| **VerticalNav** | Vertical navigation menu with nested groups |

## KUI Components That Replace MUI

When building new features, use these KUI components **instead of** their MUI equivalents:

| Instead of MUI... | Use KUI... |
|-------------------|------------|
| `<Typography>` | `<Text kind="...">` |
| `<Chip>` | `<Tag>` |
| `<Alert>` (standalone) | `<Banner status="...">` |
| `<Skeleton>` | `<Skeleton>` from KUI |
| `<Box display="flex">` | `<Flex>` or `<Stack>` |
| `<Card>` | `<Card>` from KUI |
| `<Button>` (primary actions) | `<Button kind="primary">` from KUI |
| `<Tabs>` | `<Tabs>` from KUI |
| `<Checkbox>` | `<Checkbox>` from KUI |
| `<Switch>` | `<Switch>` from KUI |
| `<Select>` | `<Select>` or `<Combobox>` from KUI |
| `<TextField>` | `<TextInput>` or `<TextArea>` from KUI |
| `<RadioGroup>` | `<RadioGroup>` from KUI |
| `<Tooltip>` | `<Tooltip>` from KUI |
| `<Modal>`/`<Dialog>` | `<Modal>` from KUI |
| `<Pagination>` | `<Pagination>` from KUI |
| `<Breadcrumbs>` | `<Breadcrumbs>` from KUI |
| `<LinearProgress>` | `<ProgressBar>` from KUI |
| `<CircularProgress>` | `<Spinner>` from KUI |
| `<Accordion>` | `<Accordion>` from KUI |
| `<Drawer>` (side panel) | `<SidePanel>` from KUI |
| `<Snackbar>` + `<Alert>` | `<Toast>` from KUI |
| `<Menu>`/`<MenuItem>` | `<Dropdown>` from KUI |

## MUI Still Needed For

These MUI components are retained because the existing codebase depends on them in complex ways or KUI migration requires significant refactoring:

- **MUI Table** (sortable/paginated RackTable with complex custom rendering)
- **MUI Drawer** (sidebar navigation shell with MUI List integration)
- **MUI AppBar/Toolbar** (top navigation shell with MUI integration)
- **MUI Dialog** (existing complex dialogs — new simple modals should use KUI Modal)

## Layout

- AppBar at top of full page layouts (unless design omits)
- VerticalNav on the left side, below AppBar
- Account for AppBar height when setting vertical heights

## Fetching Detailed Component Docs

For full API details (props, examples, sub-components), fetch:
`https://webassets.nvidia.com/kaizen-ui-foundations/docs/components/<ComponentName>.txt`
