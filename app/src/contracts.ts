/* Generated from server/contracts.py by scripts/contracts.mjs. Do not edit; run `npm run contracts`. */

export interface TriagentContracts {
  [k: string]: unknown;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Action".
 */
export interface Action {
  type: "approve" | "escalate" | "ask";
  by?: string;
  note?: string | null;
  question?: string | null;
  ts: string;
  state?: "recorded" | "queued" | "answering" | "answered" | "failed" | "not_implemented";
  id?: string | null;
  answer?: string | null;
  event_ids?: string[];
  error?: string | null;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "ActionRequest".
 */
export interface ActionRequest {
  type: "approve" | "escalate" | "ask";
  by?: string;
  note?: string | null;
  question?: string | null;
}
/**
 * Body of POST /jobs/{job_id}/ask.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "AskRequest".
 */
export interface AskRequest {
  question: string;
  by?: string;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "ChainStep".
 */
export interface ChainStep {
  timestamp: string;
  event_id: string;
  what: string;
  why_it_matters?: string;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "CriticCheck".
 */
export interface CriticCheck {
  ok: boolean;
  detail?: string;
  source: "code" | "model" | "missing";
}
/**
 * Job.critic. On a failed first review the investigator retries once; kept says which report was returned.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "CriticResult".
 */
export interface CriticResult {
  first: CriticReview;
  final: CriticReview;
  retried: boolean;
  kept?: ("initial" | "retry") | null;
}
/**
 * One critic pass. checks has exactly the seven CRITIC_CHECKS keys.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "CriticReview".
 */
export interface CriticReview {
  verdict: "pass" | "fail";
  failed?: string[];
  checks?: {
    [k: string]: CriticCheck;
  };
  corrections?: string[];
  model?: string | null;
  docs_checked?: number | null;
  docs_cited?: number | null;
  note?: string | null;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "DeviceRegistration".
 */
export interface DeviceRegistration {
  expo_push_token: string;
  user?: string;
  platform?: ("ios" | "android" | "web") | null;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "EnqueueResponse".
 */
export interface EnqueueResponse {
  job_id: string;
  ticket_id: string;
  status: "queued" | "running" | "critic" | "remediating" | "done" | "failed";
  duplicate?: boolean;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Health".
 */
export interface Health {
  ok: boolean;
  fake?: boolean;
  elasticsearch?: {
    [k: string]: unknown;
  };
  nvidia_keys_set?: {
    [k: string]: boolean;
  };
  models?: {
    [k: string]: string;
  };
  max_tool_calls?: number;
  [k: string]: unknown;
}
/**
 * GET /jobs/{job_id} and t/jobs/<job_id>.json. Times are Unix epoch seconds (floats).
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Job".
 */
export interface Job {
  job_id: string;
  ticket_id: string;
  status: "queued" | "running" | "critic" | "remediating" | "done" | "failed";
  ticket: Ticket;
  report?: Report | null;
  report_initial?: Report | null;
  critic?: CriticResult | null;
  remediation?: Remediation | null;
  actions?: Action[];
  error?: string | null;
  created: number;
  finished?: number | null;
  initial_seconds?: number | null;
  seed?: number | null;
  totals: Totals;
  totals_initial?: Totals | null;
  trace?: TraceEntry[];
}
/**
 * Body of POST /webhook and /investigate. jira_mock builds it from the INC-CREATED doc; real Jira/SNOW too.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Ticket".
 */
export interface Ticket {
  ticket_id: string;
  title: string;
  description?: string;
  priority?: string;
  created: string;
  service?: string | null;
  host?: string | null;
  labels?: {
    [k: string]: string | null;
  };
  namespace?: string;
  source_event_id?: string | null;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Report".
 */
export interface Report {
  problem: string;
  root_cause: RootCause;
  causal_chain?: ChainStep[];
  rejected_candidates?: RejectedCandidate[];
  affected?: {
    [k: string]: unknown;
  };
  confidence: number;
  open_questions?: string[];
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "RootCause".
 */
export interface RootCause {
  summary: string;
  category:
    | "cooling"
    | "power"
    | "network"
    | "storage"
    | "memory"
    | "deploy"
    | "config"
    | "certificate"
    | "security"
    | "unknown";
  entity: string;
  entity_type: string;
  identifiers?: {
    [k: string]: unknown;
  };
  event_ids?: string[];
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "RejectedCandidate".
 */
export interface RejectedCandidate {
  what: string;
  event_ids?: string[];
  why_rejected: string;
}
/**
 * Runbook-grounded steps. Every immediate step names the entity type and a command or console action.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Remediation".
 */
export interface Remediation {
  covered: boolean;
  immediate: string[];
  verify: string[];
  prevent: string[];
  rollback_risk: string;
  category?: string | null;
  runbook?: string | null;
  entity_types?: string[];
  source?: "model" | "runbook";
  note?: string | null;
  [k: string]: unknown;
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "Totals".
 */
export interface Totals {
  llm_calls?: number;
  tool_calls?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  llm_latency_ms?: number;
}
/**
 * One trace line. seq is 1-based and gapless per job; SSE uses it as the event id.
 *
 * Fields by kind (all optional beyond seq/ts/kind):
 *   status       status, retry (bool), totals
 *   llm          model, prompt_tokens, completion_tokens, latency_ms, tool_name
 *   tool         call_no, name, arguments, result (JSON truncated to TRACE_RESULT_TRIM)
 *   finding      finding, event_ids, finding_kind (symptom|precursor|candidate_cause|rejected|other)
 *   verify       checked, dropped, unverified
 *   assistant    text (the model's visible text for a turn)
 *   critic       verdict, failed, corrections, note
 *   remediation  runbook, covered, source, steps, note
 *   action       action (Action)
 *   info, error  message
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "TraceEntry".
 */
export interface TraceEntry {
  seq: number;
  ts: string;
  kind:
    | "status"
    | "llm"
    | "tool"
    | "finding"
    | "verify"
    | "info"
    | "error"
    | "assistant"
    | "critic"
    | "remediation"
    | "action";
  phase?: ("investigate" | "critic" | "retry" | "critic_retry" | "ask") | null;
  status?: ("queued" | "running" | "critic" | "remediating" | "done" | "failed") | null;
  message?: string | null;
  name?: string | null;
  call_no?: number | null;
  arguments?: {
    [k: string]: unknown;
  } | null;
  result?: string | null;
  model?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  latency_ms?: number | null;
  tool_name?: string | null;
  finding?: string | null;
  event_ids?: string[] | null;
  finding_kind?: string | null;
  text?: string | null;
  verdict?: ("pass" | "fail") | null;
  [k: string]: unknown;
}
/**
 * GET /jobs rows, newest first. Also the columns of the Supabase jobs table.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "JobSummary".
 */
export interface JobSummary {
  job_id: string;
  ticket_id: string;
  title: string;
  priority: string;
  service?: string | null;
  status: "queued" | "running" | "critic" | "remediating" | "done" | "failed";
  category?:
    | (
        | "cooling"
        | "power"
        | "network"
        | "storage"
        | "memory"
        | "deploy"
        | "config"
        | "certificate"
        | "security"
        | "unknown"
      )
    | null;
  entity?: string | null;
  confidence?: number | null;
  critic_verdict?: ("pass" | "fail") | null;
  created: number;
  finished?: number | null;
}
/**
 * What notify.py sends to Expo when a job finishes. data.job_id deep-links to Incident detail.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "PushMessage".
 */
export interface PushMessage {
  to: string;
  title: string;
  body: string;
  data: {
    [k: string]: unknown;
  };
}
/**
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "SSETerminal".
 */
export interface SSETerminal {
  job_id: string;
  status: "done" | "failed";
  error?: string | null;
  totals: Totals;
}
/**
 * One scorer row (scenario x run). Scorer output may carry more fields.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "ScoreRow".
 */
export interface ScoreRow {
  scenario: string;
  run: number;
  status: string;
  passed?: boolean | null;
  entity_ok?: boolean | null;
  category_ok?: boolean | null;
  jaccard?: number | null;
  red_herring?: boolean | null;
  confidence?: number | null;
  tool_calls?: number | null;
  seconds?: number | null;
  tokens?: number | null;
  critic_first?: string | null;
  retried?: boolean | null;
  before?: {
    [k: string]: unknown;
  } | null;
  [k: string]: unknown;
}
/**
 * t/scores/<stamp>.json and GET /scores/latest.
 *
 * This interface was referenced by `TriagentContracts`'s JSON-Schema
 * via the `definition` "ScoreRun".
 */
export interface ScoreRun {
  meta: {
    [k: string]: unknown;
  };
  rows: ScoreRow[];
}
