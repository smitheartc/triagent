import { useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Loads data while the screen is focused and reloads it after `delay(data)` ms; a delay of false stops polling.
 * Requests never overlap: the next one is scheduled after the previous one settles.
 */
export function usePoll<T>(load: () => Promise<T>, delay: (data: T | undefined) => number | false) {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [refreshing, setRefreshing] = useState(false);
  const latest = useRef({ load, delay, data });
  useEffect(() => {
    latest.current.load = load;
    latest.current.delay = delay;
  });

  const fetchOnce = useCallback(async () => {
    try {
      const next = await latest.current.load();
      latest.current.data = next;
      setData(next);
      setError(undefined);
    } catch (err) {
      setError(err instanceof Error ? err : new Error(String(err)));
    }
  }, []);

  useFocusEffect(
    useCallback(() => {
      let alive = true;
      let timer: ReturnType<typeof setTimeout> | undefined;
      const tick = async () => {
        await fetchOnce();
        const ms = latest.current.delay(latest.current.data);
        if (alive && ms !== false) timer = setTimeout(tick, ms);
      };
      tick();
      return () => {
        alive = false;
        clearTimeout(timer);
      };
    }, [fetchOnce]),
  );

  const refresh = useCallback(async () => {
    setRefreshing(true);
    await fetchOnce();
    setRefreshing(false);
  }, [fetchOnce]);

  return { data, error, refresh, refreshing };
}
