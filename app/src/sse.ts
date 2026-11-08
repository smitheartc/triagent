import { fetch } from 'expo/fetch';

import { api, ApiError } from './api';
import type { SSETerminal, TraceEntry } from './contracts';

type Handlers = {
  onOpen: () => void;
  onEntry: (entry: TraceEntry) => void;
  onEnd: (end: SSETerminal) => void;
  onError: (error: Error, retrying: boolean) => void;
};

/**
 * Follows GET /jobs/{id}/events. The server replays the recorded trace, tails it live, then sends exactly one
 * `done` or `failed` event and closes. A dropped stream reconnects and the server replays from seq 1, so entries
 * are deduplicated by seq. Returns a function that stops following.
 */
export function followJob(jobId: string, on: Handlers): () => void {
  const ctrl = new AbortController();
  let lastSeq = 0;
  let ended = false;

  async function connect(): Promise<void> {
    const res = await fetch(api.eventsUrl(jobId), { headers: { accept: 'text/event-stream' }, signal: ctrl.signal });
    if (!res.ok || !res.body) {
      throw new ApiError(res.status, res.status === 404 ? `The server has no job ${jobId}` : `HTTP ${res.status}`);
    }
    on.onOpen();
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true });
      let cut: number;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const message = parseEvent(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (message?.event === 'trace') {
          const entry = JSON.parse(message.data) as TraceEntry;
          if (entry.seq > lastSeq) {
            lastSeq = entry.seq;
            on.onEntry(entry);
          }
        } else if (message?.event === 'done' || message?.event === 'failed') {
          ended = true;
          on.onEnd(JSON.parse(message.data) as SSETerminal);
          return;
        }
      }
    }
  }

  (async () => {
    let failures = 0;
    while (!ended && !ctrl.signal.aborted) {
      try {
        await connect();
        failures = 0;
      } catch (err) {
        if (ctrl.signal.aborted) return;
        const gone = err instanceof ApiError && err.status === 404;
        on.onError(err instanceof Error ? err : new Error(String(err)), !gone);
        if (gone) return;
        failures++;
      }
      if (!ended) await new Promise((resolve) => setTimeout(resolve, Math.min(1000 * 2 ** failures, 15_000)));
    }
  })();

  return () => ctrl.abort();
}

/** One SSE message: `event:` and `data:` fields; comment lines (": keepalive") and other fields are skipped. */
export function parseEvent(block: string): { event: string; data: string } | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  return data.length ? { event, data: data.join('\n') } : null;
}
