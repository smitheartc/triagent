/** Job.created / finished are Unix epoch seconds; trace and report timestamps are ISO 8601 strings. */

export function ago(epochSeconds: number): string {
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** 16:21:00Z. Incident evidence stays in UTC so it lines up with the logs. */
export function utcTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${d.toISOString().slice(11, 19)}Z`;
}

/** Nov 7, 16:53Z */
export function utcDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const day = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });
  return `${day}, ${d.toISOString().slice(11, 16)}Z`;
}

export function pct(x: number | null | undefined): string {
  return x == null ? '–' : `${Math.round(x * 100)}%`;
}

export function count(n: number | null | undefined): string {
  if (n == null) return '–';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}k`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

export function seconds(s: number | null | undefined): string {
  if (s == null) return '–';
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${Math.round(s % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** nvidia/nemotron-3.5-super-vl-120b-a12b -> nemotron-3.5-super-vl-120b-a12b */
export function shortModel(model: string | null | undefined): string {
  return (model ?? '').split('/').pop() || 'model';
}
