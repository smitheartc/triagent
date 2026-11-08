import Constants from 'expo-constants';
import { fetch } from 'expo/fetch';
import { Platform } from 'react-native';

import type { Action, ActionRequest, AskRequest, DeviceRegistration, Job, JobSummary, ScoreRun } from './contracts';

/**
 * The Triagent server: server/main.py, or server/fake_server.py while the pipeline is being built.
 * EXPO_PUBLIC_API_URL wins. Otherwise port 8080 on the machine serving the JS bundle, which is the dev laptop when
 * the app runs in Expo Go or a browser.
 */
export const API_URL = (process.env.EXPO_PUBLIC_API_URL || defaultUrl()).replace(/\/+$/, '');

function defaultUrl(): string {
  const host =
    Platform.OS === 'web' ? globalThis.location?.hostname : Constants.expoConfig?.hostUri?.split(':')[0];
  return `http://${host || 'localhost'}:8080`;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const TIMEOUT_MS = 10_000;

async function request<T>(path: string, body?: unknown): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(API_URL + path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? undefined : { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    }).catch(() => {
      throw new ApiError(0, ctrl.signal.aborted ? 'The server did not answer in 10 s' : "Can't reach the server");
    });
    const text = await res.text();
    if (!res.ok) throw new ApiError(res.status, detail(text) || `HTTP ${res.status}`);
    return JSON.parse(text) as T;
  } finally {
    clearTimeout(timer);
  }
}

/** FastAPI errors: {"detail": "..."} from HTTPException, {"detail": [{msg}, ...]} from validation. */
function detail(text: string): string {
  try {
    const d = JSON.parse(text).detail;
    if (typeof d === 'string') return d;
    if (Array.isArray(d)) return d.map((x) => x?.msg ?? String(x)).join('; ');
  } catch {
    // not JSON; fall through
  }
  return text;
}

const id = (jobId: string) => encodeURIComponent(jobId);

export const api = {
  jobs: () => request<JobSummary[]>('/jobs'),
  job: (jobId: string) => request<Job>(`/jobs/${id(jobId)}`),
  act: (jobId: string, body: ActionRequest) => request<Action>(`/jobs/${id(jobId)}/actions`, body),
  /** Follow-up question; the answer arrives on the returned Action (by id) in GET /jobs/{id}. */
  ask: (jobId: string, body: AskRequest) => request<Action>(`/jobs/${id(jobId)}/ask`, body),
  latestScores: () => request<ScoreRun>('/scores/latest'),
  registerDevice: (body: DeviceRegistration) => request<{ ok: boolean }>('/devices', body),
  eventsUrl: (jobId: string) => `${API_URL}/jobs/${id(jobId)}/events`,
};

export const TERMINAL: ReadonlySet<string> = new Set(['done', 'failed']);
