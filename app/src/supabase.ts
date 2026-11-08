import { createClient, type SupabaseClient } from '@supabase/supabase-js';

import type { Job, JobSummary } from './contracts';

/**
 * Read-only Supabase access to the jobs table that server/store.py writes on every status change.
 *
 * Set EXPO_PUBLIC_SUPABASE_URL and EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY (app/.env) to turn it on. The publishable key
 * can only select from `jobs` (row-level security; devices and writes stay server-side), so it is safe in the bundle.
 * Without them `supabase` is null and the app reads everything from the server's HTTP API, as before.
 */
const url = process.env.EXPO_PUBLIC_SUPABASE_URL;
const key = process.env.EXPO_PUBLIC_SUPABASE_PUBLISHABLE_KEY;

export const supabase: SupabaseClient | null =
  url && key
    ? createClient(url, key, {
        // No login in this app: no session to store, refresh or read from a URL.
        auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
      })
    : null;

/** The JobSummary columns of the jobs table (contracts.SUPABASE_JOBS_SQL); the full Job JSON is in `job`. */
const SUMMARY = 'job_id,ticket_id,title,priority,service,status,category,entity,confidence,critic_verdict,created,finished';

function client(): SupabaseClient {
  if (!supabase) throw new Error('Supabase is not configured');
  return supabase;
}

/** Newest first, like GET /jobs. */
export async function supabaseJobs(limit = 100): Promise<JobSummary[]> {
  const { data, error } = await client().from('jobs').select(SUMMARY).order('created', { ascending: false }).limit(limit);
  if (error) throw new Error(`Supabase: ${error.message}`);
  return (data ?? []) as unknown as JobSummary[];
}

/** The stored Job JSON, as GET /jobs/{id} returned it at the job's last status change. */
export async function supabaseJob(jobId: string): Promise<Job> {
  const { data, error } = await client().from('jobs').select('job').eq('job_id', jobId).maybeSingle();
  if (error) throw new Error(`Supabase: ${error.message}`);
  if (!data) throw new Error(`Supabase has no job ${jobId}`);
  return (data as { job: Job }).job;
}

/**
 * Calls onChange after every insert, update or delete in jobs, and onLive(true|false) as the realtime channel comes up
 * or drops. Returns the unsubscribe function.
 */
export function watchJobs(onChange: () => void, onLive: (live: boolean) => void): () => void {
  const sb = client();
  const channel = sb
    .channel(`jobs-inbox-${Math.random().toString(36).slice(2)}`)
    .on('postgres_changes', { event: '*', schema: 'public', table: 'jobs' }, () => onChange())
    .subscribe((status) => onLive(status === 'SUBSCRIBED'));
  return () => {
    sb.removeChannel(channel);
  };
}
