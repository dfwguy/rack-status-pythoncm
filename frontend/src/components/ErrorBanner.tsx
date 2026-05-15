import { Banner } from "@nvidia/foundations-react-core";

interface ErrorBannerProps {
  message: string;
}

export function ErrorBanner({ message }: ErrorBannerProps) {
  return (
    <div style={{ marginBottom: 16 }}>
      <Banner status="error">
        {message}
      </Banner>
    </div>
  );
}
