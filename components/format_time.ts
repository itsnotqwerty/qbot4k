// Shared timezone-aware timestamp formatting for dashboard surfaces.
// Renders timestamps in the community's configured timezone, not the
// browser's local zone. Falls back to UTC when the value or zone is invalid.

const IANA_ALIAS: Readonly<Record<string, string>> = {
  CST: "America/Chicago",
  CDT: "America/Chicago",
  EST: "America/New_York",
  EDT: "America/New_York",
  MST: "America/Denver",
  MDT: "America/Denver",
  PST: "America/Los_Angeles",
  PDT: "America/Los_Angeles",
};

export function normalizeTimeZone(zone: unknown): string {
  const raw = String(zone ?? "").trim();
  if (!raw) return "UTC";
  const aliased = IANA_ALIAS[raw.toUpperCase()] ?? raw;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: aliased }).format();
    return aliased;
  } catch {
    return "UTC";
  }
}

export function formatTimeInZone(value: unknown, zone: unknown): string {
  const date = new Date(String(value ?? ""));
  if (Number.isNaN(date.valueOf())) return String(value ?? "—");
  try {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: normalizeTimeZone(zone),
      year: "numeric",
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZoneName: "short",
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}
