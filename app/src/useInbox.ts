import { useFocusEffect } from 'expo-router';
import { useCallback, useRef, useState } from 'react';

import { api } from './api';
import type { JobSummary } from './contracts';
import { supabase, supabaseJobs, watchJobs } from './supabase';

/**
 * Where the Inbox rows came from:
 *   realtime   Supabase, with a live change subscription (reloads within a second of any job change)
 *   supabase   Supabase, subscription not connected (yet): polled every 3 s
 *   server     the server's GET /jobs, polled every 3 s (Supabase not configured, or it failed)
 */
export type InboxSource = 'realtime' | 'supabase' | 'server';

const POLL_MS = 3000;
const SAFETY_MS = 30_000; // realtime can drop silently; reload this often anyway
const DEBOUNCE_MS = 250; // a job writes several status changes in a burst

/**
 * Inbox rows, newest first. With Supabase configured: one select, then a realtime subscription on the jobs table that
 * reloads on every change, so the phone sees new incidents without reaching the server. If Supabase fails, it falls
 * back to the server's GET /jobs for that load. Runs only while the screen is focused.
 */
export function useInbox() {
  const [jobs, setJobs] = useState<JobSummary[]>();
  const [error, setError] = useState<Error>();
  const [source, setSource] = useState<InboxSource>(supabase ? 'supabase' : 'server');
  const [refreshing, setRefreshing] = useState(false);
  const live = useRef(false);

  const load = useCallback(async () => {
    let sbError: Error | undefined;
    if (supabase) {
      try {
        setJobs(await supabaseJobs());
        setSource(live.current ? 'realtime' : 'supabase');
        setError(undefined);
        return;
      } catch (err) {
        sbError = err instanceof Error ? err : new Error(String(err));
      }
    }
    try {
      setJobs(await api.jobs());
      setSource('server');
      setError(undefined);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(new Error(sbError ? `${sbError.message}\nServer: ${msg}` : msg));
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      let alive = true;
      let timer: ReturnType<typeof setTimeout> | undefined;
      let debounce: ReturnType<typeof setTimeout> | undefined;

      const schedule = (ms: number) => {
        clearTimeout(timer);
        timer = setTimeout(tick, ms);
      };
      const tick = async () => {
        await load();
        if (alive) schedule(live.current ? SAFETY_MS : POLL_MS);
      };
      const unwatch = supabase
        ? watchJobs(
            () => {
              clearTimeout(debounce);
              debounce = setTimeout(() => alive && tick(), DEBOUNCE_MS);
            },
            (up) => {
              live.current = up;
              setSource((s) => (s === 'server' ? s : up ? 'realtime' : 'supabase'));
              if (alive) schedule(up ? SAFETY_MS : POLL_MS);
            },
          )
        : undefined;
      tick();
      return () => {
        alive = false;
        clearTimeout(timer);
        clearTimeout(debounce);
        unwatch?.();
        live.current = false;
      };
    }, [load]),
  );

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  }, [load]);

  return { jobs, error, source, refresh, refreshing };
}
