/** Format a duration for the activity table: "–", "520 ms" or "5.2 s". */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "–";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

/** The class for a duration cell: amber from 3 s, red from 10 s (slow calls stand out). */
export function durationClass(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  if (ms >= 10_000) return "duration-bad";
  if (ms >= 3000) return "duration-warn";
  return "";
}
