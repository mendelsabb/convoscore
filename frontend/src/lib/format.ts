// Small display helpers. Kept together so the same value never renders two different ways.

export function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatRelative(value: string | null): string {
  if (!value) return "—";
  const seconds = Math.floor((Date.now() - new Date(value).getTime()) / 1000);
  if (seconds < 60) return `${Math.max(0, seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null || milliseconds === undefined) return "—";
  if (milliseconds < 1000) return `${milliseconds} ms`;
  return `${(milliseconds / 1000).toFixed(2)} s`;
}

export function formatTokens(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

/**
 * Costs are fractions of a cent, so the usual two-decimal currency format would render every
 * value as $0.00. Enough significant digits are kept to see one conversation's cost, and the
 * label always says "estimated" because that is what it is.
 */
export function formatCost(value: string | number | null): string {
  if (value === null || value === undefined) return "—";
  const amount = typeof value === "string" ? Number.parseFloat(value) : value;
  if (Number.isNaN(amount)) return "—";
  if (amount === 0) return "$0";
  if (amount < 0.01) return `$${amount.toFixed(6)}`;
  return `$${amount.toFixed(4)}`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
