import type { ReactNode } from "react";
import { Flex, Text } from "@nvidia/foundations-react-core";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <Flex gap="3" justify="between" align="start" wrap="wrap" style={{ marginBottom: 24 }}>
      <div>
        <Text kind="display/sm" style={{ display: "block" }}>{title}</Text>
        {description && (
          <Text kind="body/regular/lg" style={{ color: "var(--text-color-secondary)", display: "block", marginTop: 6 }}>
            {description}
          </Text>
        )}
      </div>
      {actions && (
        <Flex gap="2" align="center" style={{ flexShrink: 0 }}>
          {actions}
        </Flex>
      )}
    </Flex>
  );
}
