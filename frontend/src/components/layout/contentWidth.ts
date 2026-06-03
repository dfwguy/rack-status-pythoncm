/**
 * Shared page content widths (px).
 *
 * Pages are fluid and grow with the viewport up to a readability cap, then stay
 * centered (proportion principle, docs/UX_FRAMEWORK.md Section 3.3). Apply on the
 * page wrapper as `sx={{ maxWidth: CONTENT_MAX_WIDTH.wide, mx: "auto", p: 4 }}`.
 */
export const CONTENT_MAX_WIDTH = {
  /** Table / data-dense views — use wide monitors but cap before lines get unscannable. */
  wide: 1920,
  /** Cards, forms, and overviews — comfortable reading width. */
  comfortable: 1280,
} as const;
