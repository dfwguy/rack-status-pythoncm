import { Select } from "@nvidia/foundations-react-core";
import { REFRESH_INTERVALS_MS, refreshIntervalLabel } from "../lib/refreshInterval";

interface RefreshIntervalSelectProps {
  /** Current interval in ms; 0 = off. */
  valueMs: number;
  onChangeMs: (ms: number) => void;
  size?: "small" | "medium" | "large";
  disabled?: boolean;
  "aria-label"?: string;
}

/**
 * Auto-refresh cadence picker (Off / 1m / 5m / 15m / 30m).
 *
 * KUI Select rather than Combobox: the option set is short, bounded and known,
 * which is exactly the Select/Combobox boundary the Kaizen docs draw. Shared so
 * every polled panel offers the same choices and the same wording.
 */
export function RefreshIntervalSelect({
  valueMs,
  onChangeMs,
  size = "small",
  disabled,
  ...rest
}: RefreshIntervalSelectProps) {
  const items = REFRESH_INTERVALS_MS.map((ms) => ({
    value: String(ms),
    children: refreshIntervalLabel(ms),
  }));

  return (
    <Select
      items={items}
      value={String(valueMs)}
      onValueChange={(v) => onChangeMs(Number.parseInt(v as string, 10))}
      size={size}
      disabled={disabled}
      placeholder="Refresh"
      {...rest}
    />
  );
}
