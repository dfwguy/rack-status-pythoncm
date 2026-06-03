import { Banner } from "@nvidia/foundations-react-core";

interface PreviewBannerProps {
  /** Optional extra sentence describing what is mocked / what will wire it up. */
  note?: string;
}

/**
 * Marks a not-yet-operational view as a design preview so UX testers know the
 * data is synthetic and the screen is not yet wired to live cluster systems.
 * Honesty here (design principle 3.4 — set expectations) avoids mistaking a
 * mockup for live telemetry during evaluation.
 */
export function PreviewBanner({ note }: PreviewBannerProps) {
  return (
    <div style={{ marginBottom: 20 }}>
      <Banner status="info" kind="inline">
        Design preview — synthetic data for UX evaluation, not yet wired to live cluster systems.
        {note ? ` ${note}` : ""}
      </Banner>
    </div>
  );
}
